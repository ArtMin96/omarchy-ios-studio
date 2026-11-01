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
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from .. import helper
from ..models import ActionResult, AppInstallResult, LogRead, LogSession, RunCapture
from ..state import AppState, get_state, resolve_project
from . import MAX_WAIT, READ_REMOTE, ann, tool
from .device import Udid, dcall, device_tool_error, install_ipa, new_client, package_for, resolve_app

log = logging.getLogger("iosstudio.logs")

RING = 5000            # entries kept per session
MAX_SESSIONS = 4
IDLE_STOP = 600        # seconds without a read before a session stops itself
MSG_CUT = 400
LEVELS = ["debug", "info", "default", "error", "fault"]
RANK = {lv: i for i, lv in enumerate(LEVELS)}

Level = Literal["debug", "info", "default", "error", "fault"]


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
        await s.client.connect()
        await s.client.request("syslog.start", s._args(), timeout=40)
    except Exception as exc:  # noqa: BLE001
        with contextlib.suppress(Exception):
            await s.client.close()
        if exc.__class__.__name__ == "DeviceError":
            raise device_tool_error(exc) from None
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

    @tool(mcp, annotations=ann("Start iPhone log", read_only=False, destructive=False, idempotent=False,
                              open_world=True))
    async def ios_logs_start(
        ctx: Context,
        bundle_id: Annotated[str | None, Field(description="Only this app's lines (its process, plus lines naming "
                                                           "it), e.g. 'com.example.myapp'")] = None,
        process: Annotated[list[str] | None, Field(description="Exact process names, e.g. ['MyApp', "
                                                               "'SpringBoard']")] = None,
        pid: Annotated[int | None, Field(description="One process id (cheapest: filtered on the phone)")] = None,
        min_level: Annotated[Level | None, Field(description="Minimum level: debug < info < default < error < "
                                                             "fault. 'default' alone is enough as a filter")] = None,
        text: Annotated[str | None, Field(description="Case-insensitive substring of message, process or "
                                                      "subsystem")] = None,
        subsystem: Annotated[str | None, Field(description="os_log subsystem prefix, e.g. 'com.example.myapp'")] = None,
        udid: Udid = None,
    ) -> LogSession:
        """Start capturing the iPhone's live log into a server-side buffer and return a session_id plus
        cursor 0. Needs at least one filter (the phone logs ~2000 lines/s): usually bundle_id, optionally
        min_level. Then call ios_logs_read(session_id, cursor, wait_seconds=10) repeatedly, and
        ios_logs_stop when done. Only lines logged after this call are captured. For "launch my app
        and tell me what happened", ios_app_run_and_capture does all of this in one call."""
        state = get_state(ctx)
        f = build_filters(bundle_id, process, pid, min_level, text, subsystem)
        s = await open_session(state, f, udid)
        return LogSession(session_id=s.id, running=True, filters=f, cursor=0,
                          next_steps=[f"ios_logs_read(session_id='{s.id}', cursor=0, wait_seconds=10); reproduce "
                                      "the behaviour on the phone meanwhile (ios_app_launch).",
                                      f"ios_logs_stop(session_id='{s.id}') when done."])

    @tool(mcp, annotations=READ_REMOTE("Read iPhone log"))
    async def ios_logs_read(
        ctx: Context,
        session_id: Annotated[str, Field(description="From ios_logs_start, e.g. 'L1'")],
        cursor: Annotated[int | None, Field(ge=0, description="next_cursor from the previous read (0 = from the "
                                                              "start); omit to continue where the last read "
                                                              "ended")] = None,
        limit: Annotated[int, Field(ge=1, le=500, description="Max lines to return")] = 100,
        wait_seconds: Annotated[int, Field(ge=0, le=MAX_WAIT, description="Long-poll: if nothing new is "
                                                                         "buffered, wait up to this long")] = 0,
        min_level: Annotated[Level | None, Field(description="Extra filter for this read only")] = None,
        text: Annotated[str | None, Field(description="Extra case-insensitive substring filter for this read")] = None,
    ) -> LogRead:
        """Read buffered log lines after `cursor`, oldest first, one compact line each:
        '<seq> <time> <level> <process>[<pid>] <subsystem>: <message>' (messages cut at 400 chars).
        Returns next_cursor to pass next time, has_more when more is already buffered, `missed` if the
        5000-entry ring buffer overwrote lines you hadn't read, and `dropped` if the phone outpaced the
        stream. Use wait_seconds (≤25) to wait for new lines. Read-only filters (min_level/text) narrow
        this read without changing the session."""
        state = get_state(ctx)
        s = _get(state, session_id)
        s.last_read = time.time()
        cur = s.read_cursor if cursor is None else cursor
        if cur > s.seq:
            cur = s.seq
        if wait_seconds and cur >= s.seq and s.running:
            s.changed.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(s.changed.wait(), wait_seconds)
        missed = max(0, s.buffer_start - cur)
        cur = max(cur, s.buffer_start)
        lo = RANK.get(min_level or "debug", 0)
        tl = text.lower() if text else None
        lines: list[str] = []
        snap = list(s.ring)
        end_seq = snap[-1][0] + 1 if snap else s.seq
        nxt = cur
        for seq, e in snap:
            if seq < cur:
                continue
            if len(lines) >= limit:
                break
            nxt = seq + 1
            if RANK.get(e.get("level"), 2) < lo:
                continue
            if tl and tl not in str(e.get("message", "")).lower() and tl not in str(e.get("process", "")).lower():
                continue
            lines.append(fmt(seq, e))
        if len(lines) < limit:
            nxt = max(nxt, end_seq)
        s.read_cursor = nxt
        more = nxt < s.seq
        steps = []
        if more:
            steps.append(f"More is buffered: read again with cursor={nxt}.")
        elif s.running:
            steps.append(f"Caught up. Call again with cursor={nxt} and wait_seconds=10 for new lines.")
        if not s.running:
            steps.append("The session has stopped" + (f": {s.error}" if s.error else "") +
                         ". Start a new one with ios_logs_start.")
        if missed:
            steps.append(f"{missed} lines were overwritten before you read them; read more often or narrow the filter.")
        if not lines and s.seq == 0 and s.running:
            steps.append("Nothing captured yet. Is the app running? Filters: " + _filter_desc(s.filters))
        return LogRead(session_id=s.id, running=s.running, lines=lines, count=len(lines), next_cursor=nxt,
                       has_more=more, buffer_start=s.buffer_start, missed=missed, dropped=s.dropped,
                       total_received=s.seq, error=s.error, next_steps=steps)

    @tool(mcp, annotations=ann("Stop iPhone log", read_only=False, destructive=False, idempotent=True,
                              open_world=True))
    async def ios_logs_stop(
        ctx: Context,
        session_id: Annotated[str | None, Field(description="Session to stop; omit to stop all of yours")] = None,
    ) -> ActionResult:
        """Stop a log session and free its buffer (unread lines are discarded). Omit session_id to stop
        every session this server started."""
        state = get_state(ctx)
        targets = [_get(state, session_id)] if session_id else [s for s in _sessions(state).values()
                                                                  if not s.internal]
        for s in targets:
            await close_session(state, s)
        ids = ", ".join(s.id for s in targets)
        return ActionResult(done=True, message=f"Stopped {ids}." if targets else "No log sessions were running.")

    # -------------------------------------------------------------- workflow

    @tool(mcp, annotations=ann("Run app and capture", read_only=False, destructive=False, idempotent=False,
                              open_world=True))
    async def ios_app_run_and_capture(
        ctx: Context,
        bundle_id: Annotated[str | None, Field(description="App to launch, e.g. 'com.example.myapp' (installed "
                                                           "XTL-<TEAM>. form also works)")] = None,
        project_path: Annotated[str | None, Field(description="Instead of bundle_id: a project name or path; its "
                                                              "bundle id is used")] = None,
        install: Annotated[bool, Field(description="First install the project's last signed build (or ipa_path). "
                                                   "Does not build: use ios_run to rebuild")] = False,
        ipa_path: Annotated[str | None, Field(description="A signed .ipa/.app to install first")] = None,
        seconds: Annotated[int, Field(ge=1, le=MAX_WAIT, description="How long to capture after launch")] = 8,
        min_level: Annotated[Level, Field(description="Lowest log level to capture")] = "default",
        relaunch: Annotated[bool, Field(description="Stop the app first if it's running, so you see a fresh "
                                                    "start")] = True,
        args: Annotated[list[str] | None, Field(description="Launch arguments")] = None,
        env: Annotated[dict[str, str] | None, Field(description="Launch environment variables")] = None,
        max_lines: Annotated[int, Field(ge=5, le=200, description="Max log lines in the summary")] = 40,
        udid: Udid = None,
    ) -> RunCapture:
        """"Launch it and tell me what happened" in one call: (optionally installs a signed build,) starts
        a log capture filtered to the app, launches it, records for `seconds`, checks whether it is still
        running and whether a NEW crash report appeared, then stops and summarizes: launched?, pid,
        log counts per level, key lines (faults and errors first, then the latest), the new crash's
        exception, reason and top frames, and next steps. Takes about seconds + 5 s.
        Use after ios_run, or to reproduce a crash. For longer observation use ios_logs_start/read."""
        state = get_state(ctx)
        installed = None
        proj = None
        if project_path and not bundle_id:
            proj = await resolve_project(state, project_path)
            bundle_id = proj.get("bundleId")
            if not bundle_id:
                raise ToolError(f"{proj.get('name')} has no bundle id yet. Check ios_project_info.")
        if not bundle_id:
            raise ToolError("Pass bundle_id or project_path.")
        total = seconds + 6.0
        step = [0.0]

        async def progress(done: float, msg: str) -> None:
            if done > step[0]:
                step[0] = done
                await ctx.report_progress(round(done, 1), total, message=msg)

        if install or ipa_path:
            pkg, _ = await package_for(state, ipa_path, None if ipa_path else (project_path or None))
            await progress(0.1, "Installing")
            res = await install_ipa(state, udid, pkg)
            installed = AppInstallResult(installed=True, bundle_id=res.get("bundleId"),
                                         package_bundle_id=res.get("packageBundleId"), path=helper.tilde(pkg),
                                         seconds=res.get("seconds"))
            if res.get("bundleId"):
                bundle_id = res["bundleId"]
        app = await resolve_app(state, bundle_id, udid)
        bid, exe = app["bundleId"], app.get("executable") or app.get("name")
        await progress(0.5, "Checking crash reports")
        try:
            before = {c.get("name") for c in await dcall(state, "crash.list", {"process": exe, "flush": True},
                                                         udid=udid, timeout=30) or []}
        except ToolError:
            before = None
        sess = await open_session(state, {"bundleId": bid, "minLevel": min_level}, udid, internal=True)
        launch_error, pid, running_end = None, None, None
        started_wall = time.time()
        t0 = time.monotonic()
        try:
            if relaunch and app.get("running"):
                with contextlib.suppress(ToolError):
                    await dcall(state, "app.kill", {"bundleId": bid}, udid=udid, timeout=20)
                await asyncio.sleep(0.5)
            await progress(1.5, "Launching")
            try:
                a: dict = {"bundleId": bid}
                if args:
                    a["args"] = args
                if env:
                    a["env"] = env
                res = await dcall(state, "app.launch", a, udid=udid, timeout=60) or {}
                pid = res.get("pid")
            except ToolError as exc:
                launch_error = str(exc)
            t0 = time.monotonic()
            if launch_error is None:
                end = t0 + seconds
                while (now := time.monotonic()) < end:
                    await asyncio.sleep(min(1.0, end - now))
                    await progress(1.5 + (time.monotonic() - t0), f"Capturing ({len(sess.ring)} lines)")
                with contextlib.suppress(ToolError):
                    procs = await dcall(state, "processes.list", {"appsOnly": True}, udid=udid, timeout=20) or []
                    running_end = any(p.get("bundleId") == bid or (pid and p.get("pid") == pid) for p in procs)
            else:
                await asyncio.sleep(1.0)
        finally:
            await asyncio.sleep(0.3)   # let the last batch arrive
            await close_session(state, sess)
        elapsed = round(time.monotonic() - t0, 1)
        await progress(total - 2, "Checking for new crash reports")

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
                    from .crashes import crash_detail
                    with contextlib.suppress(ToolError):
                        new_crash = await crash_detail(state, fresh[0]["name"], udid)
                    others = [c["name"] for c in fresh[1:]]

        entries = [e for _, e in sess.ring]
        counts = Counter(str(e.get("level")) for e in entries)
        bad = [e for e in entries if RANK.get(e.get("level"), 2) >= RANK["error"]]
        bad.sort(key=lambda e: -RANK.get(e.get("level"), 2))   # faults, then errors (stable: time order)
        key = bad[:max(5, max_lines * 2 // 3)]
        rest = [e for e in entries if RANK.get(e.get("level"), 2) < RANK["error"]]
        key += rest[-(max_lines - len(key)):] if max_lines > len(key) else []
        lines = [fmt(None, e) for e in key]
        omitted = len(entries) - len(key)

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

        nxt = []
        if launch_error:
            nxt.append("Fix the launch problem above (ios_device_prepare if Developer Mode/DDI, unlock the phone if "
                       "locked), then retry.")
        if new_crash:
            nxt.append(f"Map the app frames to source files; full report: ios_crash_get(name='{new_crash.name}', "
                       "full_text=true).")
        if not entries and launch_error is None:
            nxt.append("No log lines matched. Retry with min_level='info' or 'debug', or a longer seconds.")
        if launch_error is None and not new_crash:
            nxt.append("ios_screenshot shows the screen now; ios_logs_start(bundle_id) follows the log longer.")
        if proj or project_path:
            nxt.append("After fixing code, rebuild and reinstall with ios_run(project_path), then run this again.")
        return RunCapture(bundle_id=bid, launched=launch_error is None, pid=pid, running_at_end=running_end,
                          launch_error=launch_error, installed=installed, seconds=elapsed,
                          log_counts={lv: counts[lv] for lv in LEVELS if counts.get(lv)}, key_lines=lines,
                          lines_omitted=max(0, omitted), dropped=sess.dropped, new_crash=new_crash,
                          other_new_crashes=others, summary=summary, next_steps=nxt)
