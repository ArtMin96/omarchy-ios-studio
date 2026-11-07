"""Build tools (job pattern): start a build, poll it, read diagnostics and logs, answer prompts, cancel.

Builds take 30 s to 10 min, longer than many clients' tool timeouts, so ios_build_start returns a
job_id at once and ios_job_status (tools/jobs.py) long-polls it (≤25 s per call); ios_job_respond and
ios_job_cancel answer and stop it. The same tools cover every other job (toolchain installs t-N,
renewals r-N, …).
"""
from __future__ import annotations

import asyncio
import bisect
import json
import os
import re
from pathlib import Path
from typing import Annotated, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from .. import helper
from ..backends.builds import history, make_installer
from ..backends.projects import resolve_project
from ..core import budget as B
from ..core.job_status import start_and_wait
from ..core.jobs import Job
from ..core.params import MaxBytes, PresetName, ProjectPath, Udid, WaitSeconds
from ..core.registry import dry_run, page_of, tool
from ..models import ActionResult, BuildHistory, DiagGroup, DiagnosticsPage, HistoryEntry, JobStatus, LogPage
from ..state import AppState, get_state

Via = Annotated[Literal["auto", "usb", "wifi"], Field(description="auto: USB if plugged in, else Wi-Fi")]
Config = Annotated[Literal["debug", "release"], Field(description="Build configuration")]

# The helper's build logs (helper/iosstudio/features/projects/store.py BUILD_LOGS); ios_build_log reads them directly.
BUILD_LOGS = helper.appdirs.state_dir() / "build-logs"
MAX_LOG_READ = 32 * 1024 * 1024    # a bigger log is read from its tail; line numbers stay those of the file

LEVEL_RX = {"error": re.compile(r"\berror\b|\bfatal\b|\bfailed\b|\bfailure\b", re.I),
            "warning": re.compile(r"\berror\b|\bwarning\b|\bfatal\b|\bfailed\b|\bfailure\b", re.I)}


async def start_build(state: AppState, ctx: Context, project_path: str, action: str, config: str,
                      udid: str | None, via: str, wait_seconds: int, preset: str | None = None) -> JobStatus:
    p = await resolve_project(state, project_path)
    path = p["path"]
    busy = state.jobs.running_for(path)
    if busy:
        raise ToolError(f"{p.get('name')} already has a running job {busy.id} ({busy.title}). Poll it with "
                        f"ios_job_status(job_id='{busy.id}') or stop it with ios_job_cancel.")
    flags: dict = {"path": path, "config": config}
    if preset and action == "run":
        # An unknown name fails here, listing the project's presets, instead of as a failed job.
        await helper.call("projects", "presets", {"path": path, "resolve": preset})
        flags["preset"] = preset
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


def _latest_in_builds_file(project_path: str) -> str | None:
    try:
        with open(BUILD_LOGS.parent / "builds.json", encoding="utf-8") as fh:
            hist = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(hist, list):
        return None
    rows = [e for e in hist if isinstance(e, dict) and e.get("path") == project_path and e.get("logFile")]
    rows.sort(key=lambda e: e.get("time") or 0)
    return str(rows[-1]["logFile"]) if rows else None


async def _latest_log_file(project_path: str) -> str | None:
    """The log file of a project's newest build: straight from builds.json (no helper process), or from
    the helper in demo mode, whose history is made up."""
    if helper.demo_mode():
        hist = await history(project_path, 1)
        return hist[0].get("logFile") if hist else None
    return await asyncio.to_thread(_latest_in_builds_file, project_path)


async def _newer_history(job: Job, project_path: str) -> dict | None:
    """The project's latest history entry when it started after this session's job ended (a watch build, or
    one from the app); None while the job runs or when the times are equal or missing (the job wins)."""
    if not job.finished or not job.ended:
        return None
    try:
        hist = await history(project_path, 1)
    except ToolError:
        return None
    h = hist[0] if hist else None
    t = h.get("time") if h else None
    return h if isinstance(t, (int, float)) and t > job.ended else None


async def _watch_build(build_id: str) -> dict:
    """The build history entry of a watch-mode build (w-N, see tools/watch.py)."""
    for b in await history(None, 300):
        if b.get("buildId") == build_id:
            return b
    raise ToolError(f"No watch build '{build_id}' in the build history (it keeps the latest 300 builds). "
                    "ios_watch_status shows the latest one.")


def build_log_path(name: str) -> Path | None:
    """A build log the helper wrote: a path (or bare file name) inside BUILD_LOGS, else None."""
    p = Path(os.path.expanduser(str(name)))
    if not p.is_absolute():
        p = BUILD_LOGS / p
    try:
        p = p.resolve()
        root = BUILD_LOGS.resolve()
    except OSError:
        return None
    return p if root in p.parents and p.is_file() else None


def read_log(path: Path, max_read: int = MAX_LOG_READ) -> tuple[list[str], int] | None:
    """(lines, number of the first line) of a log file, None if it can't be read. A file over max_read
    bytes is read from its tail, starting at a line boundary; the lines skipped are counted so the line
    numbers match the whole file."""
    try:
        with open(path, "rb") as fh:
            size = os.fstat(fh.fileno()).st_size
            skipped = 0
            if size > max_read:
                start = size - max_read
                left = start
                while left > 0:
                    chunk = fh.read(min(left, 1 << 20))
                    if not chunk:
                        break
                    skipped += chunk.count(b"\n")
                    left -= len(chunk)
                fh.seek(start)
                data = fh.read()
                cut = data.find(b"\n")
                if cut >= 0:       # finish the line the window starts inside, so it isn't half a line
                    data = data[cut + 1:]
                    skipped += 1
            else:
                data = fh.read()
    except OSError:
        return None
    return data.decode("utf-8", "replace").splitlines(), skipped


def select_lines(numbered: list[tuple[int, str]], *, grep: str | None = None, level: str = "all",
                 context: int = 0, max_matches: int = 50, tail: int = 60, head: int | None = None,
                 before: int | None = None, offset: int | None = None, limit: int = 200,
                 line_chars: int = B.LINE_CUT, max_bytes: int | None = None) -> dict:
    """The paging core of ios_build_log, usable for any numbered log: filter (level, grep with context),
    pick a window (tail / head / before / offset), fold repeated consecutive lines into one with ×N, clip
    lines and fit the byte budget. Rows render as '<n>: text' (grep context '<n>- text', gaps '--')."""
    filters = [B.compile_pattern(grep)] if grep else []
    if level != "all":
        filters.append(LEVEL_RX[level])
    forward = head is not None or offset is not None
    count = head if head is not None else (limit if offset is not None else tail)
    if filters:
        hits = [k for k, (_, ln) in enumerate(numbered) if all(rx.search(ln) for rx in filters)]
        matched, count, ctx = len(hits), min(count, max_matches), context
    else:
        hits, matched, ctx = list(range(len(numbered))), None, 0
    nums = [numbered[k][0] for k in hits]
    if offset is not None:
        lo = bisect.bisect_left(nums, offset)
        sel = hits[lo:lo + count]
    elif head is not None:
        sel = hits[:count]
    else:
        hi = bisect.bisect_left(nums, before) if before is not None else len(nums)
        sel = hits[max(0, hi - count):hi]
    keep: dict[int, bool] = {}
    for k in sel:
        for j in range(max(0, k - ctx), min(len(numbered), k + ctx + 1)):
            keep[j] = keep.get(j, False) or j == k
    rows: list[dict] = []          # folded rows: first/last line number, text, is_hit, repeat count
    prev_j = None
    for j, is_hit in sorted(keep.items()):
        n, ln = numbered[j]
        if rows and prev_j == j - 1 and rows[-1]["text"] == ln and rows[-1]["hit"] == is_hit:
            rows[-1]["last"], rows[-1]["count"] = n, rows[-1]["count"] + 1
        else:
            rows.append({"first": n, "last": n, "text": ln, "hit": is_hit, "count": 1,
                         "gap": bool(rows) and prev_j is not None and j > prev_j + 1 and ctx > 0})
        prev_j = j
    rendered, owner = [], []       # rendered strings and the row index each belongs to
    for i, r in enumerate(rows):
        if r["gap"]:
            rendered.append("--")
            owner.append(i)
        mark = ":" if (r["hit"] or not filters) else "-"
        rendered.append(f"{r['first']}{mark} {B.clip(r['text'], line_chars)}" + (f" ×{r['count']}" if r["count"] > 1 else ""))
        owner.append(i)
    page = B.take(rendered, len(rendered), B.room(max_bytes), from_end=not forward)
    items = page.items
    got = owner[page.start:page.start + len(items)]
    while items and items[0] == "--":
        items, got = items[1:], got[1:]
    while items and items[-1] == "--":
        items, got = items[:-1], got[:-1]
    shown = sorted(set(got))
    if shown:
        first_n, last_n = rows[shown[0]]["first"], rows[shown[-1]]["last"]
        earlier = bool(nums) and nums[0] < first_n
        later = bool(nums) and nums[-1] > last_n
    else:
        first_n = last_n = offset if offset is not None else (before if before is not None else 0)
        earlier = later = False
    returned = sum(rows[i]["count"] for i in shown)
    return {"lines": items, "matched": matched, "start_line": first_n, "end_line": (last_n + 1) if shown else first_n,
            "count": len(items), "returned_lines": returned, "has_more": later if forward else earlier,
            "next_offset": last_n + 1 if shown and later else None, "before": first_n if shown and earlier else None,
            "budget_cut": len(rendered) - len(page.items), "filtered": bool(filters)}


def log_note(r: dict, total: int, grep: str | None) -> str | None:
    if r["before"] is None and r["next_offset"] is None and not r["budget_cut"]:
        return None
    what = f"{r['matched']} matching lines" if r["filtered"] else f"{total} lines"
    span = f"lines {r['start_line']}–{r['end_line'] - 1}" if r["count"] else "no lines"
    out = f"Showing {span} ({r['returned_lines']} of {what})"
    if r["budget_cut"]:
        out += f"; {r['budget_cut']} more rows cut to fit the size cap (max_bytes raises it)"
    hows = []
    if r["before"] is not None:
        hows.append(f"before={r['before']} for earlier lines")
    if r["next_offset"] is not None:
        hows.append(f"offset={r['next_offset']} for later lines")
    if not grep:
        hows.append("grep='error|warning' to search")
    return out + (". Call again with " + ", ".join(hows) + "." if hows else ".")


def register(mcp: MCPServer) -> None:

    @tool(mcp, "Start build", kind="write", open_world=True)
    async def ios_build_start(
        ctx: Context,
        project_path: ProjectPath,
        action: Annotated[Literal["compile", "ipa", "install", "run"],
                          Field(description="compile: no Apple ID/iPhone needed; ipa: signed .ipa; install: last "
                                            "signed build; run: build, install, launch")] = "compile",
        config: Config = "debug",
        udid: Udid = None,
        via: Via = "auto",
        wait_seconds: WaitSeconds = 0,
    ) -> JobStatus:
        """Start a build job and return its job_id. Poll ios_job_status(job_id, wait_seconds=20) until
        succeeded/failed, then read ios_build_diagnostics. A first build can take minutes. ipa/install/run sign
        with the user's Apple ID. One job per project at a time; ios_run is the build-install-launch shortcut."""
        state = get_state(ctx)
        return await start_build(state, ctx, project_path, action, config, udid, via, wait_seconds)

    @tool(mcp, "Build, install and launch", kind="write", open_world=True)
    async def ios_run(
        ctx: Context,
        project_path: ProjectPath,
        config: Config = "debug",
        udid: Udid = None,
        via: Via = "auto",
        wait_seconds: WaitSeconds = 20,
        preset: PresetName = None,
    ) -> JobStatus:
        """Build, sign, install and launch the app on the iPhone (the "run it" button). Returns state='running'
        + job_id if not done within wait_seconds: poll ios_job_status. Needs an Apple ID and a connected iPhone
        (ios_status). preset='<name>' launches with that run preset. Without preset= none is used."""
        state = get_state(ctx)
        return await start_build(state, ctx, project_path, "run", config, udid, via, wait_seconds, preset)

    @tool(mcp, "Build diagnostics", kind="read")
    async def ios_build_diagnostics(
        ctx: Context,
        job_id: Annotated[str | None, Field(description="Build job, or watch build 'w-N'")] = None,
        project_path: Annotated[str | None, Field(description="Instead: this project's latest build")] = None,
        severity: Literal["all", "error", "warning"] = "all",
        category: Literal["all", "compiler", "linker", "package", "signing", "device", "toolchain", "config",
                          "other"] = "all",
        include_notes: Annotated[bool, Field(description="Also compiler notes")] = False,
        grouped: Annotated[bool, Field(description="Group repeated messages; false lists each")] = True,
        offset: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=100)] = 25,
        max_bytes: MaxBytes = None,
    ) -> DiagnosticsPage:
        """Why a build failed: entries grouped by message, errors first, each with severity, category, count,
        file:line:col locations and, for non-compiler problems, a fix `hint` naming the tool. Pass job_id, or
        project_path for its newest build (app and watch builds too). Paged with offset."""
        state = get_state(ctx)
        diags: list[dict]
        result = project = root = None
        h: dict | None = None             # the history entry, when the diagnostics come from there
        job = None
        if job_id and job_id.strip().startswith("w-") and job_id.strip() not in state.jobs.jobs:
            h = await _watch_build(job_id.strip())     # watch mode's builds live in the build history
            diags, source, project, result, root = h.get("diagnostics") or [], f"history (watch build {job_id})", \
                h.get("project"), h.get("result"), h.get("path")
        elif job_id:
            job = state.jobs.get(job_id)
            diags, source, project, result, root = job.diagnostics, f"build {job.id}", job.project, job.state, \
                job.project_path
        elif project_path:
            p = await resolve_project(state, project_path)
            root = p["path"]
            job = state.jobs.latest_for(p["path"])
            newer = await _newer_history(job, p["path"]) if job else None
            if job and not newer:
                diags, source, project, result = job.diagnostics, f"build {job.id}", job.project, job.state
            elif newer:
                h, job = newer, None
                diags, source, project, result = h.get("diagnostics") or [], "history (latest build)", \
                    h.get("project"), h.get("result")
            else:
                job = None
                hist = await history(p["path"], 1)
                if not hist:
                    raise ToolError(f"{p.get('name')} has no builds yet. Start one with ios_build_start.")
                h = hist[0]
                diags, source, project, result = h.get("diagnostics") or [], "history (latest build)", \
                    h.get("project"), h.get("result")
        else:
            jobs = [j for j in state.jobs.jobs.values() if j.kind == "build"]
            if not jobs:
                raise ToolError("Pass job_id or project_path.")
            job = jobs[-1]
            diags, source, project, result, root = job.diagnostics, f"build {job.id}", job.project, job.state, \
                job.project_path
        errors = sum(1 for d in diags if d.get("severity") == "error")
        warnings = sum(1 for d in diags if d.get("severity") == "warning")
        notes = sum(1 for d in diags if d.get("severity") == "note")
        cats: dict[str, int] = {}
        for d in diags:
            if d.get("severity") in ("error", "warning"):
                cats[B.diag_category(d)] = cats.get(B.diag_category(d), 0) + 1
        err_cats = list(dict.fromkeys(B.diag_category(d) for d in diags if d.get("severity") == "error"))
        if severity != "all":
            diags = [d for d in diags if d.get("severity") in (severity, "note")]
        if category != "all":
            diags = [d for d in diags if B.diag_category(d) == category]
        if grouped:
            entries = B.group_diags(diags, root, include_notes)
        else:
            entries = [g for d in diags for g in B.group_diags([d], root, include_notes)]
        page, nxt_off = page_of(entries, offset, limit, lambda g: DiagGroup(**g), max_bytes)
        bid = h.get("buildId") if h else (job.id if job else None)
        watched = bool(h) and (h.get("trigger") == "watch" or str(h.get("buildId") or "").startswith("w-"))
        again = ("save the files: watch mode compiles again by itself; ios_watch_status(wait_seconds=20) returns "
                 "the new result") if watched else "rebuild with ios_build_start(action='compile')"
        nxt = []
        if "compiler" in err_cats:
            nxt.append(f"Open each file at the given line, fix the error, then {again}.")
        others = [c for c in err_cats if c != "compiler"]
        if others:
            nxt.append(f"{', '.join(others).capitalize()} error(s): follow each entry's hint, then "
                       + (again + "." if watched else "rebuild with ios_build_start."))
        if others and set(others) <= {"other"}:
            nxt.append("Those errors have no known fix: ios_build_log(grep='error', context=3) shows the lines "
                       "around them.")
        elif result == "failed" and not errors:
            nxt.append("No error was recognised in the output (last resort): read ios_build_log(level='error').")
        if category != "all" and not entries:
            nxt.append(f"No {category} entries in this build; `categories` counts what there is.")
        note = None
        if nxt_off is not None:
            note = (f"Showing {len(page)} of {len(entries)} entries; call again with offset={nxt_off} for more"
                    + (", or severity='error' for errors only." if severity == "all" and warnings else "."))
        if source.startswith("history") and not watched:
            nxt.append("History keeps only the first diagnostics of each build; rebuild for the full list.")
        elif watched and len(diags) >= 200:
            nxt.append("History keeps the first 200 diagnostics of a build; fix those and save to see the rest.")
        if notes and not include_notes:
            nxt.append(f"{notes} compiler notes hidden; include_notes=true shows them.")
        return DiagnosticsPage(source=source, job_id=bid, project=project, build_result=result, total=len(entries),
                               count=len(page), offset=offset, has_more=nxt_off is not None, next_offset=nxt_off,
                               errors=errors, warnings=warnings, notes=notes, categories=cats, diagnostics=page,
                               next_steps=nxt,
                               truncated=nxt_off is not None, omitted=len(entries) - len(page),
                               note=note).with_budget(max_bytes)

    @tool(mcp, "Build log", kind="read")
    async def ios_build_log(
        ctx: Context,
        job_id: Annotated[str | None, Field(description="Build job (live output)")] = None,
        project_path: Annotated[str | None, Field(description="This project's latest log")] = None,
        log_file: Annotated[str | None, Field(description="A log_file from ios_build_history")] = None,
        grep: Annotated[str | None, Field(max_length=B.MAX_PATTERN, description="Case-insensitive regex")] = None,
        context: Annotated[int, Field(ge=0, le=10, description="Lines around each grep/level match")] = 0,
        level: Annotated[Literal["all", "warning", "error"], Field(description="Only error (or warning) lines")] = "all",
        max_matches: Annotated[int, Field(ge=1, le=500)] = 50,
        tail: Annotated[int, Field(ge=1, le=2000, description="Last N lines (default mode)")] = 60,
        head: Annotated[int | None, Field(ge=1, le=2000, description="First N lines instead")] = None,
        before: Annotated[int | None, Field(ge=0, description="With tail: lines before this line (page back)")] = None,
        offset: Annotated[int | None, Field(ge=0, description="`limit` lines from this line on (page forward)")] = None,
        limit: Annotated[int, Field(ge=1, le=2000)] = 200,
        line_chars: Annotated[int, Field(ge=80, le=4000, description="Cut lines after this many chars")] = B.LINE_CUT,
        max_bytes: MaxBytes = None,
    ) -> LogPage:
        """Raw build output, last 60 lines by default, numbered '<n>: text' after the JSON header. Page with
        before=/offset=, search with grep or level='error'. Try ios_build_diagnostics first; read this only for
        context it doesn't give."""
        state = get_state(ctx)
        source, lfile = "", None
        lines: list[str]
        first = 0
        job = None
        from_disk = False
        if not job_id and not log_file and project_path:
            proj = await resolve_project(state, project_path)
            live = state.jobs.latest_for(proj["path"])
            if live:
                job_id = live.id
        if job_id:
            job = state.jobs.get(job_id)
            lfile = job.info.get("logFile")
            disk = lfile if job.finished and job.first_line > 0 and isinstance(lfile, str) else None
            loaded = await asyncio.to_thread(read_log, Path(disk)) if disk else None
            if loaded is None and job.finished and job.first_line > 0 and job.info.get("spillFile"):
                loaded = await asyncio.to_thread(read_log, Path(job.info["spillFile"]))
                if loaded is not None:
                    lfile = job.info["spillFile"]
                    loaded = (loaded[0], loaded[1] + int(job.info.get("spillFirstLine") or 0))
            if loaded is not None:      # finished and trimmed in memory: the whole log from disk
                lines, first = loaded
                source, from_disk = f"build {job.id} (log file)", True
            else:
                lines, first, source = list(job.lines), job.first_line, f"build {job.id}"
        else:
            if log_file:
                want = log_file
            elif project_path:
                p = await resolve_project(state, project_path)
                want = await _latest_log_file(p["path"])
                if not want:
                    raise ToolError(f"{p.get('name')} has no build logs yet. Start a build with ios_build_start.")
            else:
                raise ToolError("Pass job_id, project_path or log_file.")
            path = build_log_path(want)
            loaded = await asyncio.to_thread(read_log, path) if path else None
            if loaded is not None:
                lines, first = loaded
                source, lfile, from_disk = "log file", str(path), True
            elif helper.demo_mode():    # demo builds have made-up logs that only the helper knows
                data = await helper.call("build", "log", {"file": want})
                lines = (data.get("text") or "").splitlines()
                source, lfile = "log file", data.get("file")
            else:
                raise ToolError("That build log is gone (logs of builds older than the newest 300 are deleted). "
                                "ios_build_history lists the ones that are left.")
        numbered = list(enumerate(lines, start=first))
        r = select_lines(numbered, grep=grep, level=level, context=context, max_matches=max_matches, tail=tail,
                         head=head, before=before, offset=offset, limit=limit, line_chars=line_chars,
                         max_bytes=max_bytes)
        total = first + len(lines)
        note = log_note(r, total, grep)
        links: list = []
        full = helper.tilde(lfile) if lfile else None
        if first > 0 and from_disk:
            note = (note + " " if note else "") + f"Lines before {first} weren't read (the log is over " \
                   f"{MAX_LOG_READ // (1024 * 1024)} MB); the full log is {full}."
        elif job is not None and first > 0:
            note = (note + " " if note else "") + f"Lines before {first} are no longer in memory" + \
                   (f"; the full log is {full}." if full else ".")
        if note and not lfile and job is not None:   # only in memory: write it out so nothing is lost
            path, _, link = B.spill("\n".join(lines), "build-logs", f"{job.id}.log")
            full, links = helper.tilde(path), [link]
        candidates = r["matched"] if r["filtered"] else len(lines)
        return LogPage(source=source, log_file=helper.tilde(lfile) if lfile else None, total_lines=total,
                       matched=r["matched"], start_line=r["start_line"], end_line=r["end_line"], count=r["count"],
                       has_more=r["has_more"], next_offset=r["next_offset"], before=r["before"], lines=r["lines"],
                       truncated=bool(note), omitted=max(0, candidates - r["returned_lines"]), note=note,
                       full_output_path=full).with_budget(max_bytes, links)

    @tool(mcp, "Build history", kind="read")
    async def ios_build_history(
        ctx: Context,
        project_path: Annotated[str | None, Field(description="Only this project's builds")] = None,
        result: Literal["any", "ok", "failed", "cancelled"] = "any",
        limit: Annotated[int, Field(ge=1, le=50)] = 10,
    ) -> BuildHistory:
        """Recent builds (this server and the app), newest first: result, time, duration, error counts and
        log_file for ios_build_log."""
        state = get_state(ctx)
        path = (await resolve_project(state, project_path))["path"] if project_path else None
        builds = await history(path, 200)
        if result != "any":
            builds = [b for b in builds if b.get("result") == result]
        out, _ = page_of(builds, 0, limit, HistoryEntry.from_entry)
        more = len(out) < len(builds)
        return BuildHistory(total=len(builds), builds=out, has_more=more, omitted=len(builds) - len(out),
                            note=f"Showing the newest {len(out)} of {len(builds)}; narrow with project_path or "
                                 "result." if more else None)

    @tool(mcp, "Clean build folders", kind="destructive", idempotent=True)
    async def ios_build_clean(
        ctx: Context,
        project_path: ProjectPath,
        confirm: Annotated[bool, Field(description="false = dry run; true after the user agreed")] = False,
    ) -> ActionResult:
        """Delete a project's build output (.build/, xtool/, the last .ipa) for a clean rebuild; sources stay.
        Dry run unless confirm=true (after the user agrees)."""
        state = get_state(ctx)
        p = await resolve_project(state, project_path)
        if state.jobs.running_for(p["path"]):
            raise ToolError("A build of this project is running; cancel it first (ios_job_cancel).")
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
