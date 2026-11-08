"""Packet capture (helper/iosstudio/device/features/pcap/{real,demo}.py).

The real side runs in a real Daemon with a connected Session; pcapd (the module's PcapdService) is a fake that
yields made-up packets carrying pcapd's per-packet process fields and writes them out like pymobiledevice3 does.

    .venv/bin/pytest -q tests/helper/test_dev_pcap.py
    cd tests && PYTHONPATH=../helper:. ~/pymobile3-venv/bin/python -m unittest helper.test_dev_pcap
"""
from __future__ import annotations

import asyncio
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
from iosstudio.device import network as net  # noqa: E402

UDID = "00008150-PCAP"


def shape(v):
    if isinstance(v, dict):
        return {k: shape(x) for k, x in v.items()}
    if isinstance(v, list):
        return [shape(v[0])] if v else []
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, (int, float)):
        return "num"
    return type(v).__name__


def pkt(pid, comm, iface="en0", size=100, epid=0, ecomm=""):
    frame = net.make_frame("192.168.1.42", "203.0.113.24", 6, 50000, 443, size)
    return types.SimpleNamespace(pid=pid, epid=epid, comm=comm + "\x00" * 4, ecomm=ecomm, interface_name=iface,
                                 data=frame)


async def settle(pred, rounds: int = 500):
    """Let the loop run until pred() holds (no wall-clock waiting)."""
    for _ in range(rounds):
        if pred():
            return True
        await asyncio.sleep(0)
    return pred()


class Pcapd:
    """What the tests set up for the next capture: its packets, whether it then blocks, and failures."""

    def __init__(self):
        self.packets: list = []
        self.block = False
        self.fail: BaseException | None = None
        self.opened = 0
        self.closed = 0
        self.release = asyncio.Event()

    def service(self):
        cfg = self

        class PcapdService(devtest.Svc):
            async def __aenter__(self):
                cfg.opened += 1
                return self

            async def __aexit__(self, *exc):
                cfg.closed += 1
                return False

            async def watch(self):
                for p in cfg.packets:
                    yield p
                if cfg.block:
                    await cfg.release.wait()

            async def write_to_pcap(self, f, packets):
                if cfg.fail:
                    raise cfg.fail
                async for p in packets:
                    f.write(p.data)
        return PcapdService


class RealHarness:
    async def setup_real(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        stub = stub_pmd3()
        stub.__enter__()
        self.addCleanup(stub.__exit__, None, None, None)
        from iosstudio.device.real import core, daemon
        self.daemon_mod = daemon
        self.events: list = []
        p = mock.patch.object(core, "BROKER", types.SimpleNamespace(emit=lambda n, d: self.events.append((n, d)),
                                                                    syslog=None))
        p.start()
        self.addCleanup(p.stop)
        self.d = daemon.Daemon(types.SimpleNamespace(version="fake", iter_remote_pair_records_by_identifier=lambda: iter(())))
        self.pcapd = Pcapd()
        self.found: dict | None = None

        async def find_app(udid, bid, fetch):
            return self.found
        g = self.d.registry.lookup("pcap.start").__globals__
        p = mock.patch.dict(g, {"PcapdService": self.pcapd.service(), "CAPTURE_DIR": self.tmp / "captures",
                                "find_app": find_app, "device_fetcher": lambda pmd3, rsd: None})
        p.start()
        self.addCleanup(p.stop)
        self.addAsyncCleanup(self.d.shutdown)

    def connect(self, transport="usb"):
        s = self.daemon_mod.Session(self.d, UDID)
        s.rsd = types.SimpleNamespace(udid=UDID, all_values={})
        s.where = {"transport": transport}
        s.connected.set()
        self.d.session = s
        return s

    async def started(self):
        # Let pcapd open before stopping: under Python 3.11 asyncio.wait_for can swallow a cancel that lands
        # while the service is still opening (fixed in 3.12; the daemon's pymobiledevice3 venv is newer).
        self.assertTrue(await settle(lambda: self.pcapd.opened))
        for _ in range(5):
            await asyncio.sleep(0)

    async def finished(self):
        self.assertTrue(await settle(lambda: not self.d.resources.held("pcap")
                                     and not (self.events and self.events[-1][1].get("running"))))
        return await self.d.dispatch("pcap.status", {})


class RealPcap(RealHarness, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await self.setup_real()

    async def test_needs_usb_and_a_session(self):
        with self.assertRaises(HelperError) as cm:
            await self.d.dispatch("pcap.start", {})
        self.assertEqual(cm.exception.code, "no-device")
        self.connect("wifi")
        with self.assertRaises(HelperError) as cm:
            await self.d.dispatch("pcap.start", {})
        self.assertEqual(cm.exception.code, "needs-usb")
        self.assertEqual(self.pcapd.opened, 0)

    async def test_bad_numbers(self):
        self.connect()
        for args in ({"maxSeconds": "soon"}, {"maxMB": [1]}, {"pid": "x"}):
            with self.assertRaises(HelperError) as cm:
                await self.d.dispatch("pcap.start", args)
            self.assertEqual(cm.exception.code, "bad-args", args)
        self.assertFalse(self.d.resources.held("pcap"))

    async def test_pid_filter_drops_tunnel_traffic(self):
        self.connect()
        self.pcapd.packets = [pkt(812, "Habits"), pkt(900, "Safari"),
                              pkt(77, "remoted", iface="anpi0"),        # the tunnel's own link ...
                              pkt(812, "Habits", iface="anpi0"),        # ... and anything else on it
                              pkt(5, "kernel_task", epid=812, ecomm="Habits")]   # pid matches epid too
        out = await self.d.dispatch("pcap.start", {"pid": "812", "maxSeconds": 1, "maxMB": 0})
        self.assertTrue(out["running"])
        self.assertEqual((out["maxSeconds"], out["maxMB"]), (3, net.PCAP_DEFAULT_MB))   # clamped / 0 = default
        self.assertEqual(out["filter"], {"pid": 812})
        self.assertEqual(out["startedBy"], "background")
        st = await self.finished()
        self.assertFalse(st["running"])
        self.assertEqual(st["stopReason"], "time")       # pcapd's stream ended
        self.assertEqual(st["packets"], 2)
        self.assertEqual(st["excluded"], 2)
        self.assertEqual(sorted(p["process"] for p in st["byProcess"]), sorted({net.proc_name("Habits", 0, "", 812),
                                                                             net.proc_name("kernel_task", 812, "Habits", 5)}))
        f = Path(st["file"])
        self.assertTrue(f.is_file())
        self.assertEqual(st["fileBytes"], f.stat().st_size)
        self.assertGreater(st["fileBytes"], 0)
        self.assertIn("summary", st)
        self.assertEqual((self.pcapd.opened, self.pcapd.closed), (1, 1))
        self.assertEqual(self.events[-1], ("pcap", st))

    async def test_bundle_id_resolves_to_the_executable(self):
        self.connect()
        with self.assertRaises(HelperError) as cm:
            await self.d.dispatch("pcap.start", {"bundleId": "com.example.gone"})
        self.assertEqual(cm.exception.code, "not-found")
        self.found = {"executable": "Habits", "bundleId": "XTL-T.com.example.habits"}
        self.pcapd.packets = [pkt(812, "Habits"), pkt(900, "Safari")]
        out = await self.d.dispatch("pcap.start", {"bundleId": "com.example.habits"})
        self.assertEqual(out["filter"], {"process": "Habits", "bundleId": "XTL-T.com.example.habits"})
        st = await self.finished()
        self.assertEqual(st["packets"], 1)

    async def test_busy_then_stop(self):
        self.connect()
        self.pcapd.block = True
        self.pcapd.packets = [pkt(1, "a")]
        await self.d.dispatch("pcap.start", {})
        with self.assertRaises(HelperError) as cm:
            await self.d.dispatch("pcap.start", {})
        self.assertEqual(cm.exception.code, "busy")
        await self.started()
        snap = await self.d.registry.welcome(self.d)
        self.assertEqual([n for n, _ in snap], ["pcap"])
        st = await self.d.dispatch("pcap.stop", {})
        self.assertEqual((st["running"], st["stopReason"]), (False, "stopped"))
        self.assertFalse(self.d.resources.held("pcap"))
        self.assertEqual(self.pcapd.closed, 1)
        self.assertEqual(await self.d.registry.welcome(self.d), [])
        # stopping again changes nothing
        self.assertEqual((await self.d.dispatch("pcap.stop", {}))["stopReason"], "stopped")

    async def test_size_limit(self):
        self.connect()
        self.pcapd.packets = [pkt(1, "a", size=1400) for _ in range(800)]   # > 1 MB
        self.pcapd.block = True
        await self.d.dispatch("pcap.start", {"maxMB": 1})
        st = await self.finished()
        self.assertEqual(st["stopReason"], "size")
        self.assertGreaterEqual(st["bytes"], 1 << 20)
        self.assertLess(st["packets"], 800)

    async def test_tunnel_drop_stops_it(self):
        s = self.connect()
        self.pcapd.block = True
        await self.d.dispatch("pcap.start", {})
        await self.started()
        await self.d._tunnel_dropped(s)
        st = await self.d.dispatch("pcap.status", {})
        self.assertEqual((st["running"], st["stopReason"]), (False, "tunnel-lost"))

    async def test_pcapd_failure_is_reported(self):
        self.connect()
        self.pcapd.fail = ConnectionResetError("gone")
        await self.d.dispatch("pcap.start", {})
        st = await self.finished()
        self.assertEqual(st["stopReason"], "error")
        self.assertEqual(st["error"]["code"], "unreachable")


class DemoHarness:
    async def setup_demo(self):
        from iosstudio.device.demo import core as dcore
        from iosstudio.device.demo.daemon import DemoBackend
        self.demo_events: list = []
        for p in (mock.patch.object(dcore, "BROKER",
                                    types.SimpleNamespace(emit=lambda n, d: self.demo_events.append((n, d)))),
                  mock.patch.dict(dcore.DEVICE)):
            p.start()
            self.addCleanup(p.stop)
        self.dcore = dcore
        self.b = DemoBackend(None)
        self.addAsyncCleanup(self.b.shutdown)


class DemoPcap(DemoHarness, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await self.setup_demo()

    async def test_rules(self):
        with self.assertRaises(HelperError) as cm:
            await self.b.dispatch("pcap.start", {})
        self.assertEqual(cm.exception.code, "needs-usb")
        self.dcore.DEVICE["transport"] = "usb"
        with self.assertRaises(HelperError) as cm:
            await self.b.dispatch("pcap.start", {"maxSeconds": "soon"})
        self.assertEqual(cm.exception.code, "bad-args")
        with self.assertRaises(HelperError) as cm:
            await self.b.dispatch("pcap.start", {"bundleId": "com.example.gone"})
        self.assertEqual(cm.exception.code, "not-found")
        out = await self.b.dispatch("pcap.start", {"bundleId": "com.example.habits"})
        self.assertEqual(out["filter"]["process"], "HabitTracker")
        with self.assertRaises(HelperError) as cm:
            await self.b.dispatch("pcap.start", {})
        self.assertEqual(cm.exception.code, "busy")
        st = await self.b.dispatch("pcap.stop", {})
        self.assertEqual((st["running"], st["stopReason"]), (False, "stopped"))
        self.assertTrue(Path(st["file"]).is_file())
        self.assertFalse(self.b.resources.held("pcap"))


class Parity(RealHarness, DemoHarness, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await self.setup_real()
        await self.setup_demo()

    async def test_shapes(self):
        self.connect()
        self.dcore.DEVICE["transport"] = "usb"
        self.pcapd.block = True
        self.pcapd.packets = [pkt(812, "Habits")]
        self.assertEqual(await self.d.dispatch("pcap.status", {}), await self.b.dispatch("pcap.status", {}))
        r = await self.d.dispatch("pcap.start", {"pid": 812})
        m = await self.b.dispatch("pcap.start", {"pid": 812})
        self.assertEqual(shape(r), shape(m))
        await self.started()
        r, m = await self.d.dispatch("pcap.status", {}), await self.b.dispatch("pcap.status", {})
        # byProcess entries come from the same net.CaptureStats; the demo has none before its first second
        r.pop("byProcess"), m.pop("byProcess")
        self.assertEqual(shape(r), shape(m))
        r = await self.d.dispatch("pcap.stop", {})
        m = await self.b.dispatch("pcap.stop", {})
        self.assertEqual(set(r), set(m))
        self.assertEqual(set(r["summary"]), set(m["summary"]))


if __name__ == "__main__":
    unittest.main()
