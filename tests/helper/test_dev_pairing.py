"""Pairing (helper/iosstudio/device/features/pairing/{real,demo}.py): pair.status, pair.start (USB and Wi-Fi
flows, as `pair` events), pair.code, pair.cancel.

The real side runs in a real Daemon; pymobiledevice3's pairing calls are faked on the `pmd3` namespace
(create_using_usbmux, tunnel_service.RemotePairingLockdownService / PairableHostInfo / serve_pairable_host,
iter_remote_pair_records_by_identifier), and Daemon.select is replaced so no tunnel is started.

    .venv/bin/pytest -q tests/helper/test_dev_pairing.py
    cd tests && PYTHONPATH=../helper:. ~/pymobile3-venv/bin/python -m unittest helper.test_dev_pairing
"""
from __future__ import annotations

import asyncio
import plistlib
import shutil
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from support import devtest
from support.pmd3_stub import stub_pmd3

devtest.isolate()

from iosstudio.core.output import HelperError  # noqa: E402

UDID = "00008150-PAIRING"


async def settle(pred, rounds: int = 500):
    for _ in range(rounds):
        if pred():
            return True
        await asyncio.sleep(0)
    return pred()


class RemotePairingCompletedError(Exception):
    pass


class UserDeniedPairingError(Exception):
    pass


class Phone:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.record = tmp / f"remote_{UDID}.plist"
        self.records: dict = {}
        self.paired = True
        self.usbmux_errors: list = []
        self.calls: list = []
        self.serve: object = None   # coroutine function standing in for serve_pairable_host

    def pmd3(self):
        phone = self

        class Lockdown:
            def __init__(self, paired):
                self.paired = paired

            async def close(self):
                phone.calls.append("lockdown.close")

        async def create_using_usbmux(serial, autopair=False, **kw):
            phone.calls.append(("usbmux", serial, autopair))
            if phone.usbmux_errors:
                raise phone.usbmux_errors.pop(0)
            return Lockdown(phone.paired or autopair)

        class RemotePairingLockdownService:
            identifier = "host-id-1"

            @classmethod
            async def create(cls, lockdown):
                return cls()

            async def connect(self, autopair):
                phone.calls.append(("connect", autopair))
                phone.record.write_bytes(plistlib.dumps({"public_key": b"k"}))
                phone.records[UDID] = phone.record
                raise RemotePairingCompletedError()

            async def close(self):
                phone.calls.append("svc.close")

        class PairableHostInfo:
            def __init__(self, name):
                self.name = name
                self.identifier = "host-id-2"

        async def serve_pairable_host(info, pin_callback, timeout, waiting_callback):
            phone.calls.append(("serve", info.name, timeout))
            return await phone.serve(info, pin_callback, waiting_callback)

        return types.SimpleNamespace(
            version="fake", create_using_usbmux=create_using_usbmux,
            iter_remote_pair_records_by_identifier=lambda: ((u, p, {}) for u, p in list(phone.records.items())),
            get_remote_pairing_record_filename=lambda udid: f"remote_{udid}",
            exceptions=types.SimpleNamespace(RemotePairingCompletedError=RemotePairingCompletedError),
            tunnel_service=types.SimpleNamespace(RemotePairingLockdownService=RemotePairingLockdownService,
                                                 PairableHostInfo=PairableHostInfo,
                                                 serve_pairable_host=serve_pairable_host))


class RealPairing(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        stub = stub_pmd3()
        stub.__enter__()
        self.addCleanup(stub.__exit__, None, None, None)
        from iosstudio.device import link
        from iosstudio.device.real import core, daemon
        self.events: list = []
        p = mock.patch.object(link, "REMOTE_DIR", self.tmp)   # where pymobiledevice3 keeps pair records
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(core, "BROKER", types.SimpleNamespace(emit=lambda n, d: self.events.append((n, d)),
                                                                    syslog=None))
        p.start()
        self.addCleanup(p.stop)
        self.phone = Phone(self.tmp)
        self.d = daemon.Daemon(self.phone.pmd3())
        self.addAsyncCleanup(self.d.shutdown)
        g = self.d.registry.lookup("pair.start").__globals__
        p = mock.patch.dict(g, {"DEVICE_CACHE": self.tmp / "devices.json"})
        p.start()
        self.addCleanup(p.stop)
        self.selected: list = []

        async def select(udid):
            self.selected.append(udid)
            self.d.session = types.SimpleNamespace(udid=udid, data={},
                                                   reconnect_now=lambda: self.selected.append("reconnect"))
            return self.d.session
        self.d.select = select

    def pair_events(self):
        return [d for n, d in self.events if n == "pair"]

    async def ended(self):
        self.assertTrue(await settle(lambda: self.pair_events()
                                     and self.pair_events()[-1]["phase"] in ("done", "failed", "cancelled")))
        return self.pair_events()[-1]

    async def test_status(self):
        self.d.wifi = {UDID: "/r/remote.plist"}
        self.d.lockdown = {UDID, "00008150-OTHER"}
        self.d.usb = {UDID}
        out = await self.d.dispatch("pair.status", {})
        self.assertEqual(out, {"records": [{"udid": "00008150-OTHER", "wifi": False, "usb": True, "path": ""},
                                           {"udid": UDID, "wifi": True, "usb": True, "path": "/r/remote.plist"}],
                               "usb": [UDID]})

    async def test_start_bad_args(self):
        with self.assertRaises(HelperError) as cm:
            await self.d.dispatch("pair.start", {"method": "bluetooth"})
        self.assertEqual(cm.exception.code, "bad-args")
        for args in ({"method": "usb"}, {"method": "usb", "udid": "not-plugged-in"}):
            with self.assertRaises(HelperError) as cm:
                await self.d.dispatch("pair.start", args)
            self.assertEqual(cm.exception.code, "unreachable", args)
        with self.assertRaises(HelperError) as cm:
            await self.d.dispatch("pair.code", {"code": "123456"})
        self.assertEqual(cm.exception.code, "bad-args")
        self.assertEqual(self.pair_events(), [])

    async def test_usb_flow(self):
        self.d.usb = {UDID}
        self.d.cache[UDID] = {"name": "Alex's iPhone"}
        out = await self.d.dispatch("pair.start", {})
        self.assertEqual(out, {"started": True, "method": "usb"})   # USB is the default with a cable
        last = await self.ended()
        self.assertEqual((last["phase"], last["method"], last["udid"], last["name"]),
                         ("done", "usb", UDID, "Alex's iPhone"))
        phases = [e["phase"] for e in self.pair_events()]
        self.assertEqual(phases, ["starting", "waiting", "done"])     # already trusted: no "trust" step
        self.assertIn(("connect", True), self.phone.calls)           # no record yet: autopair
        self.assertIn("svc.close", self.phone.calls)
        self.assertEqual(plistlib.loads(self.phone.record.read_bytes())["host_identifier"], "host-id-1")
        self.assertEqual(self.phone.record.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.d.wifi, {UDID: str(self.phone.record)})
        self.assertEqual(self.selected, [UDID, "reconnect"])

    async def test_usb_flow_asks_for_trust(self):
        self.d.usb = {UDID}
        self.phone.paired = False
        await self.d.dispatch("pair.start", {"method": "usb", "udid": UDID})
        await self.ended()
        self.assertEqual([e["phase"] for e in self.pair_events()], ["starting", "trust", "waiting", "done"])
        self.assertEqual([c[2] for c in self.phone.calls if c[0] == "usbmux"], [False, True])

    async def test_usb_denied(self):
        self.d.usb = {UDID}
        self.phone.usbmux_errors = [OSError("not paired"), UserDeniedPairingError("no")]
        await self.d.dispatch("pair.start", {"method": "usb"})
        last = await self.ended()
        self.assertEqual(last["phase"], "failed")
        self.assertIn("Trust", last["message"])
        self.assertIn("UserDeniedPairingError", last["detail"])
        self.assertEqual(self.selected, [])

    async def test_wifi_flow_shows_the_code(self):
        self.phone.records[UDID] = self.phone.record
        self.phone.record.write_bytes(plistlib.dumps({}))

        async def serve(info, on_pin, on_wait):
            on_wait(2.4)
            on_pin(123456)
            return types.SimpleNamespace(record_path=str(self.phone.record),
                                         peer_device=types.SimpleNamespace(udid=UDID, name="Alex's iPhone",
                                                                           model="iPhone18,1"))
        self.phone.serve = serve
        out = await self.d.dispatch("pair.start", {"method": "wifi", "timeout": 30})
        self.assertEqual(out, {"started": True, "method": "wifi"})
        last = await self.ended()
        self.assertEqual((last["phase"], last["udid"], last["name"]), ("done", UDID, "Alex's iPhone"))
        evs = self.pair_events()
        self.assertEqual([e["phase"] for e in evs], ["waiting", "waiting", "show-code", "done"])
        self.assertEqual(evs[1]["elapsed"], 2)
        self.assertEqual(evs[2]["code"], "123456")
        self.assertEqual(self.phone.calls[0][2], 30.0)
        self.assertEqual(self.d.cache[UDID]["productType"], "iPhone18,1")
        self.assertTrue((self.tmp / "devices.json").is_file())
        self.assertEqual(plistlib.loads(self.phone.record.read_bytes())["host_identifier"], "host-id-2")
        self.assertEqual(self.selected, [UDID, "reconnect"])

    async def test_wifi_timeout(self):
        async def serve(info, on_pin, on_wait):
            raise asyncio.TimeoutError()
        self.phone.serve = serve
        await self.d.dispatch("pair.start", {"method": "wifi"})
        last = await self.ended()
        self.assertEqual(last["phase"], "failed")
        self.assertIn("Paired Macs", last["message"])

    async def test_busy_cancel_and_shutdown(self):
        gate = asyncio.Event()

        async def serve(info, on_pin, on_wait):
            await gate.wait()
        self.phone.serve = serve
        await self.d.dispatch("pair.start", {"method": "wifi"})
        with self.assertRaises(HelperError) as cm:
            await self.d.dispatch("pair.start", {"method": "wifi"})
        self.assertEqual(cm.exception.code, "busy")
        await settle(lambda: self.pair_events())
        self.assertEqual(await self.d.dispatch("pair.cancel", {}), {})
        self.assertEqual(self.pair_events()[-1]["phase"], "cancelled")
        self.assertEqual(await self.d.dispatch("pair.cancel", {}), {})   # nothing running: fine
        # a new flow can start, and shutdown cancels it
        await self.d.dispatch("pair.start", {"method": "wifi"})
        await settle(lambda: len([c for c in self.phone.calls if c[0] == "serve"]) == 2)
        self.assertEqual(self.pair_events()[-1]["phase"], "waiting")
        await self.d.registry.run_hooks("shutdown", self.d)
        self.assertTrue(await settle(lambda: self.pair_events()[-1]["phase"] == "cancelled"))


    async def test_cancel_before_the_flow_runs_still_reports_cancelled(self):
        await self.d.dispatch("pair.start", {"method": "wifi"})     # task created, not yet run
        self.assertEqual(await self.d.dispatch("pair.cancel", {}), {})
        self.assertEqual([e["phase"] for e in self.pair_events()], ["cancelled"])
        self.assertEqual(self.pair_events()[-1]["method"], "wifi")

class DemoPairing(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from iosstudio.device.demo.daemon import DemoBackend
        self.b = DemoBackend(None)
        self.addAsyncCleanup(self.b.shutdown)

    async def test_status_shape_matches_real(self):
        out = await self.b.dispatch("pair.status", {})
        self.assertEqual(set(out), {"records", "usb"})
        self.assertEqual(set(out["records"][0]), {"udid", "wifi", "usb", "path"})
        self.assertEqual(await self.b.dispatch("pair.cancel", {}), {})

    async def test_start_names_the_method(self):
        out = await self.b.dispatch("pair.start", {"method": "wifi"})
        self.assertEqual(out, {"started": True, "method": "wifi"})

    async def test_bad_args_like_real(self):
        with self.assertRaises(HelperError):
            await self.b.dispatch("pair.start", {"method": "bluetooth"})
        with self.assertRaises(HelperError):
            await self.b.dispatch("pair.code", {"code": "123456"})


if __name__ == "__main__":
    unittest.main()
