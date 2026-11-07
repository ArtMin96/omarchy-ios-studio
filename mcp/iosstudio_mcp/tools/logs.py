"""Live iPhone log (syslog) as cursor-polled sessions, and the run-and-capture workflow tool.

The phone emits ~1500-2000 log entries per second, so a session always has a filter. Each session is
its own connection to the device daemon (the daemon keeps one syslog subscription per connection and
filters per connection), feeding a server-side ring buffer with stable sequence numbers. Agents pull
with ios_logs_read(cursor, wait_seconds). Sessions nobody has read for IDLE_STOP seconds stop by
themselves.
"""
from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Annotated, Any, ClassVar, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from .. import helper
from ..backends.console import ConsoleSummary, capture_summary, fmt_line
from ..backends.crashes import crash_detail
from ..backends.device import AppInstallStatus, dcall, install_ipa, new_client, package_for, resolve_app
from ..backends.projects import merge_launch, resolve_preset, resolve_project
from ..core import budget as B
from ..core import deadline
from ..core.errors import device_error
from ..core.job_status import follow_job, job_result, still_running
from ..core.jobs import Job
from ..core.params import MaxBytes, OptJobId, PresetName, Udid, WaitSeconds
from ..core.registry import MAX_WAIT, tool
from ..models import ActionResult, LogRead, LogSession, RunCapture
from ..state import AppState, get_state

log = logging.getLogger("iosstudio.logs")

RING = 5000            # entries kept per session
MAX_SESSIONS = 4
IDLE_STOP = 600        # seconds without a read before a session stops itself
MSG_CUT = B.LINE_CUT
LEVELS = ["debug", "info", "default", "error", "fault"]
RANK = {lv: i for i, lv in enumerate(LEVELS)}

Level = Literal["debug", "info", "default", "error", "fault"]


class RunCaptureResult(RunCapture):
    """RunCapture, or (state='running') a run that is still going: call again with job_id."""
    BULK: ClassVar[tuple[str, ...]] = ("key_lines", "console_lines")
    state: Literal["done", "running"] = Field("done", description="running: still installing, launching or "
                                              "capturing; call ios_app_run_and_capture(job_id=...) again")
    job_id: str | None = Field(None, description="The run's job: pass back as job_id to wait for it")
    installed: AppInstallStatus | None = None
    console: ConsoleSummary | None = Field(None, description="What the app printed (stdout/stderr): counts, and "
                                                             "the session for ios_app_console_read")
    console_lines: list[str] = Field(default_factory=list, description="The app's print() output, after the log "
                                     "lines: stderr lines and the latest ones, '<time> [stderr] <text>'")
    preset: str | None = Field(None, description="Run preset the app was launched with")
    launch_args: list[str] = Field(default_factory=list, description="Launch arguments passed (preset's, then explicit)")
    launch_env: dict[str, str] = Field(default_factory=dict, description="Launch environment passed")


@dataclass
class Session:
    id: str
    filters: dict
    udid: str | None
    client: Any = None
    ring: deque = field(default_factory=lambda: deque(maxlen=RING))   # (seq, entry)
    seq: int = 0                      # next sequence number
    read_cursor: int = 0              # where the last read ended (default cursor)
    running: bool = False
    dropped: int = 0
    error: str | None = None
    created: float = field(default_factory=time.time)
    last_read: float = field(default_factory=time.time)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    internal: bool = False            # run_and_capture's private session (not listed)

    @property
    def buffer_start(self) -> int:
        return self.seq - len(self.ring)

    def on_event(self, name: str, data: Any) -> None:
        if name == "syslog" and isinstance(data, list):
            for e in data:
                if isinstance(e, dict):
                    self.ring.append((self.seq, e))
                    self.seq += 1
            self.changed.set()
        elif name == "syslogState" and isinstance(data, dict):
            self.dropped = int(data.get("dropped") or 0)
            err = data.get("error")
            if err:
                self.error = str(err.get("message") if isinstance(err, dict) else err)
            if data.get("running") is False and not data.get("reconnecting"):
                self.running = False
            self.changed.set()
        elif name == "reconnected":   # daemon restarted: our subscription is gone, redo it
            if self.running:
                asyncio.ensure_future(self._resubscribe())

    async def _resubscribe(self) -> None:
        try:
            await self.client.request("syslog.start", self._args(), timeout=30)
        except Exception as exc:  # noqa: BLE001
            self.error = f"Lost the log stream after the device service restarted: {exc}"
            self.running = False
            self.changed.set()

    def _args(self) -> dict:
        a: dict = {"filters": self.filters}
        if self.udid:
            a["udid"] = self.udid
        return a


def _sessions(state: AppState) -> dict[str, Session]:
    return state.extras.setdefault("logs.sessions", {})


_ids = itertools.count(1)


def build_filters(bundle_id: str | None, process: list[str] | None, pid: int | None, min_level: str | None,
                  text: str | None, subsystem: str | None) -> dict:
    f: dict = {}
    if bundle_id:
        f["bundleId"] = bundle_id.strip()
    if process:
        f["processes"] = [p.strip() for p in process if p and p.strip()]
    if pid:
        f["pids"] = [int(pid)]
    if min_level:
        f["minLevel"] = min_level
    if text:
        f["text"] = text
    if subsystem:
        f["subsystem"] = subsystem
    narrowing = any(k in f for k in ("bundleId", "processes", "pids", "text", "subsystem"))
    if not narrowing and RANK.get(min_level or "debug", 0) < RANK["default"]:
        raise ToolError("Give at least one filter: bundle_id, process, pid, text or subsystem, or "
                        "min_level='default'/'error'/'fault'. Unfiltered, the iPhone logs ~2000 lines a second.")
    return f


async def open_session(state: AppState, filters: dict, udid: str | None, internal: bool = False) -> Session:
    sessions = _sessions(state)
    live = [s for s in sessions.values() if s.running and not s.internal]
    if not internal and len(live) >= MAX_SESSIONS:
        raise ToolError(f"{len(live)} log sessions are already running ({', '.join(s.id for s in live)}). "
                        "Stop one with ios_logs_stop first.")
    sid = f"L{next(_ids)}"
    s = Session(id=sid, filters=filters, udid=udid, internal=internal)
    # Our own connection: the daemon has one syslog subscription per connection. events=[] means no
    # device-wide broadcasts; our syslog/syslogState still arrive.
    s.client = new_client(f"mcp-logs-{sid}", [])
    s.client.on_event(s.on_event, ["syslog", "syslogState", "reconnected"])
    try:
        await asyncio.wait_for(s.client.connect(), deadline.clamp(40, "connecting to the iPhone service"))
        await s.client.request("syslog.start", s._args(), timeout=deadline.clamp(40, "the log stream started"))
    except Exception as exc:  # noqa: BLE001
        with contextlib.suppress(Exception):
            await s.client.close()
        if exc.__class__.__name__ == "DeviceError":
            raise device_error(exc) from None
        if isinstance(exc, asyncio.TimeoutError):
            raise ToolError("The iPhone service is still starting [timeout]. Next: retry in a few seconds.") from None
        raise
    s.running = True
    sessions[sid] = s
    _ensure_reaper(state)
    return s


async def close_session(state: AppState, s: Session) -> None:
    _sessions(state).pop(s.id, None)
    was = s.running
    s.running = False
    if s.client is not None:
        if was:
            with contextlib.suppress(Exception):
                await s.client.request("syslog.stop", {}, timeout=5)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(s.client.close(), 5)
    s.changed.set()


def _ensure_reaper(state: AppState) -> None:
    t = state.extras.get("logs.reaper")
    if t is None or t.done():
        state.extras["logs.reaper"] = asyncio.create_task(_reaper(state))


async def _reaper(state: AppState) -> None:
    deadline.clear()       # a background task: the tool call that started it has its own budget
    while _sessions(state):
        await asyncio.sleep(30)
        now = time.time()
        for s in list(_sessions(state).values()):
            if now - s.last_read > IDLE_STOP:
                log.info("log session %s unread for %d s; stopping it", s.id, IDLE_STOP)
                await close_session(state, s)


def fmt(seq: int | None, e: dict, cut: int = MSG_CUT) -> str:
    ts = e.get("ts")
    try:
        t = time.strftime("%H:%M:%S", time.localtime(float(ts))) + f".{int((float(ts) % 1) * 1000):03d}"
    except (TypeError, ValueError):
        t = "--:--:--"
    msg = str(e.get("message") or "").replace("\r", "").replace("\n", " ⏎ ")
    if len(msg) > cut:
        msg = msg[:cut] + f" …[+{len(msg) - cut}]"
    sub = e.get("subsystem") or ""
    head = f"{seq} " if seq is not None else ""
    return f"{head}{t} {e.get('level', '?')} {e.get('process', '?')}[{e.get('pid', '?')}]{(' ' + sub) if sub else ''}: {msg}"


def _get(state: AppState, session_id: str) -> Session:
    s = _sessions(state).get(session_id)
    if s is None:
        raise ToolError(f"No log session '{session_id}' (it may have been stopped, or stopped itself after "
                        f"{IDLE_STOP // 60} minutes without reads). Start one with ios_logs_start.")
    return s


async def on_shutdown(state: AppState) -> None:
    t = state.extras.pop("logs.reaper", None)
    if t:
        t.cancel()
    for s in list(_sessions(state).values()):
        await close_session(state, s)


def _filter_desc(f: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in f.items())


def register(mcp: MCPServer) -> None:

    @tool(mcp, "Start iPhone log", kind="write", open_world=True)
    async def ios_logs_start(
        ctx: Context,
        bundle_id: Annotated[str | None, Field(description="Only this app's lines")] = None,
        process: Annotated[list[str] | None, Field(description="Exact process names")] = None,
        pid: Annotated[int | None, Field(description="One process id (filtered on the phone)")] = None,
        min_level: Annotated[Level | None, Field(description="Minimum level (debug < … < fault)")] = None,
        text: Annotated[str | None, Field(description="Case-insensitive substring")] = None,
        subsystem: Annotated[str | None, Field(description="os_log subsystem prefix")] = None,
        udid: Udid = None,
    ) -> LogSession:
        """Start buffering the iPhone's live log; returns session_id and cursor 0. Needs a filter (usually
        bundle_id). Then ios_logs_read(session_id, cursor, wait_seconds=10) repeatedly and ios_logs_stop.
        For "launch it and tell me what happened" use ios_app_run_and_capture."""
        state = get_state(ctx)
        f = build_filters(bundle_id, process, pid, min_level, text, subsystem)
        s = await open_session(state, f, udid)
        return LogSession(session_id=s.id, running=True, filters=f, cursor=0,
                          next_steps=[f"ios_logs_read(session_id='{s.id}', cursor=0, wait_seconds=10); reproduce "
                                      "the behaviour on the phone meanwhile (ios_app_launch).",
                                      f"ios_logs_stop(session_id='{s.id}') when done."])

    @tool(mcp, "Read iPhone log", kind="read_remote")
    async def ios_logs_read(
        ctx: Context,
        session_id: Annotated[str, Field(description="From ios_logs_start")],
        cursor: Annotated[int | None, Field(ge=0, description="next_cursor of the last read (0: start; omit: "
                                                              "continue)")] = None,
        limit: Annotated[int, Field(ge=1, le=500)] = 60,
        wait_seconds: Annotated[int, Field(ge=0, le=MAX_WAIT, description="Wait this long for new lines")] = 0,
        min_level: Annotated[Level | None, Field(description="This read only")] = None,
        text: Annotated[str | None, Field(max_length=B.MAX_PATTERN, description="Substring, this read only")] = None,
        grep: Annotated[str | None, Field(max_length=B.MAX_PATTERN, description="Regex, this read only")] = None,
        newest: Annotated[bool, Field(description="The newest `limit` lines instead of the oldest unread")] = False,
        line_chars: Annotated[int, Field(ge=80, le=4000, description="Cut messages after this many chars")] = B.LINE_CUT,
        max_bytes: MaxBytes = None,
    ) -> LogRead:
        """Read buffered log lines after `cursor`, oldest first, as plain text after the JSON header. Pass the
        returned next_cursor next time; `missed` means the buffer overwrote unread lines. wait_seconds waits for
        new lines; min_level/text/grep narrow this read only."""
        state = get_state(ctx)
        s = _get(state, session_id)
        s.last_read = time.time()
        cur = s.read_cursor if cursor is None else cursor
        if cur > s.seq:
            cur = s.seq
        if wait_seconds and cur >= s.seq and s.running:
            s.changed.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(s.changed.wait(), deadline.wait_time(wait_seconds))
        missed = max(0, s.buffer_start - cur)
        cur = max(cur, s.buffer_start)
        lo = RANK.get(min_level or "debug", 0)
        tl = text.lower() if text else None
        rx = B.compile_pattern(grep) if grep else None
        snap = list(s.ring)
        end_seq = snap[-1][0] + 1 if snap else s.seq

        def passes(e: dict) -> bool:
            if RANK.get(e.get("level"), 2) < lo:
                return False
            msg, proc = str(e.get("message", "")), str(e.get("process", ""))
            if tl and tl not in msg.lower() and tl not in proc.lower():
                return False
            return not rx or bool(rx.search(msg) or rx.search(proc))

        matches = [(seq, e) for seq, e in snap if seq >= cur and passes(e)]
        sel = matches[-limit:] if newest else matches[:limit]
        rows = B.fold_repeats(sel, key=lambda se: (se[1].get("process"), se[1].get("level"), se[1].get("message")))
        rendered = [fmt(seq, e, line_chars) + (f" ×{n}" if n > 1 else "") for (seq, e), n in rows]
        page = B.take(rendered, len(rendered), B.room(max_bytes), from_end=newest)
        cut_rows = len(rendered) - len(page.items)
        shown_rows = rows[page.start:page.start + len(page.items)]
        shown = sum(n for _, n in shown_rows)
        if newest or (not cut_rows and len(matches) <= limit):
            nxt = end_seq                    # everything up to the end was looked at
        else:                                # stop right after the last entry shown
            nxt = sel[shown - 1][0] + 1 if shown else cur
        skipped = len(matches) - shown if newest else 0
        s.read_cursor = nxt
        more = nxt < s.seq
        steps = []
        note = None
        if more:
            steps.append(f"More is buffered: read again with cursor={nxt}.")
            if cut_rows:
                note = f"{cut_rows} more lines didn't fit the size cap; read on from cursor={nxt} (or pass max_bytes)."
        elif s.running:
            steps.append(f"Caught up. Call again with cursor={nxt} and wait_seconds=10 for new lines.")
        if skipped:
            note = f"newest=true skipped {skipped} older matching lines."
        if not s.running:
            steps.append("The session has stopped" + (f": {B.clip(s.error, 200)}" if s.error else "") +
                         ". Start a new one with ios_logs_start.")
        if missed:
            steps.append(f"{missed} lines were overwritten before you read them; read more often or narrow the filter.")
        if not page.items and s.seq == 0 and s.running:
            steps.append("Nothing captured yet. Is the app running? Filters: " + _filter_desc(s.filters))
        return LogRead(session_id=s.id, running=s.running, lines=page.items, count=len(page.items), next_cursor=nxt,
                       has_more=more, buffer_start=s.buffer_start, missed=missed, dropped=s.dropped,
                       skipped=skipped, total_received=s.seq, error=B.clip(s.error, 300) if s.error else None,
                       next_steps=steps, truncated=bool(cut_rows or skipped), omitted=cut_rows,
                       note=note).with_budget(max_bytes)

    @tool(mcp, "Stop iPhone log", kind="write", idempotent=True, open_world=True)
    async def ios_logs_stop(
        ctx: Context,
        session_id: Annotated[str | None, Field(description="Omit to stop all of yours")] = None,
    ) -> ActionResult:
        """Stop a log session and drop its buffer."""
        state = get_state(ctx)
        targets = [_get(state, session_id)] if session_id else [s for s in _sessions(state).values()
                                                                  if not s.internal]
        for s in targets:
            await close_session(state, s)
        ids = ", ".join(s.id for s in targets)
        return ActionResult(done=True, message=f"Stopped {ids}." if targets else "No log sessions were running.")

    # -------------------------------------------------------------- workflow

    @tool(mcp, "Run app and capture", kind="write", open_world=True)
    async def ios_app_run_and_capture(
        ctx: Context,
        bundle_id: Annotated[str | None, Field(description="App to launch")] = None,
        project_path: Annotated[str | None, Field(description="Instead of bundle_id: its project")] = None,
        install: Annotated[bool, Field(description="First install the last signed build (no rebuild)")] = False,
        ipa_path: Annotated[str | None, Field(description="A signed .ipa/.app to install first")] = None,
        job_id: OptJobId = None,
        wait_seconds: WaitSeconds = MAX_WAIT,
        seconds: Annotated[int, Field(ge=1, le=MAX_WAIT, description="Capture time after launch")] = 8,
        min_level: Level = "default",
        relaunch: Annotated[bool, Field(description="Stop the app first for a fresh start")] = True,
        args: Annotated[list[str] | None, Field(description="Launch arguments")] = None,
        env: Annotated[dict[str, str] | None, Field(description="Launch environment")] = None,
        max_lines: Annotated[int, Field(ge=5, le=200, description="Log lines in the summary")] = 40,
        console: Annotated[bool, Field(description="Also capture print() / stdout / stderr")] = True,
        max_bytes: MaxBytes = None,
        udid: Udid = None,
        preset: PresetName = None,
    ) -> RunCaptureResult:
        """"Launch it and tell me what happened": launches the app (optionally installing first), captures its
        log and print() output for `seconds`, then reports launched/pid, key log lines, any NEW crash and next
        steps. A job: state='running' + job_id if not done within wait_seconds; call again with job_id. For
        longer observation use ios_logs_start."""
        state = get_state(ctx)
        if not job_id:
            proj = None
            if project_path and not bundle_id:
                proj = await resolve_project(state, project_path)
                bundle_id = proj.get("bundleId")
                if not bundle_id:
                    raise ToolError(f"{proj.get('name')} has no bundle id yet. Check ios_project_info.")
            if not bundle_id and not ipa_path:
                raise ToolError("Pass bundle_id or project_path (or ipa_path to install and run).")
            how = None
            if preset and (project_path or bundle_id):   # before any install: an unknown name fails at once
                how = await resolve_preset(state, preset, project_path=project_path, bundle_id=bundle_id)
            pkg = None
            if install or ipa_path:
                pkg, _ = await package_for(state, ipa_path, None if ipa_path else (project_path or None))
            req = RunRequest(bundle_id=bundle_id, project_path=project_path, pkg=pkg, preset=preset, how=how,
                             seconds=seconds, min_level=min_level, relaunch=relaunch, args=args, env=env,
                             max_lines=max_lines, console=console, max_bytes=max_bytes, udid=udid)
            job_id = state.jobs.run(lambda job: _run_and_capture(state, job, req), "run-capture",
                                    title=f"Run and capture {bundle_id or helper.tilde(pkg)}",
                                    tool="ios_app_run_and_capture", data={"bundle_id": bundle_id}).id
        job = await follow_job(state, ctx, job_id, "run-capture", wait_seconds)
        if not job.finished:
            return RunCaptureResult(state="running", job_id=job.id, bundle_id=job.data.get("bundle_id") or "",
                                    launched=bool(job.data.get("launched")), seconds=0,
                                    summary=f"Still running ({job.progress_detail or 'starting'}).",
                                    next_steps=[still_running(job)])
        out = job_result(job)
        out.job_id = job.id
        return out


@dataclass
class RunRequest:
    """The arguments of one ios_app_run_and_capture, for its job."""
    bundle_id: str | None
    project_path: str | None
    pkg: str | None              # install this first
    preset: str | None
    how: dict | None             # the resolved preset (None: resolve it from the installed bundle id)
    seconds: int
    min_level: str
    relaunch: bool
    args: list[str] | None
    env: dict[str, str] | None
    max_lines: int
    console: bool
    max_bytes: int | None
    udid: str | None


async def _run_and_capture(state: AppState, job: Job, r: RunRequest) -> RunCaptureResult:
    """The job behind ios_app_run_and_capture: (install,) launch, capture, look for a new crash, summarize."""
    bundle_id, udid, how, seconds, max_lines, max_bytes = r.bundle_id, r.udid, r.how, r.seconds, r.max_lines, r.max_bytes
    min_level, console, preset, relaunch = r.min_level, r.console, r.preset, r.relaunch
    installed = None
    total = seconds + 6.0
    if r.pkg:
        job.report(0.02, "Installing")

        def prog(pct: float, status: str) -> None:
            job.report((0.1 + 0.3 * pct / 100) / total, f"Installing {pct:.0f}%")
        res = await install_ipa(state, udid, r.pkg, prog)
        installed = AppInstallStatus(installed=True, state="succeeded", bundle_id=res.get("bundleId"),
                                     package_bundle_id=res.get("packageBundleId"), path=helper.tilde(r.pkg),
                                     seconds=res.get("seconds"))
        if installed.bundle_id:
            bundle_id = installed.bundle_id
            job.data["bundle_id"] = bundle_id
    app = await resolve_app(state, bundle_id, udid)
    bid, exe = app["bundleId"], app.get("executable") or app.get("name")
    if preset and how is None:   # the bundle id came from the install
        how = await resolve_preset(state, preset, bundle_id=bid)
    args, env = merge_launch(how, r.args, r.env)
    job.report(0.5 / total, "Checking crash reports")
    try:
        before = {c.get("name") for c in await dcall(state, "crash.list", {"process": exe, "flush": True},
                                                     udid=udid, timeout=30) or []}
    except ToolError:
        before = None
    sess = await open_session(state, {"bundleId": bid, "minLevel": min_level}, udid, internal=True)
    launch_error, pid, running_end = None, None, None
    launch_res: dict = {}
    started_wall = time.time()
    t0 = time.monotonic()
    try:
        if relaunch and app.get("running"):
            with contextlib.suppress(ToolError):
                await dcall(state, "app.kill", {"bundleId": bid}, udid=udid, timeout=20)
            await asyncio.sleep(0.5)
        job.report(1.5 / total, "Launching")
        try:
            a: dict = {"bundleId": bid}
            if args:
                a["args"] = args
            if env:
                a["env"] = env
            if console:
                a["console"] = True
            res = await dcall(state, "app.launch", a, udid=udid, timeout=60) or {}
            launch_res = res
            pid = res.get("pid")
            job.data["launched"] = True
        except ToolError as exc:
            launch_error = str(exc)
        t0 = time.monotonic()
        if launch_error is None:
            end = t0 + seconds
            while (now := time.monotonic()) < end:
                await asyncio.sleep(min(1.0, end - now))
                job.report((1.5 + time.monotonic() - t0) / total, f"Capturing ({len(sess.ring)} lines)")
            with contextlib.suppress(ToolError):
                procs = await dcall(state, "processes.list", {"appsOnly": True}, udid=udid, timeout=20) or []
                running_end = any(p.get("bundleId") == bid or (pid and p.get("pid") == pid) for p in procs)
        else:
            await asyncio.sleep(1.0)
    finally:
        await asyncio.sleep(0.3)   # let the last batch arrive
        await close_session(state, sess)
    elapsed = round(time.monotonic() - t0, 1)
    job.report((total - 2) / total, "Checking for new crash reports")

    new_crash, others = None, []
    if before is not None and (launch_error is None):
        if running_end is False:
            await asyncio.sleep(1.5)   # the phone writes the report shortly after the process dies
        with contextlib.suppress(ToolError):
            after = await dcall(state, "crash.list", {"process": exe, "flush": True}, udid=udid, timeout=30) or []
            # new names only, of this process, written since just before the launch
            fresh = [c for c in after if c.get("name") not in before and c.get("process") in (None, exe)
                     and float(c.get("ts") or 0) >= started_wall - 5]
            if fresh:
                with contextlib.suppress(ToolError):
                    new_crash = await crash_detail(state, fresh[0]["name"], udid)
                others = [c["name"] for c in fresh[1:]]

    entries = [e for _, e in sess.ring]
    counts = Counter(str(e.get("level")) for e in entries)
    bad = [e for e in entries if RANK.get(e.get("level"), 2) >= RANK["error"]]
    bad.sort(key=lambda e: -RANK.get(e.get("level"), 2))   # faults, then errors (stable: time order)
    fkey = (lambda e: (e.get("process"), e.get("level"), e.get("message")))
    bad_rows = B.fold_repeats(bad, key=fkey)
    key = bad_rows[:max(5, max_lines * 2 // 3)]
    rest = B.fold_repeats([e for e in entries if RANK.get(e.get("level"), 2) < RANK["error"]], key=fkey)
    key += rest[-(max_lines - len(key)):] if max_lines > len(key) else []
    rendered = [fmt(None, e) + (f" ×{n}" if n > 1 else "") for e, n in key]
    room = B.room(max_bytes, B.RESERVE * 2)
    con_sum, con_lines, con_entries = None, [], []
    if console and launch_error is None:
        # the console shares the bulk room with the log lines (about 45 %, more if the log is short)
        log_need = sum(B.nbytes(r) + 1 for r in rendered)
        con_room = max(room * 45 // 100, room - log_need)
        con_sum, con_lines, con_entries = await capture_summary(state, launch_res, udid, max_lines, con_room)
        room = max(1_000, room - sum(B.nbytes(x) + 1 for x in con_lines))
    page = B.take(rendered, len(rendered), room, from_end=False)
    lines = page.items
    omitted = len(entries) - sum(n for _, n in key[:len(lines)])
    full_path, links = None, []
    if entries or con_entries:
        text = "\n".join(fmt(None, e, 4000) for e in entries)
        if con_entries:
            text += "\n\n--- console (what the app printed) ---\n" + "\n".join(
                fmt_line(None, e, 4000) for e in con_entries)
        path, _, link = B.spill(text, "captures", f"{bid}-{time.strftime('%Y%m%d-%H%M%S')}.log")
        full_path, links = helper.tilde(path), [link]

    name = app.get("name") or bid
    if launch_error:
        summary = f"{name} did not launch: {launch_error}"
    elif new_crash:
        summary = (f"{name} launched (pid {pid}) and CRASHED: {new_crash.exception_type or ''} "
                   f"{new_crash.signal or ''} {new_crash.termination_reason or ''}".strip())
    elif running_end is False:
        summary = f"{name} launched (pid {pid}) but was no longer running after {elapsed} s (no crash report yet)."
    else:
        summary = f"{name} launched (pid {pid}) and was still running after {elapsed} s."
    summary += f" Captured {len(entries)} log lines ({counts.get('fault', 0)} faults, {counts.get('error', 0)} errors)."
    if con_sum is not None and con_sum.session_id:
        summary += (f" It printed {con_sum.captured} lines" +
                    (f" ({con_sum.stderr} to stderr)." if con_sum.stderr else "."))

    nxt = []
    if launch_error:
        nxt.append("Fix the launch problem above (ios_device_prepare if Developer Mode/DDI, unlock the phone if "
                   "locked), then retry.")
    if new_crash:
        nxt.append(f"Map the app frames to source files; more of the report: ios_crash_get(name='{new_crash.name}', "
                   "section='threads').")
    if not entries and launch_error is None:
        nxt.append("No log lines matched. Retry with min_level='info' or 'debug', or a longer seconds.")
    if launch_error is None and not new_crash:
        nxt.append("ios_screenshot shows the screen now; ios_logs_start(bundle_id) follows the log longer.")
    if con_sum is not None and con_sum.session_id:
        nxt.append(f"ios_app_console_read(session_id='{con_sum.session_id}', wait_seconds=10) follows what it "
                   "prints.")
    if r.project_path:
        nxt.append("After fixing code, rebuild and reinstall with ios_run(project_path), then run this again.")
    note = (f"Showing {len(lines)} key lines of {len(entries)} captured; the full capture is {full_path}."
            if omitted > 0 and full_path else None)
    con_cut = con_sum.lines_omitted if con_sum is not None else 0
    if con_cut and full_path:
        note = (note + " " if note else "") + (f"{con_cut} printed lines aren't shown; they're in {full_path} "
                                               "and ios_app_console_read.")
    if new_crash is not None:
        new_crash.next_steps = []
    return RunCaptureResult(bundle_id=bid, launched=launch_error is None, pid=pid, running_at_end=running_end,
                            launch_error=B.clip(launch_error, B.ERROR_CUT) if launch_error else None,
                            installed=installed, seconds=elapsed,
                            log_counts={lv: counts[lv] for lv in LEVELS if counts.get(lv)}, key_lines=lines,
                            lines_omitted=max(0, omitted), dropped=sess.dropped, new_crash=new_crash,
                            other_new_crashes=others[:10], summary=B.clip(summary, 600), next_steps=nxt,
                            truncated=omitted > 0 or con_cut > 0, omitted=max(0, omitted) + con_cut,
                            note=note, full_output_path=full_path, console=con_sum,
                            console_lines=con_lines, preset=how["name"] if how else None,
                            launch_args=[B.clip(x, 300) for x in args or []][:64] if how else [],
                            launch_env={k: B.clip(v, 300) for k, v in (env or {}).items()} if how else {}
                            ).with_budget(max_bytes, links)
