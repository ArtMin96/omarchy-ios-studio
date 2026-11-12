"""buildinfo.find_app / unpack_ipa / capture_baseline with what a build leaves in xtool/: only a .ipa (a Wi-Fi
build), only a .app (a cable build), both with different times, a leftover .app next to a fresh .ipa, an app older
than the build, and hostile or broken .ipa files (path traversal, oversized entries, not a zip)."""
from __future__ import annotations

import os
import plistlib
import shutil
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from iosstudio.core.output import HelperError
from iosstudio.features.hotreload import buildinfo as B
from support.hot_reload import app_and_patch, fake_build, fake_toolchain, pack_ipa


def _age(p: Path, seconds: float) -> None:
    """Make p (and a .app's files) look `seconds` old."""
    t = time.time() - seconds
    for q in ([*p.rglob("*")] if p.is_dir() else []) + [p]:
        os.utime(q, (t, t))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp(prefix="hr-ipa-")))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cache = self.tmp / "cache"

    def no_baseline(self, fn, *args, **kw) -> HelperError:
        with self.assertRaises(HelperError) as cm:
            fn(*args, **kw)
        self.assertEqual(cm.exception.code, "no-baseline")
        self.assertEqual(cm.exception.fix["action"], "hot-reload-restart")
        self.assertNotIn("build the app first", cm.exception.message)
        return cm.exception

    def leftovers(self) -> list[str]:
        return [p.name for p in self.cache.iterdir() if p.name.startswith(".")] if self.cache.is_dir() else []


class Capture(Base):
    def setUp(self):
        super().setUp()
        self.tools = fake_toolchain(self.tmp / "tools")
        self.app_bin, _ = app_and_patch(["_f"], ["_f", "_g"], [])
        self.proj = fake_build(self.tmp / "App", self.tools, app_binary=self.app_bin)
        self.app = self.proj / "xtool/App.app"

    def project_files(self) -> set[str]:
        return {str(p.relative_to(self.proj)) for p in self.proj.rglob("*")}

    def test_only_an_ipa(self):
        ipa = pack_ipa(self.app)
        before = self.project_files()
        base = B.capture_baseline(self.proj, "App", self.cache)
        self.assertEqual((self.cache / base.key / "app.bin").read_bytes(), self.app_bin)
        self.assertEqual((base.bundle_id, base.team, base.interposable), ("dev.example.app", "ABCDE12345", 1))
        self.assertEqual(base.app_path, str(ipa / "Payload/App.app/App"))
        self.assertEqual(base.built_at, ipa.stat().st_mtime)
        self.assertEqual(self.leftovers(), [])                    # the unpacked app is gone
        self.assertEqual(self.project_files(), before)            # and nothing was written into the project

    def test_only_an_ipa_after_the_snapshot(self):
        _age(self.app, 3600)
        snap = B.snapshot_sources(self.proj, self.cache)          # the build starts …
        pack_ipa(self.app)                                         # … and leaves only a .ipa
        base = B.capture_baseline(self.proj, "App", self.cache, snapshot=snap)
        self.assertEqual(Path(base.app_binary).read_bytes(), self.app_bin)
        self.assertEqual(self.leftovers(), [])

    def test_an_unsigned_ipa_has_no_team(self):
        (self.app / "embedded.mobileprovision").unlink()
        pack_ipa(self.app)
        self.assertIsNone(B.capture_baseline(self.proj, "App", self.cache).team)

    def test_only_an_app(self):
        snap = B.snapshot_sources(self.proj, self.cache)
        for p in self.app.iterdir():
            os.utime(p)                                            # the build re-signed it
        base = B.capture_baseline(self.proj, "App", self.cache, snapshot=snap)
        self.assertEqual(base.app_path, str(self.app / "App"))

    def test_the_newer_of_app_and_ipa(self):
        other, _ = app_and_patch(["_h"], ["_h"], [])
        pack_ipa(self.app, remove=False)
        (self.app / "App").write_bytes(other)
        _age(self.app.with_suffix(".ipa"), 60)                    # the .app is newer: a cable build after Wi-Fi
        self.assertEqual(B.find_app(self.proj, "App"), self.app)
        self.assertEqual(Path(B.capture_baseline(self.proj, "App", self.cache).app_binary).read_bytes(), other)
        _age(self.app, 120)                                        # now the .ipa is newer
        self.assertEqual(B.find_app(self.proj, "App"), self.app.with_suffix(".ipa"))
        self.assertEqual(Path(B.capture_baseline(self.proj, "App", self.cache).app_binary).read_bytes(),
                         self.app_bin)

    def test_a_stale_app_next_to_a_fresh_ipa(self):
        snap = B.snapshot_sources(self.proj, self.cache)
        pack_ipa(self.app, remove=False)
        (self.app / "App").write_bytes(b"an earlier build")       # would fail the capture if it were used
        _age(self.app, 3600)
        base = B.capture_baseline(self.proj, "App", self.cache, snapshot=snap)
        self.assertEqual(Path(base.app_binary).read_bytes(), self.app_bin)

    def test_a_signed_app_in_an_old_folder_is_fresh(self):
        # signing rewrites files inside the .app without changing the folder's own time
        snap = B.snapshot_sources(self.proj, self.cache)
        t = time.time() - 3600
        os.utime(self.app, (t, t))
        self.assertEqual(B.find_app(self.proj, "App", snap.started), self.app)
        B.discard_snapshot(snap)

    def test_nothing_from_this_build(self):
        pack_ipa(self.app, remove=False)
        _age(self.app, 3600)
        _age(self.app.with_suffix(".ipa"), 3000)
        snap = B.snapshot_sources(self.proj, self.cache)
        err = self.no_baseline(B.capture_baseline, self.proj, "App", self.cache, snapshot=snap)
        self.assertIn("this build made", err.message)
        self.assertEqual(B.find_app(self.proj, "App"), self.app.with_suffix(".ipa"))   # without a start time: newest

    def test_nothing_at_all(self):
        shutil.rmtree(self.app)
        (self.proj / "xtool/.xtool-tmp/App.app").mkdir(parents=True)  # xtool's work folder doesn't count
        err = self.no_baseline(B.capture_baseline, self.proj, "App", self.cache)
        self.assertIn("Press Run", err.message)

    def test_a_tie_goes_to_the_module(self):
        other = self.proj / "xtool/Other.app"
        shutil.copytree(self.app, other)
        t = time.time()
        for app in (self.app, other):
            for p in [*app.rglob("*"), app]:
                os.utime(p, (t, t))
        self.assertEqual(B.find_app(self.proj, "App"), self.app)
        self.assertEqual(B.find_app(self.proj, "Other"), other)


class Unpack(Base):
    def setUp(self):
        super().setUp()
        self.dest = self.tmp / "dest"
        self.dest.mkdir()
        self.info = plistlib.dumps({"CFBundleExecutable": "App", "CFBundleIdentifier": "dev.example.app"})

    def ipa(self, entries: dict[str, bytes]) -> Path:
        p = self.tmp / "App.ipa"
        with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
            for name, data in entries.items():
                z.writestr(name, data)
        return p

    def outside(self) -> list[str]:
        return sorted(str(p.relative_to(self.tmp)) for p in self.tmp.rglob("*")
                      if p != self.tmp / "App.ipa" and self.dest not in (p, *p.parents))

    def test_only_what_hot_reload_reads(self):
        ipa = self.ipa({"Payload/App.app/Info.plist": self.info, "Payload/App.app/App": b"exe",
                        "Payload/App.app/Assets.car": b"x" * 1000, "Payload/App.app/Frameworks/F.framework/F": b"f",
                        "Payload/App.app/embedded.mobileprovision": b"prov", "../evil": b"no",
                        "Payload/../../evil.app/x": b"no", "/abs/evil": b"no"})
        app = B.unpack_ipa(ipa, "App", self.dest)
        self.assertEqual(app, self.dest / "App.app")
        self.assertEqual(sorted(p.name for p in app.iterdir()), ["App", "Info.plist", "embedded.mobileprovision"])
        self.assertEqual((app / "App").read_bytes(), b"exe")
        self.assertEqual((app / "App").stat().st_mtime, ipa.stat().st_mtime)
        self.assertEqual(self.outside(), [])

    def test_the_module_app_among_several(self):
        ipa = self.ipa({"Payload/Aaa.app/Info.plist": self.info, "Payload/Aaa.app/App": b"a",
                        "Payload/Mod.app/Info.plist": self.info, "Payload/Mod.app/App": b"m"})
        self.assertEqual(B.unpack_ipa(ipa, "Mod", self.dest).name, "Mod.app")

    def test_traversal_in_the_executable_name(self):
        for exe in ("../../evil", "/tmp/evil", "..", "a/b"):
            with self.subTest(exe=exe):
                shutil.rmtree(self.dest)
                self.dest.mkdir()
                info = plistlib.dumps({"CFBundleExecutable": exe})
                ipa = self.ipa({"Payload/App.app/Info.plist": info, f"Payload/App.app/{exe}": b"evil",
                                "evil": b"evil"})
                self.no_baseline(B.unpack_ipa, ipa, "App", self.dest)
                self.assertEqual(self.outside(), [])

    def test_traversal_in_the_app_name(self):
        ipa = self.ipa({"Payload/../App.app/Info.plist": self.info, "Payload/../App.app/App": b"x"})
        self.no_baseline(B.unpack_ipa, ipa, "App", self.dest)
        self.assertEqual(self.outside(), [])

    def test_oversized_entries(self):
        ipa = self.ipa({"Payload/App.app/Info.plist": self.info, "Payload/App.app/App": b"\0" * 5000})
        with mock.patch.object(B, "IPA_MAX_EXE", 4096):
            err = self.no_baseline(B.unpack_ipa, ipa, "App", self.dest)
        self.assertIn("too big", err.message)
        big = self.ipa({"Payload/App.app/Info.plist": b"\0" * 5000, "Payload/App.app/App": b"x"})
        with mock.patch.object(B, "IPA_MAX_SMALL", 4096):
            self.no_baseline(B.unpack_ipa, big, "App", self.tmp / "dest2")

    def test_broken_ipas(self):
        cases = {
            "not a zip": None,
            "no app": {"Payload/readme.txt": b"x"},
            "no Info.plist": {"Payload/App.app/App": b"x"},
            "bad Info.plist": {"Payload/App.app/Info.plist": b"<plist", "Payload/App.app/App": b"x"},
            "no executable": {"Payload/App.app/Info.plist": self.info},
            "executable is a folder": {"Payload/App.app/Info.plist": self.info, "Payload/App.app/App/": b""},
        }
        for i, (name, entries) in enumerate(cases.items()):
            with self.subTest(name):
                if entries is None:
                    ipa = self.tmp / "App.ipa"
                    ipa.write_bytes(b"PK\x03\x04 garbage")
                else:
                    ipa = self.ipa(entries)
                self.no_baseline(B.unpack_ipa, ipa, "App", self.tmp / f"d{i}")


if __name__ == "__main__":
    unittest.main()
