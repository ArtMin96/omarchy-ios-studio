"""App performance metrics over time (the iPhone page's Performance tab, for agents).

A session samples one app about once a second in the device daemon (CPU, memory, threads, energy,
display FPS, GPU, network, disk, wakeups, battery) and records events (hangs, jetsam kills,
crashes, watchdog kills, thermal changes, hitches). The daemon keeps about an hour per session and
computes the stats, so the tools here stay small: a summary first (~1-2 KB), downsampled series on
request with the full CSV saved to a file, and events by cursor.

ios_launch_profile and ios_memory_leaks change the app's state (relaunch / brief pause), so they
take confirm (dry run by default) and run as jobs: poll by calling again with job_id.
"""
from __future__ import annotations

import asyncio
import itertools
import time
from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from .. import budget as B
from ..budget import Budgeted
from ..models import Out
from ..state import AppState, get_state
from . import MAX_WAIT, READ_REMOTE, ann, tool
from .device import Udid, dcall, resolve_app

Set = Literal["process", "energy", "graphics", "network", "battery", "events", "hitches"]
Metric = Literal["cpu", "mem", "rss", "threads", "wakeups", "faults", "diskR", "diskW", "energy", "eCpu", "eGpu", "eNet",
                 "eDisplay", "eLoc", "eOverhead", "thermal", "fps", "gpu", "gpuMem", "netRx", "netTx", "batt", "battMw",
                 "sysCpu", "hitchMs"]
SessionId = Annotated[str, Field(description="From ios_metrics_start, e.g. 'm1'")]
UNITS = {"cpu": "%", "mem": "MB", "rss": "MB", "threads": "", "energy": "", "fps": "fps", "gpu": "%", "netRx": "KB/s",
         "netTx": "KB/s", "diskR": "KB/s", "diskW": "KB/s", "wakeups": "/s", "battMw": "mW"}
SUMMARY_SERIES = ("cpu", "mem", "rss", "threads", "wakeups", "diskW", "netRx", "netTx", "energy", "fps", "gpu", "battMw")


# ------------------------------------------------------------------ models

class MetricsSession(Out):
    session_id: str
    bundle_id: str | None = None
    name: str | None = None
    pid: int | None = Field(None, description="Current process id; followed across relaunches")
    status: str = Field(description="starting | running | waiting-app (not running) | reconnecting | "
                                    "app-exited | stopped")
    sets: list[str] = Field(default_factory=list)
    interval_ms: int = 1000
    next_steps: list[str] = Field(default_factory=list)


class SeriesStat(Out):
    min: float | None = None
    avg: float | None = None
    p95: float | None = None
    max: float | None = None
    unit: str | None = None


class MetricsSummary(Budgeted, Out):
    session_id: str
    name: str | None = None
    bundle_id: str | None = None
    status: str
    running: bool
    seconds: int = Field(0, description="Time covered by the samples")
    samples: int = 0
    anomalies: list[str] = Field(default_factory=list, description="Plain-language findings, worst first")
    series: dict[str, SeriesStat] = Field(default_factory=dict, description="cpu % of one core, mem = memory "
                                          "footprint, fps = the whole display (not only this app)")
    memory_trend: dict[str, Any] | None = Field(None, description="Linear fit of the footprint since the last "
                                                "launch: slopeMbPerMin, r2, seconds, likelyLeak")
    cpu_spikes: int = Field(0, description="Stretches above 80% for 3 s or more")
    fps_drops: int = Field(0, description="Stretches below 50 fps for 2 s or more while the screen was animating")
    hitches: dict[str, Any] | None = None
    battery: dict[str, Any] | None = Field(None, description="drainPctPerHour, mWh used, avgMw")
    energy_by_component: dict[str, int] | None = Field(None, description="Share (%) of the app's energy impact")
    events: dict[str, int] = Field(default_factory=dict, description="Event counts by kind (ios_metrics_events)")
    error: str | None = None
    notes: list[str] = Field(default_factory=list, description="Metric sets that stopped answering for a while "
                                                                "(they resume by themselves)")
    next_steps: list[str] = Field(default_factory=list)


class Bucket(Out):
    t: int = Field(description="Seconds since the first bucket")
    min: float
    avg: float
    max: float


class MetricsSeries(Budgeted, Out):
    session_id: str
    metric: str
    label: str
    unit: str
    start: str | None = Field(None, description="Local time of t=0")
    seconds_per_bucket: float | None = None
    buckets: list[Bucket] = Field(default_factory=list)
    stats: SeriesStat | None = None
    csv_path: str | None = Field(None, description="Every sample of every metric (one row per second)")
    next_steps: list[str] = Field(default_factory=list)


class MetricsEvents(Budgeted, Out):
    BULK: ClassVar[tuple[str, ...]] = ("events",)
    session_id: str
    running: bool
    count: int
    next_cursor: int = Field(description="Pass as cursor on the next call")
    has_more: bool
    missed: int = 0
    events: list[str] = Field(default_factory=list, description="'<seq> <time> <kind> [<ms> ms] <text>'")
    next_steps: list[str] = Field(default_factory=list)


class ProfileJob(Out):
    job_id: str | None = None
    kind: str = Field(description="launch-profile | memory-leaks")
    state: str = Field(description="dry_run | running | succeeded | failed")
    message: str
    would: list[str] = Field(default_factory=list)
    elapsed_seconds: float | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    next_steps: list[str] = Field(default_factory=list)


# ------------------------------------------------------------------ helpers

def _session(d: dict, steps: list[str] | None = None) -> MetricsSession:
    return MetricsSession(session_id=d["sessionId"], bundle_id=d.get("bundleId"), name=d.get("name"), pid=d.get("pid"),
                          status=d.get("status") or "?", sets=d.get("sets") or [], interval_ms=d.get("intervalMs") or 1000,
                          next_steps=steps or [])


def _summary(snap: dict, extra_steps: list[str] | None = None) -> MetricsSummary:
    sess, st = snap.get("session") or {}, snap.get("stats") or {}
    sid = sess.get("sessionId", "?")
    series = {}
    for k in SUMMARY_SERIES:
        v = (st.get("series") or {}).get(k)
        if v:
            series[k] = SeriesStat(min=v.get("min"), avg=v.get("avg"), p95=v.get("p95"), max=v.get("max"),
                                   unit=UNITS.get(k) or None)
    mem = st.get("memory")
    if mem:
        mem = {k: mem[k] for k in ("slopeMbPerMin", "r2", "seconds", "from", "to", "likelyLeak") if k in mem}
    en = (st.get("energy") or {}).get("byComponent") or {}
    h = st.get("hitches") or {}
    steps = list(extra_steps or [])
    if sess.get("running"):
        steps.append(f"ios_metrics_series(session_id='{sid}', metric='mem') for a trend; ios_metrics_events for "
                     f"hangs/jetsam/crashes; ios_metrics_stop(session_id='{sid}') when done.")
    if mem and mem.get("likelyLeak"):
        steps.append(f"Possible leak: ios_memory_leaks(bundle_id='{sess.get('bundleId')}') counts leaked objects "
                     "(pauses the app briefly; ask the user first).")
    err = sess.get("error")
    return MetricsSummary(
        session_id=sid, name=sess.get("name"), bundle_id=sess.get("bundleId"), status=sess.get("status") or "?",
        running=bool(sess.get("running")), seconds=int((st.get("window") or {}).get("seconds") or 0),
        samples=int((st.get("window") or {}).get("samples") or 0),
        anomalies=[B.clip(a.get("text", ""), 240) for a in (st.get("anomalies") or [])[:8]],
        series=series, memory_trend=mem or None,
        cpu_spikes=int((st.get("cpuSpikes") or {}).get("count") or 0),
        fps_drops=int((st.get("fpsDrops") or {}).get("count") or 0),
        hitches={k: h[k] for k in ("count", "totalMs", "worstMs")} if h.get("count") else None,
        battery={k: v for k, v in (st.get("battery") or {}).items() if k in ("drainPctPerHour", "mWh", "avgMw", "charging")}
        or None,
        energy_by_component={k: int(v.get("share") or 0) for k, v in en.items() if v.get("share")} or None,
        events=st.get("eventCounts") or {},
        error=B.clip_error(str(err.get("message"))) if isinstance(err, dict) and err.get("message") else None,
        notes=[B.clip(n, 200) for n in (sess.get("notes") or [])[:5]],
        next_steps=steps)


def _jobs(state: AppState) -> dict:
    return state.extras.setdefault("metrics.jobs", {})


_job_ids = itertools.count(1)


def _job_view(j: dict, steps: list[str] | None = None) -> ProfileJob:
    st = j["state"]
    msg = {"running": "Still running on the iPhone.", "succeeded": "Done.", "failed": "Failed."}.get(st, st)
    out = ProfileJob(job_id=j["id"], kind=j["kind"], state=st, message=msg,
                     elapsed_seconds=round((j.get("ended") or time.time()) - j["started"], 1),
                     result=B.clip_data(j.get("result")) if j.get("result") else None,
                     error=j.get("error"), next_steps=steps or [])
    if st == "running":
        out.next_steps.append(f"Call {j['tool']}(job_id='{j['id']}', wait_seconds=20) again for the result.")
    elif st == "succeeded" and j.get("result", {}).get("verdict"):
        out.message = str(j["result"]["verdict"])
    return out


async def _run_job(state: AppState, ctx: Context, kind: str, tool_name: str, cmd: str, args: dict, wait: int,
                   timeout: float) -> ProfileJob:
    for j in _jobs(state).values():
        if j["kind"] == kind and j["state"] == "running":
            raise ToolError(f"A {kind} job ({j['id']}) is already running. Poll it with {tool_name}(job_id='{j['id']}').")
    j = {"id": f"p-{next(_job_ids)}", "kind": kind, "tool": tool_name, "state": "running", "started": time.time()}

    async def go():
        try:
            j["result"] = await dcall(state, cmd, args, timeout=timeout)
            j["state"] = "succeeded"
        except ToolError as exc:
            j["state"], j["error"] = "failed", B.clip_error(str(exc))
        except Exception as exc:  # noqa: BLE001
            j["state"], j["error"] = "failed", B.clip_error(f"{type(exc).__name__}: {exc}")
        j["ended"] = time.time()

    j["task"] = asyncio.create_task(go())
    _jobs(state)[j["id"]] = j
    jobs = _jobs(state)
    for old in [k for k, v in jobs.items() if v["state"] != "running"][:-10]:
        jobs.pop(old, None)
    return await _wait_job(j, wait)


async def _wait_job(j: dict, wait: int) -> ProfileJob:
    if j["state"] == "running" and wait > 0:
        try:
            await asyncio.wait_for(asyncio.shield(j["task"]), min(wait, MAX_WAIT))
        except asyncio.TimeoutError:
            pass
    return _job_view(j)


def _get_job(state: AppState, job_id: str, kind: str) -> dict:
    j = _jobs(state).get(job_id.strip())
    if j is None or j["kind"] != kind:
        raise ToolError(f"No {kind} job '{job_id}' (jobs live only while this MCP server runs).")
    return j


def _fmt_event(seq: int, e: dict) -> str:
    t = time.strftime("%H:%M:%S", time.localtime(float(e.get("ts") or 0)))
    ms = f" {e['durationMs']:.0f} ms" if isinstance(e.get("durationMs"), (int, float)) else ""
    return f"{seq} {t} {e.get('kind')}{ms} {B.clip(e.get('text', ''), 200)}"


# ------------------------------------------------------------------ tools

def register(mcp: MCPServer) -> None:

    @tool(mcp, annotations=ann("Start measuring an app", read_only=False, destructive=False, idempotent=False,
                              open_world=True))
    async def ios_metrics_start(
        ctx: Context,
        bundle_id: Annotated[str | None, Field(description="App to measure, e.g. 'com.example.myapp' (the installed "
                                                           "XTL-<TEAM>. form and the display name work too). It's "
                                                           "followed across relaunches")] = None,
        pid: Annotated[int | None, Field(description="Instead of bundle_id: one process id (ios_process_list); not "
                                                     "followed after it exits")] = None,
        sets: Annotated[list[Set] | None, Field(description="What to sample. Default: process (CPU, memory, threads, "
                                                            "disk, wakeups), energy, graphics (display FPS, GPU), "
                                                            "network, battery, events (hangs, jetsam, crashes, "
                                                            "watchdog, thermal). 'hitches' adds 60 s of frame "
                                                            "hitch tracking (heavy)")] = None,
        interval_ms: Annotated[int, Field(ge=1000, le=10000, description="Sample interval")] = 1000,
        udid: Udid = None,
    ) -> MetricsSession:
        """Start sampling an app's performance on the iPhone about once a second (kept ~1 hour). It doesn't
        change the app. Let the user exercise the app for a minute or two, then call ios_metrics_summary for
        stats and plain-language anomalies (possible leak, CPU spikes, FPS drops, hangs, jetsam kills), and
        ios_metrics_stop when done (it returns the final summary). The app need not be running yet: the
        session waits for it and follows relaunches."""
        state = get_state(ctx)
        if not bundle_id and pid is None:
            raise ToolError("Pass bundle_id (or pid). ios_app_list shows installed apps.")
        a: dict[str, Any] = {"intervalMs": interval_ms}
        if bundle_id:
            app = await resolve_app(state, bundle_id, udid)
            a.update(bundleId=app["bundleId"], name=app.get("name"))
        else:
            a["pid"] = pid
        if sets:
            a["sets"] = list(dict.fromkeys(sets))
        d = await dcall(state, "metrics.start", a, udid=udid, timeout=60)
        sid = d["sessionId"]
        steps = [f"Ask the user to use the app, then ios_metrics_summary(session_id='{sid}').",
                 f"ios_metrics_stop(session_id='{sid}') when done."]
        if d.get("status") == "waiting-app":
            steps.insert(0, f"The app isn't running yet: ios_app_launch(bundle_id='{d.get('bundleId')}') or ask the "
                            "user to open it. Sampling starts when it runs.")
        return _session(d, steps)

    @tool(mcp, annotations=ann("Stop measuring", read_only=False, destructive=False, idempotent=True,
                              open_world=True))
    async def ios_metrics_stop(ctx: Context, session_id: SessionId) -> MetricsSummary:
        """Stop a metrics session and return its final summary (the data stays readable for a while with
        ios_metrics_summary / ios_metrics_series / ios_metrics_events)."""
        state = get_state(ctx)
        d = await dcall(state, "metrics.stop", {"sessionId": session_id}, timeout=30,
                        not_found="ios_metrics_start starts a new session.")
        return _summary({"session": d, "stats": d.get("summary")})

    @tool(mcp, annotations=READ_REMOTE("Performance summary"))
    async def ios_metrics_summary(ctx: Context, session_id: SessionId) -> MetricsSummary:
        """Stats of a metrics session so far: min/avg/p95/max per key series, the memory trend (MB/min with
        R², flagged as a likely leak when it keeps growing), CPU spikes, display FPS drops, hitches, battery
        drain and energy by component, plus plain-language anomalies, worst first. About 1-2 KB."""
        state = get_state(ctx)
        snap = await dcall(state, "metrics.snapshot", {"sessionId": session_id}, timeout=30,
                           not_found="ios_metrics_start starts a new session.")
        return _summary(snap)

    @tool(mcp, annotations=READ_REMOTE("Metric over time"))
    async def ios_metrics_series(
        ctx: Context,
        session_id: SessionId,
        metric: Annotated[Metric, Field(description="cpu (% of one core), mem (footprint MB), rss, threads, wakeups "
                                                    "(/s), faults, diskR/diskW (KB/s), energy and its parts eCpu, "
                                                    "eGpu, eNet, eDisplay, eLoc, eOverhead, thermal, fps (whole "
                                                    "display), gpu (%), gpuMem (MB), netRx/netTx (KB/s), batt (%), "
                                                    "battMw, sysCpu, hitchMs")] = "mem",
        points: Annotated[int, Field(ge=2, le=60, description="Buckets to downsample to")] = 30,
    ) -> MetricsSeries:
        """One metric over the session, downsampled to at most 60 buckets with min/avg/max per bucket, plus
        the path of a CSV with every sample of every metric (for plotting or a closer look)."""
        state = get_state(ctx)
        d = await dcall(state, "metrics.series", {"sessionId": session_id, "metric": metric, "points": points},
                        timeout=30, not_found="ios_metrics_start starts a new session.")
        raw = d.get("buckets") or []
        t0 = raw[0]["t0"] if raw else None
        buckets = [Bucket(t=int(b["t0"] - t0), min=b["min"], avg=b["avg"], max=b["max"]) for b in raw]
        span = (raw[-1]["t1"] - t0) / len(raw) if len(raw) > 1 else None
        st = d.get("stats") or {}
        out = MetricsSeries(session_id=session_id, metric=metric, label=d.get("label") or metric, unit=d.get("unit") or "",
                            start=time.strftime("%H:%M:%S", time.localtime(t0)) if t0 else None,
                            seconds_per_bucket=round(span, 1) if span else None, buckets=buckets,
                            stats=SeriesStat(min=st.get("min"), avg=st.get("avg"), p95=st.get("p95"), max=st.get("max"),
                                             unit=d.get("unit") or None) if st else None)
        link = None
        try:
            ex = await dcall(state, "metrics.export", {"sessionId": session_id}, timeout=30)
            text = await asyncio.to_thread(Path(ex["path"]).read_text)
            path, _, link = B.spill(text, "metrics", Path(ex["path"]).name, mime_type="text/csv")
            out.csv_path = path
        except Exception as exc:  # noqa: BLE001 - the series is still useful without the file
            out.next_steps.append(f"(CSV export failed: {B.clip(str(exc), 120)})")
        if not buckets:
            out.next_steps.append("No samples of this metric yet: is the app running and is its set enabled?")
        return out.with_budget(None, [link] if link else None)

    @tool(mcp, annotations=READ_REMOTE("Performance events"))
    async def ios_metrics_events(
        ctx: Context,
        session_id: SessionId,
        cursor: Annotated[int | None, Field(ge=0, description="next_cursor from the previous call (0 = from the "
                                                              "start)")] = None,
        limit: Annotated[int, Field(ge=1, le=100, description="Max events")] = 40,
        kinds: Annotated[list[Literal["hang", "jetsam", "thermal", "crash", "watchdog", "hitch", "exit", "launch",
                                      "note"]] | None, Field(description="Only these kinds")] = None,
        max_bytes: Annotated[int | None, Field(ge=1000, le=B.MAX_BYTES, description="Raise the size cap for text + "
                                                                                  "structuredContent together (default 12000 bytes)")] = None,
    ) -> MetricsEvents:
        """Events of a metrics session by cursor: hang, jetsam (killed for memory), crash, watchdog, thermal,
        hitch (with duration), exit/launch of the app. A page holds at most `limit` events and stays under
        12 KB (max_bytes raises it); next_cursor always points right after the last event returned, so
        reading on with it never skips one. Hang rows need Hang Detection on (iPhone Settings › Developer ›
        Hang Detection)."""
        state = get_state(ctx)
        page = await dcall(state, "metrics.read", {"sessionId": session_id, "kind": "events",
                                                   "cursor": cursor if cursor is not None else 0, "limit": 500},
                           timeout=30, not_found="ios_metrics_start starts a new session.")
        first = int(page.get("cursor") or 0)
        rows: list[tuple[int, str]] = []       # (cursor after this event, rendered line)
        nxt = first
        for i, e in enumerate(page.get("entries") or []):
            nxt = first + i + 1
            if kinds and e.get("kind") not in kinds:
                continue
            rows.append((nxt, _fmt_event(first + i, e)))
            if len(rows) >= limit:
                break
        fit = B.take([ln for _, ln in rows], len(rows), B.room(max_bytes))
        lines = fit.items
        note = None
        if fit.omitted:                         # cut by size: read on right after the last event shown
            nxt = rows[len(lines) - 1][0] if lines else first
            note = (f"{fit.omitted} more events didn't fit the size cap; read on with cursor={nxt} "
                    "(or pass max_bytes).")
        has_more = nxt < int(page.get("total") or 0)
        sess = page.get("session") or {}
        steps = []
        if has_more:
            steps.append(f"More: ios_metrics_events(session_id='{session_id}', cursor={nxt}).")
        return MetricsEvents(session_id=session_id, running=bool(sess.get("running")), count=len(lines),
                             next_cursor=nxt, has_more=has_more, missed=int(page.get("missed") or 0), events=lines,
                             note=note, truncated=bool(fit.omitted), next_steps=steps).with_budget(max_bytes)

    @tool(mcp, annotations=ann("Profile an app's launch", read_only=False, destructive=True, idempotent=False,
                              open_world=True))
    async def ios_launch_profile(
        ctx: Context,
        bundle_id: Annotated[str | None, Field(description="The app to relaunch and time, e.g. 'com.example.myapp'"
                                                           )] = None,
        confirm: Annotated[bool, Field(description="false (default) = dry run; true = quit and relaunch the app now")] = False,
        job_id: Annotated[str | None, Field(description="Poll an earlier call's job instead of starting one")] = None,
        wait_seconds: Annotated[int, Field(ge=0, le=MAX_WAIT, description="Wait this long for the result")] = 20,
        udid: Udid = None,
    ) -> ProfileJob:
        """Time an app's launch: quits it, relaunches it, and reports the loader (dyld) and static-initializer
        time before main() plus the time to the first frame. Loader timing works for your own
        development-signed apps. Unsaved state in the app is lost, so ask the user, then confirm=true. Runs
        as a job: if state is 'running', call again with job_id."""
        state = get_state(ctx)
        if job_id:
            return await _wait_job(_get_job(state, job_id, "launch-profile"), wait_seconds)
        if not bundle_id:
            raise ToolError("Pass bundle_id (or job_id to poll an earlier call).")
        app = await resolve_app(state, bundle_id, udid)
        if not confirm:
            return ProfileJob(kind="launch-profile", state="dry_run",
                              message=f"This quits {app.get('name') or app['bundleId']} and relaunches it to time the "
                                      "launch. Unsaved state in the app is lost. Nothing was changed. Ask the user to "
                                      "confirm, then call again with confirm=true.",
                              would=[f"quit {app['bundleId']}", "relaunch it suspended, time dyld and the first frame"],
                              next_steps=["Ask the user whether to proceed; only then repeat with confirm=true."])
        a = {"bundleId": app["bundleId"], **({"udid": udid} if udid else {})}
        return await _run_job(state, ctx, "launch-profile", "ios_launch_profile", "metrics.launchProfile", a,
                              wait_seconds, 120)

    @tool(mcp, annotations=ann("Check an app for memory leaks", read_only=False, destructive=False, idempotent=False,
                              open_world=True))
    async def ios_memory_leaks(
        ctx: Context,
        bundle_id: Annotated[str | None, Field(description="A running app of yours, e.g. 'com.example.myapp'")] = None,
        confirm: Annotated[bool, Field(description="false (default) = dry run; true = scan now (the app pauses "
                                                   "for a few seconds)")] = False,
        job_id: Annotated[str | None, Field(description="Poll an earlier call's job instead of starting one")] = None,
        wait_seconds: Annotated[int, Field(ge=0, le=MAX_WAIT, description="Wait this long for the result")] = 20,
        udid: Udid = None,
    ) -> ProfileJob:
        """Scan a running app's heap for leaked objects (a memory graph, like Xcode's): leak count and the
        saved .memgraph file. Only your own development-signed apps; the app pauses for a few seconds while
        it's scanned, so ask the user, then confirm=true. Runs as a job: poll with job_id."""
        state = get_state(ctx)
        if job_id:
            return await _wait_job(_get_job(state, job_id, "memory-leaks"), wait_seconds)
        if not bundle_id:
            raise ToolError("Pass bundle_id (or job_id to poll an earlier call).")
        app = await resolve_app(state, bundle_id, udid)
        if not confirm:
            return ProfileJob(kind="memory-leaks", state="dry_run",
                              message=f"This pauses {app.get('name') or app['bundleId']} for a few seconds while its "
                                      "memory is scanned. Nothing was changed. Ask the user to confirm, then call "
                                      "again with confirm=true.",
                              would=[f"scan the heap of {app['bundleId']} (it must be running)"],
                              next_steps=["Ask the user whether to proceed; only then repeat with confirm=true."])
        a = {"bundleId": app["bundleId"], **({"udid": udid} if udid else {})}
        return await _run_job(state, ctx, "memory-leaks", "ios_memory_leaks", "metrics.leaks", a, wait_seconds, 240)


async def on_shutdown(state: AppState) -> None:
    for j in _jobs(state).values():
        t = j.get("task")
        if t and not t.done():
            t.cancel()
