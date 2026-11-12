"""Hot reload project setup (helper/iosstudio/features/hotreload/project_setup.py): the managed Package.swift block
added and removed byte for byte on several manifest shapes (no trailing newline, trailing whitespace, CRLF, text
after the block), enable twice changes nothing, disable to `off` (no `import HotReload`) or `stub` (some), the
runtime copy made again when templates/hot-reload/version.txt changes, dry runs, and a manifest that wouldn't work
leaving everything as it was. swift is tests/support/hot_reload_setup_swift.py; templates come from
tests/support/hot_reload_setup.py.

    .venv/bin/pytest -q tests/helper/test_hot_reload_setup.py
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from iosstudio.core.output import HelperError
from iosstudio.features.hotreload import project_setup as S
from support.hot_reload_setup import FAKE_SWIFT, make_templates
from support.projects import package_project

SHAPES = {
    "plain": "let package = Package(name: \"App\")\n",
    "no trailing newline": "let package = Package(name: \"App\")",
    "trailing blank lines": "let package = Package(name: \"App\")\n\n\n",
    "trailing whitespace": "let package = Package(name: \"App\")\n  \t\n   ",
    "spaces, no newline": "let package = Package(name: \"App\")   ",
    "crlf": "// swift-tools-version: 6.0\r\nlet package = Package(name: \"App\")\r\n",
    "crlf, no newline": "// swift-tools-version: 6.0\r\nlet package = Package(name: \"App\")",
    "unicode": "// Ünïcødé ✓ ﻿\nlet package = Package(name: \"App\")\n",
}


class Block(unittest.TestCase):
    def test_round_trip_is_byte_exact(self):
        for name, text in SHAPES.items():
            for linker in (True, False):
                with self.subTest(name, linker=linker):
                    new = S.append_block(text, "App", linker)
                    self.assertTrue(new.startswith(text))
                    self.assertEqual(S.strip_block(new), text)
                    self.assertEqual(S.block_mode(new), "on" if linker else "stub")
                    self.assertEqual(S.block_target(new), "App")
                    self.assertEqual(S.block_mode(text), "off")
                    # switching on <-> stub keeps the original bytes too
                    other = S.append_block(S.strip_block(new), "App", not linker)
                    self.assertEqual(S.strip_block(other), text)

    def test_block_text_matches_the_contract(self):
        new = S.append_block("x\n", "App")
        self.assertEqual(new, "x\n\n" + "\n".join([
            "// >>> iOS Studio hot reload (managed by iOS Studio: turn hot reload off there to remove this)",
            'package.dependencies.append(.package(path: ".iosstudio/hot-reload"))',
            'for target in package.targets where target.name == "App" {',
            '    target.dependencies.append(.product(name: "HotReload", package: "hot-reload"))',
            "    target.linkerSettings = (target.linkerSettings ?? []) + [",
            '        .unsafeFlags(["-Xlinker", "-interposable", "-Xlinker", "-u", "-Xlinker", '
            '"_iosstudio_hot_reload_loader"],',
            "                     .when(configuration: .debug)),",
            "    ]",
            "}",
            "// <<< iOS Studio hot reload"]) + "\n")
        self.assertEqual(S.append_block("x", "App"), "x\n" + S.block("App"))
        self.assertIn("\r\n", S.append_block("a\r\nb\r\n", "App"))
        self.assertNotIn("\n\n", S.append_block("a\r\nb\r\n", "App").replace("\r\n", "|"))

    def test_text_after_the_block_is_kept(self):
        for text in ("x\n", "x"):
            new = S.append_block(text, "App") + ("\n" if not text.endswith("\n") else "") + "// mine\n"
            out = S.strip_block(new)
            self.assertTrue(out.startswith("x"))
            self.assertIn("// mine\n", out)
            self.assertNotIn("hot reload", out)

    def test_target_name_is_escaped(self):
        new = S.append_block("x\n", 'My"App')
        self.assertIn('target.name == "My\\"App"', new)
        self.assertEqual(S.block_target(new), 'My"App')

    def test_damaged_block(self):
        broken = S.append_block("x\n", "App").replace(S.END, "// something else")
        for fn in (S.strip_block, S.block_mode):
            with self.assertRaises(HelperError) as cm:
                fn(broken)
            self.assertEqual(cm.exception.code, "block-damaged")

    def test_two_blocks_are_both_removed(self):
        text = "x\n"
        twice = S.append_block(S.append_block(text, "App"), "App")
        self.assertEqual(S.strip_block(twice), text)


class Imports(unittest.TestCase):
    def test_import_forms(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        files = {"Sources/App/A.swift": "import SwiftUI\nimport HotReload\n",
                 "Sources/App/B.swift": "  @testable import HotReload\n",
                 "Sources/App/Sub/C.swift": "@_exported import struct HotReload.ObserveHotReload\n",
                 "Sources/App/D.swift": "// import HotReload\nimport HotReloadExtras\nlet s = \"import HotReload\"\n",
                 "Sources/App/E.txt": "import HotReload\n",
                 "Sources/App/.build/F.swift": "import HotReload\n",
                 "Tests/AppTests/T.swift": "import HotReload\n"}
        for rel, text in files.items():
            (tmp / rel).parent.mkdir(parents=True, exist_ok=True)
            (tmp / rel).write_text(text)
        self.assertEqual(S.imported_by(tmp), ["Sources/App/A.swift", "Sources/App/B.swift", "Sources/App/Sub/C.swift"])
        self.assertEqual(S.imported_by(tmp / "nope"), [])


class Project(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp()))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.templates = make_templates(self.tmp / "templates")
        for p in (mock.patch.dict(os.environ, {"IOSSTUDIO_SWIFT": str(FAKE_SWIFT),
                                               "IOSSTUDIO_HOT_RELOAD_TEMPLATES": str(self.templates)}),):
            p.start()
            self.addCleanup(p.stop)
        os.environ.pop("FAKE_HR_SWIFT_BROKEN", None)
        self.root = package_project(self.tmp / "App")
        self.manifest = self.root / "Package.swift"
        self.before = self.manifest.read_bytes()

    def write_source(self, text: str = "import SwiftUI\nimport HotReload\n") -> None:
        p = self.root / "Sources/App/ContentView.swift"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def test_enable_then_disable_gives_back_the_same_bytes(self):
        os.chmod(self.manifest, 0o640)
        out = S.enable(self.root)
        self.assertEqual({k: out[k] for k in ("enabled", "mode", "changed", "appTarget", "dryRun", "runtimeUpdated")},
                         {"enabled": True, "mode": "on", "changed": True, "appTarget": "App", "dryRun": False,
                          "runtimeUpdated": True})
        self.assertEqual(out["packagePath"], str(self.manifest))
        self.assertIn("+// >>> iOS Studio hot reload", out["diff"])
        self.assertEqual(self.manifest.read_bytes(), self.before + b"\n" + S.block("App").encode() + b"\n")
        self.assertEqual(self.manifest.stat().st_mode & 0o777, 0o640)
        rt = self.root / ".iosstudio/hot-reload"
        self.assertEqual((self.root / ".iosstudio/.gitignore").read_text(), "*\n")
        self.assertTrue((rt / "Sources/HotReloadLoader/loader.c").is_file())
        self.assertEqual(S.runtime_marker(self.root), {"version": "1", "kind": "full"})
        self.assertEqual(S.status(self.root), {"enabled": True, "mode": "on", "current": True,
                                               "runtime": {"version": "1", "kind": "full"}})
        out = S.disable(self.root)
        self.assertEqual((out["enabled"], out["mode"], out["changed"], out["importedBy"]), (False, "off", True, []))
        self.assertEqual(self.manifest.read_bytes(), self.before)
        self.assertFalse((self.root / ".iosstudio").exists())
        self.assertEqual(S.status(self.root)["mode"], "off")
        out = S.disable(self.root)                 # already off
        self.assertEqual((out["mode"], out["changed"], out["diff"]), ("off", False, ""))

    def test_enable_twice_changes_nothing(self):
        S.enable(self.root)
        after = self.manifest.read_bytes()
        mtime = self.manifest.stat().st_mtime_ns
        out = S.enable(self.root)
        self.assertEqual((out["changed"], out["runtimeUpdated"], out["diff"]), (False, False, ""))
        self.assertEqual(self.manifest.read_bytes(), after)
        self.assertEqual(self.manifest.stat().st_mtime_ns, mtime)

    def test_other_files_in_dot_iosstudio_are_kept(self):
        (self.root / ".iosstudio").mkdir()
        (self.root / ".iosstudio/other.json").write_text("{}")
        S.enable(self.root)
        S.disable(self.root)
        self.assertTrue((self.root / ".iosstudio/other.json").is_file())
        self.assertFalse((self.root / ".iosstudio/hot-reload").exists())

    def test_disable_keeps_the_stub_while_code_imports_hot_reload(self):
        S.enable(self.root)
        self.write_source()
        out = S.disable(self.root)
        self.assertEqual((out["enabled"], out["mode"], out["importedBy"]),
                         (False, "stub", ["Sources/App/ContentView.swift"]))
        self.assertIn("-    target.linkerSettings", out["diff"])
        text = self.manifest.read_text()
        self.assertEqual(text, self.before.decode() + "\n" + S.block("App", linker=False) + "\n")
        self.assertEqual(S.runtime_marker(self.root), {"version": "1", "kind": "stub"})
        self.assertFalse((self.root / ".iosstudio/hot-reload/Sources/HotReloadLoader").exists())
        self.assertIn("false", (self.root / ".iosstudio/hot-reload/Sources/HotReload/HotReload.swift").read_text())
        self.assertEqual(S.status(self.root)["mode"], "stub")
        out = S.enable(self.root)                  # back on: full runtime, linker flags
        self.assertEqual((out["mode"], out["changed"]), ("on", True))
        self.assertEqual(S.runtime_marker(self.root), {"version": "1", "kind": "full"})
        self.assertEqual(S.block_mode(self.manifest.read_text()), "on")
        self.write_source("import SwiftUI\n")      # imports gone: off removes everything
        S.disable(self.root)
        self.assertEqual(self.manifest.read_bytes(), self.before)

    def test_disable_never_enabled_but_imported_adds_the_stub(self):
        self.write_source()
        out = S.disable(self.root)
        self.assertEqual((out["mode"], out["changed"]), ("stub", True))
        self.assertEqual(S.block_mode(self.manifest.read_text()), "stub")

    def test_new_template_version_is_copied_again(self):
        S.enable(self.root)
        stray = self.root / ".iosstudio/hot-reload/Sources/HotReload/Old.swift"
        stray.write_text("// from version 1\n")
        manifest = self.manifest.read_bytes()
        make_templates(self.tmp / "templates", version="2")
        self.assertFalse(S.status(self.root)["current"])
        out = S.enable(self.root)
        self.assertEqual((out["changed"], out["runtimeUpdated"], out["diff"]), (True, True, ""))
        self.assertEqual(self.manifest.read_bytes(), manifest)
        self.assertEqual(S.runtime_marker(self.root), {"version": "2", "kind": "full"})
        self.assertIn("v2", (self.root / ".iosstudio/hot-reload/Sources/HotReloadLoader/loader.c").read_text())
        self.assertFalse(stray.exists())
        self.assertEqual(sorted(p.name for p in (self.root / ".iosstudio").iterdir()), [".gitignore", "hot-reload"])

    def test_dry_run_writes_nothing(self):
        out = S.enable(self.root, dry_run=True)
        self.assertEqual((out["dryRun"], out["changed"], out["runtimeUpdated"]), (True, True, True))
        self.assertIn('+for target in package.targets where target.name == "App" {', out["diff"])
        self.assertEqual(self.manifest.read_bytes(), self.before)
        self.assertFalse((self.root / ".iosstudio").exists())
        S.enable(self.root)
        out = S.disable(self.root, dry_run=True)
        self.assertEqual((out["dryRun"], out["mode"], out["changed"]), (True, "off", True))
        self.assertIn("-// >>> iOS Studio hot reload", out["diff"])
        self.assertTrue((self.root / ".iosstudio/hot-reload").is_dir())
        self.assertEqual(S.block_mode(self.manifest.read_text()), "on")

    def test_diff_is_bounded(self):
        out = S.enable(self.root, dry_run=True, max_bytes=300)
        self.assertTrue(out["truncated"])
        self.assertLessEqual(len(out["diff"].encode()), 300)
        self.assertTrue(Path(out["fullDiffPath"]).is_file())

    def test_a_manifest_that_would_break_is_left_alone(self):
        os.environ["FAKE_HR_SWIFT_BROKEN"] = "1"
        with self.assertRaises(HelperError) as cm:
            S.enable(self.root)
        self.assertEqual(cm.exception.code, "manifest-invalid")
        self.assertIn("Package.swift was left as it is", cm.exception.message)
        self.assertTrue(getattr(cm.exception, "extra", {}).get("diagnostics"))
        self.assertEqual(self.manifest.read_bytes(), self.before)
        self.assertFalse((self.root / ".iosstudio").exists())

    def test_package_not_named_package(self):
        self.manifest.write_text(self.before.decode().replace("let package = Package(", "let pkg = Package("))
        with self.assertRaises(HelperError) as cm:
            S.enable(self.root)
        self.assertEqual(cm.exception.code, "manifest-invalid")
        self.assertFalse((self.root / ".iosstudio").exists())

    def test_missing_templates(self):
        shutil.rmtree(self.templates)
        with self.assertRaises(HelperError) as cm:
            S.enable(self.root)
        self.assertEqual(cm.exception.code, "missing-runtime")
        self.assertEqual(self.manifest.read_bytes(), self.before)
        S.disable(self.root)                       # turning off needs no templates

    def test_no_app_target(self):
        self.manifest.write_text('// swift-tools-version: 6.0\nimport PackageDescription\n'
                                 'let package = Package(name: "App")\n')
        with self.assertRaises(HelperError) as cm:
            S.enable(self.root)
        self.assertEqual(cm.exception.code, "no-app-target")


class DisableAfterStop(unittest.TestCase):
    """`hotreload disable` stops a running session first; when the disable then fails, the error says so."""

    def run_disable(self, stop_result: bool, error: HelperError) -> dict:
        import contextlib
        import io
        import json

        from iosstudio.features.hotreload import commands, state
        from iosstudio.features.packages import project
        saved = (state.session_pid, state.stop, S.disable, project.project_root)
        state.session_pid = lambda _k: 4242
        state.stop = lambda _k: {"stopped": stop_result, "session": None}
        S.disable = lambda *_a, **_k: (_ for _ in ()).throw(error)
        project.project_root = lambda _f: Path("/x/App")
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                rc = commands.cmd_disable({"project": "/x/App"})
        finally:
            state.session_pid, state.stop, S.disable, project.project_root = saved
        self.assertEqual(rc, 1)
        return json.loads(buf.getvalue().strip().splitlines()[-1])

    def test_a_failed_disable_after_the_stop_says_the_session_is_gone(self):
        err = HelperError("manifest-invalid", "Package.swift wouldn't work after the change to turn hot reload "
                          "off, so Package.swift was left as it is: oops")
        err.extra = {"diagnostics": [{"message": "oops"}]}
        out = self.run_disable(True, err)
        self.assertEqual((out["ok"], out["error"]["code"], out["error"]["sessionStopped"]),
                         (False, "manifest-invalid", True))
        self.assertEqual(out["error"]["diagnostics"], [{"message": "oops"}])

    def test_no_session_stopped_no_flag(self):
        out = self.run_disable(False, HelperError("block-damaged", "The hot reload block was edited by hand."))
        self.assertNotIn("sessionStopped", out["error"])


if __name__ == "__main__":
    unittest.main()
