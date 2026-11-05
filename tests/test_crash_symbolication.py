"""Crash symbolication (helper/crash_symbols.py): Mach-O UUIDs, the symbol cache and its pruning, matching
a .ips report's images to a kept build and resolving app frames to file:line. Stdlib only:
    python3 -m unittest discover -s tests

The fixture tests/fixtures/crash-symbolication/Crashy.dwarf is the dSYM DWARF of Crashy.swift, built for
arm64-apple-ios (build.sh rebuilds it). Being a Mach-O with the executable's LC_UUID, it stands in for the
kept executable. Resolving needs llvm-symbolizer (the Swift toolchain's); those tests skip without it.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "helper"))

import crash_symbols as S  # noqa: E402
import device_crash  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "crash-symbolication"
DWARF = FIX / "Crashy.dwarf"
UUID = "4c4c4476-5555-3144-a14b-4068033367a8"
APP = "/private/var/containers/Bundle/Application/0D6C1F00-0000-4000-8000-000000000000/Crashy.app/Crashy"
# Offsets in Crashy (from its disassembly): the return address after the call that traps in first()
# (`return d!`, line 6) and the return address of main's call to first() (line 14).
OFF_FIRST = 0x41D0
OFF_MAIN = 0x438C


def ips(uuid: str = UUID, crashed_frames=None, other_frames=None) -> str:
    header = {"app_name": "Crashy", "timestamp": "2026-11-05 10:00:00.00 +0000", "bug_type": "309",
              "os_version": "iPhone OS 27.1 (24A1)", "incident_id": "11111111-2222-4333-8444-555555555555"}
    images = [
        {"source": "P", "arch": "arm64", "base": 4365664256, "size": 32768, "uuid": uuid, "path": APP,
         "name": "Crashy"},
        {"source": "P", "arch": "arm64e", "base": 6000000000, "size": 4000000,
         "uuid": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "path": "/usr/lib/swift/libswiftCore.dylib",
         "name": "libswiftCore.dylib"},
    ]
    crashed = crashed_frames if crashed_frames is not None else [
        {"imageOffset": 0x1234, "symbol": "_assertionFailure(_:_:file:line:flags:)", "symbolLocation": 1240,
         "imageIndex": 1},
        {"imageOffset": OFF_FIRST, "imageIndex": 0},
        {"imageOffset": OFF_MAIN, "imageIndex": 0},
    ]
    threads = [{"triggered": True, "frames": crashed},
               {"frames": other_frames if other_frames is not None else [{"imageOffset": OFF_MAIN, "imageIndex": 0}]}]
    body = {"procName": "Crashy", "exception": {"type": "EXC_BREAKPOINT", "signal": "SIGTRAP"},
            "faultingThread": 0, "threads": threads, "usedImages": images}
    return json.dumps(header) + "\n" + json.dumps(body)


def have_symbolizer() -> bool:
    return bool(S.find_tool("llvm-symbolizer"))


class TempCache(unittest.TestCase):
    """Points the symbol cache and the state dir at a temporary folder."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="symb-"))
        self.cache = self.tmp / "symbols"
        self.patches = [mock.patch.object(S, "CACHE", self.cache), mock.patch.object(S, "INDEX", self.cache / "index.json"),
                        mock.patch.object(S.common, "STATE_DIR", self.tmp / "state")]
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
                mod = importlib.import_module("deviced_demo_features.crashes")
            except Exception as exc:  # noqa: BLE001
                self.skipTest(f"demo daemon modules not importable here ({exc})")
            with mock.patch.object(mod, "DIR", Path(tmp)):
                wg = mod.crash_pull("/WeatherGlance-2026-11-05-100000.ips")["summary"]
                ht = mod.crash_pull("/HabitTracker-2026-11-05-100000.ips")["summary"]
        self.assertEqual(wg["symbolication"]["status"], "full")
        self.assertIn("ForecastStore.swift:57", wg["frames"][4])
        self.assertEqual(wg["symbolication"]["frames"][4]["line"], 57)
        self.assertEqual(ht["symbolication"]["reason"], "no-build")


if __name__ == "__main__":
    unittest.main()
