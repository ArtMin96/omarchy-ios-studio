"""Background jobs: long helper streams (builds, toolchain installs, renewals) that outlive one tool call.

A job owns one `iosstudio-helper` process in its own process group and folds its JSON-lines events
(build, step, progress, log, notice, diagnostic, prompt, done) into a bounded record. Tools read it
with `wait()` (long-poll), answer prompts with `respond()`, and stop it with `cancel()`.
`shutdown()` kills every running job; the lifespan calls it when the server exits.

A job may carry a `finisher` coroutine that runs after the helper reports success and before the job
is marked finished (ios_run uses it to install and launch the app through the device client).
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
TERMINAL = ("succeeded", "failed", "cancelled")

Finisher = Callable[["Job"], Awaitable[None]]


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
    _task: asyncio.Task | None = None

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
            async for raw in proc.stderr:
                text = raw.decode(errors="replace").rstrip()
                if text:
                    job.add_line(f"[helper stderr] {text}")
                    log.debug("%s stderr: %s", job.id, text)

        err_task = asyncio.create_task(read_stderr())
        try:
            async for raw in proc.stdout:
                line = raw.decode(errors="replace").strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
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
                try:
                    await job.finisher(job)
                except ToolError as exc:
                    job.error = {"code": "post-build", "message": str(exc)}
                    self._finish(job, "failed")
                except Exception as exc:  # noqa: BLE001
                    log.exception("finisher failed for %s", job.id)
                    job.error = {"code": "post-build", "message": f"Installing after the build failed: {exc}"}
                    self._finish(job, "failed")
                else:
                    if not job.finished:
                        self._finish(job, "succeeded")
            elif job.result is not None and job.state == "running":
                self._finish(job, "succeeded")
        except asyncio.CancelledError:
            await self._kill(job, graceful=False)
            raise
        finally:
            if not err_task.done():
                err_task.cancel()
            if not job.finished:
                self._finish(job, "cancelled" if job.cancel_requested else "failed")

    def _apply(self, job: Job, ev: dict) -> bool:
        """Fold one event into the job. Returns True on the final `done` event."""
        name = ev.get("event")
        if name == "build":
            job.info = {k: v for k, v in ev.items() if k != "event"}
            job.project = job.project or ev.get("project")
        elif name == "step":
            job.set_step(str(ev.get("id")), ev.get("title"), str(ev.get("state") or ""),
                         detail=ev.get("detail"), owner=ev.get("owner"), reason=ev.get("reason"),
                         error=(ev.get("error") or {}).get("message") if isinstance(ev.get("error"), dict) else None)
        elif name == "progress":
            if ev.get("fraction") is not None:
                job.progress = float(ev["fraction"])
            job.progress_detail = str(ev.get("detail") or job.progress_detail)
            job.eta = ev.get("eta")
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
            job.pending_prompt = {k: v for k, v in ev.items() if k != "event"}
            job.state = "needs_input"
            job.add_line(f"[prompt {ev.get('id')}] {ev.get('title') or ''} {ev.get('message', '')}".strip())
        elif name == "renew-plan":
            job.info.setdefault("renewPlan", ev.get("projects"))
        elif name == "done":
            job.pending_prompt = None
            if ev.get("ok"):
                job.result = ev.get("data") or {}
                if job.state == "needs_input":
                    job.state = "running"
                if not job.finisher:
                    self._finish(job, "succeeded")
            else:
                err = ev.get("error") or {}
                build = err.pop("build", None) if isinstance(err, dict) else None
                job.error = err
                if build:
                    job.result = build
                    for d in build.get("diagnostics") or []:
                        if d not in job.diagnostics and len(job.diagnostics) < MAX_DIAGNOSTICS:
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
        job.touch()

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
        if job.finished:
            return job
        job.cancel_requested = True
        job.add_line("[cancel requested]")
        try:
            await self._write(job, {"cancel": True})
            if job.pending_prompt:
                await self._write(job, {"prompt": job.pending_prompt.get("id"), "cancel": True})
        except ToolError:
            pass
        await self._kill(job, graceful=True)
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
