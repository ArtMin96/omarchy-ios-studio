"""Build tools (job pattern): start a build, poll it, read diagnostics and logs, answer prompts, cancel.

Builds take 30 s to 10 min, longer than many clients' tool timeouts, so ios_build_start returns a
build_id at once and ios_build_status long-polls (≤25 s per call). The same ids cover toolchain
installs (t-N) and renewals (r-N).
"""
from __future__ import annotations

import re
from typing import Annotated, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from .. import helper
from ..jobs import Job
from ..models import (ActionResult, BuildHistory, BuildStatus, Diagnostic, DiagnosticsPage, HistoryEntry, LogPage)
from ..state import AppState, get_state, resolve_project
from . import MAX_WAIT, READ, ann, dry_run, job_status, start_and_wait, wait_with_progress, tool
from .projects import ProjectPath

BuildId = Annotated[str, Field(description="Id from ios_build_start / ios_run / ios_project_renew / "
                                           "ios_toolchain_install, e.g. 'b-1'")]
WaitSeconds = Annotated[int, Field(ge=0, le=MAX_WAIT, description="Long-poll: return as soon as the job finishes "
                                                                  "or needs input, or after this many seconds")]
Udid = Annotated[str | None, Field(description="iPhone udid; default: the active iPhone")]
Via = Annotated[Literal["auto", "usb", "wifi"], Field(description="How to install: auto prefers the USB cable "
                                                                  "when plugged in, else Wi-Fi")]
Config = Annotated[Literal["debug", "release"], Field(description="Build configuration")]

LINE_CUT = 500


# ------------------------------------------------------------------ install after a Wi-Fi build

def make_installer(state: AppState, launch: bool):
    """Finisher for run/install/renew jobs: the helper builds and signs; installing over Wi-Fi (and
    launching) is done by the app's device daemon, which the device client reaches."""

    async def finish(job: Job) -> None:
        r = job.result or {}
        if not r.get("needsInstall") or r.get("installed"):
            return
        udid, ipa, bundle = r.get("udid"), r.get("ipa"), r.get("bundleId")
        if helper.demo_mode() and state.install_app is None:
            r.update(installed=True, needsInstall=False, launched=launch, pid=4242 if launch else None)
            job.set_step("install", "Installing", "done")
            if launch:
                job.set_step("launch", "Launching", "done")
            job.next_steps.append("Demo mode: the install and launch were simulated.")
            return
        if state.install_app is None or not ipa or not udid:
            job.next_steps.append(
                f"Built and signed {ipa or 'the app'}, but installing over Wi-Fi needs the device tools, which "
                "aren't available in this server. Plug the iPhone in and rerun with via='usb', or ask the user "
                "to press Run in iOS Studio (ios_open_app(page='projects')).")
            return
        job.set_step("install", "Installing", "running", owner="mcp")
        job.current_step = "install"
        job.touch()
        await state.install_app(udid, ipa, job)
        job.set_step("install", "Installing", "done")
        r.update(installed=True, needsInstall=False)
        try:
            await helper.call("projects", "record-install",
                              {"path": job.project_path, "udid": udid, "via": "wifi", "ipa": ipa})
        except ToolError:
            pass
        if launch and bundle and state.launch_app:
            job.set_step("launch", "Launching", "running", owner="mcp")
            job.touch()
            try:
                res = await state.launch_app(udid, bundle) or {}
            except ToolError as exc:   # installed fine; only the launch failed (as in the app: keep it green)
                job.set_step("launch", "Launching", "failed", error=str(exc))
                job.next_steps.append(f"Installed, but it didn't open: {exc}")
            else:
                job.set_step("launch", "Launching", "done")
                r.update(launched=True, pid=res.get("pid"))
        job.touch()

    return finish


async def start_build(state: AppState, ctx: Context, project_path: str, action: str, config: str,
                      udid: str | None, via: str, wait_seconds: int) -> BuildStatus:
    p = await resolve_project(state, project_path)
    path = p["path"]
    busy = state.jobs.running_for(path)
    if busy:
        raise ToolError(f"{p.get('name')} already has a running job {busy.id} ({busy.title}). Poll it with "
                        f"ios_build_status('{busy.id}') or stop it with ios_build_cancel.")
    flags: dict = {"path": path, "config": config}
    if action in ("run", "install"):
        udid = udid or await state.active_udid()
        if not udid:
            raise ToolError("No iPhone is known yet. Ask the user to connect it (cable or Wi-Fi) and unlock it "
                            "(ios_open_app(page='device')), or pass udid. To only compile, use action='compile'.")
        flags["udid"] = udid
        if via != "auto":
            flags["via"] = via
    finisher = make_installer(state, launch=action == "run") if action in ("run", "install") else None
    title = {"compile": "Compile", "ipa": "Build signed .ipa", "install": "Install last build",
             "run": "Build, install and launch"}[action] + f" {p.get('name')}"
    return await start_and_wait(state, ctx, kind="build", group="build", action=action, flags=flags, title=title,
                                prefix="b", wait_seconds=wait_seconds, project_path=path, project=p.get("name"),
                                finisher=finisher)


async def _history(project_path: str | None, limit: int) -> list[dict]:
    flags = {"limit": max(limit, 50)}
    if project_path:
        flags["path"] = project_path
    data = await helper.call("build", "history", flags)
    builds = [b for b in data.get("builds") or [] if not project_path or b.get("path") == project_path]
    builds.sort(key=lambda b: b.get("time") or 0, reverse=True)
    return builds[:limit]


def _number(lines: list[str], start: int) -> list[str]:
    out = []
    for i, ln in enumerate(lines):
        text = ln if len(ln) <= LINE_CUT else ln[:LINE_CUT] + f" …[+{len(ln) - LINE_CUT} chars]"
        out.append(f"{start + i}: {text}")
    return out


def register(mcp: MCPServer) -> None:

    @tool(mcp, annotations=ann("Start build", read_only=False, destructive=False, idempotent=False, open_world=True))
    async def ios_build_start(
        ctx: Context,
        project_path: ProjectPath,
        action: Annotated[Literal["compile", "ipa", "install", "run"],
                          Field(description="compile: build only (no Apple ID or iPhone needed, fastest check "
                                            "for errors). ipa: signed .ipa. install: install the last signed "
                                            "build. run: build, sign, install and launch on the iPhone")] = "compile",
        config: Config = "debug",
        udid: Udid = None,
        via: Via = "auto",
        wait_seconds: WaitSeconds = 0,
    ) -> BuildStatus:
        """Start a build in the background and return its build_id immediately (or after wait_seconds).

        Then poll ios_build_status(build_id, wait_seconds=20) until state is succeeded/failed; on failure
        read ios_build_diagnostics (file:line errors) and ios_build_log. A first build resolves packages
        and can take several minutes; later ones are much faster. ipa/install/run sign with the user's
        Apple ID (free teams: 7-day profiles, 3 apps per iPhone). Use ios_run for the
        build-install-launch shortcut. One job per project at a time.
        """
        state = get_state(ctx)
        return await start_build(state, ctx, project_path, action, config, udid, via, wait_seconds)

    @tool(mcp, annotations=ann("Build, install and launch", read_only=False, destructive=False, idempotent=False,
                              open_world=True))
    async def ios_run(
        ctx: Context,
        project_path: ProjectPath,
        config: Config = "debug",
        udid: Udid = None,
        via: Via = "auto",
        wait_seconds: WaitSeconds = 20,
    ) -> BuildStatus:
        """Build, sign, install and launch the app on the iPhone in one call: the "run it" button.

        Waits up to wait_seconds; if the build is still going it returns state='running' with the build_id,
        so keep polling ios_build_status. Over USB xtool installs directly; over Wi-Fi the server installs
        the signed .ipa through the device tools and launches it. When it can't install,
        `result.needs_install` is true and next_steps says what to do. Needs a signed-in Apple ID and a
        connected iPhone (check ios_status first).
        """
        state = get_state(ctx)
        return await start_build(state, ctx, project_path, "run", config, udid, via, wait_seconds)

    @tool(mcp, annotations=READ("Build status"))
    async def ios_build_status(ctx: Context, build_id: BuildId, wait_seconds: WaitSeconds = 20) -> BuildStatus:
        """State of a build or other job: running / needs_input / succeeded / failed / cancelled, the current
        step and progress, error and warning counts, the first 10 diagnostics, any prompt waiting for
        the human, the result (ipa path, installed, launched) and next_steps.

        Long-polls: returns early when the job finishes or needs input, else after wait_seconds (≤25).
        If state is needs_input, show pending_prompt to the user and answer with ios_build_respond.
        """
        state = get_state(ctx)
        job = state.jobs.get(build_id)
        await wait_with_progress(state, job, wait_seconds, ctx)
        return job_status(job)

    @tool(mcp, annotations=READ("Build diagnostics"))
    async def ios_build_diagnostics(
        ctx: Context,
        build_id: Annotated[str | None, Field(description="A job from this session; omit to use project_path")] = None,
        project_path: Annotated[str | None, Field(description="Use the latest build of this project (live or "
                                                              "from history)")] = None,
        severity: Annotated[Literal["all", "error", "warning"], Field(description="Only errors, only warnings, or both")] = "all",
        offset: Annotated[int, Field(ge=0, description="Skip this many diagnostics (pagination)")] = 0,
        limit: Annotated[int, Field(ge=1, le=200, description="Max diagnostics to return")] = 50,
    ) -> DiagnosticsPage:
        """Compiler errors and warnings as file:line:col + message, errors first, paginated.

        Pass build_id for a build started in this session, or project_path for that project's latest
        build (also from earlier sessions or builds done in the app). Fix the files, then rebuild with
        ios_build_start(action='compile').
        """
        state = get_state(ctx)
        diags: list[dict]
        result = project = None
        if build_id:
            job = state.jobs.get(build_id)
            diags, source, project, result = job.diagnostics, f"build {job.id}", job.project, job.state
        elif project_path:
            p = await resolve_project(state, project_path)
            job = state.jobs.latest_for(p["path"])
            if job:
                diags, source, project, result = job.diagnostics, f"build {job.id}", job.project, job.state
            else:
                hist = await _history(p["path"], 1)
                if not hist:
                    raise ToolError(f"{p.get('name')} has no builds yet. Start one with ios_build_start.")
                h = hist[0]
                diags, source, project, result = h.get("diagnostics") or [], "history (latest build)", \
                    h.get("project"), h.get("result")
        else:
            jobs = [j for j in state.jobs.jobs.values() if j.kind == "build"]
            if not jobs:
                raise ToolError("Pass build_id or project_path.")
            job = jobs[-1]
            diags, source, project, result = job.diagnostics, f"build {job.id}", job.project, job.state
        rank = {"error": 0, "warning": 1}
        diags = sorted(diags, key=lambda d: rank.get(str(d.get("severity")), 2))
        errors = sum(1 for d in diags if d.get("severity") == "error")
        warnings = sum(1 for d in diags if d.get("severity") == "warning")
        if severity != "all":
            diags = [d for d in diags if d.get("severity") == severity]
        page = diags[offset:offset + limit]
        more = offset + limit < len(diags)
        nxt = []
        if errors:
            nxt.append("Open each file at the given line, fix the error, then rebuild with "
                       "ios_build_start(action='compile').")
        elif result == "failed":
            nxt.append("No compiler diagnostics: the failure is elsewhere. Read ios_build_log(grep='error|fail').")
        if more:
            nxt.append(f"More: offset={offset + limit}.")
        if source.startswith("history"):
            nxt.append("History keeps only the first diagnostics of each build; rebuild for the full list.")
        return DiagnosticsPage(source=source, project=project, build_result=result, total=len(diags),
                               count=len(page), offset=offset, has_more=more,
                               next_offset=offset + limit if more else None, errors=errors, warnings=warnings,
                               diagnostics=[Diagnostic.from_helper(d) for d in page], next_steps=nxt)

    @tool(mcp, annotations=READ("Build log"))
    async def ios_build_log(
        ctx: Context,
        build_id: Annotated[str | None, Field(description="A job from this session (live output)")] = None,
        project_path: Annotated[str | None, Field(description="Latest build log of this project")] = None,
        log_file: Annotated[str | None, Field(description="A log_file from ios_build_history")] = None,
        grep: Annotated[str | None, Field(description="Case-insensitive regex; only matching lines are returned, "
                                                      "e.g. 'error|warning'")] = None,
        tail: Annotated[int, Field(ge=1, le=1000, description="Return the last N lines (ignored when offset "
                                                              "is given)")] = 200,
        offset: Annotated[int | None, Field(ge=0, description="Start at this line (0-based, after grep)")] = None,
        limit: Annotated[int, Field(ge=1, le=1000, description="Max lines with offset")] = 200,
    ) -> LogPage:
        """Raw build output (xtool, swift-build), paginated: the last 200 lines by default, or a range with
        offset/limit, optionally filtered with grep. Lines are numbered; long lines are cut at 500
        characters. Use ios_build_diagnostics first for compiler errors; use this for link, signing,
        package-resolution or install failures."""
        state = get_state(ctx)
        source, lfile = "", None
        lines: list[str]
        first = 0
        if not build_id and not log_file and project_path:
            proj = await resolve_project(state, project_path)
            live = state.jobs.latest_for(proj["path"])
            if live:
                build_id = live.id
        if build_id:
            job = state.jobs.get(build_id)
            lines, first, source, lfile = list(job.lines), job.first_line, f"build {job.id}", job.info.get("logFile")
        else:
            flags: dict = {}
            if log_file:
                flags["file"] = log_file
            elif project_path:
                p = await resolve_project(state, project_path)
                hist = await _history(p["path"], 1)
                if not hist or not hist[0].get("logFile"):
                    raise ToolError(f"{p.get('name')} has no build logs yet. Start a build with ios_build_start.")
                flags["file"] = hist[0]["logFile"]
            else:
                raise ToolError("Pass build_id, project_path or log_file.")
            data = await helper.call("build", "log", flags)
            lines = (data.get("text") or "").splitlines()
            source, lfile = "log file", data.get("file")
        numbered = list(enumerate(lines, start=first))
        if grep:
            try:
                rx = re.compile(grep, re.I)
            except re.error as exc:
                raise ToolError(f"Invalid grep regex: {exc}")
            numbered = [(n, ln) for n, ln in numbered if rx.search(ln)]
        total = len(numbered)
        if offset is not None:
            start = offset
            chunk = numbered[offset:offset + limit]
            more = offset + limit < total
            nxt_off = offset + limit if more else None
        else:
            start = max(0, total - tail)
            chunk = numbered[start:]
            more = False
            nxt_off = None
        note = None
        if offset is None and start > 0:
            note = (f"Showing the last {len(chunk)} of {total} {'matching ' if grep else ''}lines; use offset/limit "
                    "or grep for earlier output.")
        if build_id and first > 0:
            note = (note or "") + f" The first {first} lines were dropped from memory; the full log is {lfile}."
        out = []
        for n, ln in chunk:
            out.extend(_number([ln], n))
        return LogPage(source=source, log_file=lfile, total_lines=total, start_line=chunk[0][0] if chunk else start,
                       count=len(chunk), has_more=more, next_offset=nxt_off, truncated_note=note, lines=out)

    @tool(mcp, annotations=ann("Answer build prompt", read_only=False, destructive=True, idempotent=False,
                              open_world=True))
    async def ios_build_respond(
        ctx: Context,
        build_id: BuildId,
        prompt_id: Annotated[str, Field(description="pending_prompt.id from ios_build_status, e.g. 'p1'")],
        value: Annotated[str, Field(description="One of pending_prompt.options[].value, e.g. 'no'")],
        confirm: Annotated[bool, Field(description="Required (true) for a destructive option, only after the user "
                                                   "explicitly chose it")] = False,
    ) -> BuildStatus:
        """Answer a question a running job is waiting on (state needs_input), e.g. "Replace your signing
        certificate?" during a build or "Install everything?" during a toolchain install.

        Always show the prompt to the user and send THEIR answer; never decide for them. Options marked
        destructive (like revoking a certificate, which stops apps signed elsewhere from opening) also
        need confirm=true. Returns the job status after the answer.
        """
        state = get_state(ctx)
        job = state.jobs.get(build_id)
        p = job.pending_prompt or {}
        opts = [o for o in p.get("options") or [] if isinstance(o, dict)]
        chosen = next((o for o in opts if str(o.get("value")) == value), None)
        if opts and chosen is None:
            vals = ", ".join(repr(str(o.get("value"))) for o in opts)
            raise ToolError(f"'{value}' isn't an option. Valid values: {vals}.")
        if chosen and chosen.get("destructive") and not confirm:
            raise ToolError(f"Not sent: '{chosen.get('label') or value}' is destructive. Show the user the prompt "
                            f"message and options; only if they explicitly choose it, call again with "
                            f"confirm=true. The job keeps waiting meanwhile.")
        await state.jobs.respond(job, prompt_id, value)
        await wait_with_progress(state, job, 2, ctx)
        return job_status(job)

    @tool(mcp, annotations=ann("Cancel build", read_only=False, destructive=False, idempotent=True))
    async def ios_build_cancel(ctx: Context, build_id: BuildId) -> BuildStatus:
        """Stop a running build or job and everything it started (xtool, swift-build). Safe: nothing is
        installed half-way. Returns the final status."""
        state = get_state(ctx)
        job = await state.jobs.cancel(state.jobs.get(build_id))
        return job_status(job)

    @tool(mcp, annotations=READ("Build history"))
    async def ios_build_history(
        ctx: Context,
        project_path: Annotated[str | None, Field(description="Only this project's builds")] = None,
        result: Annotated[Literal["any", "ok", "failed", "cancelled"], Field(description="Only builds with this result")] = "any",
        limit: Annotated[int, Field(ge=1, le=50, description="Max builds to return, newest first")] = 10,
    ) -> BuildHistory:
        """Recent builds, newest first, from this server and from the iOS Studio app: project, kind, result,
        time, duration, error/warning counts, error message and log_file (pass it to ios_build_log)."""
        state = get_state(ctx)
        path = (await resolve_project(state, project_path))["path"] if project_path else None
        builds = await _history(path, 200)
        if result != "any":
            builds = [b for b in builds if b.get("result") == result]
        out = [HistoryEntry.from_entry(b) for b in builds[:limit]]
        return BuildHistory(total=len(builds), builds=out)

    @tool(mcp, annotations=ann("Clean build folders", read_only=False, destructive=True, idempotent=True))
    async def ios_build_clean(
        ctx: Context,
        project_path: ProjectPath,
        confirm: Annotated[bool, Field(description="false = dry run. true only after the user agreed")] = False,
    ) -> ActionResult:
        """Delete a project's build output (.build/ and xtool/, including the last signed .ipa) to force a
        clean rebuild. Sources are untouched; the next build takes longer. Call with confirm=false first
        and repeat with confirm=true once the user agrees."""
        state = get_state(ctx)
        p = await resolve_project(state, project_path)
        if state.jobs.running_for(p["path"]):
            raise ToolError("A build of this project is running; cancel it first (ios_build_cancel).")
        targets = [f"{p['path']}/.build", f"{p['path']}/xtool"]
        if not confirm:
            return dry_run(f"This would delete the build folders of {p.get('name')}.",
                           [f"Delete {t}" for t in targets])
        data = await helper.call("build", "clean", {"path": p["path"]}, timeout=120)
        removed = (data or {}).get("removed") or []
        state.invalidate_projects()
        return ActionResult(done=True, message=f"Removed {len(removed)} folder(s)." if removed else
                            "Nothing to remove.", data=data,
                            next_steps=["Rebuild with ios_build_start(action='compile')."])
