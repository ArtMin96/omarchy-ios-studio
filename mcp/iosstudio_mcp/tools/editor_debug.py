"""Debug in editor: breakpoints in VS Code or Zed through their own lldb-dap (docs/features/editor-debugging.md).

    ios_debug_editor_config   `debug editor-config`: the editor's debug configuration (dry run until confirm)
    ios_debug_editor_start    `debug editor`: the bridge as a job; returns once it is ready (port, pid, config name)
    ios_debug_editor_stop     ends the bridge ({"cancel": true} on its stdin, then SIGTERM)
    ios_debug_editor_status   `debug editor-status`: running bridges, also those started from the app window

The bridge is a long-lived helper stream (helper/iosstudio/features/editor_debug_bridge.py) that holds the
phone's debugserver forward open for up to two hours. It runs as a coroutine job here (kind KIND) that reads
the stream itself, because its `ready`, `editor` and `forward` events are its state: a second call with
job_id reports the latest of them. Cancelling the job (ios_debug_editor_stop, ios_job_cancel, server exit)
asks the helper to stop, so it releases the forward and kills an app it launched suspended that no editor
ever attached to.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import time
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from .. import helper
from ..backends.device import resolve_app
from ..backends.projects import resolve_project
from ..core import budget as B
from ..core import deadline
from ..core.errors import next_step
from ..core.jobs import Job, read_lines
from ..core.params import OptJobId, ProjectPath, Udid, WaitSeconds
from ..core.registry import MAX_WAIT, tool
from ..models import ActionResult, ErrorInfo, Out
from ..state import AppState, get_state

log = logging.getLogger("iosstudio.editor_debug")

KIND = "debug-editor"
TOOL = "ios_debug_editor_start"
PREVIEW_CUT = 1500
NOTE_CUT = 300
MAX_NOTICES = 6
STDERR_KEEP = 20
STOP_GRACE = 6.0        # seconds the helper gets to release the forward after {"cancel": true}
TERM_GRACE = 3.0        # then after SIGTERM

EditorId = Literal["vscode", "zed", "neovim"]

HINTS = {
    "busy": "Another debugger is attached to this app (iOS Studio's iPhone › Debug, ios_debug_session_start or a "
            "Debug in editor bridge from the app window; ios_debug_editor_status lists bridges). End it first "
            "(ios_debug_session_end or ios_debug_editor_stop), then retry.",
    "not-installed": "Build and install the app first with ios_run(project_path), then retry.",
    "not-running": "Launch the app first (ios_app_launch), or use launch='auto' or 'suspended'.",
    "not-debuggable": "Only your own development-signed apps can be debugged: pick one from "
                      "ios_app_list(scope='own').",
    "needs-lldb": "Editors debug with the Swift toolchain's lldb-dap: call ios_toolchain_check, then "
                  "ios_toolchain_install for a newer Swift.",
    "no-bundle-id": "The project has no bundle ID yet: build it once with ios_run(project_path).",
    "write-failed": "Check that the project folder and ~/.local/state are writable, then retry.",
}


# ------------------------------------------------------------------ models

class EditorPlan(Out):
    id: str = Field(description="vscode | zed")
    name: str
    path: str = Field(description="The editor's debug config file in the project")
    state: str = Field(description="missing | present (current, nothing to do) | outdated (our entry is replaced)")
    action: str = Field(description="write (new file) | merge (adds our entry; other configs kept) | skip")
    detail: str
    preview: str | None = Field(None, description="The new file, or a unified diff for a merge (clipped)")
    done: bool = False


class EditorDebugConfig(B.Budgeted, Out):
    dry_run: bool
    applied: bool = False
    message: str
    project: str | None = None
    bundle_id: str | None = None
    config_name: str | None = Field(None, description="The configuration the user starts in the editor")
    attach_file: str | None = Field(None, description="lldb commands the configuration runs; iOS Studio rewrites it")
    editors: list[EditorPlan] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list, description="Steps for the user, e.g. Zed's lldb-dap path")
    next_steps: list[str] = Field(default_factory=list)


class EditorBridge(B.Budgeted, Out):
    job_id: str | None = Field(None, description="Pass back as job_id for the latest state, or to stop it")
    state: str = Field(description="running (starting) | ready (the editor can attach) | ended | failed")
    message: str | None = None
    editor: str | None = Field(None, description="waiting | attached | detached: the editor's connection")
    bundle_id: str | None = Field(None, description="As installed on the iPhone")
    app_name: str | None = None
    config_name: str | None = Field(None, description="The configuration to start in the editor")
    editors: list[str] = Field(default_factory=list, description="Editors whose config is in the project")
    port: int | None = Field(None, description="Loopback port of the debugserver forward")
    pid: int | None = None
    launched: bool | None = Field(None, description="True: started suspended; it waits for the editor")
    symbols_ready: bool | None = None
    local_build: bool | None = Field(None, description="False: no local build, breakpoints in your code won't resolve")
    attach_file: str | None = None
    forward_moved: int = Field(0, description="Times the forward came back on a new port (the file has it)")
    ended: str | None = Field(None, description="cancelled | app-exited | timeout")
    attached: bool | None = Field(None, description="An editor attached at some point")
    killed: bool | None = None
    notices: list[str] = Field(default_factory=list)
    error: ErrorInfo | None = None
    next_steps: list[str] = Field(default_factory=list)


class BridgeInfo(Out):
    bundle_id: str
    installed_bundle_id: str | None = None
    app_name: str | None = None
    project: str | None = None
    port: int | None = None
    pid: int | None = None
    launched: bool | None = None
    editor: str | None = Field(None, description="waiting | attached | detached")
    since: str | None = None
    job_id: str | None = Field(None, description="Set when this MCP server started it")
    started_by: str = Field(description="mcp (this server) | app (the iOS Studio window or another client)")


class EditorBridges(B.Budgeted, Out):
    bridges: list[BridgeInfo] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)


# ------------------------------------------------------------------ the bridge job

@dataclass
class Bridge:
    """What the stream told so far; kept in job.data['bridge']."""
    key: str
    proc: asyncio.subprocess.Process | None = None
    ready: dict | None = None
    editor: str = "waiting"
    attached: bool = False
    moved: int = 0
    error: dict | None = None
    done: dict | None = None
    ended: bool = False
    seq: int = 0
    stderr: list = field(default_factory=list)
    changed: asyncio.Event = field(default_factory=asyncio.Event)

    def bump(self) -> None:
        self.seq += 1
        self.changed.set()

    async def wait(self, pred, seconds: float) -> bool:
        end = time.monotonic() + deadline.wait_time(seconds)
        while not pred():
            left = end - time.monotonic()
            if left <= 0:
                return False
            self.changed.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.changed.wait(), min(left, 5.0))
        return True


def _bridge(job: Job) -> Bridge:
    return job.data["bridge"]


def _apply(job: Job, b: Bridge, ev: dict) -> None:
    name = ev.get("event")
    if name == "step":
        err = ev.get("error")
        job.set_step(str(ev.get("id")), ev.get("title"), str(ev.get("state") or ""),
                     detail=ev.get("detail"), error=err.get("message") if isinstance(err, dict) else None)
        job.report(detail=str(ev.get("title") or ev.get("id") or ""))
    elif name == "notice":
        job.notices.append({k: ev.get(k) for k in ("id", "level", "message") if ev.get(k) is not None})
        job.notices = job.notices[-20:]
    elif name == "ready":
        b.ready = {k: v for k, v in ev.items() if k != "event"}
        job.report(detail=f"Ready on port {ev.get('port')}")
    elif name == "editor":
        b.editor = str(ev.get("state") or b.editor)
        b.attached = b.attached or b.editor == "attached"
        job.report(detail=f"Editor {b.editor}")
    elif name == "forward" and b.ready is not None:
        b.ready.update(port=ev.get("port"), host=ev.get("host") or b.ready.get("host"))
        b.moved += 1
    elif name == "done":
        b.done = ev
        if not ev.get("ok"):
            e = ev.get("error")
            b.error = e if isinstance(e, dict) else {"code": "failed", "message": str(e or "The bridge failed.")}
    b.bump()


async def _drain(b: Bridge, stream: asyncio.StreamReader) -> None:
    async for raw in read_lines(stream):
        text = raw.decode(errors="replace").rstrip()
        if text:
            b.stderr = (b.stderr + [B.clip(text, 300)])[-STDERR_KEEP:]
            log.debug("debug editor stderr: %s", text)


async def _stop_proc(b: Bridge, grace: float = STOP_GRACE) -> None:
    """{"cancel": true} (the helper releases the forward and ends with `done`), then SIGTERM, then SIGKILL."""
    proc = b.proc
    if proc is None or proc.returncode is not None:
        return
    with contextlib.suppress(Exception):
        if proc.stdin and not proc.stdin.is_closing():
            proc.stdin.write(b'{"cancel": true}\n')
            await proc.stdin.drain()
    for sig, wait in ((None, grace), (signal.SIGTERM, TERM_GRACE)):
        if sig is not None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(proc.pid, sig)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(proc.wait()), wait)
            return
    helper.kill_group(proc.pid)


async def _run(job: Job, flags: dict) -> dict:
    b = _bridge(job)
    b.proc = proc = await helper.spawn_stream("debug", "editor", flags)
    assert proc.stdout and proc.stderr
    err_task = asyncio.create_task(_drain(b, proc.stderr))
    try:
        async for raw in read_lines(proc.stdout):
            with contextlib.suppress(ValueError):
                ev = json.loads(raw)
                if isinstance(ev, dict):
                    _apply(job, b, ev)
        await proc.wait()
    except asyncio.CancelledError:          # the server is stopping (or the job was cancelled hard)
        await asyncio.shield(_stop_proc(b))
        raise
    finally:
        with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(err_task, 2)
        if proc.returncode is None:
            helper.kill_group(proc.pid)
        b.ended = True
        b.bump()
    if b.done is None:
        raise ToolError(f"The Debug in editor helper stopped without a result (exit {proc.returncode}).")
    if b.error:
        raise helper.HelperFailure(b.error)
    data = b.done.get("data")
    return data if isinstance(data, dict) else {}


async def _on_cancel(job: Job) -> None:
    await _stop_proc(_bridge(job))


def _live(state: AppState) -> list[Job]:
    return [j for j in state.jobs.running(KIND) if "bridge" in j.data]


def _matches(job: Job, bundle_id: str) -> bool:
    b = _bridge(job)
    want = bundle_id.strip()
    ids = {b.key, str((b.ready or {}).get("bundleId") or "")}
    return any(i and (i == want or i.endswith("." + want) or want.endswith("." + i)) for i in ids)


# ------------------------------------------------------------------ views

def _error_steps(err: dict) -> list[str]:
    return [next_step(str(err.get("code") or ""), err.get("fix"), HINTS) or helper.next_step_for(err)
            or "Check ios_status, then retry."]


def bridge_view(job: Job, message: str | None = None) -> EditorBridge:
    b = _bridge(job)
    r = b.ready or {}
    notices = [B.clip(n.get("message"), NOTE_CUT) for n in job.notices if n.get("message")][-MAX_NOTICES:]
    common: dict[str, Any] = dict(
        job_id=job.id, bundle_id=r.get("bundleId"), app_name=r.get("appName"), config_name=r.get("configName"),
        editors=[str(e) for e in (r.get("editors") or [])][:4], port=r.get("port"), pid=r.get("pid"),
        launched=r.get("launched"), symbols_ready=r.get("symbolsReady"),
        local_build=bool(r.get("localBuild")) if b.ready else None, attach_file=helper.tilde(r.get("scriptPath")),
        forward_moved=b.moved, notices=notices)
    stop = f"ios_debug_editor_stop(job_id='{job.id}')"
    nxt: list[str] = []
    if job.finished or b.ended:
        if b.error or job.state == "failed":
            err = b.error or job.error or {"code": "failed", "message": "The bridge failed."}
            if b.stderr and not b.error:
                err = {**err, "message": f"{err.get('message')} ({b.stderr[-1]})"}
            return EditorBridge(state="failed", message=message, error=ErrorInfo(
                code=err.get("code"), message=B.clip_error(str(err.get("message") or "Failed."))),
                next_steps=_error_steps(err), **common).with_budget(None)
        d = (b.done or {}).get("data") or job.result or {}
        how = d.get("ended") or "cancelled"
        if how == "app-exited":
            nxt.append("The app exited, so the bridge ended. Start it again with ios_debug_editor_start when needed.")
        elif how == "timeout":
            nxt.append("The bridge ended after its two-hour limit. Start it again with ios_debug_editor_start.")
        else:
            nxt.append("Ended: the forward is released and pressing F5 now says to start Debug in editor first.")
        if d.get("killed"):
            nxt.append("The app was started suspended and no editor attached, so it was ended too.")
        return EditorBridge(state="ended", message=message or f"Debug in editor ended ({how}).", ended=how,
                            attached=bool(d.get("attached", b.attached)), killed=d.get("killed"),
                            editor=b.editor if b.ready else None, next_steps=nxt, **common).with_budget(None)
    if not b.ready:
        step = job.steps.get(job.current_step or "") or {}
        return EditorBridge(state="running", message=message or f"{step.get('title') or 'Starting'}…",
                            next_steps=[f"Call {TOOL}(job_id='{job.id}') again; it answers once the bridge is ready. "
                                        f"{stop} cancels it."], **common).with_budget(None)
    cfg = r.get("configName") or "the iOS Studio attach configuration"
    if b.editor == "attached":
        nxt.append("The editor is attached: breakpoints set there stop the app, and the editor shows stack, "
                   "variables and stepping. You can't see or drive that session from here.")
    else:
        if b.editor == "detached":
            nxt.append("The editor detached; the app runs on. Pressing F5 again re-attaches while the bridge runs.")
        how = ("VS Code: Run and Debug (F5) with “{c}” selected; Zed: `debugger: start` and pick “{c}”"
               .format(c=cfg))
        if not r.get("editors"):
            how = (f"No editor config was written (ios_debug_editor_config writes it; Neovim: the nvim-dap snippet "
                   f"in the Debug in editor guide). Then start “{cfg}”")
        nxt.append(f"Tell the user to start the debugger in their editor now: {how}. Attaching pauses the app "
                   "until the debugger has read it (seconds over USB, longer over Wi-Fi).")
        if r.get("launched"):
            nxt.append("The app was started suspended and waits for the editor; it runs once the editor attaches.")
    if not r.get("localBuild"):
        nxt.append("No local build was found, so breakpoints in the app's code won't resolve: ios_run(project_path) "
                   "first, then start again.")
    nxt.append(f"{TOOL}(job_id='{job.id}', wait_seconds=20) reports attached/detached. When the user is done, "
               f"{stop} ends it (and the editor's session with it).")
    return EditorBridge(state="ready", message=message or f"Ready: the editor can attach to {r.get('appName')}.",
                        editor=b.editor, attached=b.attached, next_steps=nxt, **common).with_budget(None)


# ------------------------------------------------------------------ tools

def register(mcp: MCPServer) -> None:

    @tool(mcp, "Write editor debug config", kind="write", idempotent=True)
    async def ios_debug_editor_config(
        ctx: Context,
        project_path: ProjectPath,
        editor: Annotated[EditorId | None, Field(description="Default: the one from Settings")] = None,
        confirm: Annotated[bool, Field(description="false = dry run; true only after the user agreed")] = False,
    ) -> EditorDebugConfig:
        """Add the attach configuration to .vscode/launch.json or .zed/debug.json, for breakpoints on the
        iPhone. A dry run with a preview until confirm=true after the user agrees. Other configurations in the
        file are kept."""
        state = get_state(ctx)
        p = await resolve_project(state, project_path)
        d = await helper.call("debug", "editor-config", {"project": p["path"], "editor": editor,
                                                         "dry_run": not confirm}) or {}
        plans = []
        for e in (d.get("editors") or [])[:4]:
            a = e.get("action") or {}
            plans.append(EditorPlan(id=str(e.get("id")), name=str(e.get("name") or e.get("id")),
                                    path=helper.tilde(str(e.get("path") or a.get("path") or "")) or "",
                                    state=str(e.get("state") or "missing"), action=str(a.get("kind") or "skip"),
                                    detail=B.clip(a.get("detail"), NOTE_CUT),
                                    preview=B.clip(a["preview"], PREVIEW_CUT) if a.get("preview") else None,
                                    done=bool(a.get("done"))))
        notes = [B.clip(n, NOTE_CUT) for n in (d.get("notes") or [])][:6]
        changes = [x for x in plans if x.action in ("write", "merge")]
        cfg = d.get("configName")
        nxt: list[str] = []
        if not plans:
            msg = "No editor to configure."
            nxt.append("Pass editor='vscode' or 'zed'. Neovim isn't changed: the user adds the nvim-dap snippet "
                       "from the Debug in editor guide.")
        elif not confirm and changes:
            msg = (f"Dry run: {len(changes)} file(s) would change (only the debug config; every other configuration "
                   "and comment in it is kept). Nothing was written.")
            nxt.append("Show the user the plan, ask, and only then call again with confirm=true.")
        elif confirm:
            msg = ("Written: " + ", ".join(f"{x.name} ({x.path})" for x in plans if x.done) + "."
                   if any(x.done for x in plans) else "Nothing to change: the configuration is already current.")
        else:
            msg = "Nothing to change: the configuration is already current."
        for n in notes:
            nxt.append(f"For the user: {n}")
        if confirm or not changes:
            nxt.append(f"Next: ios_debug_editor_start(project_path) starts the bridge; then the user starts “{cfg}” "
                       "in the editor.")
        return EditorDebugConfig(dry_run=not confirm, applied=bool(d.get("applied")), message=msg,
                                 project=helper.tilde(str(d.get("project") or p["path"])), bundle_id=d.get("bundleId"),
                                 config_name=cfg, attach_file=helper.tilde(d.get("scriptPath")), editors=plans,
                                 notes=notes, next_steps=nxt[:8]).with_budget(None)

    @tool(mcp, "Start Debug in editor", kind="write", open_world=True)
    async def ios_debug_editor_start(
        ctx: Context,
        project_path: Annotated[str | None, Field(description="Project folder or name")] = None,
        bundle_id: Annotated[str | None, Field(description="Or an installed app")] = None,
        launch: Annotated[Literal["auto", "attach", "suspended"],
                          Field(description="suspended: relaunch paused; auto: attach if running")] = "auto",
        editor: EditorId | None = None,
        write_config: bool = True,
        udid: Udid = None,
        job_id: OptJobId = None,
        wait_seconds: WaitSeconds = 0,
    ) -> EditorBridge:
        """Let VS Code or Zed debug your app on the iPhone with breakpoints: forwards the debugger and writes
        the editor's config; then the user presses F5. A job until ios_debug_editor_stop (2 h max): returns
        when ready, else state='running'. Call again with job_id for attached/detached."""
        state = get_state(ctx)
        if job_id:
            job = state.jobs.get(job_id, KIND)
            b = _bridge(job)
            seen = b.seq
            if b.ready and not b.ended:
                await b.wait(lambda: b.ended or b.seq != seen, min(wait_seconds, MAX_WAIT))
            else:
                await b.wait(lambda: b.ended or b.ready is not None, MAX_WAIT)
            await _settle(job)
            return bridge_view(job)
        flags: dict[str, Any] = {"launch": launch, "udid": udid, "editor": editor, "no_config": not write_config}
        if project_path:
            p = await resolve_project(state, project_path)
            flags["project"] = key = str(p["path"])
            title = f"Debug {p.get('name') or os.path.basename(key)} in the editor"
        elif bundle_id:
            app = await resolve_app(state, bundle_id, udid)
            if not app.get("developer"):
                raise ToolError(f"{app.get('name')} isn't one of your development apps; iOS only lets a debugger "
                                "attach to those. Pick one from ios_app_list(scope='own').")
            flags["bundle_id"] = key = str(app["bundleId"])
            title = f"Debug {app.get('name') or key} in the editor"
        else:
            raise ToolError("Pass project_path (or bundle_id of an installed app of yours). ios_project_list lists "
                            "projects.")
        same = next((j for j in _live(state) if _bridge(j).key == key or _matches(j, key)), None)
        if same:
            return bridge_view(same, message="Debug in editor already runs for this app; this is its state.")
        if _live(state):
            log.info("another editor bridge runs (%s); starting one more", ", ".join(j.id for j in _live(state)))
        b = Bridge(key=key)
        job = state.jobs.run(lambda j: _run(j, flags), KIND, title=title, prefix="dbe", tool=TOOL,
                             project_path=flags.get("project"), data={"bridge": b}, on_cancel=_on_cancel)
        await b.wait(lambda: b.ended or b.ready is not None, MAX_WAIT)
        await _settle(job)
        return bridge_view(job)

    @tool(mcp, "Stop Debug in editor", kind="write", idempotent=True, open_world=True)
    async def ios_debug_editor_stop(
        ctx: Context,
        bundle_id: Annotated[str | None, Field(description="Default: the only bridge")] = None,
        job_id: str | None = None,
    ) -> ActionResult:
        """End a Debug in editor bridge (also one started in the app window); an attached editor's session
        ends."""
        state = get_state(ctx)
        if job_id:
            jobs = [state.jobs.get(job_id, KIND)]
        else:
            jobs = [j for j in _live(state) if not bundle_id or _matches(j, bundle_id)]
            if not bundle_id and len(jobs) > 1:
                raise ToolError(f"{len(jobs)} bridges run ({', '.join(j.id for j in jobs)}); pass job_id or bundle_id.")
        if jobs:
            job = jobs[0]
            if not job.finished:
                await state.jobs.cancel(job)
            await _settle(job)
            v = bridge_view(job)
            return ActionResult(done=True, message=f"Debug in editor stopped ({v.ended or job.state}).",
                                data={"job_id": job.id, "bundle_id": v.bundle_id, "ended": v.ended or job.state,
                                      "attached": v.attached, "killed": v.killed},
                                next_steps=v.next_steps[:2])
        # a bridge this server didn't start (the app window, another agent): its helper stops on SIGTERM
        others = (await helper.call("debug", "editor-status", {"bundle_id": bundle_id}) or {}).get("bridges") or []
        if not others:
            return ActionResult(done=True, message="No Debug in editor bridge is running" +
                                (f" for {bundle_id}." if bundle_id else "."))
        if len(others) > 1 and not bundle_id:
            raise ToolError(f"{len(others)} bridges run ({', '.join(str(o.get('bundleId')) for o in others)}); pass "
                            "bundle_id.")
        o = others[0]
        pid = int(o.get("helperPid") or 0)
        if pid > 1:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGTERM)
        bid = str(o.get("bundleId"))
        end = time.monotonic() + deadline.wait_time(10) - 2.0      # room for one more status call
        gone = False
        while not gone and time.monotonic() < end:
            await asyncio.sleep(0.5)
            gone = not (await helper.call("debug", "editor-status", {"bundle_id": bid}, timeout=5) or {}).get("bridges")
        if not gone:
            return ActionResult(done=False, message=f"Asked the bridge for {bid} to stop; it is still running.",
                                next_steps=["Call ios_debug_editor_stop again in a few seconds, or ask the user to "
                                            "stop it in iOS Studio › iPhone › Debug."])
        return ActionResult(done=True, message=f"Debug in editor for {bid} (started outside this server) stopped.",
                            data={"bundle_id": bid, "ended": "cancelled"})

    @tool(mcp, "Debug in editor status", kind="read")
    async def ios_debug_editor_status(
        ctx: Context,
        bundle_id: str | None = None,
    ) -> EditorBridges:
        """Running Debug in editor bridges (port, pid, editor attached), also those started in the app
        window."""
        state = get_state(ctx)
        d = await helper.call("debug", "editor-status", {"bundle_id": bundle_id}) or {}
        mine = {j.id: j for j in _live(state) if not bundle_id or _matches(j, bundle_id)}
        out: list[BridgeInfo] = []
        for o in (d.get("bridges") or []):
            if not isinstance(o, dict):
                continue
            ids = [str(o.get("bundleId") or ""), str(o.get("installedBundleId") or "")]
            job = next((j for j in mine.values() if any(i and _matches(j, i) for i in ids)), None)
            if job:
                mine.pop(job.id)
            out.append(BridgeInfo(bundle_id=ids[0], installed_bundle_id=ids[1] or None, app_name=o.get("appName"),
                                  project=helper.tilde(o.get("project")), port=o.get("port"), pid=o.get("pid"),
                                  launched=o.get("launched"), editor=o.get("editor"), since=o.get("since"),
                                  job_id=job.id if job else None, started_by="mcp" if job else "app"))
        for job in mine.values():       # not in the state folder (yet, or demo mode)
            b = _bridge(job)
            r = b.ready or {}
            out.append(BridgeInfo(bundle_id=str(r.get("bundleId") or b.key), app_name=r.get("appName"),
                                  project=helper.tilde(job.project_path), port=r.get("port"), pid=r.get("pid"),
                                  launched=r.get("launched"), editor=b.editor if b.ready else "starting",
                                  job_id=job.id, started_by="mcp"))
        nxt = []
        if not out:
            nxt.append("None running. ios_debug_editor_start(project_path) starts one.")
        elif any(x.started_by == "mcp" for x in out):
            nxt.append("ios_debug_editor_start(job_id=...) shows one in detail; ios_debug_editor_stop ends one.")
        else:
            nxt.append("ios_debug_editor_stop(bundle_id=...) ends one.")
        return EditorBridges(bridges=out[:20], truncated=len(out) > 20, omitted=max(0, len(out) - 20),
                             next_steps=nxt).with_budget(None)


async def _settle(job: Job) -> None:
    """The stream ended: give the job a moment to record its final state."""
    task = job.task
    if _bridge(job).ended and task is not None and not task.done():
        await asyncio.wait({task}, timeout=deadline.wait_time(3))
