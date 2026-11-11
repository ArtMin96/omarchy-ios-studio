"""Hot reload device feature (helper/iosstudio/device/features/hotreload/): hotreload.push, clean and runtime through
the real daemon with a fake house_arrest AFC at the pymobiledevice3 service boundary, plus the demo twin.

push writes Library/Caches/HotReload/patch-<seq>.dylib.part, renames it, and reads ack-<seq>.json every 50 ms until
waitMs; a fake runtime answers after a delay (or never: acked false). clean removes only patches and acks; runtime
reads runtime.json. An app that isn't installed is `not-installed`, an App Store app `not-dev-app`.

    .venv/bin/pytest -q tests/helper/test_dev_hotreload.py
    cd tests && PYTHONPATH=../helper:. ~/pymobile3-venv/bin/python -m unittest helper.test_dev_hotreload
"""
from __future__ import annotations

import asyncio
import json
import posixpath
import shutil
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from support import devtest
from support.pmd3_stub import stub_pmd3

devtest.isolate()

from iosstudio.core.output import HelperError  # noqa: E402
from iosstudio.device.features.hotreload import core as hr  # noqa: E402

UDID = "00008150-HOTRELOAD"
BID = "XTL-TEAM1.dev.iosstudio.crashlab"
STORE_APP = "com.example.notes"
DIR = "/Library/Caches/HotReload"


class Phone:
    """An app container as a dict {path: bytes | None (folder)} behind house_arrest, with a fake hot reload
    runtime: when patch-<seq>.dylib appears it writes ack-<seq>.json after `ack_delay` s (never when None)."""

    def __init__(self, g):
        self.NotFound = g["AfcFileNotFoundError"]
        self.AfcError = g["AfcException"]
        self.tree: dict[str, bytes | None] = {"/": None, "/Documents": None, "/Library": None,
                                              "/Library/Caches": None}
        self.opened: list = []
        self.renames: list[tuple[str, str]] = []
        self.ack_delay: float | None = 0.12
        self.ack: dict | bytes | None = None
        self.tasks: list[asyncio.Task] = []
        phone = self

        class HouseArrestService:
            @staticmethod
            async def create(rsd, bid, documents_only=False):
                afc = Afc(phone)
                phone.opened.append((bid, documents_only, afc))
                return afc
        self.HouseArrestService = HouseArrestService

    def loaded(self, path: str) -> None:
        name = posixpath.basename(path)
        if not (name.startswith("patch-") and name.endswith(".dylib")) or self.ack_delay is None:
            return
        seq = int(name[len("patch-"):-len(".dylib")])

        async def answer():
            await asyncio.sleep(self.ack_delay)
            body = self.ack if self.ack is not None else {
                "seq": seq, "ok": True, "ms": 3.1, "swapped": 4, "kept": 12, "vtable": 0, "error": None,
                "generation": seq}
            self.tree[f"{DIR}/ack-{seq}.json"] = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.tasks.append(asyncio.ensure_future(answer()))


class Afc:
    def __init__(self, phone: Phone):
        self.phone, self.stale, self.closed = phone, False, False
        self.handles: dict[int, list] = {}

    def _need(self, p):
        if self.stale or p not in self.phone.tree:
            raise self.phone.NotFound(f"no {p}", 8)

    async def stat(self, p):
        self._need(p)
        v = self.phone.tree[p]
        return {"st_ifmt": "S_IFDIR" if v is None else "S_IFREG", "st_size": 0 if v is None else len(v)}

    async def exists(self, p):
        if self.stale:
            raise self.phone.NotFound(f"no {p}", 8)
        return p in self.phone.tree

    async def makedirs(self, p):
        if self.stale:
            raise self.phone.NotFound(f"no {p}", 8)
        while p not in self.phone.tree:
            self.phone.tree[p] = None
            p = posixpath.dirname(p)

    async def listdir(self, p):
        self._need(p)
        return sorted(posixpath.basename(x) for x in self.phone.tree if posixpath.dirname(x) == p and x != p)

    async def fopen(self, p, mode):
        if mode == "r":
            self._need(p)
        elif self.stale or posixpath.dirname(p) not in self.phone.tree:
            raise self.phone.NotFound(f"no {posixpath.dirname(p)}", 8)
        h = len(self.handles) + 1
        self.handles[h] = [p, mode, 0, bytearray()]
        return h

    async def fread(self, h, n):
        p, _, pos, _ = self.handles[h]
        data = self.phone.tree[p][pos:pos + n]
        self.handles[h][2] += len(data)
        return data

    async def fwrite(self, h, data):
        self.handles[h][3] += data

    async def fclose(self, h):
        p, mode, _, buf = self.handles.pop(h)
        if mode == "w":
            self.phone.tree[p] = bytes(buf)

    async def rename(self, a, b):
        self._need(a)
        self.phone.tree[b] = self.phone.tree.pop(a)
        self.phone.renames.append((a, b))
        self.phone.loaded(b)

    async def rm(self, p, force=False):
        self._need(p)
        del self.phone.tree[p]
        return []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed = True
        return False


class Harness(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        stub = stub_pmd3()
        stub.__enter__()
        self.addCleanup(stub.__exit__, None, None, None)
        from iosstudio.device.real import core, daemon
        p = mock.patch.object(core, "BROKER", types.SimpleNamespace(emit=lambda n, d: None, syslog=None))
        p.start()
        self.addCleanup(p.stop)
        self.d = daemon.Daemon(types.SimpleNamespace(version="fake",
                                                     iter_remote_pair_records_by_identifier=lambda: iter(())))
        self.addAsyncCleanup(self.d.shutdown)
        self.assertIsNotNone(self.d.registry.lookup("hotreload.push"), self.d.registry.failed)
        files_g = self.d.registry.lookup("container.list").__globals__
        self.phone = Phone(files_g)
        self.installed: dict[str, dict] = {}
        self.installed_calls = 0

        async def installed_apps(pmd3, rsd, application_type="Any", bundle_identifiers=None):
            self.installed_calls += 1
            return {b: v for b, v in self.installed.items() if not bundle_identifiers or b in bundle_identifiers}
        for name, value in (("HouseArrestService", self.phone.HouseArrestService), ("installed_apps", installed_apps)):
            p = mock.patch.dict(files_g, {name: value})
            p.start()
            self.addCleanup(p.stop)

        s = daemon.Session(self.d, UDID)
        s.rsd = types.SimpleNamespace(udid=UDID, all_values={})
        s.where = {"transport": "wifi"}
        s.connected.set()

        async def no_ddi():
            pass
        s.ensure_ddi = no_ddi
        self.d.session = self.s = s
        self.dev_app()

    async def asyncTearDown(self):
        for t in self.phone.tasks:
            t.cancel()

    def dev_app(self, bid: str = BID, developer: bool = True) -> None:
        self.installed[bid] = {"CFBundleDisplayName": "CrashLab", "CFBundleExecutable": "CrashLab",
                               "Entitlements": {"get-task-allow": developer},
                               "SignerIdentity": "Apple Development: x" if developer else "Apple iPhone OS Application Signing"}

    def patch_file(self, size: int = 3000) -> Path:
        p = self.tmp / f"patch-{size}.dylib"
        p.write_bytes(bytes(range(256)) * (size // 256) + b"x" * (size % 256))
        return p

    async def push(self, **kw):
        return await self.d.dispatch("hotreload.push", {"bundleId": BID, "src": str(self.patch_file()), "seq": 1, **kw})


class Push(Harness):
    async def test_push_writes_part_renames_and_returns_the_ack(self):
        src = self.patch_file(2_500_000)            # several AFC chunks
        out = await self.d.dispatch("hotreload.push", {"bundleId": BID, "src": str(src), "seq": 7})
        self.assertEqual(set(out), {"seq", "bytes", "pushMs", "acked", "ack", "ackMs"})
        self.assertEqual((out["seq"], out["bytes"], out["acked"]), (7, 2_500_000, True))
        self.assertEqual(out["ack"], {"seq": 7, "ok": True, "ms": 3.1, "swapped": 4, "kept": 12, "vtable": 0, "reverted": 0,
                                      "error": None, "generation": 7})
        self.assertGreaterEqual(out["ackMs"], 100)
        self.assertLess(out["ackMs"], 2000)
        self.assertEqual(self.phone.renames, [(f"{DIR}/patch-7.dylib.part", f"{DIR}/patch-7.dylib")])
        self.assertEqual(self.phone.tree[f"{DIR}/patch-7.dylib"], src.read_bytes())
        self.assertNotIn(f"{DIR}/patch-7.dylib.part", self.phone.tree)
        self.assertEqual([(b, docs) for b, docs, _ in self.phone.opened], [(BID, False)])   # whole container

    async def test_second_push_reuses_the_connection_and_app_info(self):
        await self.push(seq=1)
        calls = self.installed_calls
        out = await self.push(seq=2)
        self.assertTrue(out["acked"])
        self.assertEqual(len(self.phone.opened), 1)
        self.assertEqual(self.installed_calls, calls)

    async def test_no_ack_in_time(self):
        self.phone.ack_delay = None                  # app not running, or a build without the runtime
        t0 = time.monotonic()
        out = await self.push(waitMs=300)
        self.assertGreaterEqual(time.monotonic() - t0, 0.3)
        self.assertEqual(out["acked"], False)
        self.assertNotIn("ack", out)
        self.assertNotIn("ackMs", out)
        self.assertIn(f"{DIR}/patch-1.dylib", self.phone.tree)

    async def test_wait_is_capped(self):
        self.assertEqual(hr.wait_ms({"waitMs": 999_999}), hr.WAIT_MAX_MS)
        self.assertEqual(hr.wait_ms({}), hr.WAIT_DEFAULT_MS)
        self.assertEqual(hr.wait_ms({"waitMs": -5}), 0)
        self.phone.ack_delay = None
        out = await self.push(waitMs=0)              # one look, no waiting
        self.assertFalse(out["acked"])

    async def test_failed_ack_and_unreadable_ack(self):
        self.phone.ack = {"seq": 1, "ok": False, "ms": 0.4, "swapped": 0, "kept": 0, "vtable": 0,
                          "error": "dlopen: symbol not found", "generation": 3}
        out = await self.push()
        self.assertEqual((out["acked"], out["ack"]["ok"], out["ack"]["error"]), (True, False, "dlopen: symbol not found"))
        self.phone.ack = b"{not json"
        out = await self.push(seq=2)
        self.assertEqual((out["acked"], out["ack"]["ok"]), (True, False))
        self.assertIn("couldn't be read", out["ack"]["error"])
        self.phone.ack = b"[" + b"1," * hr.ACK_MAX + b"1]"     # bounded read: too big is a failed ack
        out = await self.push(seq=3)
        self.assertEqual((out["acked"], out["ack"]["ok"]), (True, False))

    async def test_stale_connection_is_retried_once(self):
        await self.push(seq=1)
        self.phone.opened[0][2].stale = True          # the app was relaunched meanwhile
        out = await self.push(seq=2)
        self.assertTrue(out["acked"])
        self.assertEqual(len(self.phone.opened), 2)
        self.assertTrue(self.phone.opened[0][2].closed)

    async def test_not_installed_and_not_dev(self):
        self.installed.clear()
        with self.assertRaises(HelperError) as cm:
            await self.push()
        self.assertEqual(cm.exception.code, "not-installed")
        self.assertEqual(cm.exception.fix["page"], "projects")
        self.dev_app(STORE_APP, developer=False)
        with self.assertRaises(HelperError) as cm:
            await self.d.dispatch("hotreload.push", {"bundleId": STORE_APP, "src": str(self.patch_file()), "seq": 1})
        self.assertEqual(cm.exception.code, "not-dev-app")
        self.assertEqual(self.phone.opened, [])

    async def test_bad_args(self):
        for args, code in (({"src": str(self.patch_file()), "seq": 1}, "bad-args"),
                           ({"bundleId": BID, "src": str(self.patch_file())}, "bad-args"),
                           ({"bundleId": BID, "src": str(self.patch_file()), "seq": 0}, "bad-args"),
                           ({"bundleId": BID, "seq": 1}, "bad-args"),
                           ({"bundleId": BID, "src": str(self.tmp / "gone.dylib"), "seq": 1}, "not-found")):
            with self.assertRaises(HelperError) as cm:
                await self.d.dispatch("hotreload.push", args)
            self.assertEqual(cm.exception.code, code, args)


class CleanAndRuntime(Harness):
    async def test_clean_removes_only_patches_and_acks(self):
        self.assertEqual(await self.d.dispatch("hotreload.clean", {"bundleId": BID}), {"removed": 0})   # no folder yet
        keep = {f"{DIR}/runtime.json": b"{}", f"{DIR}/notes.txt": b"x", "/Library/Caches/other.bin": b"x"}
        mine = {f"{DIR}/patch-1.dylib": b"x", f"{DIR}/patch-2.dylib.part": b"x", f"{DIR}/ack-1.json": b"{}",
                f"{DIR}/ack-2.json.tmp": b"{}"}
        self.phone.tree.update({DIR: None, **keep, **mine})
        self.assertEqual(await self.d.dispatch("hotreload.clean", {"bundleId": BID}), {"removed": 4})
        for p in mine:
            self.assertNotIn(p, self.phone.tree)
        for p in keep:
            self.assertIn(p, self.phone.tree)

    async def test_runtime_absent_then_present(self):
        self.assertEqual(await self.d.dispatch("hotreload.runtime", {"bundleId": BID}), {"present": False})
        self.phone.tree[DIR] = None
        self.phone.tree[f"{DIR}/runtime.json"] = b'{"version": 1, "pid": 4242, "started": 1793383925}'
        self.assertEqual(await self.d.dispatch("hotreload.runtime", {"bundleId": BID}),
                         {"present": True, "runtime": {"version": 1, "pid": 4242, "started": 1793383925.0}})
        self.phone.tree[f"{DIR}/runtime.json"] = b"garbage"
        self.assertEqual(await self.d.dispatch("hotreload.runtime", {"bundleId": BID}), {"present": False})

    async def test_runtime_on_a_stale_connection_is_retried(self):
        await self.d.dispatch("hotreload.runtime", {"bundleId": BID})
        self.phone.tree.update({DIR: None, f"{DIR}/runtime.json": b'{"version": 1, "pid": 9, "started": 5}'})
        self.phone.opened[0][2].stale = True
        out = await self.d.dispatch("hotreload.runtime", {"bundleId": BID})
        self.assertTrue(out["present"])
        self.assertEqual(len(self.phone.opened), 2)

    async def test_not_installed(self):
        self.installed.clear()
        for cmd in ("hotreload.clean", "hotreload.runtime"):
            with self.assertRaises(HelperError) as cm:
                await self.d.dispatch(cmd, {"bundleId": BID})
            self.assertEqual(cm.exception.code, "not-installed", cmd)


class CoreParsing(unittest.TestCase):
    def test_ack_fields_are_typed(self):
        a = hr.parse_ack(b'{"seq": 4, "ok": true, "ms": 2, "swapped": "x", "kept": 3, "generation": 2}', 4)
        self.assertEqual(a, {"seq": 4, "ok": True, "ms": 2.0, "swapped": 0, "kept": 3, "vtable": 0, "reverted": 0, "error": None,
                             "generation": 2})
        self.assertEqual(hr.parse_ack(b'{"ok": false}', 9)["error"], "The app couldn't load this patch.")
        self.assertEqual(hr.parse_ack(b'"text"', 9)["ok"], False)

    def test_ours(self):
        for n in ("patch-1.dylib", "patch-12.dylib.part", "ack-3.json", "ack-3.json.tmp"):
            self.assertTrue(hr.is_ours(n), n)
        for n in ("runtime.json", "runtime.json.tmp", "patch-x.dylib", "notes.txt"):
            self.assertFalse(hr.is_ours(n), n)


class Demo(unittest.IsolatedAsyncioTestCase):
    async def test_demo_answers_like_the_real_one(self):
        from iosstudio.device.demo.core import APPS
        from iosstudio.device.demo.daemon import DemoBackend
        reg = DemoBackend(None).registry
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        src = tmp / "patch-1.dylib"
        src.write_bytes(b"x" * 1234)
        dev = next(b for _n, b, _h, _s, d, *_ in APPS if d)
        store = next(b for _n, b, _h, _s, d, *_ in APPS if not d)
        out = await reg.lookup("hotreload.push")(None, {"bundleId": dev, "src": str(src), "seq": 1})
        self.assertEqual(set(out), {"seq", "bytes", "pushMs", "acked", "ack", "ackMs"})
        self.assertEqual((out["bytes"], out["acked"], out["ack"]["ok"], out["ack"]["swapped"]), (1234, True, True, 3))
        self.assertEqual(reg.lookup("hotreload.clean")(None, {"bundleId": dev}), {"removed": 0})
        rt = reg.lookup("hotreload.runtime")(None, {"bundleId": dev})
        self.assertEqual((rt["present"], set(rt["runtime"])), (True, {"version", "pid", "started"}))
        with self.assertRaises(HelperError) as cm:
            reg.lookup("hotreload.runtime")(None, {"bundleId": "com.example.nope"})
        self.assertEqual(cm.exception.code, "not-installed")
        with self.assertRaises(HelperError) as cm:
            reg.lookup("hotreload.clean")(None, {"bundleId": store})
        self.assertEqual(cm.exception.code, "not-dev-app")


if __name__ == "__main__":
    unittest.main()
