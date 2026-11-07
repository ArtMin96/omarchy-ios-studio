"""Metrics sessions shared by the real daemon (features/metrics.py) and the demo one
(demo.py). Stdlib only.

A session follows one app (bundleId, or a bare pid) and keeps one compact row per interval in a
ring buffer (about an hour at 1 Hz) plus its events (hang, jetsam, crash, hitch, ...). The phone-side
taps are the backend's business: the backend fills `session.collect()` rows and calls
`session.add_event()`. This module owns the commands, the rings, the events and the CSV export.

Backend interface (duck-typed), passed to register_commands(reg, backend):
    emit(name, data)                      device-wide event (thread-safe)
    err(code, message, fix=None, detail=None) -> Exception
    export_dir: Path
    async attach(ctx, session, args)      validate the target, start the taps (raise err(...))
    async detach(ctx, session)            stop this session's use of the taps
    async collect(ctx, session) -> dict | None   one row for now (None = nothing yet)
    async hitches(ctx, session, seconds)  start opt-in hitch tracking (≤ 60 s)
    async launch_profile(ctx, args) -> dict
    async leaks(ctx, args) -> dict
Handlers get (ctx, args); ctx has .resources and .client() in both daemons.
"""
from __future__ import annotations

import asyncio
import contextlib
import csv
import io
import itertools
import time
from pathlib import Path

from iosstudio.device.features.metrics import stats
from iosstudio.device.kit import CursorRing

SETS = ("process", "energy", "graphics", "network", "battery", "events", "hitches")
DEFAULT_SETS = ("process", "energy", "graphics", "network", "battery", "events")
EVENT_KINDS = ("hang", "jetsam", "thermal", "crash", "watchdog", "hitch", "exit", "launch", "note")
RING_SAMPLES = 3700          # ~1 h at 1 Hz
RING_EVENTS = 2000
MAX_RUNNING = 4
KEEP_STOPPED = 6
SESSION_TIMEOUT = 2 * 3600   # a session nobody touches (read/snapshot) for this long stops itself
HITCH_MAX_S = 60


def _now() -> float:
    return round(time.time(), 2)


class MetricsSession:
    def __init__(self, sid: str, target: dict, sets: list[str], interval: float):
        self.id = sid
        self.bundle_id: str | None = target.get("bundleId")
        self.pid: int | None = target.get("pid")
        self.follow = bool(self.bundle_id)        # follow relaunches by bundle id (else a bare pid)
        self.name: str = target.get("name") or self.bundle_id or (f"pid {self.pid}" if self.pid else "app")
        self.sets = sets
        self.interval = interval
        self.samples = CursorRing(max_items=RING_SAMPLES, max_bytes=6 << 20)
        self.events = CursorRing(max_items=RING_EVENTS, max_bytes=1 << 20)
        self.started = _now()
        self.stopped: float | None = None
        self.status = "starting"                  # starting | running | waiting-app | reconnecting | stopped
        self.error: dict | None = None
        self.task: asyncio.Task | None = None
        self.hitch_until: float | None = None
        self.hitch_pending_ms = 0.0
        self.owner = ""
        self.data: dict = {}                      # backend state for this session
        self.notes: dict[str, str] = {}          # set -> plain-language note (e.g. a gauge that stopped answering)
        self._emit = None
        self._state_sig = None

    # -- events and samples
    def add_event(self, kind: str, text: str, ts: float | None = None, **extra) -> dict:
        ev = {"ts": round(ts if ts is not None else time.time(), 2), "kind": kind, "text": str(text)[:400]}
        ev.update({k: v for k, v in extra.items() if v is not None})
        if kind == "hitch" and extra.get("durationMs"):
            self.hitch_pending_ms += float(extra["durationMs"])
        self.events.append(ev)
        if self._emit:
            self._emit("metrics.event", {"sessionId": self.id, **ev})
        return ev

    def add_sample(self, row: dict) -> dict:
        row = {k: v for k, v in row.items() if v is not None}
        row.setdefault("ts", _now())
        if self.pid is not None:
            row.setdefault("pid", self.pid)
        if self.hitch_pending_ms:
            row["hitchMs"] = round(row.get("hitchMs", 0) + self.hitch_pending_ms, 1)
            self.hitch_pending_ms = 0.0
        self.samples.append(row)
        if self._emit:
            self._emit("metrics.sample", {"sessionId": self.id, **row})
        return row

    def all_samples(self) -> list[dict]:
        return self.samples.read(None, limit=RING_SAMPLES + 10)["entries"]

    def all_events(self) -> list[dict]:
        return self.events.read(None, limit=RING_EVENTS + 10)["entries"]

    # -- state
    @property
    def running(self) -> bool:
        return self.stopped is None

    def hitches_on(self) -> bool:
        return self.hitch_until is not None and time.time() < self.hitch_until

    def describe(self) -> dict:
        out = {"sessionId": self.id, "bundleId": self.bundle_id, "name": self.name, "pid": self.pid,
               "follow": self.follow, "sets": self.sets, "intervalMs": int(self.interval * 1000),
               "started": self.started, "stopped": self.stopped, "running": self.running, "status": self.status,
               "samples": self.samples.seq, "events": self.events.seq,
               "hitchTracking": {"running": self.hitches_on(),
                                 "until": round(self.hitch_until, 1) if self.hitches_on() else None},
               "owner": self.owner}
        if self.error:
            out["error"] = self.error
        if self.notes:
            out["notes"] = list(self.notes.values())
        return out

    def set_status(self, status: str, error: dict | None = None, force: bool = False) -> None:
        self.status, self.error = status, error
        sig = (status, (error or {}).get("code"), self.pid, self.hitches_on(), tuple(sorted(self.notes)))
        if self._emit and (force or sig != self._state_sig):
            self._state_sig = sig
            self._emit("metrics.state", self.describe())


def summary(sess: MetricsSession) -> dict:
    return stats.summarize(sess.all_samples(), sess.all_events(),
                           {"name": sess.name, "bundleId": sess.bundle_id, "interval": sess.interval})


def export_csv(sess: MetricsSession, out_dir: Path) -> dict:
    """samples -> <dir>/<name>-<session>-<time>.csv (+ -events.csv). Returns paths and row counts."""
    import re
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", sess.name or "app").strip("._")[:60] or "app"
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(sess.started))
    base = out_dir / f"{safe}-{sess.id}-{stamp}"
    rows = sess.all_samples()
    cols = ["time", "ts", "pid"] + list(stats.SERIES) + ["charging"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    for r in rows:
        t = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(r.get("ts", 0)))
        w.writerow([t] + [("" if r.get(c) is None else r.get(c)) for c in cols[1:]])
    path = base.with_suffix(".csv")
    path.write_text(buf.getvalue())
    evs = sess.all_events()
    ebuf = io.StringIO()
    ew = csv.writer(ebuf)
    ew.writerow(["time", "ts", "kind", "durationMs", "text"])
    for e in evs:
        ew.writerow([time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(e.get("ts", 0))), e.get("ts"),
                     e.get("kind"), e.get("durationMs", ""), e.get("text", "")])
    epath = Path(str(base) + "-events.csv")
    epath.write_text(ebuf.getvalue())
    # keep the newest 40 exports
    with contextlib.suppress(OSError):
        files = sorted(out_dir.glob("*.csv"), key=lambda f: f.stat().st_mtime, reverse=True)
        for f in files[80:]:
            f.unlink(missing_ok=True)
    return {"path": str(path), "eventsPath": str(epath), "rows": len(rows), "events": len(evs),
            "bytes": path.stat().st_size, "columns": cols}


def _sets(raw) -> list[str]:
    if raw is None:
        return list(DEFAULT_SETS)
    if isinstance(raw, str):
        raw = [x.strip() for x in raw.split(",")]
    out = [str(x) for x in raw if str(x) in SETS]
    return out or list(DEFAULT_SETS)


def register_commands(reg, backend) -> dict:
    sessions: dict[str, MetricsSession] = {}
    ids = itertools.count(1)
    E = backend.err

    def key(sid: str) -> str:
        return f"metrics:{sid}"

    def get(a) -> MetricsSession:
        sid = str(a.get("sessionId") or "")
        if not sid:
            running = [s for s in sessions.values() if s.running]
            if len(running) == 1:
                return running[0]
            raise E("bad-args", "Which metrics session? (sessionId missing)")
        s = sessions.get(sid)
        if s is None:
            raise E("not-found", "That metrics session has ended (stopped sessions are kept for a while). "
                                 "Start a new one.")
        return s

    def prune():
        done = sorted((s for s in sessions.values() if not s.running), key=lambda s: s.stopped or 0)
        for s in done[:-KEEP_STOPPED]:
            sessions.pop(s.id, None)

    async def ticker(ctx, sess: MetricsSession):
        nxt = time.monotonic()
        while sess.running:
            nxt += sess.interval
            try:
                row = await backend.collect(ctx, sess)
                if row:
                    sess.add_sample(row)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a bad row must not end the session
                reg.log(f"metrics {sess.id}: collect failed: {exc!r}")
            if sess.hitch_until is not None and not sess.hitches_on():
                sess.hitch_until = None
                sess.set_status(sess.status, sess.error, force=True)
            await asyncio.sleep(max(0.05, nxt - time.monotonic()))

    async def finish(ctx, sess: MetricsSession, reason: str):
        if not sess.running:
            return
        sess.stopped = _now()
        t, sess.task = sess.task, None
        if t:
            t.cancel()
            with contextlib.suppress(BaseException):
                await t
        with contextlib.suppress(Exception):
            await backend.detach(ctx, sess)
        sess.set_status("stopped", None if reason in ("released", "stop") else
                        {"code": reason, "message": {"client": "The app that started it disconnected.",
                                                     "timeout": "Nobody looked at it for 2 hours.",
                                                     "session": "The iPhone changed.",
                                                     "shutdown": "The iPhone service stopped."}.get(reason, reason)},
                        force=True)
        prune()

    @reg.command("metrics.start")
    async def start(ctx, a):
        target: dict = {}
        if a.get("bundleId"):
            target["bundleId"] = str(a["bundleId"]).strip()
        elif a.get("pid") not in (None, ""):
            try:
                target["pid"] = int(a["pid"])
            except (TypeError, ValueError):
                raise E("bad-args", "pid must be a number.") from None
        else:
            raise E("bad-args", "Which app? (bundleId or pid missing)")
        if a.get("name"):
            target["name"] = str(a["name"])
        sets = _sets(a.get("sets"))
        try:
            interval = max(1.0, float(a.get("intervalMs") or 1000) / 1000.0)
        except (TypeError, ValueError):
            interval = 1.0
        client = ctx.client()
        for s in sessions.values():   # same app, same client: hand back the running session
            if s.running and s.bundle_id == target.get("bundleId") and s.pid == (target.get("pid") or s.pid) \
                    and ctx.resources.holds(key(s.id), client) and set(sets) <= set(s.sets):
                return s.describe()
        running = [s for s in sessions.values() if s.running]
        if len(running) >= MAX_RUNNING:
            raise E("busy", f"{len(running)} measurements are already running. Stop one first.")
        sess = MetricsSession(f"m{next(ids)}", target, [x for x in sets if x != "hitches"], interval)
        sess._emit = backend.emit
        sess.owner = getattr(client, "name", "") or ""
        sessions[sess.id] = sess

        async def open_():
            await backend.attach(ctx, sess, a)
            sess.task = asyncio.create_task(ticker(ctx, sess))
            return sess

        async def close(handle, reason):
            await finish(ctx, sess, reason)

        try:
            await ctx.resources.acquire(key(sess.id), client, open_, close, scope="session", timeout=SESSION_TIMEOUT)
        except BaseException:
            sessions.pop(sess.id, None)
            with contextlib.suppress(Exception):
                await backend.detach(ctx, sess)
            raise
        if "hitches" in sets:
            with contextlib.suppress(Exception):
                await backend.hitches(ctx, sess, HITCH_MAX_S)
        sess.set_status(sess.status, sess.error, force=True)
        return sess.describe()

    @reg.command("metrics.stop")
    async def stop(ctx, a):
        sess = get(a)
        if sess.running:
            if not await ctx.resources.close(key(sess.id), "released"):
                await finish(ctx, sess, "stop")
        return {**sess.describe(), "summary": summary(sess)}

    @reg.command("metrics.list")
    async def list_(ctx, a):
        return {"sessions": [s.describe() for s in sessions.values()]}

    @reg.command("metrics.read")
    async def read(ctx, a):
        sess = get(a)
        ctx.resources.touch(key(sess.id))
        kind = a.get("kind") or "samples"
        ring = sess.events if kind == "events" else sess.samples
        try:
            limit = max(1, min(int(a.get("limit") or 600), 4000))
        except (TypeError, ValueError):
            limit = 600
        cur = a.get("cursor")
        cur = None if cur in (None, "") else int(cur)
        wait = min(float(a.get("wait") or 0), 20.0) if sess.running else 0
        page = await ring.wait_read(cur, timeout=wait, limit=limit)
        return {**page, "kind": kind, "session": sess.describe()}

    @reg.command("metrics.snapshot")
    async def snapshot(ctx, a):
        if not a.get("sessionId") and not [s for s in sessions.values() if s.running]:
            return {"sessions": [s.describe() for s in sessions.values()]}
        sess = get(a)
        ctx.resources.touch(key(sess.id))
        out = {"session": sess.describe(), "stats": summary(sess)}
        if a.get("recentEvents"):
            out["recentEvents"] = sess.events.read(max(0, sess.events.seq - 20), limit=20)["entries"]
        return out

    @reg.command("metrics.series")
    async def series(ctx, a):
        sess = get(a)
        metric = str(a.get("metric") or "")
        if metric not in stats.SERIES:
            raise E("bad-args", f"Unknown metric {metric!r}. Known: {', '.join(stats.SERIES)}.")
        try:
            points = max(1, min(int(a.get("points") or 60), 600))
        except (TypeError, ValueError):
            points = 60
        label, unit = stats.SERIES[metric]
        return {"sessionId": sess.id, "metric": metric, "label": label, "unit": unit,
                "buckets": stats.downsample(sess.all_samples(), metric, points),
                "stats": stats.series_stats(sess.all_samples(), metric)}

    @reg.command("metrics.export")
    async def export(ctx, a):
        sess = get(a)
        return {"sessionId": sess.id, **await asyncio.to_thread(export_csv, sess, backend.export_dir)}

    @reg.command("metrics.hitches")
    async def hitches(ctx, a):
        sess = get(a)
        if not sess.running:
            raise E("bad-args", "That measurement has stopped. Start a new one first.")
        try:
            seconds = max(5, min(int(a.get("seconds") or HITCH_MAX_S), HITCH_MAX_S))
        except (TypeError, ValueError):
            seconds = HITCH_MAX_S
        if a.get("stop"):
            sess.hitch_until = time.time()
            sess.set_status(sess.status, sess.error, force=True)
            return sess.describe()
        await backend.hitches(ctx, sess, seconds)
        sess.set_status(sess.status, sess.error, force=True)
        return sess.describe()

    @reg.command("metrics.launchProfile")
    async def launch_profile(ctx, a):
        if not a.get("bundleId"):
            raise E("bad-args", "Which app? (bundleId missing)")
        return await backend.launch_profile(ctx, a)

    @reg.command("metrics.leaks")
    async def leaks(ctx, a):
        if not a.get("bundleId"):
            raise E("bad-args", "Which app? (bundleId missing)")
        return await backend.leaks(ctx, a)

    @reg.welcome
    def welcome(ctx):
        return [("metrics.state", s.describe()) for s in sessions.values() if s.running]

    @reg.on_shutdown
    async def shutdown(ctx):
        for s in list(sessions.values()):
            await finish(ctx, s, "shutdown")

    return sessions
