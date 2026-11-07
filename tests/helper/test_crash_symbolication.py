"""Crash symbolication (helper/iosstudio/features/crash_symbols.py): Mach-O UUIDs, the symbol cache and its pruning, matching
a .ips report's images to a kept build and resolving app frames to file:line. Stdlib only:
    python -m pytest tests/helper/test_crash_symbolication.py

The fixture tests/fixtures/crash-symbolication/Crashy.dwarf is the dSYM DWARF of Crashy.swift, built for
arm64-apple-ios (build.sh rebuilds it). Being a Mach-O with the executable's LC_UUID, it stands in for the
kept executable. Resolving needs llvm-symbolizer (the Swift toolchain's); those tests skip without it.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from iosstudio.device.features.crashes import core as device_crash
from iosstudio.features import crash_symbols as S
from support.crash import APP, DWARF, FIX, OFF_FIRST, UUID, ips


def have_symbolizer() -> bool:
    return bool(S.find_tool("llvm-symbolizer"))


class TempCache(unittest.TestCase):
    """Points the symbol cache and the state dir at a temporary folder."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="symb-"))
        self.cache = self.tmp / "symbols"
        self.patches = [mock.patch.object(S, "CACHE", self.cache), mock.patch.object(S, "INDEX", self.cache / "index.json"),
                        mock.patch.object(S.appdirs, "STATE_DIR", self.tmp / "state")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fake_build(self, uuid: str, project: str, age: float, size: int = 10) -> Path:
        d = self.cache / uuid
        d.mkdir(parents=True)
        (d / "App").write_bytes(b"x" * size)
        (d / "meta.json").write_text(json.dumps({"uuid": uuid, "exe": "App", "project": project}))
        t = time.time() - age
        os.utime(d, (t, t))
        return d


class MachO(unittest.TestCase):
    def test_uuid_and_text_address(self):
        (s,) = S.macho_slices(DWARF)
        self.assertEqual(s["uuid"], UUID)
        self.assertEqual(s["text_vmaddr"], 0x100000000)
        self.assertEqual(s["cputype"], S.CPU_ARM64)

    def test_line_info(self):
        self.assertTrue(S.has_line_info(DWARF))
        self.assertFalse(S.has_line_info(FIX / "Crashy.swift"))

    def test_not_a_macho(self):
        self.assertEqual(S.macho_slices(FIX / "Crashy.swift"), [])
        self.assertEqual(S.macho_slices(FIX / "missing"), [])

    def test_universal_binary(self):
        thin = DWARF.read_bytes()
        import struct
        fat = struct.pack(">II", S.FAT_MAGIC, 1) + struct.pack(">iIIII", S.CPU_ARM64, 0, 4096, len(thin), 12)
        data = fat + b"\0" * (4096 - len(fat)) + thin
        with tempfile.NamedTemporaryFile(suffix=".bin") as f:
            f.write(data)
            f.flush()
            self.assertEqual([s["uuid"] for s in S.macho_slices(f.name)], [UUID])

    def test_norm_uuid(self):
        self.assertEqual(S.norm_uuid("4C4C44765555 3144A14B4068033367A8"), UUID)
        self.assertEqual(S.norm_uuid(UUID.upper()), UUID)
        self.assertEqual(S.norm_uuid("nope"), "")


class Images(unittest.TestCase):
    def test_app_images(self):
        self.assertTrue(S.is_app_image({"uuid": UUID, "path": APP}))
        self.assertTrue(S.is_app_image({"uuid": UUID, "path": APP.rsplit("/", 1)[0] + "/Frameworks/Kit.framework/Kit"}))
        self.assertFalse(S.is_app_image({"uuid": UUID, "path": "/usr/lib/swift/libswiftCore.dylib"}))
        self.assertFalse(S.is_app_image({"uuid": UUID, "path": "/System/Library/Frameworks/UIKit.framework/UIKit"}))
        self.assertFalse(S.is_app_image({"path": APP}))      # no uuid: nothing to match

    def test_parse_ips_still_reads_frames(self):
        s = device_crash.parse_ips(ips())
        self.assertEqual(s["crashedThread"], 0)
        self.assertEqual(s["frames"][1], f"1 Crashy + 0x{OFF_FIRST:x}")


@unittest.skipUnless(have_symbolizer(), "llvm-symbolizer not installed")
class Resolve(TempCache):
    def finder(self, uuid, name=None):
        return S.keep(DWARF, "/work/Crashy", "Crashy", "debug") if uuid == UUID else None

    def test_full(self):
        res = S.symbolicate(ips(), self.finder)
        self.assertEqual(res["status"], "full", res)
        self.assertEqual(res["project"], "/work/Crashy")
        first, main = res["frames"]["0:1"], res["frames"]["0:2"]
        self.assertEqual((first["file"], first["line"], first["column"]), ("Sources/Crashy/Crashy.swift", 6, 17))
        self.assertEqual(first["path"], "/work/Crashy/Sources/Crashy/Crashy.swift")
        self.assertEqual(main["line"], 14)
        self.assertIn("1:0", res["frames"])                     # other threads resolve too
        self.assertNotIn("0:0", res["frames"])                   # a system frame stays as it is
        if S.find_tool("swift-demangle"):
            self.assertEqual(first["function"], "Forecast.first()")

    def test_frame_zero_is_not_adjusted(self):
        # Frame 0 is the program counter itself: the trap instruction in first() (compiler-generated, no line).
        res = S.symbolicate(ips(crashed_frames=[{"imageOffset": OFF_FIRST, "imageIndex": 0}], other_frames=[]),
                            self.finder)
        f = res["frames"]["0:0"]
        self.assertIsNone(f.get("file"))
        self.assertIn("first", f["function"])

    def test_apply_rewrites_and_aligns(self):
        summary = device_crash.parse_ips(ips())
        S.apply(summary, S.symbolicate(ips(), self.finder))
        self.assertTrue(summary["frames"][1].endswith("(Sources/Crashy/Crashy.swift:6:17)"), summary["frames"][1])
        self.assertEqual(summary["frames"][0], device_crash.parse_ips(ips())["frames"][0])
        al = summary["symbolication"]["frames"]
        self.assertIsNone(al[0])
        self.assertEqual(al[1]["line"], 6)
        self.assertEqual(summary["symbolication"]["status"], "full")

    def test_sidecar_is_reused_when_full(self):
        p = self.tmp / "Crashy-2026-11-05-100000.ips"
        p.write_text(ips())
        first = S.symbolicate_file(p, self.finder)
        self.assertEqual(S.load_result(p)["status"], "full")
        boom = mock.Mock(side_effect=AssertionError("re-symbolicated"))
        self.assertEqual(S.symbolicate_file(p, boom), first)

    def test_keep_then_find_in_cache(self):
        meta = S.keep(DWARF, "/work/Crashy", "Crashy")
        self.assertEqual(meta["uuid"], UUID)
        self.assertTrue((self.cache / UUID / "Crashy.dwarf").is_file())
        self.assertEqual(S.cached(UUID.upper())["project"], "/work/Crashy")
        self.assertIn(UUID, json.loads((self.cache / "index.json").read_text()))
        self.assertEqual(S.symbolicate(ips(), lambda u, n=None: S.find_build(u, n, projects=[]))["status"], "full")

    def test_found_in_project_build_folder(self):
        proj = self.tmp / "Crashy"
        (proj / ".build" / "debug").mkdir(parents=True)
        shutil.copy(DWARF, proj / ".build" / "debug" / "Crashy-App")
        self.assertEqual(S.build_products(proj, "Crashy"), [proj / ".build" / "debug" / "Crashy-App"])
        meta = S.find_build(UUID, "Crashy", projects=[proj])
        self.assertEqual(meta["source"], "found")
        self.assertEqual(meta["project"], str(proj))
        self.assertIsNotNone(S.cached(UUID))                    # kept for next time

    def test_remember_build(self):
        proj = self.tmp / "Crashy"
        (proj / ".build" / "debug").mkdir(parents=True)
        shutil.copy(DWARF, proj / ".build" / "debug" / "Crashy-App")
        meta = S.remember_build(proj, "Crashy", "debug")
        self.assertEqual(meta["uuid"], UUID)
        self.assertIsNone(S.remember_build(self.tmp / "nothing", "Crashy"))


class Statuses(TempCache):
    def test_no_build(self):
        with mock.patch.object(S, "find_tool", return_value="/bin/true"):
            res = S.symbolicate(ips(), lambda u, n=None: None)
        self.assertEqual((res["status"], res["reason"]), ("none", "no-build"))
        self.assertIn(UUID, res["message"])
        self.assertIn("Rebuild", res["hint"])

    def test_pruned(self):
        S._record(UUID, {"project": "/work/Crashy"})
        with mock.patch.object(S, "find_tool", return_value="/bin/true"):
            res = S.symbolicate(ips(), lambda u, n=None: None)
        self.assertEqual(res["reason"], "pruned")

    def test_no_app_frames(self):
        res = S.symbolicate(ips(crashed_frames=[{"imageOffset": 1, "imageIndex": 1}], other_frames=[]))
        self.assertEqual(res["status"], "not-needed")

    def test_old_format(self):
        self.assertEqual(S.symbolicate("Incident Identifier: X\nnot json")["reason"], "unsupported")

    def test_no_symbolizer(self):
        with mock.patch.object(S, "find_tool", return_value=None):
            self.assertEqual(S.symbolicate(ips())["reason"], "no-symbolizer")


class Prune(TempCache):
    def test_keeps_newest_per_project_and_size(self):
        for i in range(S.KEEP_PER_PROJECT + 3):
            self.fake_build(f"0000000{i:x}-0000-0000-0000-000000000000", "/p/A", age=1000 + i)
        self.fake_build("bbbbbbbb-0000-0000-0000-000000000000", "/p/B", age=5000)
        S.prune()
        left = {d.name for d in self.cache.iterdir()}
        self.assertEqual(len([n for n in left if n.startswith("0000000")]), S.KEEP_PER_PROJECT)
        self.assertNotIn("0000000c-0000-0000-0000-000000000000", left)    # the oldest of A went
        self.assertIn("bbbbbbbb-0000-0000-0000-000000000000", left)       # B has one: it stays

    def test_size_cap(self):
        for i in range(4):
            self.fake_build(f"0000000{i}-0000-0000-0000-000000000000", f"/p/{i}", age=100 * (i + 1), size=1000)
        with mock.patch.object(S, "MAX_BYTES", 2500):
            S.prune()
        self.assertEqual(sorted(d.name[:8] for d in self.cache.iterdir()), ["00000000", "00000001"])


class Demo(unittest.TestCase):
    def test_demo_crash_resolves(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": tmp}):
            try:
                import importlib
                mod = importlib.import_module("iosstudio.device.features.crashes.demo")
            except Exception as exc:  # noqa: BLE001
                self.skipTest(f"demo daemon modules not importable here ({exc})")
            with mock.patch.object(mod, "DIR", Path(tmp)):
                wg = mod.crash_pull("/WeatherGlance-2026-11-05-100000.ips")["summary"]
                ht = mod.crash_pull("/HabitTracker-2026-11-05-100000.ips")["summary"]
        self.assertEqual(wg["symbolication"]["status"], "full")
        self.assertIn("ForecastStore.swift:57", wg["frames"][4])
        self.assertEqual(wg["symbolication"]["frames"][4]["line"], 57)
        self.assertEqual(ht["symbolication"]["reason"], "no-build")


class DaemonPull(TempCache):
    """crash.pull (helper/iosstudio/device/features/crashes.py) on a report that is already on this computer."""

    def pull(self, local: Path) -> dict:
        import asyncio
        import importlib
        try:
            mod = importlib.import_module("iosstudio.device.features.crashes.real")
        except Exception as exc:  # noqa: BLE001
            self.skipTest(f"daemon modules not importable here ({exc})")
        commands = {}

        class Reg:
            def command(self, name):
                return lambda fn: commands.setdefault(name, fn)

        class Dev:
            async def rsd(self, a):
                return mock.Mock(udid=local.parent.name), None

        mod.register(Reg())
        with mock.patch.object(mod, "CRASH_DIR", local.parent.parent):
            return asyncio.run(commands["crash.pull"](Dev(), {"name": "/" + local.name}))["summary"]

    def local_report(self, text: str) -> Path:
        local = self.tmp / "crashes" / "PHONE" / "Crashy-2026-11-05-100000.ips"
        local.parent.mkdir(parents=True)
        local.write_text(text)
        return local

    def test_status_always_present(self):
        s = self.pull(self.local_report(ips()))
        self.assertEqual(s["symbolication"]["status"], "none")
        self.assertEqual(s["symbolication"]["reason"], "no-build")
        self.assertTrue(s["symbolication"]["message"])

    def test_symbolicator_error_is_a_status(self):
        local = self.local_report(ips())
        with mock.patch.object(S, "symbolicate_file", side_effect=RuntimeError("boom")):
            s = self.pull(local)
        self.assertEqual(s["symbolication"]["status"], "none")
        self.assertEqual(s["symbolication"]["reason"], "error")
        self.assertIn("boom", s["symbolication"]["message"])
        self.assertTrue(s["frames"])


if __name__ == "__main__":
    unittest.main()
