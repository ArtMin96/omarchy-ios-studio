"""Network tab helpers (helper/iosstudio/device/network.py): condition presets and their Wi-Fi safety, the view of the
phone's condition-inducer list, timeouts, packet-capture header parsing and statistics, and the demo pcapng writer.

    .venv/bin/pytest -q tests/helper/test_dev_network.py
    cd tests && PYTHONPATH=../helper:. ~/pymobile3-venv/bin/python -m unittest helper.test_dev_network
"""
from __future__ import annotations

import ipaddress
import shutil
import struct
import tempfile
import unittest
from pathlib import Path

from support import devtest

devtest.isolate()

from iosstudio.device import network as net  # noqa: E402

# What the phone's DVT condition inducer reports (shape from iOS 26, trimmed).
RAW = [
    {"identifier": "SlowNetworkCondition", "name": "Network Link", "isActive": False, "isDestructive": False,
     "profiles": [
         {"identifier": "SlowNetwork3GAverage", "name": "3G",
          "description": "Name: 3G\nDownlink Bandwidth: 780 Kbps\nUplink Bandwidth: 330 Kbps\n"
                         "Downlink Latency: 100 ms\nDownlink Packet Loss Ratio: 0%"},
         {"identifier": "SlowNetwork100PctLoss", "name": "100% Loss",
          "description": "Name: 100% Loss\nDownlink Packet Loss Ratio: 100%"},
         {"identifier": "SlowNetworkDNS", "name": "DNS delay", "description": "DNS Delay: 2 s"},
         "not a profile"]},
    {"identifier": "ThermalCondition", "name": "Thermal State", "isActive": True, "activeProfile": "ThermalFair",
     "profiles": [{"identifier": "ThermalFair", "name": "Fair", "description": "The device is warm."}]},
    {"identifier": "Secret", "name": "Internal", "isInternal": True, "profiles": []},
    {"identifier": "GPUPerformanceState", "isDestructive": True, "profiles": [{"identifier": "GPUMin"}]},
    None,
]


class Conditions(unittest.TestCase):
    def test_wifi_safety_and_allowed(self):
        self.assertEqual(net.wifi_safety("ThermalCondition", "ThermalFair"), "ok")
        self.assertEqual(net.wifi_safety("SlowNetworkCondition", "SlowNetwork3GAverage"), "warn")
        for pid in net.WIFI_BLOCKED:
            self.assertEqual(net.wifi_safety("SlowNetworkCondition", pid), "blocked")
            self.assertEqual(net.profile_allowed("SlowNetworkCondition", pid, "usb"), (True, ""))
            self.assertEqual(net.profile_allowed("SlowNetworkCondition", pid, None), (True, ""))
            self.assertEqual(net.profile_allowed("SlowNetworkCondition", pid, "wifi"), (False, net.WIFI_BLOCKED_TEXT))
        self.assertEqual(net.profile_allowed("SlowNetworkCondition", "SlowNetwork3GAverage", "wifi"),
                         (True, net.WIFI_WARNING))
        self.assertEqual(net.profile_allowed("ThermalCondition", "ThermalFair", "wifi"), (True, ""))

    def test_details_and_summary(self):
        desc = RAW[0]["profiles"][0]["description"]
        det = net.parse_details(desc)
        self.assertNotIn("Name", det)
        self.assertEqual(det["Downlink Bandwidth"], "780 Kbps")
        self.assertEqual(net.short_summary(det, desc), "780 Kbps down · 330 Kbps up · 100 ms latency")   # 0% loss hidden
        loss = net.parse_details("Downlink Packet Loss Ratio: 100%")
        self.assertEqual(net.short_summary(loss, ""), "100% loss")
        self.assertEqual(net.short_summary({"Odd": "1"}, ""), "Odd 1")                 # unknown keys still shown
        self.assertEqual(net.short_summary({}, "Plain text\nsecond line"), "Plain text")
        self.assertEqual(net.short_summary({}, "x" * 500), "x" * 160)
        self.assertEqual(net.parse_details(None), {})

    def test_view_groups(self):
        view = net.view_groups(RAW, "wifi")
        self.assertEqual([g["id"] for g in view], ["SlowNetworkCondition", "ThermalCondition", "GPUPerformanceState"])
        self.assertEqual([g["kind"] for g in view], ["network", "thermal", "gpu"])
        netg, thermal, gpu = view
        self.assertEqual(set(netg), {"id", "name", "kind", "phoneActive", "activeProfile", "destructive", "profiles"})
        self.assertEqual([p["id"] for p in netg["profiles"]], ["SlowNetwork3GAverage", "SlowNetwork100PctLoss",
                                                              "SlowNetworkDNS"])
        three_g, loss, dns = netg["profiles"]
        self.assertEqual(set(three_g), {"id", "name", "summary", "details", "description", "wifi", "allowed", "note"})
        self.assertEqual(three_g["description"], "")           # parsed into details: not repeated
        self.assertEqual((three_g["wifi"], three_g["allowed"], three_g["note"]), ("warn", True, net.WIFI_WARNING))
        self.assertEqual((loss["wifi"], loss["allowed"]), ("blocked", False))
        self.assertEqual(dns["summary"], "2 s DNS delay")
        self.assertEqual(thermal["profiles"][0]["description"], "The device is warm.")   # unparsed: kept
        self.assertEqual(thermal["profiles"][0]["details"], {})
        self.assertEqual((thermal["phoneActive"], thermal["activeProfile"]), (True, "ThermalFair"))
        self.assertTrue(gpu["destructive"])
        self.assertEqual((gpu["name"], gpu["profiles"][0]["name"]), ("GPUPerformanceState", "GPUMin"))  # id fallback
        self.assertTrue(net.view_groups(RAW, "usb")[0]["profiles"][1]["allowed"])
        self.assertEqual(net.view_groups(None, "usb"), [])

    def test_find_profile(self):
        g, p = net.find_profile(RAW, "ThermalFair")
        self.assertEqual((g["identifier"], p["name"]), ("ThermalCondition", "Fair"))
        self.assertIsNone(net.find_profile(RAW, "nope"))
        self.assertIsNone(net.find_profile(None, "x"))

    def test_timeouts_and_reasons(self):
        self.assertEqual(net.clamp_timeout(None), net.DEFAULT_TIMEOUT_S)
        self.assertEqual(net.clamp_timeout("abc"), net.DEFAULT_TIMEOUT_S)
        self.assertEqual(net.clamp_timeout(1), net.MIN_TIMEOUT_S)
        self.assertEqual(net.clamp_timeout("90.7"), 90)
        self.assertEqual(net.clamp_timeout(10 ** 9), net.MAX_TIMEOUT_S)
        self.assertEqual(net.reason_for("released"), "cleared")
        self.assertEqual(net.reason_for("session"), "tunnel-lost")
        self.assertEqual(net.reason_for("client"), "client-gone")
        self.assertEqual(net.reason_for("whatever"), "whatever")


# ====================================================================== packet capture

class Frames(unittest.TestCase):
    def test_v4_tcp_round_trip(self):
        f = net.make_frame("192.168.1.20", "17.253.144.10", 6, 51000, 443, payload_len=100)
        h = net.parse_ip(f)
        self.assertEqual((h["v"], h["proto"], h["sport"], h["dport"]), (4, 6, 51000, 443))
        self.assertEqual(h["dst"], ipaddress.ip_address("17.253.144.10"))

    def test_v6_udp_and_extension_header(self):
        f = net.make_frame("fe80::1", "2001:db8::53", 17, 50000, 53)
        self.assertEqual(net.parse_ip(f)["v"], 6)
        # insert a hop-by-hop header (8 bytes, next header UDP) between IPv6 and UDP
        eth, ip6, l4 = f[:14], bytearray(f[14:54]), f[54:]
        ip6[6] = 0
        hop = bytes([17, 0]) + b"\x00" * 6
        h = net.parse_ip(eth + bytes(ip6) + hop + l4)
        self.assertEqual((h["proto"], h["sport"], h["dport"]), (17, 50000, 53))

    def test_bad_frames(self):
        self.assertIsNone(net.parse_ip(b"\x00" * 10))
        self.assertIsNone(net.parse_ip(b"\x00" * 14 + b"\x50" + b"\x00" * 30))   # IP version 5
        self.assertIsNone(net.parse_ip(b"\x00" * 14 + b"\x45" + b"\x00" * 5))    # truncated v4
        icmp = net.make_frame("10.0.0.1", "10.0.0.2", 6, 1, 2)[:14 + 20]
        icmp = icmp[:14 + 9] + bytes([1]) + icmp[14 + 10:]
        self.assertEqual((net.parse_ip(icmp)["proto"], net.parse_ip(icmp)["sport"]), (1, None))

    def test_proc_name(self):
        self.assertEqual(net.proc_name("nsurlsessiond", ecomm="Habits"), "Habits (via nsurlsessiond)")
        self.assertEqual(net.proc_name("Habits\x00\x00", ecomm="Habits"), "Habits")
        self.assertEqual(net.proc_name("", ecomm=""), "?")


class Stats(unittest.TestCase):
    def test_summary(self):
        s = net.CaptureStats()
        out = net.make_frame("192.168.1.20", "17.253.144.10", 6, 51000, 443, 60)
        back = net.make_frame("17.253.144.10", "192.168.1.20", 6, 443, 51000, 900)
        dns = net.make_frame("192.168.1.20", "192.168.1.1", 17, 50001, 53, 20)
        odd = net.make_frame("192.168.1.20", "192.168.1.30", 17, 50002, 50003, 0)
        s.add("Habits", "en0", out)
        s.add("Habits", "en0", back)
        s.add("mDNSResponder", "en0", dns)
        s.add("Habits", "pdp_ip0", odd, length=1000)
        s.add("Habits", "", b"\x00" * 20)
        sm = s.summary(12.34)
        self.assertEqual(sm["packets"], 5)
        self.assertEqual(sm["bytes"], len(out) + len(back) + len(dns) + 1000 + 20)
        self.assertEqual(sm["seconds"], 12.3)
        self.assertEqual(sm["sentBytes"], len(out) + len(dns) + 1000)
        self.assertEqual(sm["receivedBytes"], len(back))
        self.assertEqual(sm["byProcess"][0], {"process": "Habits", "packets": 4,
                                              "bytes": len(out) + len(back) + 1000 + 20})
        self.assertEqual(sm["processCount"], 2)
        top = sm["topHosts"][0]
        self.assertEqual(top, {"host": "17.253.144.10", "port": 443, "protocol": "TCP", "service": "HTTPS",
                               "packets": 2, "bytes": len(out) + len(back)})
        self.assertEqual(sm["hostCount"], 3)
        self.assertEqual(sm["protocols"], {"TCP": 2, "UDP": 2, "other": 1})
        self.assertEqual(sm["services"], {"HTTPS": 2, "DNS": 1, "UDP 50003": 1})
        self.assertEqual(sm["ipVersions"], {"IPv4": 4})
        self.assertEqual(sm["interfaces"], {"en0": 3, "pdp_ip0": 1, "?": 1})
        self.assertEqual((sm["excludedTunnelPackets"], sm["otherProcessPackets"]), (0, 0))

    def test_top_n_bounds_the_lists(self):
        s = net.CaptureStats()
        for i in range(30):
            s.add(f"p{i}", "en0", net.make_frame("192.168.1.2", f"17.0.0.{i}", 6, 50000 + i, 443))
        sm = s.summary(1, n=10)
        self.assertEqual((len(sm["topHosts"]), len(sm["byProcess"])), (10, 10))
        self.assertEqual((sm["hostCount"], sm["processCount"]), (30, 30))
        self.assertLessEqual(len(sm["interfaces"]), 5)

    def test_capture_names_sort_by_time(self):
        self.assertRegex(net.capture_name(1_700_000_000), r"^cap-\d{8}-\d{6}\.pcapng$")
        self.assertLess(net.capture_name(1_700_000_000), net.capture_name(1_700_000_061))


class Pcapng(unittest.TestCase):
    def test_writer_produces_valid_blocks(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        frames = [net.make_frame("10.0.0.1", "10.0.0.2", 17, 1000, 53, 3), net.make_frame("::1", "::2", 6, 1, 2)]
        path = tmp / "sub" / "cap.pcapng"
        size = net.write_pcapng(path, [(1.5, frames[0], "Habits"), (2.25, frames[1], "")])
        data = path.read_bytes()
        self.assertEqual(size, len(data))
        blocks, off = [], 0
        while off < len(data):
            btype, total = struct.unpack_from("<II", data, off)
            self.assertEqual(total % 4, 0)
            self.assertEqual(struct.unpack_from("<I", data, off + total - 4)[0], total)   # trailing length matches
            blocks.append((btype, data[off + 8:off + total - 4]))
            off += total
        self.assertEqual([b[0] for b in blocks], [0x0A0D0D0A, 1, 6, 6])
        self.assertEqual(struct.unpack_from("<I", blocks[0][1])[0], 0x1A2B3C4D)          # byte-order magic
        self.assertEqual(struct.unpack_from("<H", blocks[1][1])[0], 1)                   # Ethernet link type
        for (_, body), frame, ts in zip(blocks[2:], frames, (1.5, 2.25)):
            _iface, hi, lo, cap, orig = struct.unpack_from("<IIIII", body)
            self.assertEqual(((hi << 32) | lo), int(ts * 1_000_000))
            self.assertEqual((cap, orig), (len(frame), len(frame)))
            self.assertEqual(body[20:20 + cap], frame)
        self.assertIn(b"Habits", blocks[2][1])


if __name__ == "__main__":
    unittest.main()
