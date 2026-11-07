"""Files on the iPhone: the camera roll (read-only), app containers, and the camera test loop.

Daemon commands: media.list/thumb/pull, container.list/pull/push/mkdir/delete, transfer.cancel and
cameratest.run (docs/deviced-protocol.md, "Files"). Transfers and camera tests can outlast one tool
call, so they run as background tasks in this server: the tool waits up to wait_seconds and, if the
work isn't finished, returns state='running' with an id to poll by calling the same tool again with
transfer_id / test_id (cancel=true stops it). Progress comes from the daemon's `transfer` and
`cameratest` events.
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import posixpath
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from .. import helper
from ..backends.crashes import crash_detail
from ..backends.device import client, dcall, hub, resolve_app
from ..backends.projects import resolve_project
from ..core import budget as B
from ..core.errors import device_error
from ..core.params import BundleId, Udid
from ..core.registry import MAX_WAIT, dry_run, fit_page, page_of, tool
from ..models import ActionResult, CrashDetail, Out
from ..state import AppState, get_state

log = logging.getLogger("iosstudio.files")

MAX_JOBS = 30
LIST_DIRS = 60           # folders ios_files_list opens at most per call
FILES_SHOWN = 20         # local paths listed in a transfer status
LOG_SHOWN = 30           # key log lines in a camera test result
RESULTS_SHOWN = 8        # sample entries from done.json


# ------------------------------------------------------------------ models

class MediaItem(Out):
    id: str = Field(description="Camera roll path, e.g. 'DCIM/100APPLE/IMG_0001.HEIC'; pass to ios_media_pull")
    kind: str
    date: str | None = None
    width: int | None = None
    height: int | None = None
    duration: float | None = Field(None, description="Seconds (videos)")
    size: int | None = Field(None, description="Bytes, when known")


class MediaList(B.Budgeted, Out):
    total: int
    photos: int
    videos: int
    source: str = Field(description="photos-db (dates and sizes from the Photos library) or dcim (file list only)")
    count: int
    offset: int
    has_more: bool
    next_offset: int | None = None
    items: list[MediaItem]
    next_steps: list[str] = Field(default_factory=list)


class FileEntry(Out):
    path: str
    type: str = Field(description="dir, file, link, or unknown (not readable)")
    size: int | None = None
    modified: str | None = None
    depth: int = 1


class FilesList(B.Budgeted, Out):
    bundle_id: str
    app: str | None = None
    mode: str = Field(description="container (your own build: Documents, Library, tmp) or documents (file-sharing "
                                  "App Store app: /Documents only)")
    path: str
    depth: int
    total: int
    count: int
    offset: int
    has_more: bool
    next_offset: int | None = None
    files: int
    dirs: int
    bytes: int
    unreadable: int = 0
    entries: list[FileEntry]
    next_steps: list[str] = Field(default_factory=list)


class TransferStatus(B.Budgeted, Out):
    transfer_id: str
    kind: str
    state: str = Field(description="running, succeeded, failed, cancelled, or needs_confirm (push would overwrite)")
    progress: float | None = Field(None, description="0..1")
    bytes: int = 0
    total_bytes: int | None = None
    files_done: int = 0
    file_count: int | None = None
    current_file: str | None = None
    elapsed_seconds: float
    destination: str | None = None
    files: list[str] = Field(default_factory=list, description="Files written (local paths, or phone paths for a push)")
    files_total: int = 0
    skipped: int = 0
    failed: list[str] = Field(default_factory=list)
    converted: int | None = None
    would: list[str] = Field(default_factory=list, description="needs_confirm: what would be overwritten")
    error: str | None = None
    next_steps: list[str] = Field(default_factory=list)


class StepState(Out):
    id: str
    state: str
    detail: str | None = None


class CameraTestResult(B.Budgeted, Out):
    BULK: ClassVar[tuple[str, ...]] = ("log_lines",)
    test_id: str
    state: str = Field(description="running, succeeded (the run finished; see ok), failed, cancelled")
    bundle_id: str | None = None
    step: str | None = None
    steps: list[StepState] = Field(default_factory=list)
    elapsed_seconds: float
    ok: bool | None = Field(None, description="done.json was written and no new crash report appeared")
    timed_out: bool | None = None
    app_exited: bool | None = Field(None, description="The app stopped before writing done.json")
    summary: str | None = None
    results_count: int | None = None
    results_summary: dict[str, Any] | None = Field(None, description="Per key of done.json's results: the most "
                                                                     "common values (strings) or min/mean/max (numbers)")
    results_sample: list[dict[str, Any]] = Field(default_factory=list)
    done_json_path: str | None = None
    output_dir: str | None = None
    output_files: list[str] = Field(default_factory=list)
    log_count: int | None = None
    log_file: str | None = None
    log_lines: list[str] = Field(default_factory=list, description="Key lines: faults and errors first, then the latest")
    new_crash: CrashDetail | None = None
    error: str | None = None
    next_steps: list[str] = Field(default_factory=list)


# ------------------------------------------------------------------ background work

@dataclass
class FJob:
    id: str
    kind: str
    started: float = field(default_factory=time.time)
    task: asyncio.Task | None = None
    progress: dict = field(default_factory=dict)       # last transfer event
    steps: dict = field(default_factory=dict)           # cameratest step -> event
    result: dict | None = None
    error: Exception | None = None
    raw_error: Exception | None = None
    meta: dict = field(default_factory=dict)
    changed: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def finished(self) -> bool:
        return self.task is not None and self.task.done()


_ids = itertools.count(1)


def _jobs(state: AppState) -> dict[str, FJob]:
    jobs = state.extras.get("files.jobs")
    if jobs is None:
        jobs = state.extras["files.jobs"] = {}
        h = hub(state)

        def listener(name: str, data: Any) -> None:
            if not isinstance(data, dict):
                return
            if name == "transfer":
                j = jobs.get(str(data.get("id")))
                if j:
                    j.progress = data
                    j.changed.set()
            elif name == "cameratest":
                j = jobs.get(str(data.get("testId")))
                if j:
                    j.steps[str(data.get("step"))] = data
                    j.changed.set()
        h["listeners"].append(listener)
    return jobs


async def _start(state: AppState, kind: str, prefix: str, cmd: str, args: dict, timeout: float, meta: dict) -> FJob:
    jobs = _jobs(state)
    await client(state)   # the event listener needs the connection
    finished = [k for k, j in jobs.items() if j.finished]
    for k in finished[:max(0, len(jobs) - MAX_JOBS)]:
        jobs.pop(k, None)
    jid = f"{prefix}{int(time.time()) % 100000}-{next(_ids)}"
    job = FJob(id=jid, kind=kind, meta=meta)
    a = dict(args)
    a["testId" if kind == "cameratest" else "transferId"] = jid

    async def run():
        try:
            dev = await client(state)
            job.result = await dev.request(cmd, a, timeout=timeout) or {}
        except Exception as exc:  # noqa: BLE001  kept for the status
            job.raw_error = exc
            job.error = device_error(exc) if exc.__class__.__name__ == "DeviceError" else exc
        finally:
            job.changed.set()
    jobs[jid] = job
    job.task = asyncio.create_task(run(), name=f"files-{jid}")
    return job


def _get(state: AppState, jid: str, kind: str | None = None) -> FJob:
    job = _jobs(state).get(jid.strip())
    if job is None or (kind and job.kind != kind):
        known = ", ".join(k for k, j in _jobs(state).items() if not kind or j.kind == kind) or "none"
        raise ToolError(f"Unknown id '{jid}' (known here: {known}). Ids live only while this MCP server runs.")
    return job


async def _wait(job: FJob, seconds: float, ctx: Context | None) -> None:
    deadline = time.monotonic() + max(0.0, min(seconds, MAX_WAIT))
    last = -1.0
    while not job.finished:
        left = deadline - time.monotonic()
        if left <= 0:
            break
        job.changed.clear()
        try:
            await asyncio.wait_for(job.changed.wait(), min(left, 5.0))
        except asyncio.TimeoutError:
            pass
        p = job.progress
        if ctx is not None and p.get("total"):
            pct = round(100 * float(p.get("bytes") or 0) / max(1, float(p["total"])), 1)
            if pct > last:
                last = pct
                try:
                    await ctx.report_progress(pct, 100, message=str(p.get("file") or job.kind))
                except Exception:  # noqa: BLE001
                    pass


async def _cancel(state: AppState, job: FJob) -> None:
    if job.finished:
        return
    try:
        await dcall(state, "transfer.cancel", {"transferId": job.id}, timeout=10)
    except ToolError:
        pass
    try:
        await asyncio.wait_for(asyncio.shield(job.task), 10)
    except Exception:  # noqa: BLE001
        pass


def _err_text(exc: Exception | None) -> str | None:
    if exc is None:
        return None
    return B.clip_error(str(exc))


def _code(exc: Exception | None) -> str:
    text = str(exc or "")
    return text[text.rfind("[") + 1:text.rfind("]")] if "[" in text and "]" in text else ""


def _transfer_status(job: FJob) -> TransferStatus:
    p = job.progress
    r = job.result or {}
    st = "running"
    if job.finished:
        st = "succeeded" if job.error is None else ("cancelled" if "cancel" in str(job.error).lower() else "failed")
    total = p.get("total")
    files = [helper.tilde(f) for f in (r.get("files") or [])]
    nxt: list[str] = []
    if st == "running":
        nxt.append(f"Still copying. Call {job.meta.get('tool')}(transfer_id='{job.id}') again to wait (or cancel=true).")
    elif st == "succeeded" and job.kind == "media.pull":
        nxt.append("The files are on this computer now; read or open them by path.")
    elif st == "failed":
        nxt.append("Fix the problem in the error, then start again.")
    failed = [f"{x.get('path')}: {x.get('message')}" for x in (r.get("failed") or []) if isinstance(x, dict)]
    return TransferStatus(
        transfer_id=job.id, kind=job.kind, state=st,
        progress=1.0 if st == "succeeded" else (round(min(1.0, float(p.get("bytes") or 0) / total), 3) if total else None),
        bytes=int(r.get("bytes") or p.get("bytes") or 0), total_bytes=total, files_done=int(p.get("files") or 0),
        file_count=p.get("count"), current_file=p.get("file") or None if st == "running" else None,
        elapsed_seconds=round(time.time() - job.started, 1),
        destination=helper.tilde(r.get("dest") or r.get("path") or p.get("dest") or p.get("path") or job.meta.get("dest")),
        files=files[:FILES_SHOWN], files_total=len(files), skipped=len(r.get("skipped") or []), failed=failed[:10],
        converted=r.get("converted") if job.kind == "media.pull" else None, error=_err_text(job.error),
        next_steps=nxt,
        note=f"Showing {FILES_SHOWN} of {len(files)} files." if len(files) > FILES_SHOWN else None,
        truncated=len(files) > FILES_SHOWN, omitted=max(0, len(files) - FILES_SHOWN))


async def _poll_transfer(ctx: Context, transfer_id: str, cancel: bool, wait_seconds: float) -> TransferStatus:
    state = get_state(ctx)
    job = _get(state, transfer_id)
    if job.kind == "cameratest":
        raise ToolError(f"'{transfer_id}' is a camera test; poll it with ios_camera_test(test_id=...).")
    if cancel:
        await _cancel(state, job)
    else:
        await _wait(job, wait_seconds, ctx)
    return _transfer_status(job)


def _local_path(raw: str, what: str, must_exist: bool = True) -> Path:
    p = helper.expand(raw)
    if not helper.inside_home(p):
        raise ToolError(f"The {what} must be inside your home folder (or IOSSTUDIO_MCP_ALLOW_PATHS).")
    if must_exist and not p.exists():
        raise ToolError(f"Nothing at {helper.tilde(str(p))}.")
    return p


# ------------------------------------------------------------------ camera test helpers

def results_summary(done: Any) -> tuple[int | None, dict | None, list[dict]]:
    """(result count, per-key summary, first few results) of done.json {"results": [...]} (or a list)."""
    rows = done.get("results") if isinstance(done, dict) else done if isinstance(done, list) else None
    if not isinstance(rows, list):
        return None, None, []
    rows = [r for r in rows if isinstance(r, dict)]
    summ: dict[str, Any] = {}
    keys = list(dict.fromkeys(k for r in rows for k in r))[:15]
    for k in keys:
        vals = [r[k] for r in rows if k in r]
        nums = [v for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool)]
        strs = [v for v in vals if isinstance(v, str)]
        if nums and len(nums) == len(vals):
            summ[k] = {"min": round(min(nums), 4), "mean": round(sum(nums) / len(nums), 4), "max": round(max(nums), 4)}
        elif strs:
            c = Counter(strs)
            summ[k] = {"distinct": len(c), "top": {B.clip(v, 60): n for v, n in c.most_common(5)}}
    sample = [B.clip_data(r, 6) for r in rows[:RESULTS_SHOWN]]
    return len(rows), summ, sample


def key_log_lines(lines: list[str], n: int) -> list[str]:
    bad = [ln for ln in lines if any(f" {lv} " in ln[:20] for lv in ("fault", "error"))]
    rest = [ln for ln in lines if ln not in bad]
    picked = bad[:max(5, n // 2)]
    picked += rest[-(n - len(picked)):] if n > len(picked) else []
    return [B.clip(ln, B.LINE_CUT) for ln in picked]


async def _camera_status(state: AppState, job: FJob, max_bytes: int | None) -> CameraTestResult:
    steps = [StepState(id=k, state=str(v.get("state")), detail=B.clip(v.get("detail"), 160) or None)
             for k, v in job.steps.items()]
    cur = next((s.id for s in reversed(steps) if s.state == "running"), None)
    common = dict(test_id=job.id, bundle_id=job.meta.get("bundle_id"), elapsed_seconds=round(time.time() - job.started, 1),
                  steps=steps, step=cur)
    if not job.finished:
        p = job.progress
        prog = f" ({p.get('file')}: {p.get('bytes')}/{p.get('total')} bytes)" if p.get("state") == "running" and p.get("total") else ""
        return CameraTestResult(state="running", **common, summary=f"Running: {cur or 'starting'}{prog}.",
                                next_steps=[f"Call ios_camera_test(test_id='{job.id}', wait_seconds=20) again, or "
                                            "cancel=true to stop it."]).with_budget(max_bytes)
    if job.error is not None:
        cancelled = "cancel" in str(job.error).lower()
        code = _code(job.error)
        nxt = []
        if code == "not-dev-app":
            nxt.append("Pick one of your own apps: ios_app_list(scope='own').")
        elif code == "no-file-sharing":
            nxt.append("Camera tests only work with your own development builds.")
        return CameraTestResult(state="cancelled" if cancelled else "failed", **common, error=_err_text(job.error),
                                next_steps=nxt).with_budget(max_bytes)
    r = job.result or {}
    count, summ, sample = results_summary(r.get("done"))
    out_dir = r.get("outputDir")
    done_path = str(Path(out_dir) / "done.json") if out_dir and r.get("done") is not None else None
    outputs = [helper.tilde(x) for x in (r.get("outputs") or [])]
    lines = key_log_lines(list(r.get("logLines") or []), LOG_SHOWN)
    crash = None
    if r.get("newCrashes"):
        try:
            crash = await crash_detail(state, r["newCrashes"][0]["name"])
            crash.next_steps = []
        except ToolError:
            crash = None
    name = job.meta.get("app") or r.get("bundleId")
    if r.get("done") is not None:
        summary = f"{name} wrote done.json with {count if count is not None else '?'} result(s) in {r.get('seconds')} s."
    elif r.get("doneRaw"):
        summary = f"{name} wrote done.json, but it isn't valid JSON."
    elif r.get("exited"):
        summary = f"{name} stopped before writing done.json."
    else:
        summary = f"{name} didn't write done.json within {r.get('timeout'):g} s."
    if crash is not None:
        summary += f" It CRASHED: {crash.exception_type or ''} {crash.termination_reason or ''}".rstrip() + "."
    nxt = []
    if r.get("done") is None:
        nxt.append("Check that the app has the camera test hook (docs/camera-testing.md): it must look for the "
                   "-cameraTestMode launch argument, read Documents/CameraTestInput and write "
                   "Documents/CameraTestOutput/done.json. The log lines below show what it did.")
    if crash is not None:
        nxt.append(f"Find the crashing frames in the project's Sources/; more: ios_crash_get(name='{crash.name}', "
                   "section='threads').")
    if done_path:
        nxt.append(f"The full results are in {helper.tilde(done_path)}; output images are listed in output_files.")
    return CameraTestResult(
        state="succeeded", **common, ok=bool(r.get("ok")), timed_out=r.get("timedOut"), app_exited=r.get("exited"),
        summary=B.clip(summary, 600), results_count=count, results_summary=B.clip_data(summ, 10) if summ else None,
        results_sample=sample, done_json_path=helper.tilde(done_path), output_dir=helper.tilde(out_dir),
        output_files=outputs[:FILES_SHOWN], log_count=r.get("logCount"), log_file=helper.tilde(r.get("logFile")),
        log_lines=lines, new_crash=crash, next_steps=nxt,
        note=(f"Showing {len(lines)} key log lines of {r.get('logCount')}; the whole log is "
              f"{helper.tilde(r.get('logFile'))}." if (r.get("logCount") or 0) > len(lines) and r.get("logFile") else None),
    ).with_budget(max_bytes)


# ------------------------------------------------------------------ tools

def register(mcp: MCPServer) -> None:

    @tool(mcp, "List camera roll", kind="read_remote")
    async def ios_media_list(
        ctx: Context,
        kind: Annotated[Literal["all", "photo", "video"], Field(description="Only photos or only videos")] = "all",
        offset: Annotated[int, Field(ge=0, description="Skip this many items (newest first)")] = 0,
        limit: Annotated[int, Field(ge=1, le=200, description="Max items to return")] = 50,
        refresh: Annotated[bool, Field(description="Re-read the camera roll instead of the last few seconds' copy")] = False,
        udid: Udid = None,
    ) -> MediaList:
        """Photos and videos in the iPhone's camera roll, newest first: id (path), kind, date, size,
        dimensions and video duration. Metadata only, no images. The first call can take ~10 s while
        the Photos library index is copied. Import items with ios_media_pull(ids=[...])."""
        state = get_state(ctx)
        a: dict = {"offset": offset, "limit": limit, "refresh": refresh}
        if kind != "all":
            a["kind"] = kind
        r = await dcall(state, "media.list", a, udid=udid, timeout=90) or {}
        raw = r.get("items") or []
        page, _ = page_of(raw, 0, limit, lambda x: MediaItem(**{k: x.get(k) for k in MediaItem.model_fields}))
        total = int(r.get("total") or 0)
        end = offset + len(page)
        nxt_off = end if end < total else None
        nxt = []
        if not total:
            nxt.append("The camera roll is empty (or only in iCloud).")
        else:
            nxt.append("ios_media_pull(ids=[...]) copies items to this computer (HEIC can be converted to JPEG).")
        return fit_page(MediaList(total=total, photos=int(r.get("photos") or 0), videos=int(r.get("videos") or 0),
                                  source=str(r.get("source") or "dcim"), count=len(page), offset=offset,
                                  has_more=nxt_off is not None, next_offset=nxt_off, items=page, next_steps=nxt,
                                  note=f"Showing {len(page)} of {total}; call again with offset={nxt_off}."
                                  if nxt_off else None), "items", offset)

    @tool(mcp, "Import from camera roll", kind="write", idempotent=True, open_world=True)
    async def ios_media_pull(
        ctx: Context,
        ids: Annotated[list[str] | None, Field(description="Item ids from ios_media_list, e.g. "
                                                           "['DCIM/100APPLE/IMG_0001.HEIC']")] = None,
        latest: Annotated[int | None, Field(ge=1, le=500, description="Instead of ids: the newest N items")] = None,
        kind: Annotated[Literal["all", "photo", "video"], Field(description="With latest: only photos or videos")] = "all",
        dest: Annotated[str, Field(description="Folder on this computer (inside your home folder)")] = "~/Pictures/iPhone",
        convert_heic: Annotated[bool, Field(description="Save HEIC photos as JPEG")] = False,
        transfer_id: Annotated[str | None, Field(description="Poll (or cancel) an import started earlier")] = None,
        cancel: Annotated[bool, Field(description="With transfer_id: stop it")] = False,
        wait_seconds: Annotated[float, Field(ge=0, le=MAX_WAIT, description="How long to wait before returning "
                                                                           "state='running'")] = 20,
        udid: Udid = None,
    ) -> TransferStatus:
        """Copy photos/videos from the iPhone's camera roll to a folder on this computer (read-only on the
        phone; files already imported are skipped). Big imports keep running in the background: if
        state is 'running', call again with transfer_id to wait for it."""
        if transfer_id:
            return await _poll_transfer(ctx, transfer_id, cancel, wait_seconds)
        state = get_state(ctx)
        target = _local_path(dest, "destination folder", must_exist=False)
        if latest and not ids:
            a: dict = {"offset": 0, "limit": latest}
            if kind != "all":
                a["kind"] = kind
            r = await dcall(state, "media.list", a, udid=udid, timeout=90) or {}
            ids = [x["id"] for x in r.get("items") or []]
        if not ids:
            raise ToolError("Pass ids (from ios_media_list) or latest=N.")
        bad = [i for i in ids if not str(i).lstrip("/").startswith("DCIM/")]
        if bad:
            raise ToolError(f"'{bad[0]}' isn't a camera roll id; ids look like 'DCIM/100APPLE/IMG_0001.HEIC' "
                            "(ios_media_list).")
        args = {"ids": ids, "dest": str(target), "convertHeic": convert_heic}
        if udid:
            args["udid"] = udid
        job = await _start(state, "media.pull", "m", "media.pull", args, timeout=3600,
                           meta={"tool": "ios_media_pull", "dest": str(target)})
        await _wait(job, wait_seconds, ctx)
        return _transfer_status(job)

    @tool(mcp, "List app files", kind="read_remote")
    async def ios_files_list(
        ctx: Context,
        bundle_id: BundleId,
        path: Annotated[str | None, Field(description="Folder in the app's container, e.g. '/Documents'. Default: "
                                                      "the top ('/' for your own apps, '/Documents' otherwise)")] = None,
        depth: Annotated[int, Field(ge=1, le=4, description="How many folder levels to walk")] = 1,
        offset: Annotated[int, Field(ge=0, description="Skip this many entries")] = 0,
        limit: Annotated[int, Field(ge=1, le=500, description="Max entries to return")] = 100,
        documents_only: Annotated[bool, Field(description="Your own apps: show only Documents")] = False,
        udid: Udid = None,
    ) -> FilesList:
        """Files in an app's container on the iPhone. Your own development builds expose the whole
        container (Documents, Library, tmp); App Store apps only their Documents folder, and only when
        they turn on file sharing (others fail with no-file-sharing). Returns entries with type, size
        and date plus totals; walk deeper with depth or a narrower path."""
        state = get_state(ctx)
        app = await resolve_app(state, bundle_id, udid)
        bid = app["bundleId"]
        base: dict = {"bundleId": bid, "documentsOnly": documents_only}
        first = await dcall(state, "container.list", {**base, **({"path": path} if path else {})}, udid=udid,
                            timeout=60) or {}
        top = first.get("path") or "/"
        entries: list[FileEntry] = []
        counts = Counter()
        size = 0
        todo = [(first, 1)]
        opened = 1
        capped = False
        while todo:
            listing, lvl = todo.pop(0)
            for e in listing.get("entries") or []:
                t = str(e.get("type") or "unknown")
                counts[t] += 1
                size += int(e.get("size") or 0)
                entries.append(FileEntry(path=e.get("path"), type=t, size=e.get("size"),
                                         modified=time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(e["mtime"]))
                                         if e.get("mtime") else None, depth=lvl))
                if t == "dir" and lvl < depth:
                    if opened >= LIST_DIRS:
                        capped = True
                        continue
                    opened += 1
                    try:
                        sub = await dcall(state, "container.list", {**base, "path": e.get("path")}, udid=udid, timeout=60)
                        todo.append((sub or {}, lvl + 1))
                    except ToolError:
                        counts["unknown"] += 1
        entries.sort(key=lambda x: x.path)
        page, nxt_off = page_of(entries, offset, limit, lambda x: x)
        notes = []
        if nxt_off is not None:
            notes.append(f"Showing {len(page)} of {len(entries)}; call again with offset={nxt_off}.")
        if capped:
            notes.append(f"Opened only {LIST_DIRS} folders; list a narrower path for the rest.")
        more = f" Opened only {LIST_DIRS} folders; list a narrower path for the rest." if capped else ""
        return fit_page(FilesList(bundle_id=bid, app=first.get("name"), mode=str(first.get("mode") or "container"),
                                  path=top, depth=depth, total=len(entries), count=len(page), offset=offset,
                                  has_more=nxt_off is not None, next_offset=nxt_off, files=counts["file"],
                                  dirs=counts["dir"], bytes=size, unreadable=counts["unknown"], entries=page,
                                  next_steps=["ios_files_pull copies files or folders to this computer; "
                                              "ios_files_push adds files."],
                                  note=" ".join(notes) or None, truncated=bool(notes),
                                  omitted=len(entries) - len(page)), "entries", offset,
                        lambda k, end: f"Showing {k} of {len(entries)}; call again with offset={end}.{more}")

    @tool(mcp, "Copy app files to this computer", kind="write", idempotent=True, open_world=True)
    async def ios_files_pull(
        ctx: Context,
        bundle_id: Annotated[str | None, Field(description="App bundle id (not needed when polling)")] = None,
        paths: Annotated[list[str] | None, Field(description="Files or folders in the container, e.g. "
                                                             "['/Documents/export.csv']; folders are copied whole")] = None,
        dest: Annotated[str, Field(description="Folder on this computer (inside your home folder)")] = "~/Downloads",
        transfer_id: Annotated[str | None, Field(description="Poll (or cancel) a copy started earlier")] = None,
        cancel: Annotated[bool, Field(description="With transfer_id: stop it")] = False,
        wait_seconds: Annotated[float, Field(ge=0, le=MAX_WAIT, description="How long to wait (≤25 s) before "
                                                                           "returning state='running' with the "
                                                                           "transfer_id")] = 20,
        udid: Udid = None,
    ) -> TransferStatus:
        """Copy files or folders from an app's container on the iPhone to this computer. Existing local
        files are never overwritten ('name (2).ext'). Long copies continue in the background: poll with
        transfer_id."""
        if transfer_id:
            return await _poll_transfer(ctx, transfer_id, cancel, wait_seconds)
        if not bundle_id or not paths:
            raise ToolError("Pass bundle_id and paths (see ios_files_list).")
        state = get_state(ctx)
        app = await resolve_app(state, bundle_id, udid)
        target = _local_path(dest, "destination folder", must_exist=False)
        args = {"bundleId": app["bundleId"], "paths": paths, "dest": str(target)}
        if udid:
            args["udid"] = udid
        job = await _start(state, "container.pull", "c", "container.pull", args, timeout=3600,
                           meta={"tool": "ios_files_pull", "dest": str(target)})
        await _wait(job, wait_seconds, ctx)
        return _transfer_status(job)

    @tool(mcp, "Copy files into an app", kind="destructive", open_world=True)
    async def ios_files_push(
        ctx: Context,
        bundle_id: Annotated[str | None, Field(description="App bundle id (not needed when polling)")] = None,
        sources: Annotated[list[str] | None, Field(description="Files or folders on this computer (inside your home "
                                                               "folder); folders are copied whole")] = None,
        path: Annotated[str | None, Field(description="Folder in the container to copy into, e.g. '/Documents/In' "
                                                      "(created if missing). Default: Documents")] = None,
        confirm: Annotated[bool, Field(description="Replace files that already exist there. Without it a push "
                                                   "that would overwrite returns state='needs_confirm' and changes "
                                                   "nothing")] = False,
        transfer_id: Annotated[str | None, Field(description="Poll (or cancel) a push started earlier")] = None,
        cancel: Annotated[bool, Field(description="With transfer_id: stop it")] = False,
        wait_seconds: Annotated[float, Field(ge=0, le=MAX_WAIT, description="How long to wait (≤25 s) before "
                                                                           "returning state='running' with the "
                                                                           "transfer_id")] = 20,
        udid: Udid = None,
    ) -> TransferStatus:
        """Copy files from this computer into an app's container on the iPhone (your own builds, or App
        Store apps with file sharing: Documents only). If any file already exists there, nothing is
        copied and state='needs_confirm' lists what would be replaced: ask the user, then repeat with
        confirm=true."""
        if transfer_id:
            return await _poll_transfer(ctx, transfer_id, cancel, wait_seconds)
        if not bundle_id or not sources:
            raise ToolError("Pass bundle_id and sources.")
        state = get_state(ctx)
        app = await resolve_app(state, bundle_id, udid)
        srcs = [str(_local_path(s, "source")) for s in sources]
        folder = path or "/Documents"
        args = {"bundleId": app["bundleId"], "srcs": srcs, "path": folder, "overwrite": confirm}
        if udid:
            args["udid"] = udid
        job = await _start(state, "container.push", "p", "container.push", args, timeout=3600,
                           meta={"tool": "ios_files_push", "dest": folder})
        await _wait(job, wait_seconds, ctx)
        if job.finished and _code(job.error) == "exists":
            detail = str(getattr(job.raw_error, "detail", None) or "")
            clash = [ln.strip() for ln in detail.splitlines() if ln.strip().startswith("/")]
            _jobs(state).pop(job.id, None)
            return TransferStatus(transfer_id=job.id, kind=job.kind, state="needs_confirm",
                                  elapsed_seconds=round(time.time() - job.started, 1), destination=folder,
                                  would=[f"Replace {c}" for c in clash[:B.LIST_CAP]] or ["Replace files that already exist in " + folder],
                                  error=_err_text(job.error),
                                  next_steps=["Ask the user whether to replace the existing files; only then repeat the "
                                              "call with confirm=true."])
        return _transfer_status(job)

    @tool(mcp, "Delete app files", kind="destructive", idempotent=True, open_world=True)
    async def ios_files_delete(
        ctx: Context,
        bundle_id: BundleId,
        paths: Annotated[list[str], Field(description="Files or folders in the container, e.g. ['/Documents/old.json']; "
                                                      "folders are deleted with everything in them")],
        confirm: Annotated[bool, Field(description="false = dry run. true only after the user agreed")] = False,
        udid: Udid = None,
    ) -> ActionResult:
        """Delete files or folders from an app's container on the iPhone. Cannot be undone. With
        confirm=false (default) it changes nothing and lists what would go; ask the user, then repeat
        with confirm=true."""
        state = get_state(ctx)
        app = await resolve_app(state, bundle_id, udid)
        bid = app["bundleId"]
        if not paths:
            raise ToolError("Pass the paths to delete (see ios_files_list).")
        if not confirm:
            would = []
            parents: dict[str, dict] = {}
            for p in paths:
                parent = posixpath.dirname(p.rstrip("/")) or "/"
                if parent not in parents:
                    try:
                        parents[parent] = {e["path"]: e for e in (await dcall(state, "container.list", {
                            "bundleId": bid, "path": parent}, udid=udid, timeout=60) or {}).get("entries") or []}
                    except ToolError:
                        parents[parent] = {}
                e = parents[parent].get(p.rstrip("/"))
                if e is None:
                    would.append(f"{p} (not found; nothing to delete)")
                elif e.get("type") == "dir":
                    would.append(f"Delete folder {p} and everything in it")
                else:
                    would.append(f"Delete {p} ({e.get('size') or 0} bytes)")
            return dry_run(f"This would delete {len(paths)} item(s) from {app.get('name')}'s container.", would,
                           {"bundle_id": bid})
        r = await dcall(state, "container.delete", {"bundleId": bid, "paths": paths}, udid=udid, timeout=120) or {}
        failed = r.get("failed") or []
        return ActionResult(done=not failed, message=f"Deleted {len(r.get('deleted') or [])} item(s) from {app.get('name')}."
                            + (f" {len(failed)} couldn't be deleted." if failed else ""),
                            data={"bundle_id": bid, "deleted": r.get("deleted") or [], "failed": failed})

    @tool(mcp, "Run camera test", kind="write", open_world=True)
    async def ios_camera_test(
        ctx: Context,
        bundle_id: Annotated[str | None, Field(description="Your app's bundle id, e.g. 'com.example.myapp'. Not needed "
                                                           "when polling")] = None,
        project_path: Annotated[str | None, Field(description="Instead of bundle_id: a project name or path; its "
                                                              "bundle id is used")] = None,
        inputs: Annotated[list[str] | None, Field(description="Test images/videos on this computer (inside your home "
                                                              "folder)")] = None,
        args: Annotated[list[str] | None, Field(description="Launch arguments; default ['-cameraTestMode', '1']")] = None,
        env: Annotated[dict[str, str] | None, Field(description="Environment variables for the app")] = None,
        timeout_s: Annotated[int, Field(ge=5, le=600, description="How long the app gets to write done.json")] = 60,
        test_id: Annotated[str | None, Field(description="Poll (or cancel) a test started earlier")] = None,
        cancel: Annotated[bool, Field(description="With test_id: stop it")] = False,
        wait_seconds: Annotated[float, Field(ge=0, le=MAX_WAIT, description="How long to wait (≤25 s) before "
                                                                           "returning state='running' with the "
                                                                           "test_id")] = 20,
        max_bytes: Annotated[int | None, Field(ge=1000, le=B.MAX_BYTES, description="Raise the size cap for text + "
                                                                                  "structuredContent together (default 12000 bytes)")] = None,
        udid: Udid = None,
    ) -> CameraTestResult:
        """Test your app's camera pipeline with files instead of the camera: copies the inputs to the
        app's Documents/CameraTestInput, relaunches it with -cameraTestMode, waits for
        Documents/CameraTestOutput/done.json (or the timeout), copies the outputs here, and reports a
        summary of done.json's results, the output files, key log lines and any new crash. Needs the
        test hook in the app (docs/camera-testing.md). Runs as a job: if state='running', call again
        with test_id."""
        state = get_state(ctx)
        if test_id:
            job = _get(state, test_id, "cameratest")
            if cancel:
                await _cancel(state, job)
            else:
                await _wait(job, wait_seconds, ctx)
            return await _camera_status(state, job, max_bytes)
        app = project_path or bundle_id
        if not app or not inputs:
            raise ToolError("Pass bundle_id (or project_path) and inputs (image/video files).")
        bid = app
        if project_path or "/" in app or app.startswith("~") or " " in app or "." not in app:
            proj = await resolve_project(state, app)
            bid = proj.get("bundleId") or ""
            if not bid:
                raise ToolError(f"{proj.get('name')} has no bundle id yet. Check ios_project_info.")
        a_ = await resolve_app(state, bid, udid)
        if not a_.get("developer"):
            raise ToolError(f"{a_.get('name')} isn't one of your development builds; camera tests need your own app "
                            "(ios_app_list(scope='own')). Build and install it with ios_run.")
        files = [_local_path(p, "test file") for p in inputs]
        if any(not f.is_file() for f in files):
            raise ToolError("inputs must be files (images or videos).")
        a: dict = {"bundleId": a_["bundleId"], "inputs": [str(f) for f in files], "timeout": timeout_s}
        if args is not None:
            a["args"] = args
        if env:
            a["env"] = env
        if udid:
            a["udid"] = udid
        job = await _start(state, "cameratest", "ct", "cameratest.run", a, timeout=timeout_s + 600,
                           meta={"bundle_id": a_["bundleId"], "app": a_.get("name")})
        await _wait(job, wait_seconds, ctx)
        return await _camera_status(state, job, max_bytes)
