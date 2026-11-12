"""Hot reload on the iPhone (docs/features/hot-reload.md): ios_hot_reload_set / _start / _status / _control.

    ios_hot_reload_set      `hotreload enable|disable`: the managed Package.swift block (diff; dry_run shows it only).
                            Off stops a running session first.
    ios_hot_reload_start    `hotreload run` as a coroutine job: build, install, launch, then watching. Returns once
                            watching (or failed), else state='running' + job_id.
    ios_hot_reload_status   `hotreload status`: a project's session (last reload with timings, or the error with
                            blockers and compiler errors); after_seq + wait_seconds waits for the next reload. Without
                            project_path: running sessions and the projects with hot reload on.
    ios_hot_reload_control  pause | resume | stop a session, also one started in the app window.

The session is a long-lived helper stream (helper/iosstudio/features/hotreload/session.py) whose `hotreload` events
are snapshots of its state, so the job keeps the newest one (like editor_debug.py's bridge). Cancelling the job
(ios_hot_reload_control(action='stop'), ios_job_cancel, server exit) sends {"cancel": true} on its stdin, then
SIGTERM: the session cancels its build, writes its `stopped` snapshot and ends. Status goes through the helper
(the session's state file, history included), so sessions the app started show the same way.

pause/resume/stop are one tool: all three are idempotent writes with the same annotations and the same result,
and one schema costs tools/list less than three. Reads stay in ios_hot_reload_status.
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
from typing import Annotated, Any, ClassVar, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from .. import helper
from ..backends.projects import resolve_project
from ..core import budget as B
from ..core import deadline
from ..core.errors import next_step, rehint
from ..core.jobs import Job, read_lines
from ..core.params import ProjectPath, Udid
from ..core.registry import MAX_WAIT, tool
from ..models import ErrorInfo, Out, iso
from ..state import AppState, get_state

log = logging.getLogger("iosstudio.hot_reload")

KIND = "hot-reload"
TOOL = "ios_hot_reload_start"
BLOCKERS = 10
DIAGNOSTICS = 5
FILES = 5
HISTORY = 5
HISTORY_MAX = 10        # what fits next to a failed reload's last_error in the default budget
HISTORY_CUT = 80        # characters of a history item's message (last_error has the newest failure's whole)
HISTORY_FILES = 2       # files per history item
STEPS = 10              # reload step timings kept (the helper sends 6)
SESSIONS = 10
PROJECTS = 20
MESSAGE_CUT = 300
STDERR_KEEP = 20
STOP_GRACE = 7.0        # after {"cancel": true}; + TERM_GRACE = 10 s, more than the session's whole shutdown (9 s)
TERM_GRACE = 3.0        # then after SIGTERM, before SIGKILL (jobs.cancel waits ~10 s + 5 s; the stop goes on, shielded)
STOP_TIMEOUT = 15       # `hotreload stop`: SIGTERM, ≤ 10 s for the session to end, then SIGKILL
CONTROL_TIMEOUT = 10    # `hotreload pause|resume`: ≤ 2 s for the session to show it

STARTING = ("starting", "building", "installing", "launching")
READY = ("watching", "paused", "reloading", "reloaded", "failed")

_RESTART = "ios_hot_reload_start(project_path='{p}', restart=true) builds and installs it again with the change."
HINTS = {
    "not-enabled": "ios_hot_reload_set(project_path='{p}', enabled=true) adds hot reload to Package.swift "
                   "(dry_run=true shows the diff), then ios_hot_reload_start(project_path='{p}').",
    "busy": "Hot reload already runs for this project (maybe from the iOS Studio window): ios_hot_reload_status("
            "project_path='{p}') shows it; ios_hot_reload_control(project_path='{p}', action='stop') ends it.",
    "not-running": "No session runs for this project: ios_hot_reload_start(project_path='{p}').",
    "unsupported-change": "The blockers list what a reload can't swap (new types or stored properties, enum cases, "
                          "class methods, a body returning another view type without .hotReloadable()). Undo that "
                          "part, or " + _RESTART,
    "new-file": "A reload can't add or remove Swift files. " + _RESTART,
    "removed-file": "A reload can't add or remove Swift files. " + _RESTART,
    "compile-failed": "Fix the errors at the file:line shown and save again (every reload has all changes since the "
                      "build).",
    "link-failed": _RESTART,
    "not-interposable": "The app on the iPhone was built without hot reload. " + _RESTART,
    "no-baseline": _RESTART,
    "load-failed": "The app couldn't load the change. " + _RESTART,
    "not-installed": "The app isn't on the iPhone anymore. " + _RESTART,
    "app-not-running": "Open the app (ios_app_launch(bundle_id)), then save again; if it still doesn't take, "
                       + _RESTART,
    "build-failed": "The first build failed: ios_build_diagnostics(project_path='{p}'), fix the errors, then "
                    "ios_hot_reload_start(project_path='{p}') again.",
    "sign-failed": "Check the Apple ID with ios_account_status (signing the change failed), then save again.",
    "not-signed-in": "No Apple ID is signed in: ask the user to sign in (ios_open_app(page='account')).",
    "missing-tool": "Call ios_toolchain_check, then ios_toolchain_install.",
    "no-bundle-id": "The project has no bundle ID: set it with ios_project_set, then start again.",
    # ios_hot_reload_set
    "manifest-invalid": "Package.swift was left as it is: fix its errors in the message, then retry.",
    "manifest-unsupported": "Package.swift was left as it is: it isn't `let package = Package(…)`, so the user "
                            "has to add hot reload by hand (docs: Hot reload).",
    "block-damaged": "Ask the user to restore or delete the hot reload lines at the end of Package.swift (the "
                     "message says how), then retry.",
    "no-app-target": "Package.swift has no app target to add hot reload to; ios_project_info shows the project.",
    "missing-runtime": "iOS Studio's hot reload files are missing: ask the user to update iOS Studio.",
    "changed": "Package.swift changed meanwhile: call ios_hot_reload_set again (dry_run=true for a new diff).",
    "package-busy": "Another Package.swift change of this project is running: wait a minute and retry.",
}


def _hint(code: str | None, path: str | None, fix: dict | None = None) -> str | None:
    p = helper.tilde(path) or "<project>"
    hints = {k: v.format(p=p) for k, v in HINTS.items()}
    return next_step(str(code or ""), fix, hints)


def _rehint(exc: BaseException, path: str | None, session_stopped: bool = False) -> ToolError:
    """The error with this feature's next step; session_stopped (ios_hot_reload_set off failed after stopping the
    session) says so first, since the hint alone reads as if nothing happened."""
    p = helper.tilde(path) or "<project>"
    err = rehint(exc, {k: v.format(p=p) for k, v in HINTS.items()})
    if not session_stopped:
        return err
    head, _, nxt = str(err).partition(" Next: ")
    return ToolError(f"{head} Next: The session was stopped, but Package.swift was left as it is. "
                     f"{nxt + ' ' if nxt else ''}ios_hot_reload_start(project_path='{p}') starts it again.")


def _same(a: str | None, b: str | None) -> bool:
    return bool(a and b) and os.path.realpath(os.path.expanduser(str(a))) == os.path.realpath(os.path.expanduser(str(b)))


# ------------------------------------------------------------------ models

class Blocker(Out):
    kind: str = Field(description="new-type, removed-type, stored-property, enum-case, class-method, generic-signature, "
                                  "opaque-type, type-alias, new-file, removed-file, unchecked, unparsed")
    location: str | None = Field(None, description="'Sources/App/File.swift:12'")
    type: str | None = None
    name: str | None = None
    message: str


class Diagnostic(Out):
    severity: str | None = None
    location: str | None = Field(None, description="'Sources/App/File.swift:12:5'")
    message: str


class ReloadError(Out):
    code: str
    message: str
    seq: int | None = Field(None, description="The reload that failed (none: a start problem)")
    location: str | None = None
    blockers: list[Blocker] = Field(default_factory=list, description="≤ 10: what the change does that can't be swapped")
    blockers_total: int | None = Field(None, description="Blockers found (more than listed: truncated)")
    diagnostics: list[Diagnostic] = Field(default_factory=list, description="≤ 5 compiler errors, errors first")
    diagnostics_total: int | None = None
    log_file: str | None = Field(None, description="The first build's log (build-failed): ios_build_log(log_file=...)")
    detail: str | None = None


class LastReload(Out):
    seq: int
    files: list[str] = Field(default_factory=list, description="≤ 5 files in this cumulative patch")
    file_count: int = 0
    ms: int | None = Field(None, description="Save to applied on the iPhone")
    steps: dict[str, float] = Field(default_factory=dict, description="ms per step: check, compile, link, sign, push, "
                                                                     "apply")
    swapped: int = Field(0, description="Functions now running the new code")
    reverted: int = Field(0, description="Functions of an earlier reload put back to the built code")
    not_swapped: int = 0
    time: str | None = None


class HistoryItem(Out):
    seq: int
    result: str = Field(description="reloaded, unchanged (back to the build) or failed")
    files: list[str] = Field(default_factory=list)
    ms: int | None = None
    swapped: int | None = None
    code: str | None = None
    message: str | None = None
    time: str | None = None


class Progress(Out):
    fraction: float | None = None
    step: str | None = Field(None, description="Reload step: check, compile, link, sign, push")
    detail: str | None = None


class SessionBrief(Out):
    project: str | None = None
    project_path: str | None = None
    state: str
    message: str | None = None
    running: bool = False
    paused: bool = False
    reloads: int = 0
    last_seq: int = 0
    bundle_id: str | None = None
    job_id: str | None = None
    started_by: str = Field("app", description="mcp (this server) | app (the iOS Studio window or another client)")


class ProjectMode(Out):
    project_path: str
    mode: str = Field(description="on, or stub (off, but files still import HotReload)")


class HotReloadSession(B.Budgeted, Out):
    state: str = Field(description="running (job starting) | starting | building | installing | launching | watching | "
                                   "paused | reloading | reloaded | failed (a reload failed; still watching) | stopped | "
                                   "off (no session) | overview (no project_path)")
    message: str | None = None
    job_id: str | None = Field(None, description="This server's session job: ios_hot_reload_start(job_id=...)")
    started_by: str | None = Field(None, description="mcp (this server) | app (the iOS Studio window or another client)")
    project: str | None = None
    project_path: str | None = None
    enabled: bool | None = Field(None, description="Hot reload is on in Package.swift")
    mode: str | None = Field(None, description="on | stub | off")
    running: bool = False
    bundle_id: str | None = None
    last_seq: int = Field(0, description="Newest finished reload: pass as after_seq to wait for the next one")
    reloads: int = Field(0, description="Reloads applied in this session")
    paused: bool = False
    pending: list[str] = Field(default_factory=list, description="Saved while paused or reloading (≤ 5)")
    pending_count: int = 0
    progress: Progress | None = None
    reload_result: str | None = Field(None, description="With after_seq: reloaded | unchanged | failed for the newest "
                                                        "reload after it; waiting (none finished yet) | stopped")
    last_reload: LastReload | None = None
    last_error: ReloadError | None = None
    notice: str | None = None
    stop_reason: str | None = Field(None, description="stopped, app-exited, build-failed, error, session-died")
    history: list[HistoryItem] = Field(default_factory=list, description="Newest first")
    history_total: int = 0
    log_file: str | None = Field(None, description="The session's first build log")
    error: ErrorInfo | None = Field(None, description="Why the session job failed")
    sessions: list[SessionBrief] = Field(default_factory=list, description="overview: running and recently stopped")
    enabled_projects: list[ProjectMode] = Field(default_factory=list, description="overview: projects with hot reload on")
    waited_seconds: float | None = None
    timed_out: bool = False
    next_steps: list[str] = Field(default_factory=list)


class HotReloadSetting(B.Budgeted, Out):
    BULK: ClassVar[tuple[str, ...]] = ("diff",)
    enabled: bool
    mode: str = Field(description="on | off | stub (off, but files still import HotReload: the block stays without "
                                  "its linker flags and the runtime is a no-op)")
    dry_run: bool = False
    changed: bool = False
    message: str
    project_path: str | None = None
    package_path: str | None = None
    app_target: str | None = None
    imported_by: list[str] = Field(default_factory=list, description="≤ 10 files that `import HotReload`")
    imported_by_count: int = 0
    runtime_updated: bool = False
    session_stopped: bool = False
    diff: str = Field("", description="Unified diff of Package.swift")
    next_steps: list[str] = Field(default_factory=list)


# ------------------------------------------------------------------ snapshot -> view

def _int(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _short(v: Any, n: int = 80) -> str | None:
    """A name-like field (kind, type, step) from the helper, clipped in case it isn't one."""
    return B.clip(v, n) if v not in (None, "") else None


def _loc(file: Any, line: Any = None, col: Any = None, root: str | None = None) -> str | None:
    if not file:
        return None
    out = B.rel_path(str(file), root)
    if line:
        out += f":{line}" + (f":{col}" if col else "")
    return out


def last_seq(snap: dict) -> int:
    """The newest finished reload's seq: history, lastReload/lastError, and patchSeq (it continues the previous
    session's, so a fresh session still waits for its own first reload) minus the one running now."""
    seqs = [_int(h.get("seq")) for h in snap.get("history") or [] if isinstance(h, dict)]
    for k in ("lastReload", "lastError"):
        if isinstance(snap.get(k), dict) and snap[k].get("seq"):
            seqs.append(_int(snap[k]["seq"]))
    ps = _int(snap.get("patchSeq"))
    seqs.append(ps - 1 if snap.get("state") == "reloading" else ps)
    return max(seqs, default=0)


def _error(e: Any, root: str | None) -> ReloadError | None:
    if not isinstance(e, dict):
        return None
    raw_b = [b for b in e.get("blockers") or [] if isinstance(b, dict)]
    raw_d = [d for d in e.get("diagnostics") or [] if isinstance(d, dict)]
    blockers = [Blocker(kind=_short(b.get("kind")) or "unparsed", location=_loc(b.get("file"), b.get("line"), root=root),
                        type=_short(b.get("type")), name=_short(b.get("name")),
                        message=B.clip(b.get("message"), MESSAGE_CUT)) for b in raw_b[:BLOCKERS]]
    diags = [Diagnostic(severity=_short(d.get("severity")), location=_loc(d.get("file"), d.get("line"), d.get("col"), root),
                        message=B.clip(" ".join(str(d.get("message") or "").split()), MESSAGE_CUT))
             for d in raw_d[:DIAGNOSTICS]]
    detail = None
    if e.get("detail") and not blockers and not diags:
        detail = B.clip(" ".join(str(e["detail"]).split()), MESSAGE_CUT)
    # the helper caps both lists too; blockersTotal / diagnosticsTotal (when sent) count what it found
    b_total = max(_int(e.get("blockersTotal")), len(raw_b))
    d_total = max(_int(e.get("diagnosticsTotal")), len(raw_d))
    return ReloadError(code=_short(e.get("code")) or "failed", message=B.clip(e.get("message") or "Failed.", MESSAGE_CUT),
                       seq=_int(e.get("seq")) or None, location=_loc(e.get("file"), e.get("line"), root=root),
                       blockers=blockers, blockers_total=b_total or None, diagnostics=diags,
                       diagnostics_total=d_total or None, log_file=helper.tilde(e.get("logFile")), detail=detail)


def _cut_note(err: ReloadError | None, path: str | None) -> str | None:
    """A note when the error lists fewer blockers or compiler errors than there are."""
    if err is None:
        return None
    cut = [f"{len(xs)} of {total} {what}" for xs, total, what in
           ((err.blockers, err.blockers_total, "blockers"), (err.diagnostics, err.diagnostics_total, "compiler errors"))
           if total and total > len(xs)]
    if not cut:
        return None
    p = helper.tilde(path) or "<project>"
    more = (f"ios_build_diagnostics(project_path='{p}') lists them all" if err.log_file else
            "fix these and save again; the next reload lists the rest")
    return f"last_error shows {' and '.join(cut)}: {more}."


def _reload(r: Any) -> LastReload | None:
    if not isinstance(r, dict) or not r.get("seq"):
        return None
    files = [str(f) for f in r.get("files") or []]
    raw = r.get("steps") if isinstance(r.get("steps"), dict) else {}
    steps = {B.clip(k, 20): round(float(v), 1) for k, v in list(raw.items())[:STEPS] if isinstance(v, (int, float))}
    return LastReload(seq=_int(r["seq"]), files=files[:FILES], file_count=_int(r.get("fileCount")) or len(files),
                      ms=r.get("ms"), steps=steps, swapped=_int(r.get("swapped")), reverted=_int(r.get("reverted")),
                      not_swapped=_int(r.get("notSwapped")), time=iso(r.get("time")))


def _history(h: dict) -> HistoryItem:
    result = "failed" if not h.get("ok") else "unchanged" if h.get("unchanged") else "reloaded"
    return HistoryItem(seq=_int(h.get("seq")), result=result, files=[str(f) for f in h.get("files") or []][:HISTORY_FILES],
                       ms=h.get("ms"), swapped=h.get("swapped"), code=_short(h.get("code")),
                       message=B.clip(h["message"], HISTORY_CUT) if h.get("message") else None, time=iso(h.get("time")))


def _progress(p: Any) -> Progress | None:
    if not isinstance(p, dict):
        return None
    frac = p.get("fraction")
    return Progress(fraction=round(float(frac), 3) if isinstance(frac, (int, float)) else None, step=p.get("step"),
                    detail=B.clip(p["detail"], 200) if p.get("detail") else None)


def to_view(snap: dict | None, *, path: str | None = None, running: bool | None = None, history: int = HISTORY,
            after_seq: int | None = None, **extra: Any) -> HotReloadSession:
    """A helper snapshot (stream event, state file or `status`) -> HotReloadSession, without next_steps."""
    if not snap:
        return HotReloadSession(state="off", project_path=helper.tilde(path), **extra)
    root = snap.get("path") or path
    st = str(snap.get("state") or "starting")
    hist = [h for h in snap.get("history") or [] if isinstance(h, dict)]
    hist.sort(key=lambda h: -_int(h.get("seq")))
    notice = snap.get("notice") if isinstance(snap.get("notice"), dict) else None
    v = HotReloadSession(
        state=st, message=B.clip(snap.get("message"), MESSAGE_CUT) if snap.get("message") else None,
        project=snap.get("project"), project_path=helper.tilde(root),
        running=(st != "stopped") if running is None else running, bundle_id=snap.get("bundleId"),
        last_seq=last_seq(snap), reloads=_int(snap.get("reloads")), paused=bool(snap.get("paused")),
        pending=[str(f) for f in snap.get("pending") or []][:FILES], pending_count=_int(snap.get("pendingCount")),
        progress=_progress(snap.get("progress")) if st != "stopped" else None,
        last_reload=_reload(snap.get("lastReload")), last_error=_error(snap.get("lastError"), root),
        notice=B.clip(notice.get("message"), MESSAGE_CUT) if notice and notice.get("message") else None,
        stop_reason=snap.get("stopReason") if st == "stopped" else None,
        history=[_history(h) for h in hist[:history]], history_total=len(hist), log_file=helper.tilde(snap.get("logFile")),
        **extra)
    note = _cut_note(v.last_error, root)
    if note:
        v.truncated, v.note = True, note
    if after_seq is not None:
        newer = [h for h in hist if _int(h.get("seq")) > after_seq]
        if newer:
            v.reload_result = _history(newer[0]).result
        else:
            v.reload_result = "waiting" if v.running else "stopped"
    return v


def _next_steps(v: HotReloadSession, path: str | None) -> list[str]:
    p = helper.tilde(path or v.project_path) or "<project>"
    out: list[str] = []
    wait = f"ios_hot_reload_status(project_path='{p}', after_seq={v.last_seq}, wait_seconds=20)"
    if v.notice:
        out.append(f"Notice: {v.notice}")
    if v.state == "off":
        out.append(f"No session. ios_hot_reload_start(project_path='{p}') starts one." if v.enabled is not False else
                   f"Hot reload is off for this project: ios_hot_reload_set(project_path='{p}', enabled=true), then "
                   f"ios_hot_reload_start(project_path='{p}').")
        return out
    if v.error or v.state == "stopped":
        err = v.last_error
        code = (v.error.code if v.error else None) or (err.code if err else None)
        if code and v.stop_reason != "stopped":
            hint = _hint(code, p)
            if hint:
                out.append(hint)
        if v.stop_reason == "app-exited":
            out.append(f"The app left the iPhone, so the session ended. ios_hot_reload_start(project_path='{p}').")
        else:
            out.append(f"Stopped ({v.stop_reason or (v.error.code if v.error else 'stopped')}). "
                       f"ios_hot_reload_start(project_path='{p}') starts it again.")
        return out
    if v.state in STARTING or v.state == "running":
        if v.job_id:
            out.append(f"Starting (the first build can take minutes): call ios_hot_reload_start(job_id='{v.job_id}') "
                       "again; it answers once it is watching.")
        else:
            out.append(f"Starting: ios_hot_reload_status(project_path='{p}', wait_seconds=20) shows its progress.")
        if v.last_error and v.last_error.code == "app-not-running":
            out.append(_hint("app-not-running", p) or "")
        out.append(f"ios_hot_reload_control(project_path='{p}', action='stop') cancels it.")
        return out
    if v.reload_result == "waiting":
        out.append(f"No reload finished yet{' (paused)' if v.paused else ''}: call {wait} again.")
    if v.state == "failed" and v.last_error:
        out.append(f"Reload {v.last_error.seq or ''} failed [{v.last_error.code}]: "
                   + (_hint(v.last_error.code, p) or "read the message, fix and save again."))
    elif v.last_error and v.last_error.code == "app-not-running":
        out.append(_hint("app-not-running", p) or "")
    if v.paused:
        out.append(f"Paused: saves are collected (pending) but not applied. ios_hot_reload_control(project_path='{p}', "
                   "action='resume') applies them in one reload.")
    elif v.state == "reloading":
        out.append(f"A reload is running: {wait} returns its result.")
    else:
        out.append(f"Edit and save Swift files under Sources/: each save reloads on the iPhone in seconds. Then {wait} "
                   "returns that reload's result; check the screen with ios_screenshot or ios_ui_tree.")
    if v.state in ("watching", "reloaded") and not v.reloads:
        out.append("A view redraws after a reload only with `import HotReload`, `@ObserveHotReload var reload` and "
                   "`.hotReloadable()` at the end of its body.")
    out.append("New files, types or stored properties, Package.swift and resources need a build: "
               f"ios_hot_reload_start(project_path='{p}', restart=true). Pause, resume or stop: "
               "ios_hot_reload_control.")
    return [s for s in out if s][:6]


# ------------------------------------------------------------------ the session job

@dataclass
class Live:
    """What the `hotreload run` stream said so far; kept in job.data['live']."""
    path: str
    proc: asyncio.subprocess.Process | None = None
    snap: dict | None = None
    done: dict | None = None
    error: dict | None = None
    ended: bool = False
    stopper: asyncio.Task | None = None
    seq: int = 0
    stderr: list = field(default_factory=list)
    changed: asyncio.Event = field(default_factory=asyncio.Event)

    def bump(self) -> None:
        self.seq += 1
        self.changed.set()

    def ready(self) -> bool:
        return self.ended or str((self.snap or {}).get("state")) in READY

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


def _live(job: Job) -> Live:
    return job.data["live"]


def _apply(job: Job, s: Live, ev: dict) -> None:
    name = ev.get("event")
    if name == "hotreload":
        s.snap = {k: v for k, v in ev.items() if k != "event"}
        prog = ev.get("progress") if isinstance(ev.get("progress"), dict) else {}
        job.report(prog.get("fraction") if isinstance(prog.get("fraction"), (int, float)) else None,
                   str(ev.get("message") or ev.get("state") or ""))
    elif name == "done":
        s.done = ev
        if isinstance(ev.get("data"), dict):
            s.snap = ev["data"]
        if not ev.get("ok"):
            e = ev.get("error")
            s.error = e if isinstance(e, dict) else {"code": "failed", "message": str(e or "Hot reload stopped.")}
    s.bump()


async def _drain(s: Live, stream: asyncio.StreamReader) -> None:
    async for raw in read_lines(stream):
        text = raw.decode(errors="replace").rstrip()
        if text:
            s.stderr = (s.stderr + [B.clip(text, 300)])[-STDERR_KEEP:]
            log.debug("hot reload stderr: %s", text)


async def _stop_proc(s: Live, grace: float = STOP_GRACE) -> None:
    """{"cancel": true} (the session cancels its build and ends with `done`), then SIGTERM, then SIGKILL."""
    proc = s.proc
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
    s = _live(job)
    s.proc = proc = await helper.spawn_stream("hotreload", "run", flags)
    assert proc.stdout and proc.stderr
    err_task = asyncio.create_task(_drain(s, proc.stderr))
    try:
        async for raw in read_lines(proc.stdout):
            with contextlib.suppress(ValueError):
                ev = json.loads(raw)
                if isinstance(ev, dict):
                    _apply(job, s, ev)
        await proc.wait()
    except asyncio.CancelledError:          # the server is stopping (or the job was cancelled hard)
        await asyncio.shield(_stopping(s))
        raise
    finally:
        with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(err_task, 2)
        if proc.returncode is None:
            helper.kill_group(proc.pid)
        s.ended = True
        s.bump()
    if s.done is None:
        why = f" ({s.stderr[-1]})" if s.stderr else ""
        raise ToolError(f"Hot reload stopped without a result (exit {proc.returncode}){why}.")
    if s.error:
        raise helper.HelperFailure(s.error)
    return s.snap or {}


def _stopping(s: Live) -> asyncio.Task:
    """One stop per session, so a cancel cut short by its caller's timeout keeps going (and isn't restarted)."""
    if s.stopper is None:
        s.stopper = asyncio.ensure_future(_stop_proc(s))
    return s.stopper


async def _on_cancel(job: Job) -> None:
    await asyncio.shield(_stopping(_live(job)))


def _jobs(state: AppState) -> list[Job]:
    return [j for j in state.jobs.running(KIND) if "live" in j.data]


def _job_for(state: AppState, path: str | None) -> Job | None:
    return next((j for j in _jobs(state) if _same(_live(j).path, path)), None)


async def _settle(job: Job) -> None:
    """The stream ended (or said `stopped`): give the job a moment to record its final state."""
    task = job.task
    s = _live(job)
    if (s.ended or (s.snap or {}).get("state") == "stopped") and task is not None and not task.done():
        await asyncio.wait({task}, timeout=deadline.wait_time(3))


def job_view(job: Job, message: str | None = None) -> HotReloadSession:
    s = _live(job)
    snap = s.snap or {}
    running = not job.finished and not s.ended and snap.get("state") != "stopped"
    v = to_view(snap or {"state": "starting", "path": s.path}, path=s.path, running=running, job_id=job.id,
                started_by="mcp")
    if not snap and not job.finished:
        v.state = "running"
    if job.finished and job.state == "failed":
        err = s.error or job.error or {"code": "failed", "message": "Hot reload failed."}
        v.error = ErrorInfo(code=err.get("code"), message=B.clip_error(str(err.get("message") or "Failed.")))
        v.state = "stopped"
        v.stop_reason = v.stop_reason or "error"
    elif job.finished and v.state != "stopped":
        v.state, v.stop_reason = "stopped", v.stop_reason or "stopped"
    if message:
        v.message = message
    v.next_steps = _next_steps(v, s.path)
    return v.with_budget(None)


def status_view(data: dict, path: str | None, state: AppState, *, history: int = HISTORY,
                after_seq: int | None = None, message: str | None = None) -> HotReloadSession:
    """`hotreload status --project` -> HotReloadSession (marked as this server's when one of our jobs runs it)."""
    snap = data.get("session") if isinstance(data.get("session"), dict) else None
    mine = _job_for(state, path or data.get("project"))
    v = to_view(snap, path=path or data.get("project"), running=bool(snap.get("running")) if snap else False,
                history=history, after_seq=after_seq, enabled=data.get("enabled"), mode=data.get("mode"),
                job_id=mine.id if mine else None,
                started_by=("mcp" if mine else "app") if snap else None,
                waited_seconds=data.get("waited"), timed_out=bool(data.get("timedOut")))
    if message:
        v.message = message
    v.next_steps = _next_steps(v, path)
    return v.with_budget(None)


def _overview(data: dict, state: AppState) -> HotReloadSession:
    sessions = []
    for snap in data.get("sessions") or []:
        if not isinstance(snap, dict):
            continue
        mine = _job_for(state, snap.get("path"))
        sessions.append(SessionBrief(
            project=snap.get("project"), project_path=helper.tilde(snap.get("path")), state=str(snap.get("state")),
            message=B.clip(snap.get("message"), 200) if snap.get("message") else None,
            running=bool(snap.get("running")), paused=bool(snap.get("paused")), reloads=_int(snap.get("reloads")),
            last_seq=last_seq(snap), bundle_id=snap.get("bundleId"), job_id=mine.id if mine else None,
            started_by="mcp" if mine else "app"))
    projects = data.get("projects") if isinstance(data.get("projects"), dict) else {}
    on = [ProjectMode(project_path=helper.tilde(p) or p, mode=str(m.get("mode")))
          for p, m in projects.items() if isinstance(m, dict) and m.get("mode") in ("on", "stub")]
    running = [x for x in sessions if x.running]
    nxt = []
    if running:
        nxt.append("ios_hot_reload_status(project_path=...) shows a session's last reload or error.")
    if on:
        nxt.append("ios_hot_reload_start(project_path=...) starts a session for a project with mode 'on'.")
    else:
        nxt.append("No project has hot reload on: ios_hot_reload_set(project_path, enabled=true).")
    omitted = max(0, len(sessions) - SESSIONS) + max(0, len(on) - PROJECTS)
    return HotReloadSession(
        state="overview", running=bool(running), sessions=sessions[:SESSIONS], enabled_projects=on[:PROJECTS],
        truncated=bool(omitted), omitted=omitted, note="Lists cut; pass project_path for one project." if omitted else None,
        message=f"{len(running)} session(s) running; hot reload on in {len(on)} project(s).",
        next_steps=nxt).with_budget(None)


# ------------------------------------------------------------------ tools

Preset = Annotated[str | None, Field(description="Run preset name (ios_project_presets); default: the one selected in "
                                                  "the app")]


def register(mcp: MCPServer) -> None:

    @tool(mcp, "Turn hot reload on or off", kind="write", idempotent=True)
    async def ios_hot_reload_set(
        ctx: Context,
        project_path: ProjectPath,
        enabled: bool,
        dry_run: Annotated[bool, Field(description="Only return the Package.swift diff")] = False,
    ) -> HotReloadSetting:
        """Turn hot reload on or off for a project: a marked block at the end of Package.swift plus the runtime in
        .iosstudio/. Returns the diff. Off stops a running session; files that still `import HotReload` keep
        building (mode 'stub')."""
        state = get_state(ctx)
        p = await resolve_project(state, project_path)
        path = str(p["path"])
        stopped = False
        if not enabled and not dry_run:
            mine = _job_for(state, path)
            if mine:
                await state.jobs.cancel(mine)
                stopped = True
        flags = {"project": path, "dry_run": dry_run, "max_bytes": int(B.room(None) * 0.9)}
        try:
            d = await helper.call("hotreload", "enable" if enabled else "disable", flags, timeout=90) or {}
        except ToolError as exc:     # off stops the session before it checks Package.swift (on purpose)
            err = getattr(exc, "error", None)
            helper_stopped = isinstance(err, dict) and bool(err.get("sessionStopped"))
            raise _rehint(exc, path, session_stopped=stopped or helper_stopped) from None
        users = [str(u) for u in d.get("importedBy") or []]
        mode = str(d.get("mode") or ("on" if enabled else "off"))
        changed = bool(d.get("changed"))
        tp = helper.tilde(path)
        nxt: list[str] = []
        if dry_run:
            msg = ("Dry run: nothing was written. " + ("The diff shows the change to Package.swift." if changed
                                                       else "Nothing would change."))
            if changed:
                nxt.append("Show the user the diff; then call again without dry_run.")
        elif enabled:
            msg = (f"Hot reload is on for {p.get('name') or tp} (target {d.get('appTarget')})." if changed else
                   "Hot reload was already on.")
            nxt += [f"ios_hot_reload_start(project_path='{tp}') builds, installs and launches the app, then reloads "
                    "every save.",
                    "In each SwiftUI view: `import HotReload`, `@ObserveHotReload var reload` and `.hotReloadable()` "
                    "at the end of `body`, or a reload only shows when the view redraws.",
                    "Turn hot reload off before a release or TestFlight build (its linker flags apply there too)."]
        elif mode == "stub":
            msg = (f"Hot reload is off. {len(users)} file(s) still `import HotReload`, so the block stays without its "
                   "linker flags and the runtime is a no-op stub: the app builds as before.")
            nxt.append("Remove those imports (and @ObserveHotReload / .hotReloadable()), then turn it off again to "
                       "remove the block entirely.")
        else:
            msg = "Hot reload is off: the block and .iosstudio/hot-reload were removed." if changed else \
                "Hot reload was already off."
        if not dry_run and not enabled and (stopped or d.get("sessionStopped")):
            msg += " The running session was stopped."
        r = HotReloadSetting(
            enabled=bool(d.get("enabled", enabled)), mode=mode, dry_run=dry_run, changed=changed, message=msg,
            project_path=tp, package_path=helper.tilde(d.get("packagePath")), app_target=d.get("appTarget"),
            imported_by=users[:10], imported_by_count=len(users), runtime_updated=bool(d.get("runtimeUpdated")),
            session_stopped=stopped or bool(d.get("sessionStopped")), diff=str(d.get("diff") or ""), next_steps=nxt)
        if d.get("truncated"):
            r.truncated = True
            r.full_output_path = helper.tilde(d.get("fullDiffPath"))
            r.note = "The diff was cut; the whole diff is in full_output_path."
        return r.with_budget(None)

    @tool(mcp, "Start hot reload", kind="write", open_world=True)
    async def ios_hot_reload_start(
        ctx: Context,
        project_path: Annotated[str | None, Field(description="Project folder or name (not with job_id)")] = None,
        udid: Udid = None,
        preset: Preset = None,
        restart: Annotated[bool, Field(description="Stop a running session, build again")] = False,
        job_id: Annotated[str | None, Field(description="From an earlier call")] = None,
    ) -> HotReloadSession:
        """Build, install and launch the app, then swap each saved Swift change into it on the iPhone in
        seconds. Needs ios_hot_reload_set(enabled=true). A job: returns once watching, else state='running'
        (call again with job_id)."""
        state = get_state(ctx)
        if job_id:
            job = state.jobs.get(job_id, KIND)
            s = _live(job)
            await s.wait(s.ready, MAX_WAIT)
            await _settle(job)
            return job_view(job)
        if not project_path:
            raise ToolError("Pass project_path (ios_project_list lists projects), or the job_id of a session this "
                            "server started.")
        p = await resolve_project(state, project_path)
        path = str(p["path"])
        mine = _job_for(state, path)
        if mine and not restart:
            return job_view(mine, message="Hot reload already runs for this project; this is its state.")
        if mine:
            await state.jobs.cancel(mine)
        try:
            st = await helper.call("hotreload", "status", {"project": path}) or {}
            snap = st.get("session") if isinstance(st.get("session"), dict) else None
            if snap and snap.get("running"):
                if not restart:
                    return status_view(st, path, state, message="Hot reload already runs for this project (started "
                                       "in the iOS Studio window or by another client); this is its state.")
                await helper.call("hotreload", "stop", {"project": path}, timeout=STOP_TIMEOUT)
        except ToolError as exc:
            raise _rehint(exc, path) from None
        if not st.get("enabled"):
            raise ToolError("Hot reload is off for this project [not-enabled]. Next: " + (_hint("not-enabled", path) or ""))
        if preset:      # an unknown name fails here, listing the project's presets
            await helper.call("projects", "presets", {"path": path, "resolve": preset})
        flags: dict[str, Any] = {"project": path, "udid": udid, "preset": preset}
        s = Live(path=path)
        job = state.jobs.run(lambda j: _run(j, flags), KIND, title=f"Hot reload {p.get('name') or os.path.basename(path)}",
                             prefix="hr", tool=TOOL, project_path=path, project=p.get("name"), data={"live": s},
                             on_cancel=_on_cancel)
        await s.wait(s.ready, MAX_WAIT)
        await _settle(job)
        return job_view(job)

    @tool(mcp, "Hot reload status", kind="read")
    async def ios_hot_reload_status(
        ctx: Context,
        project_path: Annotated[str | None, Field(description="Omit: all sessions")] = None,
        after_seq: Annotated[int | None, Field(ge=0, description="last_seq of an earlier result")] = None,
        wait_seconds: Annotated[int, Field(ge=0, le=MAX_WAIT, description="Wait for a reload after after_seq")] = 0,
        history: Annotated[int, Field(ge=0, le=HISTORY_MAX, description="Reloads listed")] = HISTORY,
    ) -> HotReloadSession:
        """A project's hot reload session: state, the last reload (timings) or why it failed (blockers, compiler
        errors, the fix). After saving a file, pass after_seq=last_seq with wait_seconds=20 to get that save's
        result (reload_result)."""
        state = get_state(ctx)
        if not project_path:
            return _overview(await helper.call("hotreload", "status", {}) or {}, state)
        path = str((await resolve_project(state, project_path))["path"])
        flags: dict[str, Any] = {"project": path}
        wait = int(max(0.0, deadline.wait_time(wait_seconds + 3) - 3)) if wait_seconds else 0
        if wait:
            flags["wait_seconds"] = wait
        if after_seq is not None:
            flags["after_seq"] = after_seq
        data = await helper.call("hotreload", "status", flags, timeout=wait + 15) or {}
        return status_view(data, path, state, history=history, after_seq=after_seq)

    @tool(mcp, "Pause, resume or stop hot reload", kind="write", idempotent=True)
    async def ios_hot_reload_control(
        ctx: Context,
        project_path: ProjectPath,
        action: Annotated[Literal["pause", "resume", "stop"],
                          Field(description="pause: keep saves until resume; stop: end the session")],
    ) -> HotReloadSession:
        """Pause, resume or stop a project's hot reload session (also one started in the app window). Paused,
        saves are collected and resume applies them in one reload."""
        state = get_state(ctx)
        path = str((await resolve_project(state, project_path))["path"])
        if action == "stop":
            mine = _job_for(state, path)
            if mine:
                await state.jobs.cancel(mine)
                await _settle(mine)
                return job_view(mine, message="Hot reload stopped.")
            try:
                d = await helper.call("hotreload", "stop", {"project": path}, timeout=STOP_TIMEOUT) or {}
            except ToolError as exc:
                raise _rehint(exc, path) from None
            msg = "Hot reload stopped." if d.get("stopped") else "Hot reload wasn't running for this project."
            return status_view({"session": d.get("session")}, path, state, message=msg)
        try:
            d = await helper.call("hotreload", action, {"project": path}, timeout=CONTROL_TIMEOUT) or {}
        except ToolError as exc:
            raise _rehint(exc, path) from None
        msg = "Paused: saves wait until resume." if d.get("paused") else "Resumed: saves reload again."
        return status_view({"session": d.get("session")}, path, state, message=msg)
