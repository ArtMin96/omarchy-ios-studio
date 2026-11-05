"""LLDB on the user's own apps: a one-shot backtrace (a job) and interactive breakpoint sessions.

Both run `iosstudio-helper debug backtrace|session` (helper/debugger.py), which asks the device
daemon to forward the phone's debugserver over its tunnel and drives the toolchain's lldb. A
session is one helper process per session id; its JSON events are folded into a small record that
ios_debug_session_wait / _command read. Attaching pauses the app, so the backtrace needs confirm=true,
and every session is detached when it ends, when nobody polls it for IDLE_END seconds, or when the
server stops.

Output budget: a backtrace returns the top FOCUS_FRAMES frames of the crashed (else selected)
thread and the other threads grouped by identical stacks; every thread's frames go to a file under
~/.cache/ios-studio/mcp/debug/. A stop returns the top STOP_FRAMES frames and frame 0's locals
(depth 1, values clipped).
"""
from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from .. import budget as B
from .. import helper
from ..jobs import Job
from ..models import ActionResult, ErrorInfo, Out
from ..state import AppState, get_state
from . import MAX_WAIT, READ_REMOTE, ann, tool, wait_with_progress
from .device import BundleId, Udid, dcall, resolve_app

log = logging.getLogger("iosstudio.debug")

FOCUS_FRAMES = 20
STOP_FRAMES = 20
GROUP_FRAMES = 3
MAX_GROUPS = 12
MAX_LOCALS = 30
VALUE_CUT = 200
EXPR_CUT = 500
CONSOLE_KEEP = 400
CONSOLE_SHOW = 8
MAX_SESSIONS = 2
IDLE_END = 600
KEEP_ENDED = 300        # seconds an ended session stays readable before it is dropped


# ------------------------------------------------------------------ models

class DebugFrame(Out):
    index: int
    text: str = Field(description="'module`function at File.swift:42' (app frames first-class)")
    in_app: bool = False
    file: str | None = Field(None, description="Source path relative to the project when it's yours")
    line: int | None = None


class DebugThread(Out):
    index: int
    name: str | None = None
    queue: str | None = None
    reason: str | None = Field(None, description="Why it's stopped: exception (a crash), breakpoint, step, signal, none")
    description: str | None = None
    frames_total: int = 0
    frames: list[DebugFrame] = Field(default_factory=list)


class ThreadGroup(Out):
    threads: list[int] = Field(description="Thread indexes with this identical stack")
    count: int
    names: list[str] = Field(default_factory=list)
    top: list[str] = Field(default_factory=list, description="Top frames of the shared stack")


class DebugBacktrace(B.Budgeted, Out):
    job_id: str | None = Field(None, description="Pass back as job_id to keep waiting while state is running")
    state: str = Field(description="dry_run | running | succeeded | failed")
    message: str | None = None
    bundle_id: str | None = None
    pid: int | None = None
    thread_count: int = 0
    crashed_thread: int | None = None
    focus: DebugThread | None = Field(None, description="The crashed thread, else the selected (main) one")
    other_threads: list[ThreadGroup] = Field(default_factory=list)
    local_build: bool | None = Field(None, description="False: no local build, so your frames have no names")
    symbols_ready: bool | None = None
    error: ErrorInfo | None = None
    next_steps: list[str] = Field(default_factory=list)


class DebugVar(Out):
    name: str
    type: str | None = None
    value: str | None = None
    children: list[str] = Field(default_factory=list, description="'name: type = value', one level deep")
    child_count: int = 0


class DebugStop(Out):
    stop_id: int
    reason: str = Field(description="breakpoint | step | exception | signal | paused | watchpoint")
    description: str | None = None
    location: str | None = Field(None, description="'File.swift:42' of frame 0")
    function: str | None = None
    breakpoint: str | None = None
    thread: int | None = None
    frames: list[DebugFrame] = Field(default_factory=list)
    frames_total: int = 0
    locals: list[DebugVar] = Field(default_factory=list)
    locals_more: bool = False


class DebugSession(B.Budgeted, Out):
    session_id: str
    state: str = Field(description="starting | running | stopped | ended | failed")
    bundle_id: str
    pid: int | None = None
    launched: bool | None = Field(None, description="True: started suspended by the debugger")
    stop: DebugStop | None = None
    new_stop: bool = Field(False, description="stop is newer than what the last call returned")
    breakpoints: list[str] = Field(default_factory=list, description="'#id spec (N locations)'")
    console: list[str] = Field(default_factory=list, description="Newest lldb console lines")
    notices: list[str] = Field(default_factory=list)
    result: dict[str, Any] | None = Field(None, description="frame_variables / evaluate result")
    error: ErrorInfo | None = None
    next_steps: list[str] = Field(default_factory=list)


class DebugSymbols(Out):
    debugger_ready: bool
    debugger_problem: str | None = None
    ios: str | None = None
    build: str | None = None
    symbols_ready: bool = Field(False, description="A usable set is on this computer (also when outdated)")
    state: str | None = Field(None, description="missing | downloading | extracting | ready | outdated | incomplete | failed")
    update_needed: bool = False
    update_reason: str | None = Field(None, description="Why the set needs an update, in plain words")
    how_to_fix: str | None = Field(None, description="What the user presses in iOS Studio")
    can_unpack: bool = Field(False, description="The download is kept, so 'Unpack again' needs no download")
    libs: int | None = Field(None, description="Libraries in the unpacked set")
    last_error: str | None = Field(None, description="Why the last download or unpack failed")
    downloading: bool = False
    progress: float | None = None
    phase: str | None = None
    download_bytes: int | None = Field(None, description="What the download needs (estimated until listed)")
    free_bytes: int | None = None
    size_on_disk: int | None = None
    ipsw: bool | None = None
    next_steps: list[str] = Field(default_factory=list)


# ------------------------------------------------------------------ formatting

def _frame(f: dict, root: str | None) -> DebugFrame:
    fn = f.get("function") or f.get("pc") or "?"
    text = f"{f.get('module') or '?'}`{B.clip(fn, 160)}"
    rel = None
    if f.get("file"):
        rel = B.rel_path(str(f["file"]), root) if f.get("inApp") else os.path.basename(str(f["file"]))
        text += f" at {rel}:{f.get('line')}"
    elif f.get("offset") is not None:
        text += f" + {f.get('offset')}"
    return DebugFrame(index=int(f.get("index") or 0), text=text, in_app=bool(f.get("inApp")), file=rel,
                      line=f.get("line"))


def _thread(t: dict, n: int, root: str | None) -> DebugThread:
    fr = t.get("frames") or []
    return DebugThread(index=int(t.get("index") or 0), name=t.get("name"), queue=t.get("queue"), reason=t.get("reason"),
                       description=B.clip(t.get("description"), 200) if t.get("description") else None,
                       frames_total=int(t.get("numFrames") or len(fr)), frames=[_frame(f, root) for f in fr[:n]])


def _root(data: dict) -> str | None:
    lb = data.get("localBuild")
    if lb and "/xtool/" in str(lb):
        return str(lb).split("/xtool/")[0]
    return None


def _full_text(data: dict, root: str | None) -> str:
    out = [f"Backtrace of {data.get('bundleId')} (pid {data.get('pid')}), {len(data.get('threads') or [])} threads",
           ""]
    for t in data.get("threads") or []:
        head = f"Thread {t.get('index')}"
        bits = [x for x in (t.get("name"), t.get("queue") and f"queue {t['queue']}", t.get("description")) if x]
        out.append(head + (" CRASHED" if t.get("crashed") else "") + (": " + ", ".join(bits) if bits else ""))
        for f in t.get("frames") or []:
            fr = _frame(f, root)
            out.append(f"  #{fr.index} {f.get('pc') or ''} {fr.text}")
        if int(t.get("numFrames") or 0) > len(t.get("frames") or []):
            out.append(f"  … {int(t['numFrames']) - len(t['frames'])} more frames")
        out.append("")
    return "\n".join(out)


def backtrace_summary(job: Job | None, data: dict, state: str, error: dict | None = None) -> DebugBacktrace:
    root = _root(data)
    threads = data.get("threads") or []
    crashed = data.get("crashedThread")
    sel = data.get("selectedThread")
    focus_raw = next((t for t in threads if t.get("index") == crashed), None) or \
        next((t for t in threads if t.get("index") == sel), None) or (threads[0] if threads else None)
    groups: dict[tuple, dict] = {}
    for t in threads:
        if t is focus_raw:
            continue
        key = tuple((f.get("module"), f.get("function")) for f in t.get("frames") or [])
        g = groups.setdefault(key, {"threads": [], "names": [], "top": [_frame(f, root).text for f in
                                                                     (t.get("frames") or [])[:GROUP_FRAMES]]})
        g["threads"].append(int(t.get("index") or 0))
        nm = t.get("name") or t.get("queue")
        if nm and nm not in g["names"] and len(g["names"]) < 3:
            g["names"].append(B.clip(nm, 80))
    ordered = sorted(groups.values(), key=lambda g: -len(g["threads"]))
    others = [ThreadGroup(threads=g["threads"][:20], count=len(g["threads"]), names=g["names"], top=g["top"])
              for g in ordered[:MAX_GROUPS]]
    nxt: list[str] = []
    note = None
    links: list = []
    path = None
    if threads:
        path, size, link = B.spill(_full_text(data, root), "debug",
                                   f"backtrace-{data.get('bundleId') or 'app'}-{time.strftime('%Y%m%d-%H%M%S')}.txt")
        links.append(link)
        if len(ordered) > MAX_GROUPS:
            note = f"{len(ordered) - MAX_GROUPS} more thread groups are only in the file."
        nxt.append(f"Every thread's frames are in {helper.tilde(path)} (resource link attached).")
    if focus_raw and any(f.get("inApp") for f in focus_raw.get("frames") or []):
        own = next(f for f in focus_raw["frames"] if f.get("inApp"))
        nxt.append(f"Your code is at {_frame(own, root).text.split(' at ')[-1]}: read that function in the project.")
    if data.get("localBuild") is None and state == "succeeded":
        nxt.append("No local build was found, so your frames show only addresses: build and run the app with ios_run "
                   "first, then capture again.")
    if state == "succeeded" and not data.get("symbolsReady"):
        nxt.append("The iPhone's debug symbols aren't downloaded (ios_debug_symbols_status); system frames may lack "
                   "names. The user can download them once in iOS Studio › iPhone › Debug.")
    err = ErrorInfo(code=error.get("code"), message=B.clip_error(str(error.get("message") or "Failed."))) if error else None
    if err:
        nxt.append(helper.next_step_for(error) or "Check ios_device_info and retry.")
    return DebugBacktrace(job_id=job.id if job else None, state=state, bundle_id=data.get("bundleId"),
                          pid=data.get("pid"), thread_count=len(threads), crashed_thread=crashed,
                          focus=_thread(focus_raw, FOCUS_FRAMES, root) if focus_raw else None, other_threads=others,
                          local_build=bool(data.get("localBuild")) if threads else None,
                          symbols_ready=data.get("symbolsReady"), error=err, next_steps=nxt, truncated=bool(note),
                          note=note, full_output_path=helper.tilde(path) if path else None).with_budget(None, links)


def _var(v: dict) -> DebugVar:
    kids = []
    for c in (v.get("children") or [])[:12]:
        kids.append(f"{c.get('name')}: {c.get('type') or '?'}" + (f" = {B.clip(c.get('value'), 80)}" if c.get("value") is not None else ""))
    if v.get("more") or int(v.get("childCount") or 0) > len(kids) and kids:
        kids.append(f"… {int(v.get('childCount') or 0) - len(kids)} more")
    return DebugVar(name=str(v.get("name") or ""), type=B.clip(v.get("type"), 120) if v.get("type") else None,
                    value=B.clip(v.get("value"), VALUE_CUT) if v.get("value") is not None else
                    (B.clip(v.get("error"), VALUE_CUT) if v.get("error") else None),
                    children=kids, child_count=int(v.get("childCount") or 0))


def _stop(s: dict, root: str | None) -> DebugStop:
    t = s.get("thread") or {}
    frames = t.get("frames") or []
    f0 = frames[0] if frames else {}
    loc = (s.get("locals") or {}).get("variables") or []
    bp = s.get("breakpoint") or {}
    return DebugStop(stop_id=int(s.get("stopId") or 0), reason=str(s.get("reason") or "other"),
                     description=B.clip(s.get("description"), 200) if s.get("description") else None,
                     location=f"{os.path.basename(str(f0['file']))}:{f0.get('line')}" if f0.get("file") else None,
                     function=B.clip(f0.get("function"), 200) if f0.get("function") else None,
                     breakpoint=bp.get("spec"), thread=t.get("index"),
                     frames=[_frame(f, root) for f in frames[:STOP_FRAMES]], frames_total=int(t.get("numFrames") or len(frames)),
                     locals=[_var(v) for v in loc[:MAX_LOCALS]], locals_more=len(loc) > MAX_LOCALS or bool((s.get("locals") or {}).get("more")))


# ------------------------------------------------------------------ sessions

@dataclass
class Session:
    id: str
    bundle_id: str
    proc: asyncio.subprocess.Process
    state: str = "starting"
    pid: int | None = None
    launched: bool | None = None
    local_build: str | None = None
    stop: dict | None = None
    reported_stop: int = 0
    breakpoints: list = field(default_factory=list)
    console: deque = field(default_factory=lambda: deque(maxlen=CONSOLE_KEEP))
    console_seen: int = 0
    console_total: int = 0
    notices: list = field(default_factory=list)
    results: dict = field(default_factory=dict)     # requestId -> event, only for requests a tool still awaits
    waiting: set = field(default_factory=set)       # requestIds a tool call is waiting for
    ended_at: float | None = None                   # when _prune first saw it ended
    acks: deque = field(default_factory=lambda: deque(maxlen=20))
    error: dict | None = None
    final: dict | None = None
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    last_poll: float = field(default_factory=time.time)
    task: asyncio.Task | None = None
    reaper: asyncio.Task | None = None

    @property
    def ended(self) -> bool:
        return self.state in ("ended", "failed")

    def apply(self, ev: dict) -> None:
        name = ev.get("event")
        if name == "started":
            self.pid, self.launched, self.local_build = ev.get("pid"), ev.get("launched"), ev.get("localBuild")
        elif name == "console":
            for ln in str(ev.get("text") or "").splitlines():
                self.console.append(B.clip(ln, 300))
                self.console_total += 1
        elif name == "notice":
            self.notices.append(B.clip(ev.get("message"), 300))
            self.notices = self.notices[-5:]
        elif name == "breakpoints":
            self.breakpoints = ev.get("items") or []
        elif name == "running":
            self.state = "running"
        elif name == "stopped":
            self.state = "stopped"
            self.stop = ev
        elif name in ("variables", "evaluated", "backtrace") and ev.get("requestId"):
            if str(ev["requestId"]) in self.waiting:    # a late answer to a call that gave up is dropped
                self.results[str(ev["requestId"])] = ev
        elif name == "ack":
            self.acks.append(ev)
            if ev.get("requestId") and str(ev["requestId"]) in self.waiting:
                self.results[str(ev["requestId"])] = ev
        elif name == "exited":
            self.notices.append(f"The app exited (status {ev.get('status')}).")
        elif name == "done":
            self.final = ev
            self.state = "ended" if ev.get("ok") else "failed"
            if not ev.get("ok"):
                self.error = ev.get("error") or {"code": "failed", "message": "The debugger stopped."}
        self.changed.set()

    async def send(self, obj: dict) -> None:
        if self.ended or not self.proc.stdin or self.proc.stdin.is_closing():
            raise ToolError(f"Debug session {self.id} has ended. Start a new one with ios_debug_session_start.")
        self.proc.stdin.write((json.dumps(obj) + "\n").encode())
        try:
            await self.proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            raise ToolError(f"Debug session {self.id} stopped before the command arrived.")

    async def request(self, msg: dict, until, seconds: float) -> dict | None:
        """Send a command with a requestId and wait up to `seconds` for until(); returns the answer (or ack) for
        it, if one came. Answers that arrive after this returns are not kept."""
        rid = str(msg["requestId"])
        self.waiting.add(rid)
        try:
            await self.send(msg)
            await self.wait(until, seconds)
            return self.results.pop(rid, None)
        finally:
            self.waiting.discard(rid)
            self.results.pop(rid, None)

    async def wait(self, pred, seconds: float) -> bool:
        deadline = time.monotonic() + max(0.0, seconds)
        while not pred():
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            self.changed.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.changed.wait(), min(left, 5.0))
        return True


_ids = itertools.count(1)


def _sessions(state: AppState) -> dict[str, Session]:
    return state.extras.setdefault("debug.sessions", {})


def _prune(state: AppState) -> None:
    """Forget sessions that ended (idle detach, a crash, the debugger exiting) more than KEEP_ENDED seconds ago;
    until then ios_debug_session_wait/end can still report how they ended."""
    now = time.time()
    sessions = _sessions(state)
    for sid, s in list(sessions.items()):
        if not s.ended:
            continue
        if s.ended_at is None:
            s.ended_at = now
        elif now - s.ended_at > KEEP_ENDED:
            if s.reaper:
                s.reaper.cancel()
            sessions.pop(sid, None)


async def _pump(s: Session) -> None:
    proc = s.proc
    assert proc.stdout
    try:
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(ev, dict):
                s.apply(ev)
        await proc.wait()
    finally:
        if not s.ended:
            s.state = "failed"
            s.error = s.error or {"code": "helper-exited", "message": f"The debugger stopped (exit {proc.returncode})."}
            s.changed.set()


async def _reap(s: Session) -> None:
    """Detach a session nobody has polled for IDLE_END seconds, so the app never stays paused."""
    while not s.ended:
        await asyncio.sleep(15)
        if time.time() - s.last_poll > IDLE_END and not s.ended:
            log.info("debug session %s idle for %ds: detaching", s.id, IDLE_END)
            await _end(s, "detach")


async def _end(s: Session, how: str = "detach", wait: float = 15) -> None:
    if s.ended:
        return
    with contextlib.suppress(ToolError):
        await s.send({"cmd": how})
    if not await s.wait(lambda: s.ended, wait):
        with contextlib.suppress(ProcessLookupError):
            s.proc.terminate()
        if not await s.wait(lambda: s.ended, 10):
            # lldb and its helpers run in the session's own process group (spawn_stream starts a new session)
            helper.kill_group(s.proc.pid)
            with contextlib.suppress(asyncio.TimeoutError, ProcessLookupError):
                await asyncio.wait_for(s.proc.wait(), 5)
            if not s.ended:
                s.state = "failed"
                s.error = s.error or {"code": "killed", "message": "The debugger didn't detach in time and was killed."}
                s.changed.set()


def _get(state: AppState, session_id: str) -> Session:
    _prune(state)
    s = _sessions(state).get(session_id.strip())
    if s is None:
        known = ", ".join(_sessions(state)) or "none"
        raise ToolError(f"Unknown session_id '{session_id}' (open: {known}). Sessions live only while this MCP server "
                        "runs; start one with ios_debug_session_start.")
    return s


def session_view(s: Session, result: dict | None = None) -> DebugSession:
    s.last_poll = time.time()
    root = s.local_build.split("/xtool/")[0] if s.local_build and "/xtool/" in s.local_build else None
    stop = _stop(s.stop, root) if s.stop and s.state == "stopped" else None
    new = bool(stop and stop.stop_id > s.reported_stop)
    if stop:
        s.reported_stop = stop.stop_id
    fresh = list(s.console)[-min(CONSOLE_SHOW, max(0, s.console_total - s.console_seen)):] \
        if s.console_total > s.console_seen else []
    s.console_seen = s.console_total
    bps = [f"#{b.get('id')} {b.get('spec')} ({b.get('locations', 0)} location{'s' if b.get('locations') != 1 else ''})"
           for b in s.breakpoints[:20]]
    nxt: list[str] = []
    sid = s.id
    if s.state == "stopped":
        nxt.append(f"Paused. ios_debug_session_command(session_id='{sid}', command='next'|'step'|'finish'|'continue'), "
                   "or 'frame_variables' / 'evaluate' to look closer. End with ios_debug_session_end.")
    elif s.state == "running":
        nxt.append(f"The app is running. ios_debug_session_wait(session_id='{sid}', wait_seconds=20) returns at the next "
                   "breakpoint; ask the user to trigger the code path if needed.")
        if any(b.get("locations") == 0 for b in s.breakpoints):
            nxt.append("A breakpoint has 0 locations, so it can't hit: check the file name and line (or the function "
                       "name), and that a local build exists.")
    elif s.state == "starting":
        nxt.append(f"Attaching (can take ~30 s over Wi-Fi). Call ios_debug_session_wait(session_id='{sid}').")
    elif s.state == "failed":
        nxt.append(helper.next_step_for(s.error or {}) or "Check ios_debug_symbols_status and ios_device_info, then retry.")
    else:
        nxt.append("Ended; the app was detached and runs normally.")
    err = ErrorInfo(code=(s.error or {}).get("code"), message=B.clip_error(str((s.error or {}).get("message") or ""))) \
        if s.error else None
    return DebugSession(session_id=sid, state=s.state, bundle_id=s.bundle_id, pid=s.pid, launched=s.launched, stop=stop,
                        new_stop=new, breakpoints=bps, console=fresh, notices=s.notices[-3:], result=result, error=err,
                        next_steps=nxt)


def _clip_result(ev: dict) -> dict:
    if ev.get("event") == "variables":
        vs = ev.get("variables") or []
        return {"frame": ev.get("frame"), "function": B.clip(ev.get("function"), 200),
                "variables": [B.dump(_var(v)) for v in vs[:MAX_LOCALS]], "more": len(vs) > MAX_LOCALS or bool(ev.get("more"))}
    if ev.get("event") == "evaluated":
        r = ev.get("result") or {}
        return {"expr": B.clip(ev.get("expr"), 200), "ok": bool(ev.get("ok")), **B.dump(_var(r))}
    return {"ok": bool(ev.get("ok")), "message": B.clip(ev.get("message"), 300), "code": ev.get("code")}


# ------------------------------------------------------------------ tools

async def on_shutdown(state: AppState) -> None:
    sessions = [s for s in _sessions(state).values() if not s.ended]
    await asyncio.gather(*(_end(s, "detach", 8) for s in sessions), return_exceptions=True)


def register(mcp: MCPServer) -> None:

    @tool(mcp, annotations=ann("Capture a backtrace", read_only=False, idempotent=False, open_world=True))
    async def ios_debug_backtrace(
        ctx: Context,
        bundle_id: Annotated[str | None, Field(description="Your app's bundle id (the XTL-<TEAM>. form also works). "
                                                           "Not needed when job_id is given")] = None,
        confirm: Annotated[bool, Field(description="Attaching PAUSES the app for a few seconds. false = only explain; "
                                                   "true after the user agreed")] = False,
        job_id: Annotated[str | None, Field(description="Keep waiting for a capture started earlier")] = None,
        wait_seconds: Annotated[float, Field(ge=0, le=MAX_WAIT, description="How long to wait for it")] = 20,
        udid: Udid = None,
    ) -> DebugBacktrace:
        """Attach LLDB to one of your own running apps, read every thread's stack, and detach (the app
        pauses for a few seconds, then carries on). Call with confirm=false first: it only explains; ask
        the user, then repeat with confirm=true to capture. Returns the crashed (else main) thread's top 20
        frames with file:line for your code, the other threads grouped by identical stacks, and the
        path of a file with every frame. Runs as a job: if state is running, call again with job_id.
        Use it when the app hangs or misbehaves; for crashes that already happened use ios_crash_get."""
        state = get_state(ctx)
        if job_id:
            job = state.jobs.get(job_id)
        else:
            if not bundle_id:
                raise ToolError("Pass bundle_id (one of your apps, see ios_app_list(scope='own')) or job_id.")
            app = await resolve_app(state, bundle_id, udid)
            if not app.get("developer"):
                raise ToolError(f"{app.get('name')} isn't one of your development apps, and iOS only lets a debugger attach "
                                "to those. Pick one from ios_app_list(scope='own').")
            if not app.get("running"):
                raise ToolError(f"{app.get('name')} isn't running. Launch it first (ios_app_launch), then capture.")
            if not confirm:
                return DebugBacktrace(state="dry_run", bundle_id=app["bundleId"],
                                      message=f"This attaches the debugger to {app.get('name')}, which pauses it for a few "
                                              "seconds (longer over Wi-Fi), reads every thread and detaches. Nothing "
                                              "else changes.",
                                      next_steps=["Ask the user whether the app may pause briefly; only then call again "
                                                  "with confirm=true."])
            flags: dict[str, Any] = {"bundle_id": app["bundleId"]}
            if udid:
                flags["udid"] = udid
            job = await state.jobs.start("debug", "debug", "backtrace", flags, title=f"Backtrace of {app.get('name')}",
                                         prefix="dbg")
        await wait_with_progress(state, job, wait_seconds, ctx)
        if not job.finished:
            return DebugBacktrace(job_id=job.id, state="running", bundle_id=bundle_id,
                                  message=f"{(job.steps.get(job.current_step or '', {}) or {}).get('title') or 'Starting'}…",
                                  next_steps=[f"Call ios_debug_backtrace(job_id='{job.id}', wait_seconds=20) again."])
        if job.state == "succeeded":
            return backtrace_summary(job, job.result or {}, "succeeded")
        return backtrace_summary(job, job.result or {}, "failed", job.error or {"code": "failed", "message": "Failed."})

    @tool(mcp, annotations=ann("Start a debug session", read_only=False, idempotent=False, open_world=True))
    async def ios_debug_session_start(
        ctx: Context,
        bundle_id: BundleId,
        breakpoints: Annotated[list[str] | None, Field(max_length=20, description="'File.swift:42' (a file in the app's "
                                                                          "Sources) or a function name such as "
                                                                          "'ForecastModel.refresh'")] = None,
        launch: Annotated[Literal["auto", "attach", "suspended"],
                          Field(description="attach: the running app. suspended: (re)launch it paused so startup "
                                            "breakpoints hit (stops a running copy). auto: attach if running, else "
                                            "launch suspended")] = "auto",
        wait_seconds: Annotated[float, Field(ge=0, le=MAX_WAIT, description="Wait this long for the first stop")] = 20,
        udid: Udid = None,
    ) -> DebugSession:
        """Attach LLDB to one of your own apps with breakpoints, then let it run. Returns a session_id;
        the result says where it stopped (frames, frame 0's locals) or that it's running. Drive it with
        ios_debug_session_wait / ios_debug_session_command and always finish with ios_debug_session_end
        (the app is detached and keeps running). At most 2 sessions; a session nobody polls for 10
        minutes detaches by itself."""
        state = get_state(ctx)
        _prune(state)
        live = [s for s in _sessions(state).values() if not s.ended]
        if len(live) >= MAX_SESSIONS:
            raise ToolError(f"{len(live)} debug sessions are open ({', '.join(s.id for s in live)}). End one with "
                            "ios_debug_session_end first.")
        app = await resolve_app(state, bundle_id, udid)
        if not app.get("developer"):
            raise ToolError(f"{app.get('name')} isn't one of your development apps; iOS only lets a debugger attach to "
                            "those. Pick one from ios_app_list(scope='own').")
        if any(s.bundle_id == app["bundleId"] for s in live):
            raise ToolError(f"A session for {app['bundleId']} is already open; use it or end it first.")
        bps = [B.clip(b.strip(), 300) for b in (breakpoints or []) if b and b.strip()][:20]
        flags: dict[str, Any] = {"bundle_id": app["bundleId"], "launch": launch, "breakpoints": json.dumps(bps)}
        if udid:
            flags["udid"] = udid
        proc = await helper.spawn_stream("debug", "session", flags)
        s = Session(id=f"dbg-s{next(_ids)}", bundle_id=app["bundleId"], proc=proc)
        _sessions(state)[s.id] = s
        s.task = asyncio.create_task(_pump(s), name=f"debug-{s.id}")
        s.reaper = asyncio.create_task(_reap(s), name=f"debug-reap-{s.id}")
        await s.wait(lambda: s.state in ("stopped", "ended", "failed"), wait_seconds)
        return session_view(s)

    @tool(mcp, annotations=ann("Wait for the debugger", read_only=True, idempotent=False, open_world=True))
    async def ios_debug_session_wait(
        ctx: Context,
        session_id: Annotated[str, Field(description="From ios_debug_session_start")],
        wait_seconds: Annotated[float, Field(ge=0, le=MAX_WAIT, description="Return early at a new stop or the end")] = 20,
    ) -> DebugSession:
        """The session's state: running, or stopped with the reason, file:line, top 20 frames and frame 0's
        locals. Waits up to wait_seconds for a stop newer than the last one returned (or the end)."""
        state = get_state(ctx)
        s = _get(state, session_id)
        seen = s.reported_stop
        await s.wait(lambda: s.ended or (s.state == "stopped" and s.stop and int(s.stop.get("stopId") or 0) > seen),
                     wait_seconds)
        return session_view(s)

    @tool(mcp, annotations=ann("Control the debugger", read_only=False, idempotent=False, open_world=True))
    async def ios_debug_session_command(
        ctx: Context,
        session_id: Annotated[str, Field(description="From ios_debug_session_start")],
        command: Annotated[Literal["continue", "next", "step", "finish", "pause", "frame_variables", "evaluate"],
                           Field(description="continue: run to the next breakpoint. next: step over. step: step "
                                             "into. finish: step out. pause: stop a running app. frame_variables: "
                                             "locals of `frame`. evaluate: a Swift expression in the current frame "
                                             "(it may call code)")],
        expr: Annotated[str | None, Field(max_length=EXPR_CUT, description="For evaluate")] = None,
        frame: Annotated[int | None, Field(ge=0, le=200, description="For frame_variables (default: the current frame)")] = None,
        wait_seconds: Annotated[float, Field(ge=0, le=MAX_WAIT, description="Wait this long for the resulting stop")] = 15,
    ) -> DebugSession:
        """Step, continue or inspect a paused debug session. Run commands (continue/next/step/finish) return
        at the next stop or after wait_seconds; frame_variables and evaluate answer in `result`."""
        state = get_state(ctx)
        s = _get(state, session_id)
        if s.ended:
            return session_view(s)
        rid = f"r{time.monotonic_ns()}"
        if command in ("frame_variables", "evaluate"):
            if s.state != "stopped":
                raise ToolError("The app is running; pause it (command='pause') or wait for a breakpoint first.")
            msg: dict[str, Any] = {"cmd": command, "requestId": rid}
            if command == "evaluate":
                if not expr or not expr.strip():
                    raise ToolError("Pass expr, e.g. expr='days.count'.")
                msg["expr"] = expr.strip()[:EXPR_CUT]
            elif frame is not None:
                msg["frame"] = frame
            ev = await s.request(msg, lambda: rid in s.results or s.ended, min(wait_seconds or 15, MAX_WAIT))
            if ev is None:
                raise ToolError("The debugger didn't answer in time (memory reads are slow over Wi-Fi). Try again.")
            return session_view(s, _clip_result(ev))
        seen = int((s.stop or {}).get("stopId") or 0)
        def refused() -> bool:
            return rid in s.results and not s.results[rid].get("ok")
        ack = await s.request({"cmd": command, "requestId": rid}, lambda: refused() or s.ended or (
            s.state == "stopped" and int((s.stop or {}).get("stopId") or 0) > seen), wait_seconds)
        if ack and not ack.get("ok"):
            raise ToolError(B.clip_error(str(ack.get("message") or "The debugger refused that command.")))
        return session_view(s)

    @tool(mcp, annotations=ann("End a debug session", read_only=False, idempotent=True, open_world=True))
    async def ios_debug_session_end(
        ctx: Context,
        session_id: Annotated[str, Field(description="From ios_debug_session_start")],
        kill: Annotated[bool, Field(description="Quit the app instead of letting it run on")] = False,
    ) -> ActionResult:
        """Detach the debugger (the app keeps running), or quit the app with kill=true. Always call this
        when you're done with a session."""
        state = get_state(ctx)
        s = _get(state, session_id)
        if not s.ended:
            await _end(s, "kill" if kill else "detach")
        data = (s.final or {}).get("data") or {}
        how = data.get("ended") or ("failed" if s.state == "failed" else s.state)
        if s.reaper:
            s.reaper.cancel()
        _sessions(state).pop(s.id, None)
        return ActionResult(done=True, message=f"Session {s.id} ended ({how}).",
                            data={"bundle_id": s.bundle_id, "ended": how, "stops": data.get("stops")},
                            next_steps=[] if how in ("detached", "killed", "exited") else
                            ["The debugger may not have detached cleanly; if the app seems frozen, relaunch it with "
                             "ios_app_launch."])

    @tool(mcp, annotations=READ_REMOTE("Debugger and symbols status"))
    async def ios_debug_symbols_status(ctx: Context, udid: Udid = None) -> DebugSymbols:
        """Whether the debugger (lldb) is installed and the state of this iPhone's debug symbols (the
        Swift runtime and system libraries of its iOS build, a one-time ~7 GB download done by the
        user in iOS Studio › iPhone › Debug). Without them backtraces still work, but Swift values can
        read wrong; an outdated or incomplete set makes attaching slow (minutes over Wi-Fi), and
        how_to_fix names the button that fixes it."""
        state = get_state(ctx)
        chk = await helper.call("debug", "check", timeout=30) or {}
        st = await dcall(state, "debug.symbols", {"action": "status"}, udid=udid, timeout=30) or {}
        prog = st.get("progress") or {}
        nxt = []
        if not chk.get("ready"):
            e = chk.get("error") or {}
            nxt.append(helper.next_step_for(e) or "Run ios_toolchain_check; the debugger comes with the Swift toolchain.")
        state_ = st.get("state") or ("ready" if st.get("ready") else "missing")
        upd = st.get("update") or {}
        unpack = bool(st.get("canUnpack"))
        button = ("Unpack again" if unpack else {"missing": "Download", "failed": "Try again"}.get(state_, "Update"))
        how = None
        if state_ not in ("ready", "downloading", "extracting"):
            how = (f"Ask the user to open iOS Studio › iPhone › Debug and press {button} on the Debug symbols card "
                   "(ios_open_app(page='device'))" + ("; it unpacks the kept download, no download needed."
                                                       if unpack else "; it downloads about "
                                                       f"{(st.get('estimatedBytes') or 0) / 1e9:.1f} GB once, so "
                                                       "that much must be free (download_bytes)."))
        if state_ in ("outdated", "incomplete"):
            nxt.append("Until then backtraces and breakpoints still work, but attaching can take minutes over "
                       "Wi-Fi while the app stays paused.")
        return DebugSymbols(debugger_ready=bool(chk.get("ready")),
                            debugger_problem=B.clip((chk.get("error") or {}).get("message"), 300) if not chk.get("ready") else None,
                            ios=st.get("ios"), build=st.get("build"), symbols_ready=bool(st.get("ready")),
                            state=state_, update_needed=bool(upd.get("needed")),
                            update_reason=B.clip(upd.get("reason"), 300) if upd.get("needed") else None,
                            how_to_fix=how, can_unpack=unpack, libs=st.get("libs"),
                            last_error=B.clip((st.get("error") or {}).get("message"), 300) if st.get("error") else None,
                            downloading=bool(st.get("downloading")), progress=prog.get("fraction"), phase=prog.get("phase"),
                            download_bytes=st.get("estimatedBytes"), free_bytes=st.get("freeBytes"),
                            size_on_disk=st.get("sizeOnDisk"), ipsw=st.get("ipsw"), next_steps=nxt)
