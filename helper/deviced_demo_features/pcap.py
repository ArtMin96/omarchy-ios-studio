"""Demo: pcap.start, pcap.stop, pcap.status with made-up traffic.

Like the real daemon, a capture needs the (made-up) USB cable: over Wi-Fi it answers needs-usb, and
`demo.transport {transport: "usb"}` (conditions.py) plugs it in. Packets are invented headers with
empty payloads, from documentation address ranges; the file is a small valid pcapng.
"""
from __future__ import annotations

import asyncio
import contextlib
import random
import time

import device_network as net
from common import HelperError
from deviced_demo_core import APPS, DEVICE, DIR, event

KEY = "pcap"
CAPTURE_DIR = DIR / "captures"
USB_MESSAGE = "Packet capture needs a USB cable. Connect your iPhone with a cable and try again."
PHONE4, PHONE6 = "192.168.1.42", "2001:db8:1::42"
MAX_KEPT = 2000   # packets kept for the demo file

# (process, remote, protocol, port, weight); remotes are documentation addresses (RFC 5737 / 3849)
FLOWS = [
    ("{app}", "203.0.113.24", 6, 443, 6), ("{app}", "198.51.100.7", 17, 443, 4),
    ("{app}", "2001:db8:44::12", 6, 443, 3), ("mDNSResponder", "192.0.2.53", 17, 53, 2),
    ("nsurlsessiond", "203.0.113.90", 6, 443, 2), ("apsd", "198.51.100.30", 6, 5223, 1),
    ("timed", "192.0.2.123", 17, 123, 1), ("Safari", "203.0.113.150", 6, 443, 2),
]


def exe_for(bundle_id: str) -> tuple[str, str] | None:
    for name, bid, *_ in APPS:
        if bid == bundle_id or bid.endswith("." + bundle_id):
            return name.replace(" ", ""), bid
    return None


class DemoCapture:
    def __init__(self):
        self.task: asyncio.Task | None = None
        self.stats = net.CaptureStats()
        self.st: dict = {"running": False}
        self.last: dict | None = None
        self.packets: list = []

    def status(self) -> dict:
        if not self.st.get("running"):
            return dict(self.last) if self.last else {"running": False}
        return {**self.st, "packets": self.stats.packets, "bytes": self.stats.bytes,
                "byProcess": self.stats.top_processes(5), "excluded": self.stats.excluded,
                "seconds": round(time.time() - self.st["startedAt"], 1)}

    async def run(self, want: dict, max_s: int, max_bytes: int) -> None:
        app = want.get("process") or "WeatherGlance"
        rng = random.Random(7)
        weights = [f[4] for f in FLOWS]
        end = time.time() + max_s
        reason = "time"
        while time.time() < end:
            await asyncio.sleep(1)
            for _ in range(rng.randint(12, 40)):
                proc, host, proto, port, _w = rng.choices(FLOWS, weights)[0]
                proc = proc.format(app=app)
                self.stats.excluded += rng.random() < 0.3   # the made-up tunnel's own packets
                if want.get("process") and proc != want["process"]:
                    self.stats.skipped += 1
                    continue
                local = PHONE6 if ":" in host else PHONE4
                out = rng.random() < 0.45
                size = rng.choice([0, 0, 64, 512, 1200, 1400]) if proto == 6 else rng.choice([48, 120, 1200])
                sport, dport = (rng.randint(49152, 65000), port) if out else (port, rng.randint(49152, 65000))
                src, dst = (local, host) if out else (host, local)
                frame = net.make_frame(src, dst, proto, sport, dport, size)
                iface = "en0" if rng.random() < 0.8 else "pdp_ip0"
                self.stats.add(proc, iface, frame)
                if len(self.packets) < MAX_KEPT:
                    self.packets.append((time.time(), frame, f"PID: {400 + len(proc)}, ProcName: {proc}, EPID: 0, "
                                                             f"EProcName: , SVC: 0"))
            event("pcap", self.status())
            if self.stats.bytes >= max_bytes:
                reason = "size"
                break
        self.finish(reason)

    def finish(self, reason: str) -> None:
        if not self.st.get("running"):
            return
        now = time.time()
        st = dict(self.st)
        path = CAPTURE_DIR / net.capture_name(st["startedAt"])
        size = net.write_pcapng(path, self.packets)
        net.prune_captures(CAPTURE_DIR)
        st.update(running=False, endedAt=now, stopReason=reason, file=str(path), fileBytes=size,
                  packets=self.stats.packets, bytes=self.stats.bytes, byProcess=self.stats.top_processes(5),
                  excluded=self.stats.excluded, seconds=round(now - st["startedAt"], 1),
                  summary=self.stats.summary(now - st["startedAt"]))
        self.st, self.last, self.packets = {"running": False}, st, []
        event("pcap", st)

    async def stop(self, reason: str) -> None:
        t, self.task = self.task, None
        if t and not t.done():
            t.cancel()
            with contextlib.suppress(BaseException):
                await t
        self.finish({"released": "stopped", "client": "client-gone", "timeout": "time", "tunnel": "tunnel-lost",
                     "shutdown": "shutdown"}.get(reason, reason))


def register(reg):
    cap = DemoCapture()

    @reg.command("pcap.start")
    async def c_start(ctx, a):
        if DEVICE.get("transport") != "usb":
            raise HelperError("needs-usb", USB_MESSAGE)
        if cap.st.get("running"):
            raise HelperError("busy", "A packet capture is already running. Stop it first.")
        try:
            max_s = max(3, min(net.PCAP_MAX_S, int(a.get("maxSeconds") or net.PCAP_DEFAULT_S)))
            max_mb = max(1, min(net.PCAP_MAX_MB, int(a.get("maxMB") or net.PCAP_DEFAULT_MB)))
        except (TypeError, ValueError):
            raise HelperError("bad-args", "maxSeconds, maxMB and pid must be numbers.")
        want: dict = {}
        bid = str(a.get("bundleId") or "").strip()
        if a.get("pid"):
            want["pid"] = int(a["pid"])
        elif bid:
            hit = exe_for(bid)
            if hit is None:
                raise HelperError("not-found", f"The app {bid} isn't installed on your iPhone.")
            want["process"], want["bundleId"] = hit
        client = ctx.client()

        def open_():
            cap.stats, cap.packets = net.CaptureStats(), []
            cap.st = {"running": True, "file": str(CAPTURE_DIR / net.capture_name()), "startedAt": time.time(),
                      "maxSeconds": max_s, "maxMB": max_mb, "filter": want,
                      "startedBy": getattr(client, "name", None) or "background"}
            cap.task = asyncio.create_task(cap.run(want, max_s, max_mb << 20))
            cap.task.add_done_callback(lambda t: (ctx.resources.forget(KEY) if not cap.st.get("running") and cap.task is t
                                                  else None))
            return cap

        await ctx.resources.acquire(KEY, client, open_, lambda _h, why: cap.stop(why), scope="tunnel",
                                    timeout=max_s + 30)
        event("pcap", cap.status())
        return cap.status()

    @reg.command("pcap.stop")
    async def c_stop(ctx, a):
        if not await ctx.resources.close(KEY, "released"):
            await cap.stop("released")
        return cap.status()

    @reg.command("pcap.status")
    def c_status(ctx, a):
        return cap.status()

    @reg.welcome
    def snapshot(ctx):
        return [("pcap", cap.status())] if cap.st.get("running") else []
