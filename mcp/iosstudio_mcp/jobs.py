"""Background jobs: long helper streams (builds, toolchain installs, renewals) that outlive one tool call.

A job owns one `iosstudio-helper` process in its own process group and folds its JSON-lines events
(build, step, progress, log, notice, diagnostic, prompt, done) into a bounded record. Tools read it
with `wait()` (long-poll), answer prompts with `respond()`, and stop it with `cancel()`.
`shutdown()` kills every running job; the lifespan calls it when the server exits.

A job may carry a `finisher` coroutine that runs after the helper reports success and before the job
is marked finished (ios_run uses it to install and launch the app through the device client). It runs as
its own task: cancel() cancels it, and a finisher calls job.check_cancel() between its steps. A job
reaches a terminal state only after its helper and its finisher have both stopped.

Malformed helper output never kills the pump: unknown or ill-typed fields are ignored, an over-long line
is skipped, and any other error fails the job with a message and stops the helper.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
import signal
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from mcp.server.mcpserver.exceptions import ToolError

from . import helper

log = logging.getLogger("iosstudio.jobs")

MAX_LOG_LINES = 20_000       # ring buffer; older lines stay in the helper's log file
MAX_LINE_CHARS = 4_000
MAX_DIAGNOSTICS = 1_000
MAX_FINISHED_JOBS = 50
FINISHED_LINES = 2_000       # a finished job keeps only its newest lines in memory (the rest: log file)
CANCEL_SETTLE = 15.0         # seconds cancel() waits for the helper and finisher to stop
TERMINAL = ("succeeded", "failed", "cancelled")

Finisher = Callable[["Job"], Awaitable[None]]


class JobCancelled(Exception):
    """Raised by Job.check_cancel() inside a finisher once the job was asked to stop."""


@dataclass
class Job:
    id: str
    kind: str                       # build | toolchain-install | renew
    action: str                     # compile | ipa | install | run | renew | all | <setup step>
    title: str
    project_path: str | None = None
    project: str | None = None
    state: str = "running"          # running | needs_input | succeeded | failed | cancelled
    started: float = field(default_factory=time.time)
    ended: float | None = None
    info: dict = field(default_factory=dict)         # the helper's "build" event (logFile, bundleId, plan…)
    steps: dict[str, dict] = field(default_factory=dict)
    current_step: str | None = None
    progress: float | None = None
    progress_detail: str = ""
    eta: float | None = None
    lines: deque = field(default_factory=lambda: deque(maxlen=MAX_LOG_LINES))
    line_count: int = 0             # total lines ever logged (line numbers are 0-based and stable)
    diagnostics: list[dict] = field(default_factory=list)
    notices: list[dict] = field(default_factory=list)
    pending_prompt: dict | None = None
    answered: list[dict] = field(default_factory=list)
    result: dict | None = None
    error: dict | None = None
    next_steps: list[str] = field(default_factory=list)
    finisher: Finisher | None = None
    proc: asyncio.subprocess.Process | None = None
    _changed: asyncio.Event = field(default_factory=asyncio.Event)
    _progress_seq: int = 0
    cancel_requested: bool = False
    poll_mark: tuple[int, int, int] | None = None   # (line_count, errors, warnings) at the last status
    _task: asyncio.Task | None = None
    _finisher_task: asyncio.Task | None = None

    # ---------------------------------------------------------------- derived
    @property
    def finished(self) -> bool:
        return self.state in TERMINAL

    @property
    def elapsed(self) -> float:
        return round((self.ended or time.time()) - self.started, 1)

    @property
    def first_line(self) -> int:
        return self.line_count - len(self.lines)

    def counts(self) -> tuple[int, int]:
        e = sum(1 for d in self.diagnostics if d.get("severity") == "error")
        w = sum(1 for d in self.diagnostics if d.get("severity") == "warning")
        return e, w

    def add_line(self, text: str) -> None:
        for ln in str(text).splitlines() or [""]:
            self.lines.append(ln[:MAX_LINE_CHARS])
            self.line_count += 1

    def check_cancel(self) -> None:
        """For finishers: stop between steps once cancel() was called."""
        if self.cancel_requested:
            raise JobCancelled()

    def touch(self) -> None:
        self._progress_seq += 1
        self._changed.set()

    def set_step(self, step_id: str, title: str | None, state: str, **extra: Any) -> None:
        st = self.steps.setdefault(step_id, {"id": step_id, "title": title or step_id})
        if title:
            st["title"] = title
        st["state"] = state
        for k in ("detail", "owner", "reason", "error"):
            if k in extra and extra[k] is not None:
                st[k] = extra[k]
        if state == "running":
            self.current_step = step_id


class JobManager:
    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self._ids = itertools.count(1)

    # ---------------------------------------------------------------- lookup
    def get(self, job_id: str) -> Job:
        job = self.jobs.get(job_id.strip())
        if job is None:
            known = ", ".join(list(self.jobs)[-5:]) or "none yet"
            raise ToolError(f"Unknown build_id '{job_id}'. Jobs live only while this MCP server runs (recent: "
                            f"{known}). For older builds use ios_build_history, ios_build_log(project_path=...) "
                            "or ios_build_diagnostics(project_path=...).")
        return job

    def running_for(self, project_path: str) -> Job | None:
        for j in self.jobs.values():
            if j.project_path == project_path and not j.finished:
                return j
        return None

    def latest_for(self, project_path: str) -> Job | None:
        for j in reversed(list(self.jobs.values())):
            if j.project_path == project_path:
                return j
        return None

    # ---------------------------------------------------------------- start
    async def start(self, kind: str, group: str, action: str, flags: dict, *, title: str,
                    prefix: str = "b", project_path: str | None = None, project: str | None = None,
                    finisher: Finisher | None = None) -> Job:
        self._prune()
        job = Job(id=f"{prefix}-{next(self._ids)}", kind=kind, action=action, title=title,
                  project_path=project_path, project=project, finisher=finisher)
        job.proc = await helper.spawn_stream(group, action, flags)
        self.jobs[job.id] = job
        job._task = asyncio.create_task(self._pump(job), name=f"job-{job.id}")
        return job

    def _prune(self) -> None:
        done = [j for j in self.jobs.values() if j.finished]
        for j in done[:-MAX_FINISHED_JOBS]:
            self.jobs.pop(j.id, None)

    # ---------------------------------------------------------------- event pump
    async def _pump(self, job: Job) -> None:
        proc = job.proc
        assert proc and proc.stdout and proc.stderr
        done_seen = False

        async def read_stderr() -> None:
            async for raw in read_lines(proc.stderr):
                text = raw.decode(errors="replace").rstrip()
                if text:
                    job.add_line(f"[helper stderr] {text}")
                    log.debug("%s stderr: %s", job.id, text)

        err_task = asyncio.create_task(read_stderr())
        try:
            async for raw in read_lines(proc.stdout):
                line = raw.decode(errors="replace").strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    job.add_line(line)
                    job.touch()
                    continue
                if not isinstance(ev, dict):
                    continue
                if self._apply(job, ev):
                    done_seen = True
                job.touch()
            await proc.wait()
            await err_task
            if not done_seen and not job.finished:
                code = proc.returncode
                if job.cancel_requested:
                    job.error = {"code": "cancelled", "message": "Cancelled."}
                    self._finish(job, "cancelled")
                elif job.state != "cancelled":
                    job.error = {"code": "helper-exited", "message": f"The helper stopped without a result (exit {code})."}
                    self._finish(job, "failed")
            elif job.result is not None and job.state == "running" and job.finisher:
                if job.cancel_requested:
                    job.error = {"code": "cancelled", "message": "Cancelled after the build, before installing."}
                    self._finish(job, "cancelled")
                else:
                    await self._run_finisher(job)
            elif job.result is not None and job.state == "running":
                self._finish(job, "succeeded")
        except asyncio.CancelledError:
            if job._finisher_task and not job._finisher_task.done():
                job._finisher_task.cancel()
            await self._kill(job, graceful=False)
            raise
        except Exception as exc:  # noqa: BLE001  never leave a job half-alive with its helper still running
            log.exception("reading the helper's output failed for %s", job.id)
            if not job.finished:
                job.error = {"code": "internal", "message": f"The server couldn't read the helper's output "
                                                            f"({type(exc).__name__}: {exc}); the job was stopped."}
            await self._kill(job, graceful=False)
            self._finish(job, "failed")
        finally:
            if not err_task.done():
                err_task.cancel()
            if not job.finished:
                self._finish(job, "cancelled" if job.cancel_requested else "failed")
            if job.proc is not None and job.proc.returncode is not None:
                job.proc = None      # reaped: drop the transports with it

    async def _run_finisher(self, job: Job) -> None:
        """Run job.finisher as its own task (so cancel() can stop it) and settle the job from its outcome."""
        assert job.finisher
        ft = asyncio.create_task(job.finisher(job), name=f"job-{job.id}-finish")
        job._finisher_task = ft
        try:
            await asyncio.wait({ft})
        except asyncio.CancelledError:
            ft.cancel()
            raise
        exc = None if ft.cancelled() else ft.exception()
        if ft.cancelled() or isinstance(exc, JobCancelled):
            step = next((sid for sid, st in job.steps.items() if st.get("state") == "running"), None)
            for st in job.steps.values():
                if st.get("state") in ("running", "pending"):
                    st["state"] = "cancelled"
            msg = "Cancelled after the build."
            if step == "install":
                msg = ("Cancelled while installing: the iPhone may still finish installing this build, but the app "
                       "won't be launched.")
            elif step == "launch":
                msg = "Cancelled while launching; the app is installed."
            job.error = {"code": "cancelled", "message": msg}
            self._finish(job, "cancelled")
        elif isinstance(exc, ToolError):
            job.error = {"code": "post-build", "message": str(exc)}
            self._finish(job, "failed")
        elif exc is not None:
            log.error("finisher failed for %s", job.id, exc_info=exc)
            job.error = {"code": "post-build", "message": f"Installing after the build failed: {exc}"}
            self._finish(job, "failed")
        elif not job.finished:
            self._finish(job, "succeeded")

    def _apply(self, job: Job, ev: dict) -> bool:
        """Fold one event into the job. Returns True on the final `done` event."""
        name = ev.get("event")
        if name == "build":
            job.info = {k: v for k, v in ev.items() if k != "event"}
            job.project = job.project or ev.get("project")
        elif name == "step":
            err = ev.get("error")
            job.set_step(str(ev.get("id")), _text(ev.get("title")), str(ev.get("state") or ""),
                         detail=_text(ev.get("detail")), owner=_text(ev.get("owner")), reason=_text(ev.get("reason")),
                         error=_text(err.get("message") if isinstance(err, dict) else err))
        elif name == "progress":
            frac = _number(ev.get("fraction"))
            if frac is not None:
                job.progress = min(1.0, max(0.0, frac))
            job.progress_detail = _text(ev.get("detail")) or job.progress_detail
            job.eta = _number(ev.get("eta"))
        elif name == "log":
            job.add_line(str(ev.get("text", "")))
        elif name == "notice":
            job.notices.append({k: ev.get(k) for k in ("id", "level", "message") if ev.get(k) is not None})
            job.notices = job.notices[-20:]
            job.add_line(f"[notice] {ev.get('message', '')}")
        elif name == "diagnostic":
            if len(job.diagnostics) < MAX_DIAGNOSTICS:
                job.diagnostics.append({k: v for k, v in ev.items() if k != "event"})
        elif name == "prompt":
            if not isinstance(ev.get("options", []), list):
                ev["options"] = []
            job.pending_prompt = {k: v for k, v in ev.items() if k != "event"}
            job.state = "needs_input"
            job.add_line(f"[prompt {ev.get('id')}] {ev.get('title') or ''} {ev.get('message', '')}".strip())
        elif name == "renew-plan":
            job.info.setdefault("renewPlan", ev.get("projects"))
        elif name == "done":
            job.pending_prompt = None
            if ev.get("ok"):
                data = ev.get("data")
                job.result = data if isinstance(data, dict) else {}
                if job.state == "needs_input":
                    job.state = "running"
                if not job.finisher:
                    self._finish(job, "succeeded")
            else:
                err = ev.get("error")
                if not isinstance(err, dict):   # a bare string (or nothing): keep it as the message
                    err = {"code": "failed", "message": _text(err) or "The job failed."}
                build = err.pop("build", None)
                job.error = err
                if isinstance(build, dict):
                    job.result = build
                    diags = build.get("diagnostics")
                    for d in diags if isinstance(diags, list) else []:
                        if isinstance(d, dict) and d not in job.diagnostics and len(job.diagnostics) < MAX_DIAGNOSTICS:
                            job.diagnostics.append(d)
                self._finish(job, "cancelled" if err.get("code") == "cancelled" else "failed")
            return True
        return False

    def _finish(self, job: Job, state: str) -> None:
        if job.finished:
            return
        job.state = state
        job.ended = time.time()
        job.pending_prompt = None
        if job.proc and job.proc.stdin and not job.proc.stdin.is_closing():
            try:
                job.proc.stdin.close()
            except Exception:  # noqa: BLE001
                pass
        log.info("job %s %s in %.1fs", job.id, state, job.elapsed)
        self._release(job)
        job.touch()

    def _release(self, job: Job) -> None:
        """Shrink a finished job to what status and log calls still need (50 are kept): the newest
        FINISHED_LINES lines (ios_build_log reads older ones from the log file; a job without one gets its
        full output spilled to a file first) and no finisher. The process handle stays until the pump has
        reaped it."""
        if len(job.lines) > FINISHED_LINES:
            if not job.info.get("logFile") and not job.info.get("spillFile"):
                try:
                    from . import budget as B
                    path, _, _ = B.spill("\n".join(job.lines), "build-logs", f"{job.id}.log")
                    job.info["spillFile"] = path
                    job.info["spillFirstLine"] = job.first_line
                except OSError:
                    pass
            job.lines = deque(list(job.lines)[-FINISHED_LINES:], maxlen=FINISHED_LINES)
        job.finisher = None
        if job.proc is not None and job.proc.returncode is not None:
            job.proc = None

    # ---------------------------------------------------------------- wait / respond / cancel
    async def wait(self, job: Job, seconds: float,
                   on_progress: Callable[[Job], Awaitable[None]] | None = None) -> Job:
        """Return when the job finishes, needs input, or `seconds` pass."""
        deadline = time.monotonic() + max(0.0, seconds)
        while not job.finished and job.state != "needs_input":
            left = deadline - time.monotonic()
            if left <= 0:
                break
            job._changed.clear()
            try:
                await asyncio.wait_for(job._changed.wait(), timeout=min(left, 5.0))
            except asyncio.TimeoutError:
                pass
            if on_progress:
                try:
                    await on_progress(job)
                except Exception:  # noqa: BLE001  progress is best effort
                    pass
        return job

    async def respond(self, job: Job, prompt_id: str, value: str) -> None:
        if job.finished:
            raise ToolError(f"Job {job.id} already {job.state}; there is nothing to answer.")
        p = job.pending_prompt
        if not p:
            raise ToolError(f"Job {job.id} isn't waiting for an answer right now (state {job.state}).")
        if str(p.get("id")) != prompt_id:
            raise ToolError(f"The pending prompt is '{p.get('id')}', not '{prompt_id}'. Call ios_build_status first.")
        await self._write(job, {"prompt": prompt_id, "value": value})
        job.answered.append({"id": prompt_id, "value": value})
        job.pending_prompt = None
        job.state = "running"
        job.add_line(f"[answered {prompt_id}] {value}")
        job.touch()

    async def _write(self, job: Job, obj: dict) -> None:
        stdin = job.proc.stdin if job.proc else None
        if not stdin or stdin.is_closing():
            raise ToolError(f"Job {job.id} can't receive input anymore.")
        stdin.write((json.dumps(obj) + "\n").encode())
        try:
            await stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            raise ToolError(f"Job {job.id} stopped before the answer arrived.")

    async def cancel(self, job: Job) -> Job:
        """Stop the helper (or the finisher, if the build is done and it is installing) and wait until the
        job has really stopped, so nothing is installed or launched after the job says cancelled."""
        if job.finished:
            return job
        job.cancel_requested = True
        job.add_line("[cancel requested]")
        ft = job._finisher_task
        if ft is not None and not ft.done():
            ft.cancel()
        else:
            try:
                await self._write(job, {"cancel": True})
                if job.pending_prompt:
                    await self._write(job, {"prompt": job.pending_prompt.get("id"), "cancel": True})
            except ToolError:
                pass
            await self._kill(job, graceful=True)
        pump = job._task
        if pump is not None and not pump.done():
            await asyncio.wait({pump}, timeout=CANCEL_SETTLE)
        if not job.finished:
            job.error = job.error or {"code": "cancelled", "message": "Cancelled."}
            self._finish(job, "cancelled")
        return job

    async def _kill(self, job: Job, graceful: bool) -> None:
        """Ask nicely, then SIGTERM the helper, then SIGKILL it and sweep its process group.

        The helper (session leader) kills its own build tree on SIGTERM, and its reaper process kills
        the tree if the helper dies hard, so signal the helper itself first and the whole group last.
        """
        proc = job.proc
        if not proc or proc.returncode is not None:
            return
        steps = [(None, 4.0), (signal.SIGTERM, 4.0), (signal.SIGKILL, 1.5)] if graceful else \
                [(signal.SIGTERM, 1.5), (signal.SIGKILL, 1.0)]
        for sig, grace in steps:
            if sig is not None:
                try:
                    os.kill(proc.pid, sig)
                except ProcessLookupError:
                    break
            try:
                await asyncio.wait_for(asyncio.shield(proc.wait()), grace)
                break
            except asyncio.TimeoutError:
                continue
        await asyncio.sleep(0.2)
        helper.kill_group(proc.pid, signal.SIGKILL)

    async def shutdown(self) -> None:
        running = [j for j in self.jobs.values() if not j.finished]
        if running:
            log.info("stopping %d running job(s)", len(running))
        await asyncio.gather(*(self._kill(j, graceful=False) for j in running), return_exceptions=True)
        for j in running:
            if j._task and not j._task.done():
                j._task.cancel()
            if j.proc:
                helper.kill_group(j.proc.pid)


# ---------------------------------------------------------------- tolerant parsing

def _text(v: Any) -> str | None:
    if v is None or isinstance(v, (dict, list)):
        return None
    return str(v)


def _number(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


async def read_lines(stream: asyncio.StreamReader):
    """Lines of a stream like `async for`, but a line over the reader's limit is skipped (with a marker
    line) instead of raising, so one huge event can't stop the job from reading the rest."""
    skipping = False
    while True:
        try:
            raw = await stream.readuntil(b"\n")
        except asyncio.IncompleteReadError as exc:
            if exc.partial and not skipping:
                yield exc.partial
            return
        except asyncio.LimitOverrunError as exc:
            await stream.read(max(1, exc.consumed))
            skipping = True
            continue
        if skipping:
            skipping = False
            yield b"[helper output line too long; skipped]"
            continue
        yield raw
