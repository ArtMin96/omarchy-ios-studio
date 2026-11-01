"""metrics.* : performance and developer metrics of an app over time (Performance tab, MCP metrics tools).

Sessions, rings, stats and CSV export are shared with demo mode (helper/metrics_core.py,
helper/metrics_stats.py). This module is the phone side: one shared tap per metric set, opened on
the daemon's own tunnel (each long-held instrument on a DVT connection of its own,
s.dedicated_dvt()) and fanned out to every session that wants it. A tap starts with its first user
and stops with its last one.

    process   Sysmontap at 1 Hz (rows are deltas: last-known values per pid, cumulative counters -> rates)
    energy    EnergyMonitor for the followed pids (first, cumulative sample per pid dropped)
    graphics  Graphics: CoreAnimation FPS of the whole display, GPU utilization and memory
    network   NetworkStatistics for the followed pids
    battery   IOPMPowerSource over diagnostics every 5 s
    events    the daemon's syslog stream (one more subscriber of the broker's SyslogHub) filtered for
              jetsam, watchdog, HangTracer and thermal rows; crash reports after the app quits
    hitches   opt-in, ≤ 60 s: ActivityTraceTap signposts (very heavy, filtered by pid here)
launchProfile and leaks change the app's state (relaunch / brief pause); the UI and MCP confirm first.
"""
from __future__ import annotations

import asyncio
import contextlib
import re
import time
from pathlib import Path

from pymobiledevice3.services.diagnostics import DiagnosticsService
from pymobiledevice3.services.dvt.instruments.activity_trace_tap import ActivityTraceTap
from pymobiledevice3.services.dvt.instruments.device_info import DeviceInfo
from pymobiledevice3.services.dvt.instruments.dyld_metrics import DyldMetrics
from pymobiledevice3.services.dvt.instruments.energy_monitor import EnergyMonitor
from pymobiledevice3.services.dvt.instruments.graphics import Graphics
from pymobiledevice3.services.dvt.instruments.leaks import Leaks
from pymobiledevice3.services.dvt.instruments.network_statistics import NetworkStatistics
from pymobiledevice3.services.dvt.instruments.process_control import ProcessControl
from pymobiledevice3.services.dvt.instruments.sysmontap import Sysmontap

import common
import device_crash
import deviced_core as core
import metrics_core
from deviced_core import classify, dlog, emit, err, safe_name, write_bytes

PROC_ATTRS = ["pid", "cpuUsage", "physFootprint", "memResidentSize", "threadCount", "memCompressed", "nfiles",
              "machPortCount", "wqBlockedThreads", "memAnonPeak", "powerScore",
              # cumulative counters (rates are computed from consecutive rows)
              "diskBytesRead", "diskBytesWritten", "intWakeups", "platIdleWakeups", "timerWakeBin1",
              "timerWakeBin2", "faults", "cowFaults", "vmPageIns", "msgSent", "cpuTotalUser", "cpuTotalSystem",
              "totalEnergyScore"]
COUNTERS = {"diskBytesRead", "diskBytesWritten", "intWakeups", "platIdleWakeups", "timerWakeBin1", "timerWakeBin2",
            "faults", "cowFaults", "vmPageIns", "msgSent", "cpuTotalUser", "cpuTotalSystem", "totalEnergyScore"}
SYS_ATTRS = ["netBytesIn", "netBytesOut", "diskBytesRead", "diskBytesWritten", "vmCompressorPageCount",
             "vmFreeCount", "physMemSize"]
BACKOFF = [2, 5, 10, 20, 30]
FRESH_S = 5.0            # a tap value older than this isn't used in a row
BATTERY_EVERY_S = 5.0
HITCH_MS = 33.4          # a frame interval longer than two 60 Hz frames counts as a hitch
HITCH_SIGNPOSTS = {("com.apple.coreanimation", "FrameUpdate"), ("com.apple.coreanimation", "Commit"),
                   ("com.apple.UIKit", "UpdateSequence")}
EXPORT_DIR = common.CACHE_DIR / "metrics"
MB = 1024 * 1024


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _lower_keys(rec: dict) -> dict:
    return {str(k).lower(): v for k, v in rec.items()} if isinstance(rec, dict) else {}


def _by_pid(res) -> dict[int, dict]:
    out = {}
    if isinstance(res, dict):
        for k, v in res.items():
            with contextlib.suppress(TypeError, ValueError):
                if isinstance(v, dict):
                    out[int(k)] = v
    return out


# ====================================================================== taps

class Tap:
    """A phone-side stream shared by every session that wants it (refcounted by session id)."""
    name = "tap"

    def __init__(self, hub: "Hub"):
        self.hub = hub
        self.users: set[str] = set()
        self.task: asyncio.Task | None = None
        self.error: dict | None = None
        self.up = False
        self.shape_logged = False

    def acquire(self, sid: str):
        self.users.add(sid)
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._loop(), name=f"metrics-{self.name}")

    async def release(self, sid: str):
        self.users.discard(sid)
        if not self.users:
            await self.stop()

    async def stop(self):
        t, self.task = self.task, None
        self.up = False
        if t:
            t.cancel()
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(t, 8)

    async def restart(self):
        if self.users:
            await self.stop()
            self.acquire(next(iter(self.users)))

    def log_shape(self, what: str, value) -> None:
        if not self.shape_logged:
            self.shape_logged = True
            dlog(f"metrics {self.name}: first {what}: {repr(value)[:600]}")

    def wanted(self) -> bool:
        return bool(self.users)

    async def _loop(self):
        fails = 0
        while self.wanted():
            s = self.hub.d.session
            if s is None:
                return
            try:
                await s.get_rsd(timeout=3600)
                await self.run(s)
                if not self.wanted():
                    return
                raise ConnectionError(f"the {self.name} stream ended")
            except asyncio.CancelledError:
                raise
            except BaseException as exc:  # noqa: BLE001
                e = classify(exc)
                self.up = False
                self.error = e.to_json()
                dlog(f"metrics {self.name}: {exc!r}")
                await asyncio.sleep(BACKOFF[min(fails, len(BACKOFF) - 1)])
                fails += 1

    async def run(self, s):   # pragma: no cover - subclasses
        raise NotImplementedError


class ProcessTap(Tap):
    name = "process"

    def __init__(self, hub):
        super().__init__(hub)
        self.attrs: list[str] = []
        self.sys_attrs: list[str] = []
        self.rows: dict[int, tuple[float, list, tuple | None]] = {}   # pid -> (t, values, (t, values) before)
        self.system: dict = {}
        self.dead: set[int] = set()

    def view(self, pid: int) -> dict | None:
        hit = self.rows.get(pid)
        if hit is None or pid in self.dead:
            return None
        t, vals, prev = hit
        cur = dict(zip(self.attrs, vals))
        stale = time.monotonic() - t > 2.5   # not in the latest rows: nothing changed for this pid
        rate = {}
        if prev is not None and not stale:
            pt, pvals = prev
            old = dict(zip(self.attrs, pvals))
            dt = max(0.2, t - pt)
            for k in COUNTERS:
                a, b = _num(cur.get(k)), _num(old.get(k))
                if a is not None and b is not None and a >= b:
                    rate[k] = (a - b) / dt
        elif stale:
            rate = {k: 0.0 for k in COUNTERS}

        def mb(k):
            v = _num(cur.get(k))
            return round(v / MB, 1) if v is not None else None
        wake = None
        if "intWakeups" in rate or "platIdleWakeups" in rate:
            wake = round(rate.get("intWakeups", 0) + rate.get("platIdleWakeups", 0), 1)
        cpu = _num(cur.get("cpuUsage"))
        return {"cpu": round(cpu, 1) if cpu is not None else None, "mem": mb("physFootprint"),
                "rss": mb("memResidentSize"), "threads": _num(cur.get("threadCount")), "wakeups": wake,
                "faults": round(rate["faults"], 1) if "faults" in rate else None,
                "diskR": round(rate["diskBytesRead"] / 1024, 1) if "diskBytesRead" in rate else None,
                "diskW": round(rate["diskBytesWritten"] / 1024, 1) if "diskBytesWritten" in rate else None}

    def _procs(self, row: dict, now: float):
        procs = row.get("Processes") or {}
        self.log_shape("process row", {k: (len(v) if isinstance(v, dict) else v) for k, v in row.items()})
        for pid, vals in procs.items():
            with contextlib.suppress(TypeError, ValueError):
                p = int(pid)
                if isinstance(vals, (list, tuple)):
                    old = self.rows.get(p)
                    self.rows[p] = (now, list(vals), (old[0], old[1]) if old else None)
                    self.dead.discard(p)
        for pid in row.get("DeadProcesses") or []:
            with contextlib.suppress(TypeError, ValueError):
                self.dead.add(int(pid))
                self.rows.pop(int(pid), None)
                self.hub.on_dead(int(pid))

    def _sys(self, row: dict, now: float):
        out = {"t": now}
        sysv = row.get("System")
        if isinstance(sysv, list) and sysv and isinstance(sysv[-1], (list, tuple)):
            sysv = sysv[-1]
        if isinstance(sysv, (list, tuple)):
            out.update(dict(zip(self.sys_attrs, sysv)))
        load = row.get("SystemCPUUsage")
        ncpu = _num(row.get("CPUCount")) or _num(row.get("EnabledCPUs")) or 1
        if isinstance(load, dict):
            tl = _num(load.get("CPU_TotalLoad"))
            if tl is not None:
                out["sysCpu"] = round(tl / max(1, ncpu), 1)
        elif _num(load) is not None:
            out["sysCpu"] = round(load / max(1, ncpu), 1)
        self.system = out

    async def run(self, s):
        async with s.dedicated_dvt() as dvt:
            async with DeviceInfo(dvt) as di:
                pa = set(await di.sysmon_process_attributes())
                sa = set(await di.sysmon_system_attributes())
            self.attrs = [a for a in PROC_ATTRS if a in pa]
            self.sys_attrs = [a for a in SYS_ATTRS if a in sa]
            self.rows.clear()
            async with Sysmontap(dvt, self.attrs, self.sys_attrs, interval_ms=1000) as tap:
                self.up, self.error = True, None
                async for row in tap:
                    if not self.users:
                        return
                    if not isinstance(row, dict):
                        continue
                    now = time.monotonic()
                    if "Processes" in row:
                        self._procs(row, now)
                    if "System" in row or "SystemCPUUsage" in row:
                        self._sys(row, now)


class PidPoller(Tap):
    """Base for the per-pid debug-gauge providers (energy, network): sample the followed pids once a second,
    starting and stopping pids as sessions come and go. The first sample of a pid is dropped."""

    def __init__(self, hub):
        super().__init__(hub)
        self.latest: dict[int, tuple[float, dict]] = {}
        self.count: dict[int, int] = {}

    def parse(self, rec: dict) -> dict:
        raise NotImplementedError

    def value(self, pid: int) -> dict | None:
        hit = self.latest.get(pid)
        if hit is None or time.monotonic() - hit[0] > FRESH_S:
            return None
        return hit[1]

    async def run(self, s):
        async with s.dedicated_dvt() as dvt:
            inst = self.factory()(dvt, [])
            await inst.connect()
            svc = inst.service
            current: set[int] = set()
            self.up, self.error = True, None
            try:
                while self.users:
                    want = self.hub.pids_for(self.name)
                    for p in sorted(want - current):
                        await svc.start_sampling_for_pids_([p])
                        self.count[p] = 0
                    for p in sorted(current - want):
                        with contextlib.suppress(Exception):
                            await svc.stop_sampling_for_pids_([p])
                        self.latest.pop(p, None)
                    current = set(want)
                    if current:
                        res = await asyncio.wait_for(svc.sample_attributes_for_pids_({}, sorted(current)), 10)
                        self.log_shape("sample", res)
                        now = time.monotonic()
                        for pid, rec in _by_pid(res).items():
                            n = self.count.get(pid, 0)
                            self.count[pid] = n + 1
                            if n == 0:
                                continue   # cumulative / left over from the previous sampler
                            self.latest[pid] = (now, self.parse(rec))
                    await asyncio.sleep(1.0)
            finally:
                if current:
                    with contextlib.suppress(BaseException):
                        await asyncio.wait_for(svc.stop_sampling_for_pids_(sorted(current)), 3)


class EnergyTap(PidPoller):
    name = "energy"

    @staticmethod
    def factory():
        return EnergyMonitor

    def parse(self, rec: dict) -> dict:
        low = _lower_keys(rec)

        def g(k):
            v = _num(low.get(k))
            return round(v, 2) if v is not None else None
        over = [_num(low.get(f"energy.{c}.overhead")) for c in
                ("cpu", "gpu", "networking", "display", "location", "appstate", "thermalstate")]
        overhead = g("energy.overhead")
        if overhead is None and any(v is not None for v in over):
            overhead = round(sum(v for v in over if v is not None), 2)
        return {"energy": g("energy.cost"), "eCpu": g("energy.cpu.cost"), "eGpu": g("energy.gpu.cost"),
                "eNet": g("energy.networking.cost"), "eDisplay": g("energy.display.cost"),
                "eLoc": g("energy.location.cost"), "eOverhead": overhead, "thermal": g("energy.thermalstate.cost")}


class NetTap(PidPoller):
    name = "network"

    @staticmethod
    def factory():
        return NetworkStatistics

    def __init__(self, hub):
        super().__init__(hub)
        self.prev: dict[int, tuple[float, float, float]] = {}

    def parse(self, rec: dict) -> dict:
        low = _lower_keys(rec)
        rx_d, tx_d = _num(low.get("net.rx.bytes.delta")), _num(low.get("net.tx.bytes.delta"))
        if rx_d is None or tx_d is None:   # no deltas: from the cumulative counters
            pid = int(low.get("pid") or 0)
            rx, tx, now = _num(low.get("net.rx.bytes")) or 0, _num(low.get("net.tx.bytes")) or 0, time.monotonic()
            prev = self.prev.get(pid)
            self.prev[pid] = (now, rx, tx)
            if prev is None:
                return {}
            dt = max(0.2, now - prev[0])
            rx_d, tx_d = max(0, rx - prev[1]) / dt, max(0, tx - prev[2]) / dt
        return {"netRx": round(rx_d / 1024, 1), "netTx": round(tx_d / 1024, 1)}


class GraphicsTap(Tap):
    name = "graphics"

    def __init__(self, hub):
        super().__init__(hub)
        self.latest: tuple[float, dict] | None = None
        self.recoveries: int | None = None

    def value(self) -> dict | None:
        if self.latest is None or time.monotonic() - self.latest[0] > FRESH_S:
            return None
        return self.latest[1]

    async def run(self, s):
        async with s.dedicated_dvt() as dvt:
            async with Graphics(dvt) as g:
                self.up, self.error = True, None
                first = True
                async for sample in g:
                    if not self.users:
                        return
                    if not isinstance(sample, dict):
                        continue
                    if first:     # covers less than a second
                        first = False
                        self.log_shape("sample", sample)
                        continue
                    fps = _num(sample.get("CoreAnimationFramesPerSecond"))
                    util = _num(sample.get("Device Utilization %"))
                    if util is None:
                        util = max((v for k, v in sample.items() if "Utilization" in str(k) and _num(v) is not None),
                                   default=None)
                    mem = next((_num(sample.get(k)) for k in ("In use system memory", "Alloc system memory",
                                                              "In use system memory (driver)")
                                if _num(sample.get(k)) is not None), None)
                    self.latest = (time.monotonic(), {
                        "fps": round(fps, 1) if fps is not None else None,
                        "gpu": round(util, 1) if util is not None else None,
                        "gpuMem": round(mem / MB, 1) if mem is not None else None})
                    rc = _num(sample.get("recoveryCount"))
                    if rc is not None:
                        if self.recoveries is not None and rc > self.recoveries:
                            self.hub.broadcast("note", "The GPU recovered from a hang (recoveryCount went up).")
                        self.recoveries = rc


class BatteryTap(Tap):
    name = "battery"

    def __init__(self, hub):
        super().__init__(hub)
        self.latest: tuple[float, dict] | None = None

    def value(self) -> dict | None:
        if self.latest is None or time.monotonic() - self.latest[0] > BATTERY_EVERY_S * 4:
            return None
        return self.latest[1]

    @staticmethod
    def parse(b: dict) -> dict:
        def signed(v):
            v = _num(v)
            if v is None:
                return None
            return v - (1 << 64) if v > (1 << 63) else v
        amp, volt = signed(b.get("InstantAmperage")), _num(b.get("Voltage"))
        tel = b.get("PowerTelemetryData") if isinstance(b.get("PowerTelemetryData"), dict) else {}
        mw = signed(tel.get("BatteryPower"))
        if mw is None and amp is not None and volt:
            mw = amp * volt / 1000.0
        return {"batt": _num(b.get("CurrentCapacity")), "battMw": round(abs(mw), 0) if mw is not None else None,
                "charging": 1 if b.get("IsCharging") else 0,
                "sysPowerInMw": _num(tel.get("SystemPowerIn"))}

    async def run(self, s):
        self.up, self.error = True, None
        while self.users:
            async with s.open_service(DiagnosticsService) as diag:
                while self.users:
                    b = await asyncio.wait_for(diag.get_battery(), 10)
                    self.log_shape("battery", {k: b.get(k) for k in ("CurrentCapacity", "InstantAmperage",
                                                                    "Voltage", "IsCharging", "PowerTelemetryData")}
                                   if isinstance(b, dict) else b)
                    if isinstance(b, dict):
                        self.latest = (time.monotonic(), self.parse(b))
                    await asyncio.sleep(BATTERY_EVERY_S)


class _SyslogSink:
    """Stands in for a broker client in device_broker.SyslogHub: receives the filtered entries."""
    name = "metrics-events"

    def __init__(self, tap: "EventsTap"):
        self.tap = tap
        self.closed = False

    def send(self, obj: dict):
        if obj.get("event") == "syslog" and isinstance(obj.get("data"), list):
            for e in obj["data"]:
                self.tap.feed(e)


class EventsTap(Tap):
    name = "events"

    def __init__(self, hub):
        super().__init__(hub)
        self.sink = _SyslogSink(self)
        self.subscribed = False

    def acquire(self, sid: str):
        self.users.add(sid)
        if not self.subscribed and core.BROKER is not None:
            self.subscribed = True
            self.task = asyncio.create_task(self._subscribe())

    async def _subscribe(self):
        try:
            # Only "default" and up: jetsam, watchdog and HangTracer rows are default/error level.
            await core.BROKER.syslog.start(self.sink, {"filters": {"minLevel": "default"}})
            self.up, self.error = True, None
        except Exception as exc:  # noqa: BLE001
            self.subscribed = False
            self.error = classify(exc).to_json()
            dlog(f"metrics events: can't follow the log: {exc!r}")

    async def stop(self):
        self.up = False
        if self.subscribed and core.BROKER is not None:
            self.subscribed = False
            with contextlib.suppress(Exception):
                await core.BROKER.syslog.stop(self.sink, emit_state=False)

    async def restart(self):
        pass   # the broker's syslog stream reconnects by itself

    def feed(self, e: dict):
        try:
            self.hub.on_syslog(e)
        except Exception as exc:  # noqa: BLE001
            dlog(f"metrics events: {exc!r}")


class HitchTap(Tap):
    """Opt-in and time limited: the activity trace is 2-3.5k rows a second for the whole phone."""
    name = "hitches"

    def wanted(self) -> bool:
        return bool(self.users) and bool(self.hub.hitch_pids())

    async def run(self, s):
        async with s.dedicated_dvt() as dvt:
            tap = ActivityTraceTap(dvt)
            tap._config["ur"] = 200
            open_: dict[tuple, float] = {}
            last_emit: dict[int, float] = {}
            async with tap:
                self.up, self.error = True, None
                it = tap.__aiter__()
                while self.users and self.hub.hitch_pids():
                    try:
                        msg = await asyncio.wait_for(it.__anext__(), 1.0)
                    except asyncio.TimeoutError:
                        continue
                    pids = self.hub.hitch_pids()
                    pid = getattr(msg, "process", None)
                    if pid not in pids:
                        continue
                    sub, name = getattr(msg, "subsystem", "") or "", getattr(msg, "name", "") or ""
                    if (sub, name) not in HITCH_SIGNPOSTS:
                        continue
                    et = str(getattr(msg, "event_type", "") or getattr(msg, "message_type", "") or "")
                    when = getattr(msg, "device_time", None)
                    t = when.timestamp() if when is not None else time.time()
                    k = (pid, sub, name, getattr(msg, "thread", 0))
                    if "Begin" in et:
                        open_[k] = t
                    elif "End" in et and k in open_:
                        ms = (t - open_.pop(k)) * 1000.0
                        if ms >= HITCH_MS and t - last_emit.get(pid, 0) > 0.25:
                            last_emit[pid] = t
                            self.hub.on_hitch(pid, ms, name)
        self.up = False


# ====================================================================== hub

class Hub:
    def __init__(self, d):
        self.d = d
        self.sessions: dict[str, metrics_core.MetricsSession] = {}
        self.taps: dict[str, Tap] = {}

    def tap(self, name: str) -> Tap:
        if name not in self.taps:
            self.taps[name] = {"process": ProcessTap, "energy": EnergyTap, "network": NetTap,
                               "graphics": GraphicsTap, "battery": BatteryTap, "events": EventsTap,
                               "hitches": HitchTap}[name](self)
        return self.taps[name]

    def pids_for(self, set_name: str) -> set[int]:
        return {s.pid for s in self.sessions.values() if s.running and s.pid and set_name in s.sets}

    def hitch_pids(self) -> set[int]:
        return {s.pid for s in self.sessions.values() if s.running and s.pid and s.hitches_on()}

    def broadcast(self, kind: str, text: str):
        for s in self.sessions.values():
            if s.running:
                s.add_event(kind, text)

    def on_dead(self, pid: int):
        for s in self.sessions.values():
            if s.running and s.pid == pid:
                ev = s.data.get("wake")
                if ev:
                    ev.set()

    def on_hitch(self, pid: int, ms: float, name: str):
        for s in self.sessions.values():
            if s.running and s.pid == pid and s.hitches_on():
                s.add_event("hitch", f"Hitch: a {name} took {ms:.0f} ms", durationMs=round(ms, 1))

    # -- syslog -> events
    def on_syslog(self, e: dict):
        msg = str(e.get("message") or "")
        proc = str(e.get("process") or "")
        sub = str(e.get("subsystem") or "")
        cat = str(e.get("category") or "")
        low = msg.lower()
        pl = proc.lower()
        thermal = sub.startswith("com.apple.cltm") or "thermalmonitor" in pl
        jets = ((sub == "com.apple.xnu" and "memorystatus" in cat.lower()) or (pl == "kernel" and "memorystatus" in low)) \
            and ("kill" in low or "jetsam" in low)
        watchdog = sub.startswith("com.apple.watchdogd") or pl == "watchdogd" or \
            ("watchdog" in low and pl in ("springboard", "runningboardd", "frontboard"))
        hang = ("hangtracer" in pl or "hangtracer" in sub.lower()) and "hang" in low
        if not (thermal or jets or watchdog or hang):
            return
        for s in list(self.sessions.values()):
            if not s.running or "events" not in s.sets:
                continue
            if thermal:
                now = time.time()
                if now - s.data.get("lastThermal", 0) > 30:
                    s.data["lastThermal"] = now
                    s.add_event("thermal", f"Thermal: {msg[:200]}", ts=e.get("ts"))
                continue
            if not self._mentions(s, msg):
                continue
            kind = "jetsam" if jets else "watchdog" if watchdog else "hang"
            now = time.time()
            last = s.data.setdefault("lastEvent", {})
            if now - last.get(kind, 0) < 2:
                continue
            last[kind] = now
            dur = None
            if kind == "hang":
                m = re.search(r"(\d+(?:\.\d+)?)\s*(ms|s|sec|seconds)\b", low)
                if m:
                    dur = float(m.group(1)) * (1 if m.group(2) == "ms" else 1000)
            text = {"jetsam": "Killed for using too much memory (jetsam)",
                    "watchdog": "Killed by the watchdog: it took too long to respond",
                    "hang": "Hang: the main thread was blocked"}[kind]
            s.add_event(kind, f"{text}. {msg[:200]}", ts=e.get("ts"), durationMs=dur, source=proc)

    @staticmethod
    def _mentions(s, msg: str) -> bool:
        exe = s.data.get("exe")
        if exe and exe in msg:
            return True
        if s.bundle_id and (s.bundle_id in msg or s.bundle_id.split(".", 1)[-1] in msg):
            return True
        return bool(s.pid and re.search(rf"(?<![\d.]){s.pid}(?![\d.])", msg))

    # -- rows
    def row(self, sess) -> dict:
        row: dict = {"ts": round(time.time(), 2)}
        if sess.pid:
            row["pid"] = sess.pid
        if "process" in sess.sets and "process" in self.taps:
            pt = self.taps["process"]
            if sess.pid:
                row.update(pt.view(sess.pid) or {})
            if pt.system and time.monotonic() - pt.system.get("t", 0) < FRESH_S:
                row["sysCpu"] = pt.system.get("sysCpu")
        for name in ("energy", "network"):
            if name in sess.sets and name in self.taps and sess.pid:
                row.update(self.taps[name].value(sess.pid) or {})
        if "graphics" in sess.sets and "graphics" in self.taps:
            row.update(self.taps["graphics"].value() or {})
        if "battery" in sess.sets and "battery" in self.taps:
            b = dict(self.taps["battery"].value() or {})
            b.pop("sysPowerInMw", None)
            row.update(b)
        return {k: v for k, v in row.items() if v is not None}

    def tap_state(self, sess) -> dict | None:
        """The error to show when every wanted tap is failing (else None)."""
        wanted = [self.taps[n] for n in sess.sets if n in self.taps and n != "events"]
        if wanted and all(not t.up and t.error for t in wanted):
            return wanted[0].error
        return None


# ====================================================================== backend

class Backend:
    def __init__(self):
        self.export_dir = EXPORT_DIR
        self.hub: Hub | None = None

    emit = staticmethod(emit)
    err = staticmethod(err)

    def _hub(self, d) -> Hub:
        if self.hub is None or self.hub.d is not d:
            self.hub = Hub(d)
        return self.hub

    async def _exe(self, s, bid: str) -> tuple[str | None, str, str | None]:
        """(executable, installed bundle id, display name) of an installed app."""
        rsd = await s.get_rsd()
        apps = await s.pmd3.InstallationProxyService(lockdown=rsd).get_apps(application_type="Any")
        for k, v in apps.items():
            if k == bid or k.endswith("." + bid):
                return v.get("CFBundleExecutable") or None, k, v.get("CFBundleDisplayName") or v.get("CFBundleName")
        raise err("not-found", "That app isn't installed on your iPhone.")

    async def _pid_for(self, s, bid: str) -> int:
        async def q():
            pc = await s.dvt_instrument("metrics.pc", ProcessControl)
            return int(await asyncio.wait_for(pc.process_identifier_for_bundle_identifier(bid), 10) or 0)
        return await s.dvt_retry(q)

    async def attach(self, d, sess, a):
        s = d.need_session(a)
        hub = self._hub(d)
        if sess.bundle_id:
            exe, full, name = await self._exe(s, sess.bundle_id)
            sess.bundle_id = full
            sess.data["exe"] = exe
            if not a.get("name"):
                sess.name = name or exe or full
            await s.ensure_ddi()
            pid = await self._pid_for(s, full)
            sess.pid = pid or None
        else:
            await s.ensure_ddi()
        hub.sessions[sess.id] = sess
        for name in sess.sets:
            hub.tap(name).acquire(sess.id)
        sess.data["wake"] = asyncio.Event()
        sess.data["follower"] = asyncio.create_task(self._follow(d, s, sess))
        sess.set_status("running" if sess.pid else "waiting-app")

    async def detach(self, d, sess):
        hub = self.hub
        t = sess.data.pop("follower", None)
        if t:
            t.cancel()
            with contextlib.suppress(BaseException):
                await t
        if hub is None:
            return
        hub.sessions.pop(sess.id, None)
        for name in list(sess.sets) + ["hitches"]:
            if name in hub.taps:
                await hub.taps[name].release(sess.id)

    async def collect(self, d, sess):
        hub = self._hub(d)
        e = hub.tap_state(sess)
        if e and sess.status not in ("waiting-app", "app-exited"):
            sess.set_status("reconnecting", e)
        elif not e and sess.status == "reconnecting":
            sess.set_status("running" if sess.pid else "waiting-app")
        row = hub.row(sess)   # (sysmontap's first row has no CPU yet: that field is just missing)
        return row if len(row) > 2 or (not sess.pid and len(row) > 1) else None

    async def _follow(self, d, s, sess):
        """Follow the app: a new pid after a relaunch, waiting while it isn't running, crash check on exit."""
        wake: asyncio.Event = sess.data["wake"]
        while sess.running:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(wake.wait(), 2.0)
            wake.clear()
            if not sess.follow:
                pt = self.hub.taps.get("process") if self.hub else None
                if sess.pid and pt is not None and sess.pid in pt.dead:
                    sess.add_event("exit", f"{sess.name} quit (pid {sess.pid}).")
                    sess.set_status("app-exited")
                    return
                continue
            try:
                pid = await self._pid_for(s, sess.bundle_id) or None
            except asyncio.CancelledError:
                raise
            except BaseException as exc:  # noqa: BLE001
                dlog(f"metrics {sess.id}: pid lookup: {exc!r}")
                await asyncio.sleep(3)
                continue
            if pid == sess.pid:
                continue
            old = sess.pid
            sess.pid = pid
            if old and not pid:
                sess.add_event("exit", f"{sess.name} quit (pid {old}).")
                if "events" in sess.sets:
                    asyncio.create_task(self._crash_check(d, s, sess, time.time()))
                sess.set_status("waiting-app")
            elif pid:
                sess.add_event("launch", f"{sess.name} {'relaunched' if old else 'started'} (pid {pid}).")
                sess.set_status("running")

    async def _crash_check(self, d, s, sess, exit_ts: float):
        exe = sess.data.get("exe")
        await asyncio.sleep(4)
        try:
            rsd = await s.get_rsd()
            async with d.pmd3.CrashReportsManager(rsd) as c:
                with contextlib.suppress(BaseException):
                    await asyncio.wait_for(c.flush(), 20)
                paths = await asyncio.wait_for(c.ls("/", depth=2), 30)
        except BaseException as exc:  # noqa: BLE001
            dlog(f"metrics {sess.id}: crash check: {exc!r}")
            return
        for p in paths:
            item = device_crash.classify(p)
            if not item or item["ts"] < exit_ts - 120:
                continue
            if item["kind"] == "jetsam":
                if any(e.get("kind") == "jetsam" and e["ts"] >= exit_ts - 120 for e in sess.all_events()):
                    continue   # already seen in the log
                sess.add_event("jetsam", "A memory report (JetsamEvent) was written when the app quit.",
                               ts=item["ts"], report=item["name"])
            elif exe and item["process"] == exe and item["kind"] in ("crash", "fault", "resource", "hang"):
                kind = "crash" if item["kind"] in ("crash", "fault") else ("hang" if item["kind"] == "hang" else "note")
                sess.add_event(kind, f"{item['kind'].capitalize()} report: {item['file']}", ts=item["ts"],
                               report=item["name"])

    async def hitches(self, d, sess, seconds):
        if not sess.pid:
            raise err("not-running", f"{sess.name} isn't running. Open it on the iPhone first.")
        sess.hitch_until = time.time() + seconds
        self._hub(d).tap("hitches").acquire(sess.id)

    # -- opt-in actions that change the app's state
    async def launch_profile(self, d, a):
        bid = str(a["bundleId"])
        s = d.need_session(a)
        exe, full, name = await self._exe(s, bid)
        await s.ensure_ddi()
        t_start = time.time()
        first_frame: dict = {}
        async with s.dedicated_dvt() as dvt:
            async with ProcessControl(dvt) as pc:
                tap = ActivityTraceTap(dvt)
                tap._config["ur"] = 100
                async with tap:
                    it = tap.__aiter__()
                    with contextlib.suppress(asyncio.TimeoutError):   # wait until rows flow
                        await asyncio.wait_for(it.__anext__(), 15)
                    pid = await pc.launch(full, kill_existing=True, start_suspended=True)
                    dyld = DyldMetrics(dvt)
                    await dyld.connect()
                    cap = asyncio.create_task(dyld.capture(pid, at_main=True))
                    await asyncio.sleep(0.2)
                    t0 = time.monotonic()
                    await pc.resume(pid)

                    async def watch():
                        while True:
                            msg = await it.__anext__()
                            if getattr(msg, "process", None) != pid:
                                continue
                            key = (getattr(msg, "subsystem", "") or "", getattr(msg, "name", "") or "")
                            if key in HITCH_SIGNPOSTS:
                                first_frame["ms"] = round((time.monotonic() - t0) * 1000)
                                first_frame["signpost"] = key[1]
                                return
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(watch(), 20)
                    snap, dyld_error = None, None
                    try:
                        snap = await asyncio.wait_for(cap, 10)
                    except BaseException as exc:  # noqa: BLE001
                        cap.cancel()
                        dyld_error = str(exc)[:300]
        return self._profile(full, name or exe, pid, snap, dyld_error, first_frame, time.time() - t_start)

    @staticmethod
    def _profile(bid, name, pid, snap, dyld_error, first_frame, seconds) -> dict:
        out: dict = {"bundleId": bid, "name": name, "pid": pid, "seconds": round(seconds, 1), "phases": []}
        timing = (snap or {}).get("timing") if isinstance(snap, dict) else None
        if isinstance(timing, dict):
            sp, it, mn = _num(timing.get("spawn")), _num(timing.get("init")), _num(timing.get("main"))
            if sp is not None and it is not None:
                out["phases"].append({"name": "Loading libraries (dyld)", "ms": round((it - sp) * 1000, 1)})
            if it is not None and mn is not None:
                out["phases"].append({"name": "Static initializers", "ms": round((mn - it) * 1000, 1)})
            if sp is not None and mn is not None:
                out["preMainMs"] = round((mn - sp) * 1000, 1)
            out["timing"] = {k: v for k, v in timing.items() if _num(v) is not None}
        if isinstance(snap, dict):
            counts = snap.get("counts")
            if isinstance(counts, dict):
                out["counts"] = {k: v for k, v in counts.items() if _num(v) is not None}
        if dyld_error:
            out["dyldError"] = dyld_error
            out["note"] = "Loader timing works only for your own development-signed apps."
        if first_frame.get("ms") is not None:
            out["firstFrameMs"] = first_frame["ms"]
            out["phases"].append({"name": "First frame (timed on this computer)", "ms": first_frame["ms"]})
        total = out.get("firstFrameMs") or out.get("preMainMs")
        out["verdict"] = _launch_verdict(total)
        return out

    async def leaks(self, d, a):
        bid = str(a["bundleId"])
        s = d.need_session(a)
        exe, full, name = await self._exe(s, bid)
        await s.ensure_ddi()
        pid = await self._pid_for(s, full)
        if not pid:
            raise err("not-running", f"{name or exe} isn't running. Open it on the iPhone, use it for a bit, "
                                     "then check for leaks.")
        t0 = time.time()
        async with s.dedicated_dvt() as dvt:
            async with ProcessControl(dvt) as pc:
                if not await pc.is_debuggable(pid):
                    raise err("not-debuggable", "Leak checks only work for your own apps built and installed by "
                                                "iOS Studio (development-signed).")
            lk = Leaks(dvt)
            await lk.connect()
            try:
                graph = await asyncio.wait_for(lk.memory_graph(pid, leaked_only=True), 180)
            except asyncio.TimeoutError:
                raise err("timeout", "The leak check took too long. Try again with the app idle.") from None
            except Exception as exc:  # noqa: BLE001
                raise err("failed", "Your iPhone couldn't scan the app's memory.", None, str(exc)) from None
        out = {"bundleId": full, "name": name or exe, "pid": pid, "leakCount": graph.leaked_count,
               "leakedBytes": None, "seconds": round(time.time() - t0, 1)}
        if graph.data:
            p = EXPORT_DIR / f"{safe_name(name or exe or 'app')}-{time.strftime('%Y%m%d-%H%M%S')}.memgraph"
            await asyncio.to_thread(write_bytes, p, graph.data)
            out.update(memgraphPath=str(p), memgraphBytes=len(graph.data))
        out["verdict"] = ("No leaks found." if not graph.leaked_count else
                          f"{graph.leaked_count} leaked allocation{'s' if graph.leaked_count != 1 else ''} found. "
                          "Open the .memgraph with Xcode or `leaks` on a Mac for sizes and stacks.")
        return out


def _launch_verdict(ms) -> str:
    if ms is None:
        return "Couldn't time the launch."
    if ms < 400:
        return f"Fast launch ({ms:.0f} ms)."
    if ms < 1000:
        return f"OK launch ({ms:.0f} ms). Apple suggests the first frame within 400 ms."
    return f"Slow launch ({ms / 1000:.1f} s). Look at work done before the first frame (static initializers, " \
           "synchronous I/O in app startup)."


def register(reg):
    backend = Backend()
    metrics_core.register_commands(reg, backend)

    @reg.on_tunnel_drop
    async def dropped(d, s):
        if backend.hub is not None:
            for t in list(backend.hub.taps.values()):
                await t.restart()

    @reg.on_shutdown
    async def shutdown(d):
        if backend.hub is not None:
            for t in list(backend.hub.taps.values()):
                await t.stop()
