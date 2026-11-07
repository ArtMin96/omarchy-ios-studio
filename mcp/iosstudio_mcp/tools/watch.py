"""Watch mode (compile on save): ios_watch_start / ios_watch_stop / ios_watch_status.

The watcher is one helper process per user (`watch serve`, see helper/iosstudio/features/watch.py and
docs/features/watch-mode.md), shared with the app: whoever starts it, both see its status and either can stop
it. A watcher this server starts (or joins) lists this server's pid as an owner, so it stops by itself once
this server exits and the app isn't using it either. Each compile is a normal `build compile` (build history,
ios_build_diagnostics(job_id='w-N'), ios_build_log(log_file=...)). Status results stay small: counts and
the first ≤ 5 errors.
"""
from __future__ import annotations

import os
from typing import Annotated, Any

from mcp.server.mcpserver import Context, MCPServer
from pydantic import Field

from .. import helper
from ..backends.projects import resolve_project
from ..core import budget as B
from ..core import deadline
from ..core.job_status import job_result, settle, shared_job
from ..core.jobs import Job
from ..core.params import ProjectPath
from ..core.registry import MAX_WAIT, tool
from ..models import Out, iso
from ..state import get_state

FIRST_ERRORS = 5
START_TIMEOUT = 40      # `watch run`: stopping another project's watcher (≤ 15 s) + starting this one (≤ 8 s)
MESSAGE_CUT = 200


class WatchError(Out):
    location: str | None = Field(None, description="'Sources/App/File.swift:12:5' (relative to the project)")
    message: str
    category: str | None = None


class WatchBuild(Out):
    job_id: str = Field(description="w-N: pass to ios_build_diagnostics(job_id=...) for the full list")
    result: str = Field(description="ok or failed")
    duration_seconds: float | None = None
    finished_at: str | None = None
    errors: int = 0
    warnings: int = 0
    error_code: str | None = None
    error_message: str | None = None
    log_file: str | None = Field(None, description="Pass to ios_build_log(log_file=...)")


class WatchRun(Out):
    state: str = Field(description="checking, building, installing, launching, launched, failed or skipped")
    message: str
    job_id: str | None = Field(None, description="The run's own build (w-N)")
    bundle_id: str | None = None


class WatchStatus(B.Budgeted, Out):
    watching: bool = Field(description="True while the watcher runs")
    state: str = Field(description="watching (idle), changed (waiting for the debounce), compiling, ok, failed, "
                                   "stopped, off (no watcher), or starting (call ios_watch_start again)")
    message: str | None = None
    project: str | None = None
    project_path: str | None = None
    mode: str | None = Field(None, description="inotify, or poll (checks files every second)")
    mode_reason: str | None = None
    auto_run: bool = False
    debounce_ms: int | None = None
    changed_files: list[str] = Field(default_factory=list, description="Files of the pending or running build")
    changed_count: int = 0
    current_job_id: str | None = Field(None, description="The build compiling right now (w-N)")
    progress: float | None = Field(None, description="0..1 of the running build")
    last_build: WatchBuild | None = Field(None, description="The latest finished build (ok or failed)")
    first_errors: list[WatchError] = Field(default_factory=list, description="≤ 5 errors of last_build")
    run: WatchRun | None = Field(None, description="Auto-run after the latest clean build")
    builds: int = 0
    superseded: int = Field(0, description="Builds cancelled because files changed while they ran")
    stop_reason: str | None = Field(None, description="stopped, replaced, no-owner, project-gone, watcher-died")
    attached: bool = Field(False, description="ios_watch_start joined a watcher that was already running")
    replaced: str | None = Field(None, description="The other project whose watcher ios_watch_start stopped")
    other_project: str | None = Field(None, description="The project being watched instead of the one asked about")
    stopped: bool = Field(False, description="ios_watch_stop stopped a watcher")
    waited_seconds: float | None = None
    timed_out: bool = False
    next_steps: list[str] = Field(default_factory=list)


def _errors(last: dict, root: str | None) -> list[WatchError]:
    out = []
    for d in last.get("diagnostics") or []:
        if not isinstance(d, dict) or d.get("severity") != "error":
            continue
        loc = None
        if d.get("file"):
            loc = B.rel_path(str(d["file"]), root)
            if d.get("line"):
                loc += f":{d['line']}" + (f":{d['col']}" if d.get("col") else "")
        out.append(WatchError(location=loc, message=B.clip(" ".join(str(d.get("message") or "").split()),
                                                          MESSAGE_CUT), category=d.get("category")))
        if len(out) >= FIRST_ERRORS:
            break
    return out


def to_status(d: dict, project_path: str | None = None) -> WatchStatus:
    """The helper's snapshot -> WatchStatus with next steps."""
    root = d.get("path")
    last = d.get("lastBuild") if isinstance(d.get("lastBuild"), dict) else None
    lb = None
    if last:
        lb = WatchBuild(job_id=str(last.get("id")), result=str(last.get("result")),
                        duration_seconds=last.get("duration"), finished_at=iso(last.get("time")),
                        errors=int(last.get("errors") or 0), warnings=int(last.get("warnings") or 0),
                        error_code=last.get("errorCode"),
                        error_message=B.clip(last["errorMessage"], 300) if last.get("errorMessage") else None,
                        log_file=helper.tilde(last.get("logFile")))
    run = d.get("run") if isinstance(d.get("run"), dict) else None
    state = str(d.get("state") or "off")
    prog = d.get("progress") if isinstance(d.get("progress"), dict) else None
    other = d.get("other") if isinstance(d.get("other"), dict) else None
    st = WatchStatus(
        watching=bool(d.get("watching")), state=state, message=B.clip(d.get("message"), 300) if d.get("message") else None,
        project=d.get("project"), project_path=helper.tilde(root), mode=d.get("mode"), mode_reason=d.get("modeReason"),
        auto_run=bool(d.get("autoRun")), debounce_ms=d.get("debounceMs"),
        changed_files=[B.clip(f, 200) for f in (d.get("changed") or [])[:5]], changed_count=int(d.get("changedCount") or 0),
        current_job_id=d.get("buildId") if state == "compiling" else None,
        progress=round(float(prog["fraction"]), 3) if prog and isinstance(prog.get("fraction"), (int, float)) else None,
        last_build=lb, first_errors=_errors(last, root) if last else [],
        run=WatchRun(state=str(run.get("state")), message=B.clip(run.get("message") or "", 300),
                     job_id=run.get("buildId"), bundle_id=run.get("bundleId")) if run else None,
        builds=int(d.get("builds") or 0), superseded=int(d.get("superseded") or 0),
        stop_reason=d.get("stopReason") if state == "stopped" else None, attached=bool(d.get("attached")),
        replaced=helper.tilde((d.get("replaced") or {}).get("path")) if isinstance(d.get("replaced"), dict) else None,
        other_project=helper.tilde(other.get("path")) if other else None,
        stopped=bool(d.get("stopped")),
        waited_seconds=d.get("waited"), timed_out=bool(d.get("timedOut")))
    st.next_steps = _next_steps(st, project_path or root)
    return st


def _next_steps(st: WatchStatus, path: str | None) -> list[str]:
    p = helper.tilde(path) or "<project>"
    out: list[str] = []
    if not st.watching:
        if st.other_project:
            out.append(f"The watcher is on {st.other_project}. ios_watch_start(project_path='{p}') moves it here "
                       "(only one project is watched at a time).")
        else:
            out.append(f"Not watching. ios_watch_start(project_path='{p}') compiles on every save.")
        if st.last_build and st.last_build.errors:
            out.append(f"The last watch build had errors: ios_build_diagnostics(job_id='{st.last_build.job_id}').")
        return out
    if st.state in ("changed", "compiling"):
        out.append("A build is pending or running: ios_watch_status(wait_seconds=20) returns when it finishes.")
    lb = st.last_build
    if lb and lb.result == "failed" and st.state == "failed":
        if lb.errors:
            more = f" (first {len(st.first_errors)} shown)" if lb.errors > len(st.first_errors) else ""
            out.append(f"{lb.errors} error(s){more}. Full list with hints: ios_build_diagnostics(job_id="
                       f"'{lb.job_id}'). Fix and save the files: the watcher compiles again by itself; then "
                       "ios_watch_status(wait_seconds=20).")
        else:
            nxt = helper.next_step_for({"code": lb.error_code}) if lb.error_code else None
            out.append(f"The build failed without a compiler error: ios_build_diagnostics(job_id='{lb.job_id}')"
                       + (f". {nxt}" if nxt else "."))
    elif lb and st.state == "ok":
        out.append("Compiled cleanly" + (f" with {lb.warnings} warning(s) (ios_build_diagnostics(job_id='"
                                         f"{lb.job_id}', severity='warning'))" if lb.warnings else "") + ".")
        if not st.auto_run:
            out.append(f"To try it on the iPhone: ios_run(project_path='{p}').")
    if st.run and st.run.state in ("skipped", "failed"):
        out.append(f"Auto-run {st.run.state}: {st.run.message}")
    if st.mode == "poll":
        out.append("Files are checked once a second (inotify unavailable: see docs/troubleshooting.md).")
    out.append("ios_watch_stop() stops watching (and any running compile).")
    return out


DebounceMs = Annotated[int | None, Field(ge=100, le=10_000, description="Delay after the last save (default "
                                                                        "600)")]


def register(mcp: MCPServer) -> None:

    @tool(mcp, "Start watch mode", kind="write", idempotent=True)
    async def ios_watch_start(
        ctx: Context,
        project_path: ProjectPath,
        auto_run: Annotated[bool | None, Field(description="Also run each clean compile on the iPhone")] = None,
        debounce_ms: DebounceMs = None,
    ) -> WatchStatus:
        """Compile on save: a compile-only build (no Apple ID or iPhone) shortly after the project's files
        change; a save during a build restarts it. One watched project per user, shared with the app (another
        project moves it: `replaced`). After edits, ios_watch_status(wait_seconds=20) gives the result. If
        state='starting', call again."""
        state = get_state(ctx)
        p = await resolve_project(state, project_path)
        flags: dict[str, Any] = {"project": p["path"], "detach": True, "owner": os.getpid()}
        if auto_run is not None:                  # only what's passed changes a running watcher
            flags["auto_run"] = "true" if auto_run else "false"
        if debounce_ms is not None:
            flags["debounce_ms"] = debounce_ms
        async def start(job: Job) -> Any:
            job.report(None, "starting the watcher")
            return await helper.call("watch", "run", flags, timeout=START_TIMEOUT)
        job = shared_job(state, "watch-start", p["path"], start, title=f"Start watch mode for {p.get('name') or p['path']}")
        await settle(state, ctx, job)
        if not job.finished:     # stopping another project's watcher first can take ~15 s
            return WatchStatus(watching=False, state="starting", project=p.get("name"),
                               project_path=helper.tilde(p["path"]), message="Starting the watcher…",
                               next_steps=[f"Call ios_watch_start(project_path='{helper.tilde(p['path'])}') again "
                                           "in a few seconds: it joins the start that is running."])
        data = job_result(job)
        state.extras["watch.path"] = p["path"]
        return to_status(data or {}, p["path"]).with_budget(None)

    @tool(mcp, "Stop watch mode", kind="write", idempotent=True)
    async def ios_watch_stop(
        ctx: Context,
        project_path: Annotated[str | None, Field(description="Only if this project is watched (omit: "
                                                              "any)")] = None,
    ) -> WatchStatus:
        """Stop watch mode (also when the app started it) and cancel a running compile. The last status stays
        readable with ios_watch_status."""
        state = get_state(ctx)
        flags: dict[str, Any] = {}
        path = None
        if project_path:
            path = (await resolve_project(state, project_path))["path"]
            flags["project"] = path
        data = await helper.call("watch", "stop", flags, timeout=30)
        state.extras.pop("watch.path", None)
        st = to_status({**(data or {}), "watching": bool((data or {}).get("watching"))}, path)
        if not st.stopped and data and data.get("path") and path and data.get("path") != path:
            st.other_project = helper.tilde(data.get("path"))
            st.next_steps = [f"{st.other_project} is watched, not that project; ios_watch_stop() without "
                             "project_path stops it."]
        return st

    @tool(mcp, "Watch mode status", kind="read")
    async def ios_watch_status(
        ctx: Context,
        project_path: Annotated[str | None, Field(description="Report on this project")] = None,
        wait_seconds: Annotated[int, Field(ge=0, le=MAX_WAIT, description="Wait for the next build to "
                                                                         "finish")] = 0,
    ) -> WatchStatus:
        """The watcher's state, the latest build (w-N, result, counts) and its first ≤ 5 errors (file:line:col).
        wait_seconds waits for a build that finishes after this call (use it right after saving). Full list:
        ios_build_diagnostics(job_id=last_build.job_id)."""
        state = get_state(ctx)
        flags: dict[str, Any] = {}
        path = None
        if project_path:
            path = (await resolve_project(state, project_path))["path"]
            flags["project"] = path
        wait = int(max(0.0, deadline.wait_time(wait_seconds + 3) - 3))   # 3 s for the helper to start and answer
        if wait:
            flags["wait_seconds"] = wait
        data = await helper.call("watch", "status", flags, timeout=wait + 20)
        return to_status(data or {}, path).with_budget(None)
