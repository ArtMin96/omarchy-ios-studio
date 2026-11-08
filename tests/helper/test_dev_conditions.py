"""Network/thermal/GPU conditions (helper/iosstudio/device/features/conditions/{real,demo}.py).

The real side runs in a real Daemon with a connected Session; the DVT connection (pmd3.DvtProvider) and the
condition inducer (the module's ConditionInducer) are fakes backed by one made-up phone that starts from the demo's
own condition list, so both sides answer the same input and their replies can be compared directly.

    .venv/bin/pytest -q tests/helper/test_dev_conditions.py
    cd tests && PYTHONPATH=../helper:. ~/pymobile3-venv/bin/python -m unittest helper.test_dev_conditions
"""
from __future__ import annotations

import asyncio
import copy
import types
import unittest
from unittest import mock

from support import devtest
from support.pmd3_stub import stub_pmd3

devtest.isolate()

from iosstudio.core.output import HelperError  # noqa: E402
from iosstudio.device import network as net  # noqa: E402

UDID = "00008150-CONDITIONS"
_SLEEP = asyncio.sleep


async def fast_sleep(delay, result=None):
    return await _SLEEP(0, result)


def shape(v):
    """Keys and value kinds of a reply (numbers are one kind), for real/demo parity checks."""
    if isinstance(v, dict):
        return {k: shape(x) for k, x in v.items()}
    if isinstance(v, list):
        return [shape(v[0])] if v else []
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, (int, float)):
        return "num"
    return type(v).__name__


class Phone:
    """The made-up phone's condition inducer state; ConditionInducer(dvt) channels all talk to it."""

    def __init__(self, groups):
        self.groups = copy.deepcopy(groups)
        for g in self.groups:
            g["isActive"], g["activeProfile"] = False, ""
        self.calls: list = []
        self.channels = 0
        self.closed = 0
        self.enable_error: BaseException | None = None

    def active(self):
        return [(g["identifier"], g["activeProfile"]) for g in self.groups if g["isActive"]]

    def inducer(self):
        phone = self

        class ConditionInducer:
            def __init__(self, dvt):
                self.dvt = dvt
                self.service = self
                phone.channels += 1

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                phone.closed += 1
                return False

            async def list(self):
                phone.calls.append("list")
                return copy.deepcopy(phone.groups)

            async def clear(self):
                phone.calls.append("clear")
                for g in phone.groups:
                    g["isActive"], g["activeProfile"] = False, ""

            async def enable_condition_with_identifier_profile_identifier_(self, gid, pid):
                phone.calls.append(("enable", gid, pid))
                if phone.enable_error:
                    raise phone.enable_error
                for g in phone.groups:
                    on = g["identifier"] == gid
                    g["isActive"], g["activeProfile"] = on, (pid if on else "")
        return ConditionInducer


def fake_pmd3(dvt_log: list):
    class Svc(devtest.Svc):
        pass

    class MobileImageMounterService(Svc):
        async def is_image_mounted(self, kind):
            return True

    class DvtProvider(Svc):
        async def __aenter__(self):
            dvt_log.append("open")
            return self

        async def __aexit__(self, *exc):
            dvt_log.append("close")
            return False

    return types.SimpleNamespace(version="fake", iter_remote_pair_records_by_identifier=lambda: iter(()),
                                 image_type_for_device=lambda rsd: "Developer",
                                 MobileImageMounterService=MobileImageMounterService, DvtProvider=DvtProvider)


def demo_groups():
    from iosstudio.device.features.conditions import demo
    return demo.GROUPS


class RealHarness:
    async def setup_real(self):
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
        p = mock.patch("asyncio.sleep", fast_sleep)   # close() polls the phone every 0.4 s
        p.start()
        self.addCleanup(p.stop)
        self.dvt_log: list = []
        self.d = daemon.Daemon(fake_pmd3(self.dvt_log))
        self.addAsyncCleanup(self.d.shutdown)
        self.phone = Phone(demo_groups())
        g = self.d.registry.lookup("condition.set").__globals__
        p = mock.patch.dict(g, {"ConditionInducer": self.phone.inducer()})
        p.start()
        self.addCleanup(p.stop)

    def connect(self, transport="usb"):
        s = self.daemon_mod.Session(self.d, UDID)
        s.rsd = types.SimpleNamespace(udid=UDID, all_values={})
        s.where = {"transport": transport}
        s.connected.set()
        self.d.session = s
        return s


class RealConditions(RealHarness, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await self.setup_real()

    def open_dvts(self):
        return self.dvt_log.count("open") - self.dvt_log.count("close")

    def cond_events(self):
        return [d for n, d in self.events if n == "condition"]

    async def test_list_reports_the_phones_groups(self):
        self.connect("usb")
        out = await self.d.dispatch("condition.list", {})
        self.assertEqual(out["groups"], net.view_groups(self.phone.groups, "usb"))
        self.assertEqual(out["transport"], "usb")
        self.assertEqual(out["active"], {"active": False})
        self.assertEqual(out["phoneActive"], [])
        self.assertEqual((out["defaultTimeoutS"], out["maxTimeoutS"]), (net.DEFAULT_TIMEOUT_S, net.MAX_TIMEOUT_S))
        self.assertEqual(self.phone.channels, self.phone.closed)   # a short-lived channel, closed again
        self.assertEqual(self.d.session.tunnel_data["conditionRaw"], self.phone.groups)

    async def test_set_status_clear(self):
        self.connect("usb")
        out = await self.d.dispatch("condition.set", {"profile": "SlowNetwork3G", "timeoutS": 5})
        self.assertIn(("enable", "SlowNetworkCondition", "SlowNetwork3G"), self.phone.calls)
        self.assertEqual(self.phone.active(), [("SlowNetworkCondition", "SlowNetwork3G")])
        self.assertTrue(out["active"])
        self.assertEqual((out["group"], out["profile"], out["kind"]), ("SlowNetworkCondition", "SlowNetwork3G", "network"))
        self.assertEqual(out["name"], "3G Network - best")
        self.assertEqual(out["timeoutS"], net.MIN_TIMEOUT_S)          # clamped up
        self.assertEqual(out["setBy"], "background")
        self.assertTrue(self.d.resources.held("condition"))
        self.assertEqual(self.open_dvts(), 2)     # the shared one (listing) + a dedicated one, held
        self.assertEqual(self.cond_events()[-1]["reason"], "set")
        self.assertIn(("deviceInfo", {"udid": UDID, "condition": {k: v for k, v in out.items() if k != "remainingS"}}),
                      self.events)

        st = await self.d.dispatch("condition.status", {"verify": True})
        self.assertTrue(st["active"])
        self.assertEqual(st["phoneActive"], [{"group": "SlowNetworkCondition", "profile": "SlowNetwork3G"}])
        info = self.d.registry.info(self.d, self.d.session)
        self.assertEqual(info["condition"]["profile"], "SlowNetwork3G")

        out = await self.d.dispatch("condition.clear", {})
        self.assertEqual(out, {"active": False, "cleared": True})
        self.assertIn("clear", self.phone.calls)
        self.assertEqual(self.phone.active(), [])
        self.assertFalse(self.d.resources.held("condition"))
        self.assertEqual(self.open_dvts(), 1)     # the dedicated one closed
        ended = self.cond_events()[-1]
        self.assertEqual((ended["active"], ended["reason"], ended["profile"]), (False, "cleared", "SlowNetwork3G"))
        self.assertEqual(await self.d.dispatch("condition.status", {}), {"active": False})

    async def test_set_replaces_the_previous_one(self):
        self.connect("usb")
        await self.d.dispatch("condition.set", {"profile": "ThermalFair"})
        n = len(self.cond_events())
        out = await self.d.dispatch("condition.set", {"profile": "GPUPerformanceStateMin"})
        self.assertEqual(out["kind"], "gpu")
        self.assertEqual(self.phone.active(), [("GPUPerformanceState", "GPUPerformanceStateMin")])
        self.assertEqual(self.d.resources.holders("condition"), 1)
        # replacing doesn't announce the old one as ended: the next event is the new one's "set"
        self.assertEqual([e["reason"] for e in self.cond_events()[n:]], ["set"])
        self.assertEqual(self.open_dvts(), 2)

    async def test_wifi_safety_rules(self):
        self.connect("wifi")
        with self.assertRaises(HelperError) as cm:
            await self.d.dispatch("condition.set", {"profile": "SlowNetwork100PctLoss"})
        self.assertEqual(cm.exception.code, "unsafe-over-wifi")
        with self.assertRaises(HelperError) as cm:
            await self.d.dispatch("condition.set", {"profile": "SlowNetwork3G"})
        self.assertEqual(cm.exception.code, "wifi-risk")
        self.assertFalse([c for c in self.phone.calls if c != "list"])   # nothing switched on
        out = await self.d.dispatch("condition.set", {"profile": "SlowNetwork3G", "acceptWifiRisk": True})
        self.assertEqual(out["transport"], "wifi")
        # thermal is fine over Wi-Fi without confirming
        out = await self.d.dispatch("condition.set", {"profile": "ThermalSerious"})
        self.assertEqual(out["kind"], "thermal")

    async def test_bad_args_and_unknown_profile(self):
        with self.assertRaises(HelperError) as cm:
            await self.d.dispatch("condition.set", {"profile": "SlowNetwork3G"})
        self.assertEqual(cm.exception.code, "no-device")
        self.connect()
        for args in ({}, {"profile": "  "}):
            with self.assertRaises(HelperError) as cm:
                await self.d.dispatch("condition.set", args)
            self.assertEqual(cm.exception.code, "bad-args")
        with self.assertRaises(HelperError) as cm:
            await self.d.dispatch("condition.set", {"profile": "Nope"})
        self.assertEqual(cm.exception.code, "not-found")
        with self.assertRaises(HelperError) as cm:
            await self.d.dispatch("condition.list", {"udid": "someone-else"})
        self.assertEqual(cm.exception.code, "bad-args")

    async def test_enable_failure_holds_nothing(self):
        self.connect()
        self.phone.enable_error = RuntimeError("refused")
        with self.assertRaises(RuntimeError):
            await self.d.dispatch("condition.set", {"profile": "ThermalFair"})
        self.assertFalse(self.d.resources.held("condition"))
        self.assertEqual(self.open_dvts(), 1)     # only the shared one: the dedicated one closed
        self.assertEqual(await self.d.dispatch("condition.status", {}), {"active": False})
        self.assertEqual(self.cond_events(), [])

    async def test_clear_leftover_on_the_phone(self):
        self.connect()
        self.assertEqual(await self.d.dispatch("condition.clear", {}), {"active": False, "cleared": False})
        g = self.phone.groups[1]
        g["isActive"], g["activeProfile"] = True, "ThermalCritical"   # left over from an earlier daemon
        self.assertEqual(await self.d.dispatch("condition.clear", {}), {"active": False, "cleared": True})
        self.assertEqual(self.phone.active(), [])
        self.assertEqual(await self.d.dispatch("condition.clear", {"force": False}), {"active": False, "cleared": False})

    async def test_tunnel_drop_ends_it_without_talking_to_the_phone(self):
        s = self.connect()
        await self.d.dispatch("condition.set", {"profile": "ThermalFair"})
        self.phone.calls.clear()
        s.closing = True   # no reconnect cleanup task in this test
        await self.d._tunnel_dropped(s)
        self.assertNotIn("clear", self.phone.calls)
        self.assertFalse(self.d.resources.held("condition"))
        self.assertEqual(self.cond_events()[-1]["reason"], "tunnel-lost")

    async def test_after_reconnect_clears_what_the_phone_still_has(self):
        s = self.connect()
        s.data["conditionUsed"] = True
        g = self.phone.groups[0]
        g["isActive"], g["activeProfile"] = True, "SlowNetworkEdge"
        await self.d.registry.run_hooks("tunnel_drop", self.d, s)
        task = s.data["conditionCleanup"]
        await asyncio.wait_for(task, 2)
        self.assertEqual(self.phone.active(), [])
        self.assertNotIn("conditionCleanup", s.data)

    async def test_welcome_snapshot(self):
        self.connect()
        self.assertEqual(await self.d.registry.welcome(self.d), [])
        await self.d.dispatch("condition.set", {"profile": "ThermalFair"})
        snap = await self.d.registry.welcome(self.d)
        self.assertEqual([(n, d["reason"], d["profile"]) for n, d in snap], [("condition", "set", "ThermalFair")])


class DemoHarness:
    async def setup_demo(self):
        from iosstudio.device.demo import core as dcore
        from iosstudio.device.demo.daemon import DemoBackend
        self.demo_events: list = []
        for p in (mock.patch.object(dcore, "BROKER",
                                    types.SimpleNamespace(emit=lambda n, d: self.demo_events.append((n, d)))),
                  mock.patch.dict(dcore.DEVICE), mock.patch.dict(dcore.INFO)):
            p.start()
            self.addCleanup(p.stop)
        groups = demo_groups()
        saved = copy.deepcopy(groups)
        self.addCleanup(lambda: groups.__setitem__(slice(None), saved))
        self.b = DemoBackend(None)
        self.addAsyncCleanup(self.b.shutdown)


class DemoConditions(DemoHarness, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await self.setup_demo()

    async def test_list_matches_real_for_the_same_phone(self):
        out = await self.b.dispatch("condition.list", {})
        self.assertEqual(out["transport"], "wifi")
        self.assertEqual(out["groups"], net.view_groups(demo_groups(), "wifi"))
        self.assertEqual(out["active"], {"active": False})
        self.assertEqual(out["phoneActive"], [])

    async def test_set_status_clear_and_rules(self):
        with self.assertRaises(HelperError) as cm:
            await self.b.dispatch("condition.set", {})
        self.assertEqual(cm.exception.code, "bad-args")
        with self.assertRaises(HelperError) as cm:
            await self.b.dispatch("condition.set", {"profile": "Nope"})
        self.assertEqual(cm.exception.code, "not-found")
        with self.assertRaises(HelperError) as cm:
            await self.b.dispatch("condition.set", {"profile": "SlowNetwork100PctLoss"})
        self.assertEqual(cm.exception.code, "unsafe-over-wifi")
        with self.assertRaises(HelperError) as cm:
            await self.b.dispatch("condition.set", {"profile": "SlowNetwork3G"})
        self.assertEqual(cm.exception.code, "wifi-risk")

        out = await self.b.dispatch("condition.set", {"profile": "SlowNetwork3G", "acceptWifiRisk": True, "timeoutS": 5})
        self.assertEqual(out["timeoutS"], net.MIN_TIMEOUT_S)
        st = await self.b.dispatch("condition.status", {"verify": True})
        self.assertEqual(st["phoneActive"], [{"group": "SlowNetworkCondition", "profile": "SlowNetwork3G"}])
        self.assertEqual(await self.b.dispatch("condition.clear", {}), {"active": False, "cleared": True})
        self.assertEqual(await self.b.dispatch("condition.clear", {}), {"active": False, "cleared": False})
        reasons = [d["reason"] for n, d in self.demo_events if n == "condition"]
        self.assertEqual(reasons, ["set", "cleared"])

    async def test_unplugging_the_cable_ends_it(self):
        self.assertEqual(await self.b.dispatch("demo.transport", {"transport": "usb"}), {"transport": "usb"})
        await self.b.dispatch("condition.set", {"profile": "SlowNetwork100PctLoss"})   # allowed over USB
        await self.b.dispatch("demo.transport", {"transport": "wifi"})
        self.assertFalse(self.b.resources.held("condition"))
        self.assertEqual([d["reason"] for n, d in self.demo_events if n == "condition"][-1], "tunnel-lost")


class Parity(RealHarness, DemoHarness, unittest.IsolatedAsyncioTestCase):
    """The same requests on both sides answer with the same shapes."""

    async def asyncSetUp(self):
        await self.setup_real()
        await self.setup_demo()

    async def test_shapes(self):
        self.connect("wifi")
        rd, dd = self.d, self.b
        calls = [("condition.list", {}), ("condition.status", {}),
                 ("condition.set", {"profile": "SlowNetwork3G", "acceptWifiRisk": True}),
                 ("condition.status", {"verify": True}), ("condition.list", {}),
                 ("condition.clear", {}), ("condition.clear", {}), ("condition.status", {})]
        for cmd, args in calls:
            r, m = await rd.dispatch(cmd, dict(args)), await dd.dispatch(cmd, dict(args))
            self.assertEqual(shape(r), shape(m), cmd)
            if cmd == "condition.list":
                self.assertEqual(r["groups"], m["groups"])
        rev = [shape(d) for n, d in self.events if n == "condition"]
        dev = [shape(d) for n, d in self.demo_events if n == "condition"]
        self.assertEqual(rev, dev)


if __name__ == "__main__":
    unittest.main()
