"""Format and lint (helper/format.py): parsing `swift-format lint` output and SwiftLint's JSON (captured in
tests/fixtures/format/), diff bounding at hunk and file boundaries, file discovery (.build/, hidden and xtool
folders skipped, symlinks out of the project ignored, --file outside the project refused), .swift-format detection
(project, parent folder, nested, invalid JSON), apply writing only changed files atomically, the stored lint result,
diff file pruning and demo mode. The CLI is in tests/cli/test_format_cli.py, the real swift-format in
tests/e2e/test_format_e2e.py.

    python -m pytest tests/helper/test_format.py
"""
from __future__ import annotations

import json
import os
import stat
import unittest
from pathlib import Path
from unittest import mock

import common
import format as F
from support import ROOT
from support.projects import project_with_files as make_project
from support.swift_format import TempHome

FIX = ROOT / "tests/fixtures/format"


# ================================================================ parsing

class LintParsing(unittest.TestCase):
    def test_swift_format_lint(self):
        text = (FIX / "swift-format-lint.txt").read_text()
        root = Path("/home/u/Developer/Demo")
        diags = F.parse_swift_format_lint(text, root)
        self.assertEqual(len(diags), 23)       # the "warning: Unable to read configuration" line isn't one
        first = diags[0]
        self.assertEqual(first, {"severity": "warning", "category": "style", "message": "remove 1 space",
                                 "file": "/home/u/Developer/Demo/Package.swift", "line": 3, "col": 47,
                                 "source": "swift-format", "rule": "Spacing", "hint": F.HINT_FIXABLE})
        syntax = [d for d in diags if d["rule"] == "syntax"]
        self.assertEqual(len(syntax), 2)
        self.assertEqual(syntax[0]["severity"], "error")
        self.assertEqual(syntax[0]["hint"], F.HINT_PARSE)
        # Hints are shown in the app: app terms only, no MCP tool names (the MCP layer adds those).
        for h in (F.HINT_FIXABLE, F.HINT_PARSE, F.HINT_MISSING):
            self.assertNotIn("ios_", h)
        self.assertEqual(syntax[0]["file"], "/home/u/Developer/Demo/Sources/Demo/Broken.swift")
        other = diags[-1]
        self.assertEqual((other["file"], other["rule"]), ("/home/u/Developer/Demo/Sources/Demo/Other.swift",
                                                          "NeverForceUnwrap"))
        self.assertNotIn("hint", other)        # not a whitespace rule: formatting doesn't fix it
        self.assertTrue(all(os.path.isabs(d["file"]) for d in diags))

    def test_swiftlint_json(self):
        root = Path("/home/u/Developer/Demo")
        diags = F.parse_swiftlint_json((FIX / "swiftlint.json").read_text(), root)
        self.assertEqual(len(diags), 6)
        d = diags[0]
        self.assertEqual((d["severity"], d["category"], d["source"], d["rule"], d["line"], d["col"]),
                         ("error", "style", "swiftlint", "identifier_name", 1, 5))
        self.assertEqual(d["file"], "/home/u/Developer/Demo/Sources/Demo/Clean.swift")
        self.assertLessEqual({x["severity"] for x in diags}, {"warning", "error"})
        # relative paths, junk before the JSON, bad JSON, non-list
        rel = F.parse_swiftlint_json('Linting...\n[{"file":"Sources/A.swift","line":2,"character":null,'
                                     '"reason":"x  y","rule_id":"r","severity":"Warning"}]', root)
        self.assertEqual((rel[0]["file"], rel[0]["col"], rel[0]["message"], rel[0]["severity"]),
                         ("/home/u/Developer/Demo/Sources/A.swift", None, "x y", "warning"))
        self.assertEqual(F.parse_swiftlint_json("[not json", root), [])
        self.assertEqual(F.parse_swiftlint_json('{"a":1}', root), [])
        self.assertEqual(F.parse_swiftlint_json("", root), [])


# ================================================================ diff

class DiffBounding(unittest.TestCase):
    def diff(self, n: int) -> str:
        return "".join(F.unified(f"Sources/F{i}.swift", "let a=1\nlet b=2\n" * 3, "let a = 1\nlet b = 2\n" * 3)
                       for i in range(n))

    def test_small_diff_unchanged(self):
        d = self.diff(1)
        self.assertEqual(F.bound_diff(d, 10_000), (d, False))
        self.assertTrue(d.startswith("--- a/Sources/F0.swift\n+++ b/Sources/F0.swift\n@@ "))

    def test_cut_at_file_or_hunk_boundary(self):
        d = self.diff(20)
        for limit in (700, 1000, 1500, 2345):
            shown, cut = F.bound_diff(d, limit)
            self.assertTrue(cut)
            self.assertLessEqual(len(shown.encode()), limit)
            self.assertTrue(d.startswith(shown))
            self.assertTrue(shown.endswith("\n"))
            nxt = d[len(shown):]
            self.assertTrue(nxt.startswith("--- a/") or nxt.startswith("@@ "), nxt[:30])
            self.assertFalse(shown.rstrip("\n").splitlines()[-1].startswith(("--- ", "+++ ")))

    def test_cut_at_line_when_no_boundary(self):
        d = F.unified("A.swift", "".join(f"let v{i}=1\n" for i in range(500)),
                      "".join(f"let v{i} = 1\n" for i in range(500)))
        shown, cut = F.bound_diff(d, 3000)
        self.assertTrue(cut)
        self.assertTrue(shown.endswith("\n"))
        self.assertLessEqual(len(shown.encode()), 3000)
        self.assertGreater(len(shown), 2500)

    def test_multibyte_never_split(self):
        d = F.unified("A.swift", "let s=\"ü\"\n" * 400, "let s = \"ü\"\n" * 400)
        shown, _ = F.bound_diff(d, 1001)
        shown.encode("utf-8")       # valid text
        self.assertLessEqual(len(shown.encode()), 1001)

    def test_no_newline_at_end(self):
        d = F.unified("A.swift", "let a=1", "let a = 1\n")
        self.assertIn("\\ No newline at end of file", d)


# ================================================================ files and config

class Discovery(TempHome):
    def test_skips_build_hidden_and_outside_symlinks(self):
        root = make_project(self.dir / "App", {
            "Sources/App/A.swift": "", "Sources/App/Sub/B.swift": "", "Tests/AppTests/T.swift": "",
            "Sources/App/.hidden/H.swift": "", "Sources/App/.Dot.swift": "", "Sources/App/xtool/X.swift": "",
            ".build/checkouts/Dep/D.swift": "", "Sources/App/.build/G.swift": "", "Other/O.swift": "",
            "Sources/App/notes.txt": "", "Package@swift-6.0.swift": "", "Plugins/P/P.swift": ""})
        (self.dir / "outside.swift").write_text("")
        os.symlink(self.dir / "outside.swift", root / "Sources/App/Link.swift")
        os.symlink(root / "Sources/App/A.swift", root / "Sources/App/InLink.swift")
        (root / "Sources/App/Big.swift").write_text("x" * (F.MAX_FILE_BYTES + 1))
        files, skipped = F.discover(root)
        rels = sorted(F.rel(root, p) if not p.is_symlink() else str(p.relative_to(root)) for p in files)
        self.assertEqual(rels, ["Package.swift", "Package@swift-6.0.swift", "Plugins/P/P.swift",
                                "Sources/App/A.swift", "Sources/App/InLink.swift", "Sources/App/Sub/B.swift",
                                "Tests/AppTests/T.swift"])
        self.assertEqual(skipped, 1)
        with mock.patch.object(F, "MAX_FILES", 2):
            files, skipped = F.discover(root, 2)
        self.assertEqual((len(files), skipped), (2, 6))

    def test_select(self):
        root = make_project(self.dir / "App", {"Sources/App/A.swift": "", "Sources/App/readme.md": ""})
        (self.dir / "evil.swift").write_text("")
        files, errors = F.select(root, ["Sources/App/A.swift", str(root / "Sources/App/A.swift"),
                                        "../evil.swift", str(self.dir / "evil.swift"), "Nope.swift",
                                        "Sources/App/readme.md", ""])
        self.assertEqual(files, [root / "Sources/App/A.swift"])
        self.assertEqual([e["message"] for e in errors],
                         ["Outside the project: left alone.", "Outside the project: left alone.", "No such file.",
                          "Not a .swift file."])

    def test_config(self):
        root = make_project(self.dir / "ws/App", {"Sources/App/A.swift": "", "Sources/App/Sub/.swift-format": "{}"})
        self.assertEqual(F.find_config(root), (None, ["Sources/App/Sub/.swift-format"]))
        (self.dir / "ws/.swift-format").write_text("{}")
        self.assertEqual(F.find_config(root)[0], str(self.dir / "ws/.swift-format"))
        (root / ".swift-format").write_text('{"version": 1}')
        self.assertEqual(F.find_config(root)[0], ".swift-format")
        F.check_config(root, ".swift-format")
        (root / ".swift-format").write_text("{nope")
        with self.assertRaises(common.HelperError) as cm:
            F.check_config(root, ".swift-format")
        self.assertEqual(cm.exception.code, "bad-config")
        self.assertTrue(cm.exception.hint)


# ================================================================ format and lint with the fake tool

class FormatRuns(TempHome):
    def project(self) -> Path:
        return make_project(self.dir / "App", {
            "Sources/App/A.swift": "let c = a+b\n", "Sources/App/B.swift": "let d = 1\n",
            "Sources/App/Bad.swift": "SYNTAX ERROR\n", ".build/x/G.swift": "let e = a+b\n"})

    def test_dry_run_writes_nothing(self):
        root = self.project()
        before = {p: p.read_bytes() for p in root.rglob("*.swift")}
        res = F.run_format(root, sf=[str(self.fake)])
        self.assertEqual(res["files_changed"], ["Sources/App/A.swift"])
        self.assertEqual(res["files_checked"], 4)        # Package.swift, A, B and Bad (which failed)
        self.assertIn("-let c = a+b\n+let c = a + b\n", res["diff"])
        self.assertEqual(res["errors"], [{"file": "Sources/App/Bad.swift", "message": "1:1: error: expected declaration"}])
        self.assertEqual((res["tool"], res["tool_version"], res["apply"], res["truncated"]),
                         ("swift-format", "9.9.9", False, False))
        self.assertIsNone(res["full_output_path"])
        self.assertEqual({p: p.read_bytes() for p in root.rglob("*.swift")}, before)

    def test_apply_writes_only_changed_files_atomically(self):
        root = self.project()
        a, b = root / "Sources/App/A.swift", root / "Sources/App/B.swift"
        a.chmod(0o600)
        b_ino, b_mtime = b.stat().st_ino, b.stat().st_mtime_ns
        a_ino = a.stat().st_ino
        res = F.run_format(root, apply=True, sf=[str(self.fake)])
        self.assertTrue(res["apply"])
        self.assertEqual(res["files_changed"], ["Sources/App/A.swift"])
        self.assertEqual(a.read_text(), "let c = a + b\n")
        self.assertNotEqual(a.stat().st_ino, a_ino)                  # replaced, not rewritten in place
        self.assertEqual(stat.S_IMODE(a.stat().st_mode), 0o600)       # mode kept
        self.assertEqual((b.stat().st_ino, b.stat().st_mtime_ns), (b_ino, b_mtime))   # untouched
        self.assertEqual((root / ".build/x/G.swift").read_text(), "let e = a+b\n")
        self.assertEqual([p.name for p in a.parent.iterdir() if p.name.startswith(".")], [])   # no temp files left
        again = F.run_format(root, apply=True, sf=[str(self.fake)])
        self.assertEqual(again["files_changed"], [])

    def test_apply_skips_file_changed_meanwhile(self):
        root = self.project()
        p = root / "Sources/App/A.swift"
        self.assertEqual(F.write_atomic(p, b"new", b"something else"),
                         "Changed while formatting: left alone, run Format again.")
        self.assertEqual(p.read_text(), "let c = a+b\n")

    def test_truncated_diff_saved_and_pruned(self):
        root = make_project(self.dir / "Big", {f"Sources/App/F{i:02}.swift": "let c = a+b\n" * 30 for i in range(30)})
        res = F.run_format(root, max_bytes=1000, sf=[str(self.fake)])
        self.assertTrue(res["truncated"])
        self.assertLessEqual(len(res["diff"].encode()), 1000)
        full = Path(res["full_output_path"]).read_text()
        self.assertEqual(len(full.encode()), res["diff_bytes"])
        self.assertTrue(full.startswith(res["diff"]))
        self.assertIn("full_output_path", res["note"])
        for _ in range(F.DIFF_KEEP + 3):
            F.save_diff(root, "x")
        self.assertLessEqual(len(list(F.DIFF_DIR.glob("*.diff"))), F.DIFF_KEEP)

    def test_deadline(self):
        root = self.project()
        res = F.run_format(root, deadline=0, sf=[str(self.fake)])
        self.assertTrue(res["timed_out"])
        self.assertEqual(res["files_checked"], 0)
        self.assertIn("Stopped after", res["note"])

    def test_missing_tool(self):
        root = self.project()
        with mock.patch.object(F, "find_swift_format", return_value=None):
            with self.assertRaises(common.HelperError) as cm:
                F.run_format(root)
        self.assertEqual(cm.exception.code, "missing-swift-format")
        self.assertIn("Setup", cm.exception.hint)

    def test_lint_and_last(self):
        root = self.project()
        self.assertFalse(F.last_lint(root)["cached"])
        res = F.run_lint(root, sf=[str(self.fake)], use_swiftlint=False)
        self.assertEqual([(d["severity"], d["rule"], Path(d["file"]).name) for d in res["diagnostics"]],
                         [("error", "syntax", "Bad.swift"), ("warning", "Spacing", "A.swift")])
        self.assertEqual(res["counts"], {"error": 1, "warning": 1})
        self.assertEqual(res["tools"], ["swift-format"])
        self.assertFalse(res["swiftlint"])
        last = F.last_lint(root, max_items=1)
        self.assertTrue(last["cached"])
        self.assertEqual((last["total"], len(last["diagnostics"]), last["truncated"]), (2, 1, True))
        # a lint of chosen files doesn't replace the stored project result
        F.run_lint(root, files=[root / "Sources/App/B.swift"], sf=[str(self.fake)], use_swiftlint=False)
        self.assertEqual(F.last_lint(root)["total"], 2)
        capped = F.run_lint(root, max_items=1, sf=[str(self.fake)], use_swiftlint=False)
        self.assertTrue(capped["truncated"])
        self.assertIn("Showing 1 of 2", capped["note"])

    def test_swiftlint_that_does_not_start(self):
        shim = self.dir / "bin/swiftlint"
        shim.write_text("#!/bin/sh\necho 'mise ERROR No version is set for shim: swiftlint' >&2\nexit 1\n")
        shim.chmod(0o755)
        with mock.patch.dict(os.environ, {"IOSSTUDIO_SWIFTLINT": str(shim)}):
            path, ver, why = F.find_swiftlint()
        self.assertIsNone(path)
        self.assertIn("didn't start", why)

    def test_swiftlint_results_merged(self):
        root = self.project()
        out = json.dumps([{"file": str(root / "Sources/App/B.swift"), "line": 1, "character": 5, "reason": "Short",
                           "rule_id": "identifier_name", "severity": "Warning"},
                          {"file": "/etc/elsewhere.swift", "line": 1, "character": 1, "reason": "x", "rule_id": "y",
                           "severity": "Error"}])
        sl = self.dir / "bin/swiftlint"
        sl.write_text(f"#!/bin/sh\n[ \"$1\" = version ] && {{ echo 0.65.1; exit 0; }}\ncat <<'EOF'\n{out}\nEOF\n")
        sl.chmod(0o755)
        with mock.patch.dict(os.environ, {"IOSSTUDIO_SWIFTLINT": str(sl)}):
            res = F.run_lint(root, sf=[str(self.fake)])
        self.assertEqual(res["tools"], ["swift-format", "swiftlint"])
        self.assertTrue(res["swiftlint"])
        self.assertEqual(res["swiftlint_version"], "0.65.1")
        self.assertEqual(res["by_source"], {"swift-format": 2, "swiftlint": 1})   # the outside file is dropped


# ================================================================ CLI and demo


# ================================================================ the real swift-format


if __name__ == "__main__":
    unittest.main()
