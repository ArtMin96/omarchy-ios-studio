"""pcap.start, pcap.stop, pcap.status (packet capture through pcapd, USB only).

The phone refuses pcapd over the Wi-Fi tunnel, so a capture needs the cable. Every packet comes
with the sending process and interface; the daemon filters on that (an app's executable, or a pid),
drops its own tunnel traffic, and writes standard pcapng with a per-packet comment naming the
process (~/.cache/ios-studio/captures/cap-<ts>.pcapng). The capture is a d.resources resource
(scope "tunnel"): it stops at its time or size limit, on pcap.stop, when the starting client
disconnects, on a tunnel drop and at shutdown. The summary reads only pcapd's header and the
IP/TCP/UDP headers, never payloads. Protocol: docs/deviced-protocol.md#packet-capture.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import time

from pymobiledevice3.exceptions import InvalidServiceError, StartServiceError
from pymobiledevice3.services.pcapd import PcapdService

import common
import device_network as net
from deviced_core import classify, dlog, emit, err
from deviced_features._app_lookup import find_app
from deviced_features.app_index import device_fetcher

KEY = "pcap"
CAPTURE_DIR = common.CACHE_DIR / "captures"
USB_MESSAGE = "Packet capture needs a USB cable. Connect your iPhone with a cable and try again."
STOP_REASONS = {"released": "stopped", "client": "client-gone", "timeout": "time", "tunnel": "tunnel-lost",
                "session": "tunnel-lost", "shutdown": "shutdown", "error": "error"}


@contextlib.asynccontextmanager
async def open_pcapd(s):
    """pcapd over the tunnel's RSD (USB), or over plain usbmux lockdown if the shim refuses."""
    async with contextlib.AsyncExitStack() as stack:
        try:
            svc = await stack.enter_async_context(s.open_service(PcapdService))
        except (StartServiceError, InvalidServiceError) as exc:
            dlog(f"pcap: RSD pcapd refused ({exc!r}); trying lockdown over usbmux")
            lockdown = await stack.enter_async_context(s.usb_lockdown())
            svc = await stack.enter_async_context(PcapdService(lockdown=lockdown))
        yield svc


class Capture:
    def __init__(self):
        self.task: asyncio.Task | None = None
        self.ticker: asyncio.Task | None = None
        self.stats = net.CaptureStats()
        self.st: dict = {"running": False}
        self.last: dict | None = None      # the previous capture's final status (with summary)
        self.tunnel_ifaces: set[str] = set()

    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def status(self) -> dict:
        if not self.st.get("running"):
            return dict(self.last) if self.last else {"running": False}
        out = dict(self.st)
        out.update(packets=self.stats.packets, bytes=self.stats.bytes, byProcess=self.stats.top_processes(5),
                   excluded=self.stats.excluded, seconds=round(time.time() - out["startedAt"], 1))
        return out

    def tick(self) -> None:
        emit("pcap", self.status())

    # -- the capture
    def matches(self, p, want: dict) -> bool:
        if want.get("pid"):
            return want["pid"] in (p.pid, p.epid)
        exe = want.get("process")
        if exe:
            exe = exe[:16]
            return (p.comm or "").strip("\x00") == exe or (p.ecomm or "").strip("\x00") == exe
        return True

    def is_tunnel(self, p) -> bool:
        comm = (p.comm or "").strip("\x00")
        ecomm = (p.ecomm or "").strip("\x00")
        if comm in net.TUNNEL_PROCS or ecomm in net.TUNNEL_PROCS:
            iface = p.interface_name or ""
            if iface and iface != "en0" and not iface.startswith(("pdp_ip", "lo")):
                self.tunnel_ifaces.add(iface)   # the USB link's interface: nothing of the app's goes there
            return True
        return (p.interface_name or "") in self.tunnel_ifaces

    async def run(self, s, path, want: dict, max_s: float, max_bytes: int) -> None:
        reason = "time"

        async def packets(svc):
            nonlocal reason
            async for p in svc.watch():
                if self.is_tunnel(p):
                    self.stats.excluded += 1
                    continue
                if not self.matches(p, want):
                    self.stats.skipped += 1
                    continue
                self.stats.add(net.proc_name(p.comm, p.epid, p.ecomm, p.pid), p.interface_name, p.data)
                yield p
                if self.stats.bytes >= max_bytes:
                    reason = "size"
                    return

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            async with open_pcapd(s) as svc:
                with open(path, "wb") as f:
                    try:
                        await asyncio.wait_for(svc.write_to_pcap(f, packets(svc)), max_s)
                    except asyncio.TimeoutError:
                        reason = "time"
            self.finish(reason)
        except asyncio.CancelledError:
            raise   # stop() finishes it with the right reason
        except BaseException as exc:  # noqa: BLE001
            dlog(f"pcap: capture failed: {exc!r}")
            self.finish("error", classify(exc).to_json())

    def finish(self, reason: str, error: dict | None = None) -> None:
        if not self.st.get("running"):
            return
        end = time.time()
        st = dict(self.st)
        path = st.get("file")
        size = 0
        with contextlib.suppress(OSError):
            size = os.path.getsize(path)
        st.update(running=False, endedAt=end, stopReason=reason, fileBytes=size,
                  packets=self.stats.packets, bytes=self.stats.bytes, byProcess=self.stats.top_processes(5),
                  excluded=self.stats.excluded, seconds=round(end - st["startedAt"], 1),
                  summary=self.stats.summary(end - st["startedAt"]))
        if error:
            st["error"] = error
        self.st = {"running": False}
        self.last = st
        if self.ticker:
            self.ticker.cancel()
            self.ticker = None
        net.prune_captures(CAPTURE_DIR)
        emit("pcap", st)

    async def stop(self, reason: str) -> None:
        t, self.task = self.task, None
        if t and not t.done():
            t.cancel()
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(t, 5)
        self.finish(STOP_REASONS.get(reason, reason))

    async def tick_loop(self) -> None:
        while self.st.get("running"):
            await asyncio.sleep(1)
            if self.st.get("running"):
                self.tick()


async def resolve_process(s, bundle_id: str) -> tuple[str, str]:
    """bundle id (plain or XTL-<TEAM>. form) -> (executable, installed bundle id)."""
    app = await find_app(s.udid, bundle_id, device_fetcher(s.pmd3, await s.get_rsd()))
    if app and app.get("executable"):
        return app["executable"], app["bundleId"]
    raise err("not-found", f"The app {bundle_id} isn't installed on your iPhone.")


def register(reg):
    cap = Capture()

    @reg.command("pcap.start")
    async def c_start(d, a):
        s = d.need_session(a)
        await s.get_rsd()
        if s.transport != "usb":
            raise err("needs-usb", USB_MESSAGE)
        if cap.running() or d.resources.held(KEY):
            raise err("busy", "A packet capture is already running. Stop it first.")
        try:
            max_s = max(3, min(net.PCAP_MAX_S, int(a.get("maxSeconds") or net.PCAP_DEFAULT_S)))
            max_mb = max(1, min(net.PCAP_MAX_MB, int(a.get("maxMB") or net.PCAP_DEFAULT_MB)))
            pid = int(a["pid"]) if a.get("pid") not in (None, "") else None
        except (TypeError, ValueError):
            raise err("bad-args", "maxSeconds, maxMB and pid must be numbers.")
        want: dict = {}
        bid = str(a.get("bundleId") or "").strip()
        if pid:
            want["pid"] = pid
        elif bid:
            want["process"], want["bundleId"] = await resolve_process(s, bid)
        path = CAPTURE_DIR / net.capture_name()
        client = d.client()

        async def open_():
            cap.stats = net.CaptureStats()
            cap.tunnel_ifaces = set()
            cap.st = {"running": True, "file": str(path), "startedAt": time.time(), "maxSeconds": max_s,
                      "maxMB": max_mb, "filter": want, "startedBy": getattr(client, "name", None) or "background"}
            cap.task = asyncio.create_task(cap.run(s, path, want, max_s, max_mb << 20))
            cap.ticker = asyncio.create_task(cap.tick_loop())
            return cap

        async def close(_h, reason):
            await cap.stop(reason)

        def ended(t: asyncio.Task):
            # stopped by itself (limit or error): drop the resource without calling close again
            if cap.task is t and not cap.st.get("running"):
                cap.task = None
                d.resources.forget(KEY)

        await d.resources.acquire(KEY, client, open_, close, scope="tunnel", timeout=max_s + 30)
        if cap.task:
            cap.task.add_done_callback(ended)
        cap.tick()
        return cap.status()

    @reg.command("pcap.stop")
    async def c_stop(d, a):
        if not await d.resources.close(KEY, "released"):
            await cap.stop("released")
        return cap.status()

    @reg.command("pcap.status")
    async def c_status(d, a):
        return cap.status()

    @reg.welcome
    def snapshot(d):
        return [("pcap", cap.status())] if cap.st.get("running") else []
