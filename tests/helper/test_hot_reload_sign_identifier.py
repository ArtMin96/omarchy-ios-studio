"""A hot-reload patch is signed with the installed app's bundle id as its code-signing identifier (rcodesign
--binary-identifier), read from the built app's Info.plist for a .app and a .ipa baseline: iOS only loads a dylib
from the app's data container when that identifier matches the installed profile (`patch-N` was refused by AMFI).
No bundle id means no-baseline before anything compiles. And the app's load error keeps dyld's reason when the
container path is long (session.load_error_text)."""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from iosstudio.core.output import HelperError
from iosstudio.features.hotreload import buildinfo, patch, signing
from iosstudio.features.hotreload.session import LOAD_ERROR_SHOWN, load_error_text
from support import FIXTURES
from support.hot_reload import OPENSSL, app_and_patch, fake_build, fake_toolchain, make_identity, pack_ipa

FIX = FIXTURES / "hot-reload"
CARD = (FIX / "base.swift").read_text()
APP_SWIFT = "import SwiftUI\n\n@main\nstruct TheApp: App {\n    var body: some Scene { WindowGroup { Card() } }\n}\n"
XTL_ID = "XTL-ABCDE12345.dev.iosstudio.crashlab"


@unittest.skipUnless(OPENSSL, "needs openssl")
class PatchIdentifier(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp(prefix="hr-sid-")))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        env = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(env)))
        self.tools = fake_toolchain(self.tmp / "tools")
        objs = self.tmp / "objs"
        objs.mkdir()
        shutil.copy(FIX / "text-any.o", objs / "default.o")
        self.app_bin, dylib = app_and_patch(bound=["_f"], app_funcs=["_f", "_g"], patch_funcs=["_f", "_g"])
        (self.tmp / "patch.dylib").write_bytes(dylib)
        fake_sign = self.tmp / "rcodesign"
        fake_sign.write_text("#!/bin/sh\ncat > /dev/null\necho \"$@\" >> \"$(dirname \"$0\")/tools/calls\"\n")
        fake_sign.chmod(0o755)
        make_identity(self.tmp / "xtool-data")
        os.environ.update(FAKE_OBJ_DIR=str(objs), FAKE_PATCH=str(self.tmp / "patch.dylib"),
                          IOSSTUDIO_RCODESIGN=str(fake_sign), IOSSTUDIO_XTOOL_DATA=str(self.tmp / "xtool-data"))

    def build(self, bundle_id: str) -> Path:
        return fake_build(self.tmp / "App", self.tools, files={"Card.swift": CARD, "TheApp.swift": APP_SWIFT},
                          objects={"Card.swift": (FIX / "base-any.o").read_bytes(),
                                   "TheApp.swift": (FIX / "base-any.o").read_bytes()},
                          app_binary=self.app_bin, bundle_id=bundle_id)

    def sign_argv(self, proj: Path) -> list[str]:
        base = buildinfo.capture_baseline(proj, "App", self.tmp / "cache")
        (proj / "Sources/App/Card.swift").write_text(CARD.replace("Hello", "Hi"))
        res = patch.make_patch(base, None, 2, sign=True)
        self.assertTrue(res.signed)
        calls = (self.tools / "calls").read_text().splitlines()
        return next(c for c in calls if c.startswith("sign ")).split()

    def assert_identifier(self, argv: list[str], want: str) -> None:
        self.assertIn("--binary-identifier", argv)
        self.assertEqual(argv[argv.index("--binary-identifier") + 1], want)
        self.assertNotIn("patch-2", argv[argv.index("--binary-identifier") + 1])
        self.assertTrue(argv[-1].endswith("patch-2.dylib"))

    def test_app_baseline_signs_with_the_installed_id(self):
        self.assert_identifier(self.sign_argv(self.build(XTL_ID)), XTL_ID)

    def test_ipa_baseline_signs_with_the_installed_id(self):
        proj = self.build(XTL_ID)
        pack_ipa(proj / "xtool/App.app")
        self.assert_identifier(self.sign_argv(proj), XTL_ID)

    def test_plain_id_kept_as_built(self):
        self.assert_identifier(self.sign_argv(self.build("dev.example.app")), "dev.example.app")

    def test_no_bundle_id_is_no_baseline_before_compiling(self):
        proj = self.build(XTL_ID)
        base = buildinfo.capture_baseline(proj, "App", self.tmp / "cache")
        base.bundle_id = None
        (proj / "Sources/App/Card.swift").write_text(CARD.replace("Hello", "Hi"))
        with self.assertRaises(HelperError) as cm:
            patch.make_patch(base, None, 1, sign=True)
        self.assertEqual(cm.exception.code, "no-baseline")
        self.assertEqual(cm.exception.fix["action"], "hot-reload-restart")
        calls = (self.tools / "calls").read_text().splitlines() if (self.tools / "calls").exists() else []
        self.assertFalse([c for c in calls if c.startswith(("frontend", "sign "))])


class SignDylibIdentifier(unittest.TestCase):
    def test_empty_identifier_refused_without_running_rcodesign(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        with self.assertRaises(HelperError) as cm:
            signing.sign_dylib(tmp / "x.dylib", "ABCDE12345", tmp, rcodesign=str(tmp / "no-tool"), identifier="")
        self.assertEqual(cm.exception.code, "sign-failed")


CONTAINER = "/var/mobile/Containers/Data/Application/0B1C2D3E-4F5A-6B7C-8D9E-0F1A2B3C4D5E/Library/Caches/HotReload"


class LoadErrorText(unittest.TestCase):
    def test_long_container_paths_keep_the_reason(self):
        p = f"{CONTAINER}/patch-1.dylib"
        why = (f"could not load the patch: dlopen({p}, 0x0002): tried: '{p}' (code signature invalid in "
               f"<1A2B3C4D-1234-5678-9ABC-DEF012345678> '{p}' (errno=1) sliceOffset=0x00000000, codeBlobOffset="
               f"0x0000C450, codeBlobSize=0x00004A10), '/System/Volumes/Preboot/Cryptexes/OS{p}' (no such file), "
               f"'/private{p}' (code signature invalid in <1A2B> '/private{p}' (errno=1))")
        text = load_error_text(why)
        self.assertLessEqual(len(text), LOAD_ERROR_SHOWN)
        self.assertIn("code signature invalid", text)
        self.assertIn("patch-1.dylib", text)
        self.assertNotIn("Containers", text)

    def test_clips_in_the_middle_keeping_the_end(self):
        why = "could not load the patch: " + "x" * 2000 + " (code signature invalid)"
        text = load_error_text(why)
        self.assertLessEqual(len(text), LOAD_ERROR_SHOWN)
        self.assertTrue(text.startswith("could not load the patch:"))
        self.assertTrue(text.endswith("(code signature invalid)"))
        self.assertIn("…", text)

    def test_short_text_unchanged(self):
        self.assertEqual(load_error_text("missing symbol _foo"), "missing symbol _foo")


if __name__ == "__main__":
    unittest.main()
