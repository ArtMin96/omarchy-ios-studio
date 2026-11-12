"""Hot-reload patch pipeline end to end with the real toolchain (slow; skipped without ~/Projects/CrashLab, xtool,
the darwin SDK, openssl or rcodesign).

A copy of CrashLab (without .build/, xtool/ and .git/; CrashLab itself is never touched) gets the -interposable
linker flag, is built with `xtool dev build --ipa` (as Run over Wi-Fi builds it: only xtool/CrashLab.ipa, no .app;
an earlier build's .app left in the copy must not be used), and its baseline captured from the .ipa. Then a Text string in ContentView.swift
changes: make_patch must give a signed arm64 MH_DYLIB that exports ContentView.body's getter, which the app calls
through an indirect pointer. Changing CrashRow.body's view structure must be refused (opaque-type).

Set IOSSTUDIO_E2E_CRASHLAB to an already built copy (it's edited in place) to skip the ~4 min first build.
Timings are printed (pytest -s).
"""
from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

import pytest

from iosstudio.core import appdirs, subproc
from iosstudio.core.output import HelperError
from iosstudio.features.hotreload import buildinfo, macho, patch, signing
from support.hot_reload import OPENSSL, make_identity

CRASHLAB = Path.home() / "Projects/CrashLab"
XTOOL = subproc.which("xtool")
SDK = appdirs.sdk_bundle(Path.home() / ".config", Path.home())
BODY = "_$s8CrashLab11ContentViewV4bodyQrvg"
FLAG_BLOCK = """
for target in package.targets where target.name == "CrashLab" {
    target.linkerSettings = (target.linkerSettings ?? []) + [.unsafeFlags(["-Xlinker", "-interposable"])]
}
"""


def _copy_crashlab(dest: Path) -> Path:
    shutil.copytree(CRASHLAB, dest, ignore=shutil.ignore_patterns(".build", "xtool", ".git"), symlinks=True)
    pkg = dest / "Package.swift"
    pkg.write_text(pkg.read_text() + FLAG_BLOCK)
    return dest


@pytest.mark.slow
@unittest.skipUnless(CRASHLAB.is_dir() and XTOOL and SDK and OPENSSL and signing.rcodesign_path(),
                     "needs ~/Projects/CrashLab, xtool, the darwin SDK, openssl and rcodesign")
class CrashLabPatch(unittest.TestCase):
    def test_text_edit_patch_and_opaque_refusal(self):
        tmp = Path(os.path.realpath(tempfile.mkdtemp(prefix="hot-reload-e2e-")))
        given = os.environ.get("IOSSTUDIO_E2E_CRASHLAB")
        proj = Path(given) if given else _copy_crashlab(tmp / "CrashLab")
        self.assertNotEqual(proj.resolve(), CRASHLAB.resolve())
        timings: dict[str, float] = {}
        snap = None
        if not given:
            stale = proj / "xtool/CrashLab.app"            # an earlier cable build's leftover: never this build's
            stale.mkdir(parents=True)
            (stale / "Info.plist").write_bytes(b"not this build")
            os.utime(stale / "Info.plist", (time.time() - 3600,) * 2)
            os.utime(stale, (time.time() - 3600,) * 2)
            snap = buildinfo.snapshot_sources(proj, tmp / "cache")
            t0 = time.monotonic()
            env = subproc.tool_env("xtool", {"XDG_CONFIG_HOME": str(Path.home() / ".config")})
            rc = subproc.Runner().run([XTOOL, "dev", "build", "--ipa"], cwd=proj, env=env, timeout=1200)
            self.assertEqual(rc, 0, "xtool dev build --ipa failed")
            timings["build"] = time.monotonic() - t0
            self.assertTrue((proj / "xtool/CrashLab.ipa").is_file())

        content = proj / "Sources/CrashLab/ContentView.swift"
        original = content.read_text()
        try:
            t0 = time.monotonic()
            base = buildinfo.capture_baseline(proj, "CrashLab", tmp / "cache", snapshot=snap)
            if not given:
                self.assertTrue(base.app_path.startswith(str(proj / "xtool/CrashLab.ipa/")), base.app_path)
            timings["baseline"] = time.monotonic() - t0
            self.assertGreater(base.interposable, 0, "the app wasn't linked with -interposable")
            self.assertIsNotNone(base.ld)
            timings["warm_up"] = patch.warm_up(base) or -1

            data = make_identity(tmp / "xtool-data")
            content.write_text(original.replace("Each button crashes the app on purpose.",
                                                "Every button here crashes the app on purpose."))
            res = patch.make_patch(base, None, 1, data_dir=data)
            timings.update({f"patch1.{k}": v / 1000 for k, v in res.steps.items()})
            dylib = macho.MachO.load(res.dylib)
            self.assertEqual(dylib.filetype, macho.MH_DYLIB)
            self.assertEqual(dylib.cputype, macho.CPU_TYPE_ARM64)
            self.assertEqual(dylib.id_dylib, "@rpath/patch-1.dylib")
            self.assertTrue(dylib.has_code_signature)
            self.assertIn(BODY, dylib.exported("__TEXT", "__text"))
            app = macho.MachO.load(base.app_binary)
            self.assertIn(BODY, app.indirect_names())
            self.assertIn(BODY, res.swappable)
            self.assertEqual(res.files, ["Sources/CrashLab/ContentView.swift"])

            content.write_text(original.replace("Each button crashes", "Each button here crashes"))
            res = patch.make_patch(base, None, 2, data_dir=data)
            timings.update({f"patch2.{k}": v / 1000 for k, v in res.steps.items()})

            content.write_text(original.replace("                Text(crash.detail)\n",
                                                "                Text(crash.detail)\n                Text(\"more\")\n"))
            with self.assertRaises(HelperError) as cm:
                patch.make_patch(base, None, 3, data_dir=data)
            self.assertEqual(cm.exception.code, "unsupported-change")
            self.assertEqual([(b["kind"], b.get("type")) for b in cm.exception.blockers],
                             [("opaque-type", "CrashRow")])
        finally:
            content.write_text(original)
            print("\nhot-reload timings (s):", {k: round(v, 2) for k, v in timings.items()})
            if not given:
                shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
