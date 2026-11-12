"""helper/iosstudio/features/hotreload/patch.py: make_patch / warm_up against a fake xtool build and fake
swiftc / swift-frontend / clang / ld64.lld / rcodesign (tests/support/hot_reload.py) - the real code paths with
fakes at the process boundary. The compiled objects are small real ones (tests/fixtures/hot-reload/)."""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from iosstudio.core import subproc
from iosstudio.core.output import HelperError
from iosstudio.features.hotreload import buildinfo, macho, patch
from support import FIXTURES, ROOT
from support.hot_reload import OPENSSL, app_and_patch, fake_build, fake_toolchain, make_identity

FIX = FIXTURES / "hot-reload"
CARD = (FIX / "base.swift").read_text()
APP_SWIFT = "import SwiftUI\n\n@main\nstruct TheApp: App {\n    var body: some Scene { WindowGroup { Card() } }\n}\n"


class PatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp()))
        env = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(env)))
        self.tools = fake_toolchain(self.tmp / "tools")
        self.objs = self.tmp / "objs"
        self.objs.mkdir()
        shutil.copy(FIX / "text-any.o", self.objs / "default.o")
        app, dylib = app_and_patch(bound=["_f"], app_funcs=["_f", "_g"], patch_funcs=["_f", "_g", "_new"])
        (self.tmp / "patch.dylib").write_bytes(dylib)
        fake_sign = self.tmp / "rcodesign"
        fake_sign.write_text("#!/bin/sh\ncat > /dev/null\necho \"$@\" >> \"$(dirname \"$0\")/tools/calls\"\n")
        fake_sign.chmod(0o755)
        self.data = self.tmp / "xtool-data"
        if OPENSSL:
            make_identity(self.data)                     # a throwaway key; the fake rcodesign just reads it
        os.environ.update(FAKE_OBJ_DIR=str(self.objs), FAKE_PATCH=str(self.tmp / "patch.dylib"),
                          IOSSTUDIO_RCODESIGN=str(fake_sign), IOSSTUDIO_XTOOL_DATA=str(self.data))
        self.proj = fake_build(self.tmp / "App", self.tools, files={"Card.swift": CARD, "TheApp.swift": APP_SWIFT},
                               objects={"Card.swift": (FIX / "base-any.o").read_bytes(),
                                        "TheApp.swift": (FIX / "base-any.o").read_bytes()}, app_binary=app)
        self.base = buildinfo.capture_baseline(self.proj, "App", self.tmp / "cache")
        self.card = self.proj / "Sources/App/Card.swift"

    def calls(self) -> list[str]:
        p = self.tools / "calls"
        return p.read_text().splitlines() if p.exists() else []

    def make(self, seq: int = 1, **kw) -> patch.PatchResult:
        kw.setdefault("sign", False)
        return patch.make_patch(self.base, None, seq, **kw)

    def error(self, **kw) -> HelperError:
        with self.assertRaises(HelperError) as cm:
            self.make(**kw)
        return cm.exception

    def test_text_edit(self):
        self.card.write_text(CARD.replace('Text("Hello")', 'Text("Changed")'))
        steps = []
        res = self.make(on_step=steps.append)
        self.assertEqual(res.files, ["Sources/App/Card.swift"])
        self.assertEqual(res.link_mode, "ld")
        self.assertEqual(res.swappable, ["_f"])
        self.assertEqual(res.missing, ["_g"])
        self.assertEqual(Path(res.dylib), self.base.patches_dir / "patch-1.dylib")
        self.assertEqual(macho.MachO.load(res.dylib).filetype, macho.MH_DYLIB)
        self.assertEqual(set(res.steps), {"check", "compile", "link", "sign"})
        self.assertEqual(steps, ["check", "compile", "link"])
        self.assertFalse(res.signed)
        self.assertEqual([c for c in self.calls() if c.startswith("frontend")], ["frontend Card.swift"])
        ld = next(c for c in self.calls() if c.startswith("ld "))
        self.assertIn("@rpath/patch-1.dylib", ld)
        self.assertEqual(sorted(p.name for p in self.base.patches_dir.iterdir()), ["patch-1.dylib"])

    @unittest.skipUnless(OPENSSL, "needs openssl")
    def test_signs(self):
        self.card.write_text(CARD.replace("Hello", "Hi"))
        res = self.make(seq=4, sign=True)
        self.assertTrue(res.signed)
        sign = next(c for c in self.calls() if c.startswith("sign "))
        self.assertIn("--team-name ABCDE12345", sign)
        self.assertTrue(sign.endswith("patch-4.dylib"))

    def test_driver_link_when_ld_fails(self):
        self.card.write_text(CARD.replace("Hello", "Hi"))
        os.environ["FAKE_LD_FAIL"] = "1"
        self.assertEqual(self.make().link_mode, "driver")
        os.environ["FAKE_LINK_FAIL"] = "1"
        err = self.error()
        self.assertEqual(err.code, "link-failed")
        self.assertTrue(err.diagnostics)
        self.assertFalse(list(self.base.patches_dir.glob(".patch-*")))

    def test_compile_error(self):
        self.card.write_text(CARD.replace('Text("Hello")', 'Text("Hello")\n            COMPILE_ERROR', 1))
        err = self.error()
        self.assertEqual(err.code, "compile-failed")
        self.assertEqual(err.file, "Sources/App/Card.swift")
        self.assertEqual(err.line, 7)
        self.assertEqual([d["severity"] for d in err.diagnostics], ["error", "note"])
        self.assertIn("COMPILE_ERROR", err.message)
        js = err.to_json()
        self.assertEqual(js["code"], "compile-failed")
        self.assertLessEqual(len(js["diagnostics"]), 5)

    def test_structural_blocker_stops_before_compiling(self):
        self.card.write_text(CARD.replace("struct Card: View {", "struct Card: View {\n    @State var on = false"))
        err = self.error()
        self.assertEqual(err.code, "unsupported-change")
        self.assertEqual([b["kind"] for b in err.blockers], ["stored-property"])
        self.assertEqual(err.to_json()["blockers"][0]["name"], "on")
        self.assertEqual(err.fix["action"], "hot-reload-restart")
        self.assertFalse([c for c in self.calls() if c.startswith("frontend")])

    def test_opaque_type_change(self):
        self.card.write_text((FIX / "struct.swift").read_text())
        shutil.copy(FIX / "struct-any.o", self.objs / "Card.o")
        err = self.error()
        self.assertEqual(err.code, "unsupported-change")
        self.assertEqual([(b["kind"], b["type"]) for b in err.blockers], [("opaque-type", "Card")])

    def test_unreadable_source_compiles_first(self):
        self.card.write_text(CARD + "\nlet s = #/ never closed\n")
        err = self.error()
        self.assertEqual([b["kind"] for b in err.blockers], ["unparsed"])
        self.assertTrue([c for c in self.calls() if c.startswith("frontend")])

    def test_new_and_removed_files(self):
        (self.proj / "Sources/App/Extra.swift").write_text("struct Extra {}\n")
        err = self.error()
        self.assertEqual(err.code, "new-file")
        self.assertEqual(err.blockers[0]["kind"], "new-file")
        (self.proj / "Sources/App/Extra.swift").unlink()
        (self.proj / "Sources/App/TheApp.swift").unlink()
        err = self.error()
        self.assertEqual((err.code, err.blockers[0]["kind"]), ("unsupported-change", "removed-file"))
        with self.assertRaises(HelperError) as cm:
            patch.make_patch(self.base, ["Sources/App/Nope.swift"], 1, sign=False)
        self.assertEqual(cm.exception.code, "new-file")

    def test_nothing_changed(self):
        self.assertEqual(patch.source_changes(self.base).empty, True)
        self.assertEqual(self.error().code, "no-changes")

    def test_given_files_and_parallel_compile(self):
        self.card.write_text(CARD.replace("Hello", "Hi"))
        (self.proj / "Sources/App/TheApp.swift").write_text(APP_SWIFT.replace("Card()", "Card().padding()"))
        changes = patch.source_changes(self.base)
        self.assertEqual(changes.changed, ["Sources/App/Card.swift", "Sources/App/TheApp.swift"])
        res = patch.make_patch(self.base, [str(self.card), "Sources/App/TheApp.swift"], 2, sign=False)
        self.assertEqual(res.files, ["Sources/App/Card.swift", "Sources/App/TheApp.swift"])
        self.assertEqual(sorted(c for c in self.calls() if c.startswith("frontend")),
                         ["frontend Card.swift", "frontend TheApp.swift"])

    def test_not_interposable(self):
        app, _ = app_and_patch(bound=[], app_funcs=["_f", "_g"], patch_funcs=[])
        Path(self.base.app_binary).write_bytes(app)
        self.card.write_text(CARD.replace("Hello", "Hi"))
        self.assertEqual(self.error().code, "not-interposable")

    def test_cancel(self):
        self.card.write_text(CARD.replace("Hello", "Hi"))
        with self.assertRaises(subproc.Cancelled):
            self.make(cancel=lambda: True)
        self.assertFalse(list(self.base.patches_dir.glob(".patch-*")))
        self.assertFalse(list(self.base.patches_dir.glob("patch-*.dylib")))

    def test_keeps_newest_ten(self):
        self.card.write_text(CARD.replace("Hello", "Hi"))
        for seq in range(1, 13):
            res = self.make(seq=seq)
            os.utime(res.dylib, (1000 + seq, 1000 + seq))
        names = sorted(int(p.stem.split("-")[1]) for p in self.base.patches_dir.glob("patch-*.dylib"))
        self.assertEqual(names, list(range(3, 13)))

    def test_missing_baseline_copy(self):
        self.card.write_text(CARD.replace("Hello", "Hi"))
        shutil.rmtree(self.base.sources_dir)
        self.assertEqual(self.error().code, "no-baseline")

    # ---- review fixes (PR #46)
    def test_missing_baseline_object_is_refused_not_skipped(self):
        """Without the baseline .o the opaque-type check can't run: a changed `some View` body must not slip through."""
        self.card.write_text((FIX / "struct.swift").read_text())
        shutil.copy(FIX / "struct-any.o", self.objs / "Card.o")
        Path(self.base.objects["Sources/App/Card.swift"]).unlink()
        err = self.error()
        self.assertEqual(err.code, "unsupported-change")
        self.assertEqual(err.message, "Can't check Card.swift against the running app. Press Run to rebuild.")
        self.assertEqual(err.fix["action"], "hot-reload-restart")
        self.assertEqual([(b["kind"], b["file"]) for b in err.blockers], [("unchecked", "Sources/App/Card.swift")])
        self.assertFalse([c for c in self.calls() if c.startswith("frontend")])     # refused before compiling
        del self.base.objects["Sources/App/Card.swift"]                             # not captured at all
        self.assertEqual(self.error().blockers[0]["kind"], "unchecked")

    def test_totals_when_lists_are_capped(self):
        body = "".join(f"    @State var p{i} = false\n" for i in range(40))
        self.card.write_text(CARD.replace("struct Card: View {", "struct Card: View {\n" + body))
        js = self.error().to_json()
        self.assertEqual(len(js["blockers"]), 10)
        self.assertEqual(js["blockersTotal"], 40)
        self.assertIn("(and 39 more)", js["message"])
        broken = "".join("            COMPILE_ERROR\n" for _ in range(8))
        self.card.write_text(CARD.replace('Text("Hello")', 'Text("Hello")\n' + broken, 1))
        js = self.error().to_json()
        self.assertEqual((len(js["diagnostics"]), js["diagnosticsTotal"]), (2, 2))   # the fake: first error + note
        err = patch.PatchError("compile-failed", "x", diagnostics_=[{"severity": "error"}] * 12)
        self.assertEqual((len(err.to_json()["diagnostics"]), err.to_json()["diagnosticsTotal"]), (5, 12))
        self.assertNotIn("blockersTotal", err.to_json())

    def test_excluded_and_hidden_files_are_not_new(self):
        for rel in ("Sources/App/Excluded.swift", "Sources/App/Resources/Template.swift"):
            p = self.proj / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("struct Excluded {}\n")   # there at build time, not compiled: `exclude:` or a resource
        time.sleep(0.01)
        self.base.built_at = time.time()           # the build ends after them
        old = self.base.built_at - 60
        for rel in (".build/checkouts/Dep/Sources/Dep/Dep.swift", "Sources/App/.hidden/X.swift",
                    "Sources/App/Package.swift", "Sources/App/Vendor/Lib/Package.swift",
                    "Sources/App/Vendor/Lib/Sources/Lib/Lib.swift"):
            p = self.proj / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("// new\n")           # newer than the build, but never a source of this target
        self.assertTrue(patch.source_changes(self.base).empty)
        self.card.write_text(CARD.replace("Hello", "Hi"))
        self.assertEqual(self.make().files, ["Sources/App/Card.swift"])
        (self.proj / "Sources/App/Added.swift").write_text("struct Added {}\n")
        self.assertEqual(patch.source_changes(self.base).added, ["Sources/App/Added.swift"])
        moved = self.tmp / "Moved.swift"
        moved.write_text("struct Moved {}\n")
        os.utime(moved, (old, old))
        os.rename(moved, self.proj / "Sources/App/Moved.swift")   # mtime from before the build, ctime now
        self.assertIn("Sources/App/Moved.swift", patch.source_changes(self.base).added)

    def test_target_at_the_package_root(self):
        """A target with `path: "."`: its folder is the project, which also holds .build, .iosstudio and
        Package.swift - none of them are new files."""
        root = self.tmp / "Flat"
        root.mkdir()
        (root / "Main.swift").write_text("struct Main {}\n")
        base = buildinfo.Baseline(project=str(root), module="Flat", key="k", cache_dir=str(self.tmp / "flat-cache"),
                                  swiftc="swiftc", frontend={"Main.swift": ["swift-frontend", "-o", "x.o"]}, link=[],
                                  ld=None, app_binary="", app_path="", bundle_id=None, team=None,
                                  built_at=time.time(), captured_at=time.time())
        base.source_copy("Main.swift").parent.mkdir(parents=True)
        shutil.copy(root / "Main.swift", base.source_copy("Main.swift"))
        time.sleep(0.01)
        for rel in ("Package.swift", "Package@swift-6.0.swift", ".build/out/Gen.swift", ".iosstudio/x.swift",
                    ".swiftpm/y.swift"):
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text("// x\n")
        self.assertTrue(patch.source_changes(base).empty)
        (root / "Second.swift").write_text("struct Second {}\n")
        self.assertEqual(patch.source_changes(base).added, ["Second.swift"])

    def cwds(self) -> list[str]:
        p = self.tools / "cwds"
        return p.read_text().splitlines() if p.exists() else []

    def test_tools_run_in_the_builds_working_directory(self):
        self.assertEqual(patch.work_dir(self.base), str(self.proj / "xtool"))    # -Xcc -working-directory -Xcc …
        self.card.write_text(CARD.replace("Hello", "Hi"))
        self.make()
        patch.warm_up(self.base)
        self.assertEqual(self.cwds(), [str(self.proj / "xtool")] * 2)
        self.base.working_dir = str(self.proj)          # stored in the baseline: that one wins
        self.assertEqual(patch.work_dir(self.base), str(self.proj))
        del self.base.working_dir
        self.base.frontend = {k: [a for a in v if a not in ("-Xcc", "-working-directory", str(self.proj / "xtool"))]
                              for k, v in self.base.frontend.items()}
        self.assertEqual(patch.work_dir(self.base), str(self.proj))   # nothing recorded: the project folder

    def test_a_killed_session_leaves_no_compile_behind(self):
        """SIGKILL the process that runs warm_up (300 s timeout) mid-compile: the compile dies with it."""
        pid_file = self.tmp / "frontend.pid"
        code = ("import sys; from pathlib import Path; from iosstudio.features.hotreload import buildinfo, patch; "
                "b = buildinfo.load_baseline(sys.argv[1], Path(sys.argv[2])); patch.warm_up(b)")
        env = {**os.environ, "FAKE_FRONTEND_HANG": str(pid_file), "PYTHONPATH": str(ROOT / "helper")}
        proc = subprocess.Popen([sys.executable, "-c", code, str(self.proj), str(self.tmp / "cache")], env=env)
        child = None
        try:
            deadline = time.monotonic() + 20
            while not (pid_file.exists() and pid_file.read_text()) and time.monotonic() < deadline:
                time.sleep(0.05)
            child = int(pid_file.read_text())
            proc.kill()
            proc.wait(10)
            deadline = time.monotonic() + 5
            while _alive(child) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertFalse(_alive(child), "the compile outlived the killed session")
        finally:
            if child and _alive(child):
                os.kill(child, signal.SIGKILL)
            if proc.poll() is None:
                proc.kill()

    def test_warm_up(self):
        secs = patch.warm_up(self.base)
        self.assertIsInstance(secs, float)
        self.assertEqual(self.calls(), ["frontend Card.swift"])
        os.environ["FAKE_OBJ_DIR"] = str(self.tmp / "missing")
        self.assertIsNone(patch.warm_up(self.base))


def _alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"     # a zombie is dead, just not reaped yet
    except (OSError, IndexError):
        return False


if __name__ == "__main__":
    unittest.main()
