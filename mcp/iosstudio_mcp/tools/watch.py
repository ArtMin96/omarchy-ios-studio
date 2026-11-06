"""Watch mode (compile on save): ios_watch_start / ios_watch_stop / ios_watch_status.

The watcher is one helper process per user (`watch serve`, see helper/watch.py and
docs/features/watch-mode.md), shared with the app: whoever starts it, both see its status and either can stop
it. A watcher this server starts (or joins) lists this server's pid as an owner, so it stops by itself once
this server exits and the app isn't using it either. Each compile is a normal `build compile` (build history,
ios_build_diagnostics(build_id='w-N'), ios_build_log(log_file=...)). Status results stay small: counts and
the first ≤ 5 errors.
"""
from __future__ import annotations

import os
from typing import Annotated, Any

from mcp.server.mcpserver import Context, MCPServer
from pydantic import Field

from .. import budget as B
from .. import helper
from ..models import Out, iso
from ..state import get_state, resolve_project
from . import MAX_WAIT, READ, ann, tool
from .projects import ProjectPath

FIRST_ERRORS = 5
MESSAGE_CUT = 200


class WatchError(Out):
    location: str | None = Field(None, description="'Sources/App/File.swift:12:5' (relative to the project)")
    message: str
    category: str | None = None


class WatchBuild(Out):
    build_id: str = Field(description="w-N: pass to ios_build_diagnostics(build_id=...) for the full list")
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
    build_id: str | None = None
    bundle_id: str | None = None


class WatchStatus(B.Budgeted, Out):
    watching: bool = Field(description="True while the watcher runs")
    state: str = Field(description="watching (idle), changed (waiting for the debounce), compiling, ok, failed, "
                                   "stopped, or off (no watcher)")
    message: str | None = None
    project: str | None = None
    project_path: str | None = None
    mode: str | None = Field(None, description="inotify, or poll (checks files every second)")
    mode_reason: str | None = None
    auto_run: bool = False
    debounce_ms: int | None = None
    changed_files: list[str] = Field(default_factory=list, description="Files of the pending or running build")
    changed_count: int = 0
    current_build_id: str | None = Field(None, description="The build compiling right now")
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
        lb = WatchBuild(build_id=str(last.get("id")), result=str(last.get("result")),
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
        current_build_id=d.get("buildId") if state == "compiling" else None,
        progress=round(float(prog["fraction"]), 3) if prog and isinstance(prog.get("fraction"), (int, float)) else None,
        last_build=lb, first_errors=_errors(last, root) if last else [],
        run=WatchRun(state=str(run.get("state")), message=B.clip(run.get("message") or "", 300),
                     build_id=run.get("buildId"), bundle_id=run.get("bundleId")) if run else None,
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
            out.append(f"The last watch build had errors: ios_build_diagnostics(build_id='{st.last_build.build_id}').")
        return out
    if st.state in ("changed", "compiling"):
        out.append("A build is pending or running: ios_watch_status(wait_seconds=20) returns when it finishes.")
    lb = st.last_build
    if lb and lb.result == "failed" and st.state == "failed":
        if lb.errors:
            more = f" (first {len(st.first_errors)} shown)" if lb.errors > len(st.first_errors) else ""
            out.append(f"{lb.errors} error(s){more}. Full list with hints: ios_build_diagnostics(build_id="
                       f"'{lb.build_id}'). Fix and save the files: the watcher compiles again by itself; then "
                       "ios_watch_status(wait_seconds=20).")
        else:
            nxt = helper.next_step_for({"code": lb.error_code}) if lb.error_code else None
            out.append(f"The build failed without a compiler error: ios_build_diagnostics(build_id='{lb.build_id}')"
                       + (f". {nxt}" if nxt else "."))
    elif lb and st.state == "ok":
        out.append("Compiled cleanly" + (f" with {lb.warnings} warning(s) (ios_build_diagnostics(build_id='"
                                         f"{lb.build_id}', severity='warning'))" if lb.warnings else "") + ".")
        if not st.auto_run:
            out.append(f"To try it on the iPhone: ios_run(project_path='{p}').")
    if st.run and st.run.state in ("skipped", "failed"):
        out.append(f"Auto-run {st.run.state}: {st.run.message}")
    if st.mode == "poll":
        out.append("Files are checked once a second (inotify unavailable: see docs/troubleshooting.md).")
    out.append("ios_watch_stop() stops watching (and any running compile).")
    return out


DebounceMs = Annotated[int | None, Field(ge=100, le=10_000, description="Wait this long after the last save "
                                                                        "before compiling (rapid saves make one "
                                                                        "build); omit to keep the running "
                                                                        "watcher's value (600 for a new one)")]


def register(mcp: MCPServer) -> None:

    @tool(mcp, annotations=ann("Start watch mode", read_only=False, idempotent=True))
    async def ios_watch_start(
        ctx: Context,
        project_path: ProjectPath,
        auto_run: Annotated[bool | None, Field(description="After each clean compile, also Run it (build, "
                                                           "install, launch) when an iPhone is connected and an "
                                                           "Apple ID is signed in; otherwise the status says why "
                                                           "it was skipped. Omit to keep the running watcher's "
                                                           "setting (off for a new one)")] = None,
        debounce_ms: DebounceMs = None,
    ) -> WatchStatus:
        """Compile on save: watch the project's Sources/, Package.swift and xtool.yml and run a compile-only build
        (no Apple ID or iPhone needed) a moment after files change. Saves during a build cancel it and start one
        more; there is never more than one build at a time and one watched project per user (starting another
        project moves the watcher there, reported in `replaced`). The app shares the same watcher. It starts
        with one build of the current state. Then edit files and call ios_watch_status(wait_seconds=20) to get
        the result: state, counts and the first errors (file:line:col). Stops by itself when this MCP session
        ends and the app isn't using it."""
        state = get_state(ctx)
        p = await resolve_project(state, project_path)
        flags: dict[str, Any] = {"project": p["path"], "detach": True, "owner": os.getpid()}
        if auto_run is not None:                  # only what's passed changes a running watcher
            flags["auto_run"] = "true" if auto_run else "false"
        if debounce_ms is not None:
            flags["debounce_ms"] = debounce_ms
        data = await helper.call("watch", "run", flags, timeout=40)
        state.extras["watch.path"] = p["path"]
        return to_status(data or {}, p["path"]).with_budget(None)

    @tool(mcp, annotations=ann("Stop watch mode", read_only=False, idempotent=True))
    async def ios_watch_stop(
        ctx: Context,
        project_path: Annotated[str | None, Field(description="Only stop if this project is the one watched; "
                                                              "omit to stop whatever is watched")] = None,
    ) -> WatchStatus:
        """Stop watch mode, also when the app started it, and cancel a compile that is running (it shows as
        cancelled in the build history). The last status stays readable with ios_watch_status for a while."""
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

    @tool(mcp, annotations=READ("Watch mode status"))
    async def ios_watch_status(
        ctx: Context,
        project_path: Annotated[str | None, Field(description="Report on this project (says so when another one "
                                                              "is watched)")] = None,
        wait_seconds: Annotated[int, Field(ge=0, le=MAX_WAIT, description="Long-poll: return when the next build "
                                                                         "finishes (or the watcher stops), at "
                                                                         "most after this many seconds")] = 0,
    ) -> WatchStatus:
        """The watcher's state (watching, changed, compiling, ok, failed, stopped, off), the latest finished
        build (w-N id, result, duration, error and warning counts) and its first ≤ 5 errors as
        'file:line:col' + message. wait_seconds blocks until a build that finishes after this call (use it right
        after saving a file); timed_out says it was still busy. The full list: ios_build_diagnostics(build_id=
        last_build.build_id)."""
        state = get_state(ctx)
        flags: dict[str, Any] = {}
        path = None
        if project_path:
            path = (await resolve_project(state, project_path))["path"]
            flags["project"] = path
        if wait_seconds:
            flags["wait_seconds"] = wait_seconds
        data = await helper.call("watch", "status", flags, timeout=wait_seconds + 20)
        return to_status(data or {}, path).with_budget(None)
