"""helper/iosstudio/features/hotreload/patch.py: make_patch / warm_up against a fake xtool build and fake
swiftc / swift-frontend / clang / ld64.lld / rcodesign (tests/support/hot_reload.py) - the real code paths with
fakes at the process boundary. The compiled objects are small real ones (tests/fixtures/hot-reload/)."""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from iosstudio.core import subproc
from iosstudio.core.output import HelperError
from iosstudio.features.hotreload import buildinfo, macho, patch
from support import FIXTURES
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

    def test_warm_up(self):
        secs = patch.warm_up(self.base)
        self.assertIsInstance(secs, float)
        self.assertEqual(self.calls(), ["frontend Card.swift"])
        os.environ["FAKE_OBJ_DIR"] = str(self.tmp / "missing")
        self.assertIsNone(patch.warm_up(self.base))


if __name__ == "__main__":
    unittest.main()
