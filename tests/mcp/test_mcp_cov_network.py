"""Network tools (mcp/iosstudio_mcp/tools/network.py) against a fake device daemon that answers condition.* and
pcap.* like the real one (docs/deviced-protocol.md "Network conditions", "Packet capture"): typed results within
budget, one-line errors with a next step, the right annotations, a condition dry run that sends nothing, and the
capture job (state='running' + job_id, pollable with ios_job_status and the tool itself).

Needs the MCP server's dependencies (the dev venv; skipped otherwise):
    python -m pytest tests/mcp/test_mcp_cov_network.py
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest import mock

from iosstudio_mcp.core import budget as B
from iosstudio_mcp.tools import jobs as JOBS
from iosstudio_mcp.tools import network as N
from support.mcp_harness import ToolCase, err, registered, size_of


def profile(pid, name, wifi="ok", allowed=True, note=None):
    return {"id": pid, "name": name, "summary": f"{name} summary", "details": {}, "description": "", "wifi": wifi,
            "allowed": allowed, **({"note": note} if note else {})}


def groups(transport="usb"):
    wifi = transport == "wifi"
    return [
        {"id": "SlowNetworkCondition", "name": "Network Link", "kind": "network", "phoneActive": False,
         "activeProfile": "", "destructive": False,
         "profiles": [profile("SlowNetwork3G", "3G Network - best", "warn",
                              note="Your iPhone is connected over Wi-Fi, …" if wifi else None),
                      profile("SlowNetwork100PctLoss", "100% Loss", "blocked", allowed=not wifi,
                              note="Would cut the Wi-Fi tunnel." if wifi else None)]},
        {"id": "ThermalCondition", "name": "Thermal State", "kind": "thermal", "phoneActive": False,
         "activeProfile": "", "destructive": False, "profiles": [profile("ThermalFair", "Fair")]},
    ]


ACTIVE = {"active": True, "group": "ThermalCondition", "groupName": "Thermal State", "kind": "thermal",
          "profile": "ThermalFair", "name": "Fair", "setAt": 1793536000.0, "expiresAt": 1793536300.0, "timeoutS": 300,
          "remainingS": 284, "setBy": "gui-1", "transport": "usb"}


def listing(transport="usb", active=None):
    return {"groups": groups(transport), "transport": transport, "active": active or {"active": False},
            "phoneActive": [], "defaultTimeoutS": 300, "maxTimeoutS": 1800}


class Annotations(ToolCase):
    DAEMON = False

    async def test_kinds(self):
        a = registered(N).annotations
        self.assertTrue(a["ios_network_conditions"].read_only_hint)
        self.assertTrue(a["ios_network_conditions"].open_world_hint)
        self.assertTrue(a["ios_pcap_capture"].read_only_hint)
        self.assertFalse(a["ios_pcap_capture"].idempotent_hint)        # a new capture each time
        for name in ("ios_network_condition_set", "ios_network_condition_clear"):
            self.assertFalse(a[name].read_only_hint, name)
            self.assertFalse(a[name].destructive_hint, name)
            self.assertTrue(a[name].idempotent_hint, name)
            self.assertTrue(a[name].open_world_hint, name)


class Conditions(ToolCase):
    async def test_list_over_usb(self):
        self.daemon.replies["condition.list"] = (0, listing(active=ACTIVE))
        res = await self.call(N, "ios_network_conditions")
        self.assertLessEqual(size_of(res), B.DEFAULT_BYTES)
        d = res.structured_content
        self.assertEqual([g["kind"] for g in d["groups"]], ["network", "thermal"])
        p3g = d["groups"][0]["profiles"][0]
        self.assertNotIn("wifi", p3g)                  # the Wi-Fi rule only matters over Wi-Fi
        self.assertEqual(d["active"]["remaining_seconds"], 284)
        self.assertEqual(d["active"]["group"], "Thermal State")
        self.assertNotIn("Wi-Fi", d["note"])

    async def test_list_over_wifi_marks_the_risky_profiles(self):
        self.daemon.replies["condition.list"] = (0, listing("wifi"))
        d = await self.data(N, "ios_network_conditions")
        net = d["groups"][0]["profiles"]
        self.assertEqual((net[0]["wifi"], net[0].get("allowed", True)), ("warn", True))
        self.assertEqual((net[1]["wifi"], net[1]["allowed"]), ("blocked", False))
        self.assertNotIn("active", d)
        self.assertIn("Wi-Fi", d["note"])

    async def test_set_without_confirm_is_a_dry_run(self):
        self.daemon.replies["condition.list"] = (0, listing(active=ACTIVE))
        d = await self.data(N, "ios_network_condition_set", profile="SlowNetwork3G", minutes=3)
        self.assertFalse(d["done"])
        self.assertIn("confirm=true", d["message"])
        self.assertTrue(any("Replace the active condition Fair" in w for w in d["would"]))
        self.assertTrue(any("3 minute" in w for w in d["would"]))
        self.assertEqual(self.daemon.count("condition.set"), 0)

    async def test_set_with_confirm_applies_it(self):
        self.daemon.replies["condition.list"] = (0, listing())
        self.daemon.replies["condition.set"] = (0, {**ACTIVE, "profile": "SlowNetwork3G", "transport": "usb"})
        d = await self.data(N, "ios_network_condition_set", profile="SlowNetwork3G", minutes=2, confirm=True)
        self.assertTrue(d["done"])
        self.assertEqual(d["data"]["profile"], "SlowNetwork3G")
        self.assertIn("ios_network_condition_clear", d["next_steps"][0])
        self.assertEqual(self.daemon.sent("condition.set")[0],
                         {"profile": "SlowNetwork3G", "timeoutS": 120, "acceptWifiRisk": True})

    async def test_an_unknown_profile_lists_the_valid_ones(self):
        self.daemon.replies["condition.list"] = (0, listing())
        text = await self.error(N, "ios_network_condition_set", profile="Nope", confirm=True)
        self.assertIn("SlowNetwork3G", text)
        self.assertEqual(self.daemon.count("condition.set"), 0)

    async def test_a_profile_refused_over_wifi_is_an_error_even_with_confirm(self):
        self.daemon.replies["condition.list"] = (0, listing("wifi"))
        text = await self.error(N, "ios_network_condition_set", profile="SlowNetwork100PctLoss", confirm=True)
        self.assertIn("[unsafe-over-wifi] Next:", text)
        self.assertEqual(self.daemon.count("condition.set"), 0)

    async def test_a_device_error_has_the_features_next_step(self):
        self.daemon.replies["condition.list"] = (0, err("developer-mode-off", "Developer Mode is off."))
        text = await self.error(N, "ios_network_conditions")
        self.assertIn("[developer-mode-off] Next: Call ios_device_prepare", text)

    async def test_clear(self):
        self.daemon.replies["condition.clear"] = (0, {"active": False, "cleared": True})
        d = await self.data(N, "ios_network_condition_clear")
        self.assertTrue(d["done"])
        self.assertIn("back to normal", d["message"])
        self.daemon.replies["condition.clear"] = (0, {"active": False, "cleared": False})
        d = await self.data(N, "ios_network_condition_clear")
        self.assertIn("No condition", d["message"])


FILE = "/home/u/.cache/ios-studio/captures/cap-20261101-142233.pcapng"
SUMMARY = {"packets": 312, "bytes": 180214, "seconds": 3.2, "sentBytes": 40000, "receivedBytes": 140214,
           "byProcess": [{"process": "MyApp", "packets": 300, "bytes": 170000}], "processCount": 2,
           "topHosts": [{"host": f"203.0.113.{i}", "port": 443, "protocol": "TCP", "service": "HTTPS", "packets": 20,
                         "bytes": 12000} for i in range(15)],
           "hostCount": 15, "protocols": {"TCP": 250, "UDP": 62}, "services": {"HTTPS": 250, "QUIC": 50, "DNS": 12},
           "excludedTunnelPackets": 40}


def capture(running, **kw):
    return {"running": running, "file": FILE, "fileBytes": 41312, "seconds": 3.2 if not running else 1.0,
            "maxSeconds": 3, "maxMB": 50, "filter": {"process": "MyApp", "bundleId": "XTL-A.com.example.app"},
            "packets": 312, "bytes": 180214, "excluded": 40, "byProcess": SUMMARY["byProcess"],
            "stopReason": None if running else "time", "error": None, **({} if running else {"summary": SUMMARY}),
            **kw}


class Pcap(ToolCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        fast = SimpleNamespace(sleep=lambda s: _sleep(0.05))     # the job polls pcap.status every 1 s
        p = mock.patch.object(N, "asyncio", fast)
        p.start()
        self.addCleanup(p.stop)

    def statuses(self, *replies):
        seq = list(replies)

        def answer(args):
            return (0, seq.pop(0) if len(seq) > 1 else seq[0])
        self.daemon.replies["pcap.status"] = answer

    async def test_a_capture_is_a_job_then_its_summary(self):
        self.daemon.replies["pcap.start"] = (0, capture(True, seconds=0))
        self.statuses(capture(True), capture(False))
        d = await self.data(N, "ios_pcap_capture", bundle_id="com.example.app", seconds=3, wait_seconds=0)
        self.assertEqual(d["state"], "running")
        jid = d["job_id"]
        self.assertIn(f"ios_pcap_capture(job_id='{jid}'", d["next_steps"][0])
        self.assertEqual(self.daemon.sent("pcap.start")[0], {"maxSeconds": 3, "bundleId": "com.example.app"})
        st = await self.data(JOBS, "ios_job_status", job_id=jid, wait_seconds=4)
        self.assertEqual(st["state"], "succeeded")
        res = await self.call(N, "ios_pcap_capture", job_id=jid, wait_seconds=0)
        self.assertLessEqual(size_of(res), B.DEFAULT_BYTES)
        d = res.structured_content
        self.assertEqual((d["state"], d["packets"], d["stop_reason"]), ("done", 312, "time"))
        self.assertEqual(len(d["top_hosts"]), 10)                       # capped
        self.assertEqual(d["excluded_tunnel_packets"], 40)
        self.assertIn(FILE, d["next_steps"][0])

    async def test_over_wifi_the_start_error_says_plug_in_the_cable(self):
        self.daemon.replies["pcap.start"] = (0, err("needs-usb", "Packet capture needs a USB cable."))
        text = await self.error(N, "ios_pcap_capture", seconds=3)
        self.assertIn("[needs-usb] Next: Ask the user to connect the iPhone with a USB cable", text)
        self.assertEqual(self.state.jobs.jobs, {})

    async def test_an_unknown_app_gets_the_capture_hint(self):
        self.daemon.replies["pcap.start"] = (0, err("not-found", "No app com.nope."))
        text = await self.error(N, "ios_pcap_capture", bundle_id="com.nope", seconds=3)
        self.assertIn("Next: Check the bundle id with ios_app_list(scope='own').", text)

    async def test_a_capture_replaced_by_another_fails_its_job(self):
        self.daemon.replies["pcap.start"] = (0, capture(True))
        self.statuses(capture(True, file="/x/cap-other.pcapng"))
        d = await self.data(N, "ios_pcap_capture", seconds=3, wait_seconds=3)
        self.assertEqual(d["state"], "failed")
        self.assertIn("newer capture", d["error"])
        self.assertIn("USB", d["next_steps"][0])

    async def test_no_packets_suggests_making_traffic(self):
        self.daemon.replies["pcap.start"] = (0, capture(True))
        self.statuses(capture(False, packets=0, summary={}))
        d = await self.data(N, "ios_pcap_capture", seconds=3, wait_seconds=3)
        self.assertEqual(d["state"], "done")
        self.assertIn("No packets matched", d["next_steps"][0])

    async def test_cancel_stops_the_capture_on_the_phone_and_keeps_it(self):
        self.daemon.replies["pcap.start"] = (0, capture(True))
        self.daemon.replies["pcap.stop"] = (0, capture(False, stopReason="stopped"))
        # like the daemon: running until someone sends pcap.stop, then the finished capture
        self.daemon.replies["pcap.status"] = lambda a: (0, capture(False, stopReason="stopped")
                                                        if self.daemon.count("pcap.stop") else capture(True))
        d = await self.data(N, "ios_pcap_capture", seconds=3, wait_seconds=0)
        st = await self.data(JOBS, "ios_job_cancel", job_id=d["job_id"])
        self.assertEqual(self.daemon.count("pcap.stop"), 1)
        self.assertIn(st["state"], ("succeeded", "cancelled"))
        d = await self.data(N, "ios_pcap_capture", job_id=d["job_id"], wait_seconds=0)
        self.assertEqual((d["state"], d["stop_reason"], d["file"]), ("done", "stopped", FILE))

    async def test_a_job_id_of_another_kind_is_refused(self):
        job = self.state.jobs.run(lambda j: _noop(), "recording", title="x")
        text = await self.error(N, "ios_pcap_capture", job_id=job.id)
        self.assertIn("pcap", text)


_sleep = asyncio.sleep


async def _noop():
    return {}
