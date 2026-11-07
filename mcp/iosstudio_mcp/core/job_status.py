"""Jobs as tool results: the BuildStatus view of a helper job (jobs.py) and the long-poll helpers.

    job_status(job)     BuildStatus model for any job, with next_steps
    start_and_wait(...) start a helper stream as a job and long-poll it briefly
    wait_with_progress  job long-poll that forwards progress notifications to the client
"""
from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Context

from .. import helper
from ..jobs import Job
from ..models import (
    BuildResult,
    BuildStatus,
    DiagGroup,
    ErrorInfo,
    PendingPrompt,
    PromptOption,
    StepInfo,
    iso,
)
from ..state import AppState
from . import budget as B
from .registry import MAX_WAIT


def _prompt_model(p: dict | None) -> PendingPrompt | None:
    if not p:
        return None
    opts = [PromptOption(value=str(o.get("value")), label=o.get("label"), destructive=bool(o.get("destructive")))
            for o in (p.get("options") or []) if isinstance(o, dict)]
    return PendingPrompt(id=str(p.get("id")), kind=p.get("kind"), title=p.get("title"),
                         message=B.clip(p.get("message", ""), 1500), options=opts, default=p.get("default"),
                         destructive=any(o.destructive for o in opts))


def _result_model(job: Job) -> BuildResult | None:
    r = job.result
    if r is None:
        return None
    prof = r.get("profile") or {}
    return BuildResult(ipa=r.get("ipa"), app=r.get("app"), bundle_id=r.get("bundleId"), udid=r.get("udid"),
                       via=r.get("via"), installed=bool(r.get("installed")),
                       needs_install=bool(r.get("needsInstall")) and not r.get("installed"),
                       launched=bool(r.get("launched")), pid=r.get("pid"),
                       profile_expires=iso(prof.get("expires")) if isinstance(prof, dict) else None,
                       duration_seconds=r.get("duration"), **_launch_fields(r.get("launch"), r.get("preset")))


def _launch_fields(how: Any, preset: Any = None) -> dict:
    """preset / launch_args / launch_env of a run's `launch` data (run presets)."""
    if not isinstance(how, dict):
        return {"preset": str(preset)} if preset else {}
    return {"preset": how.get("preset") or preset or None,
            "launch_args": [B.clip(a, 300) for a in how.get("args") or []][:64],
            "launch_env": {str(k): B.clip(v, 300) for k, v in list((how.get("env") or {}).items())[:64]}}


def job_next_steps(job: Job) -> list[str]:
    bid = job.id
    out: list[str] = []
    errors, _ = job.counts()
    if job.state == "running":
        out.append(f"Still running ({job.current_step or 'starting'}). Call ios_build_status(build_id='{bid}', "
                   "wait_seconds=20) again; ios_build_log shows live output.")
    elif job.state == "needs_input":
        p = job.pending_prompt or {}
        out.append(f"The job is waiting for a decision: \"{p.get('title') or p.get('message', '')[:120]}\". Show the "
                   "user the message and options, ask them, then call ios_build_respond(build_id, prompt_id, value).")
        if any(o.get("destructive") for o in p.get("options") or [] if isinstance(o, dict)):
            out.append("One option is destructive: never pick it unless the user explicitly chose it.")
    elif job.state == "failed":
        cats = [B.diag_category(d) for d in job.diagnostics if d.get("severity") == "error"]
        if "compiler" in cats:
            out.append(f"{cats.count('compiler')} compiler error(s). Call ios_build_diagnostics(build_id='{bid}'), "
                       "fix the files, then rebuild with ios_build_start(action='compile').")
        others = list(dict.fromkeys(c for c in cats if c != "compiler"))
        if others:
            out.append(f"{', '.join(others).capitalize()} error(s): ios_build_diagnostics(build_id='{bid}') lists "
                       "each one with a hint for the fix.")
        nxt = helper.next_step_for(job.error or {})
        if nxt and not (errors and (job.error or {}).get("code") in ("build-failed", "compile-errors")):
            out.append(nxt)
        if not errors:
            out.append(f"No error was recognised in the output (last resort): ios_build_log(build_id='{bid}', "
                       "grep='error|fail') shows what went wrong.")
    elif job.state == "succeeded":
        out.extend(job.next_steps)
        r = job.result or {}
        if job.kind == "build" and job.action == "compile":
            out.append("Compiled cleanly. Call ios_run(project_path) to build, install and launch it on the iPhone.")
        elif job.kind == "build" and job.action == "ipa" and r.get("ipa"):
            out.append(f"Signed .ipa at {r.get('ipa')}.")
        elif r.get("launched"):
            pre = ((r.get("launch") or {}).get("preset") if isinstance(r.get("launch"), dict) else None) \
                or r.get("preset")
            out.append("The app is running on the iPhone. To see what it does, call ios_app_run_and_capture("
                       f"bundle_id='{r.get('installedBundleId') or r.get('bundleId')}'"
                       + (f", preset='{pre}'" if pre else "") + ") (relaunches it and reports "
                       "logs and crashes), or ios_logs_start + ios_logs_read, ios_screenshot.")
        elif r.get("installed"):
            out.append("Installed on the iPhone.")
    elif job.state == "cancelled":
        out.append("Cancelled. Start again with ios_build_start when ready.")
    return out


STATUS_DIAGS = 5        # grouped diagnostics embedded in a status
NEW_LINES = 5           # newest log lines in a delta poll


def job_status(job: Job, verbose: bool = False) -> BuildStatus:
    """Full view when the job finished, needs input, or verbose; else a delta: state, step, progress, ETA and
    what changed since the previous status of this job (new log lines, new error/warning counts)."""
    errors, warnings = job.counts()
    lines0, err0, warn0 = job.poll_mark or (0, 0, 0)
    job.poll_mark = (job.line_count, errors, warnings)
    new_count = max(0, job.line_count - max(lines0, job.first_line))
    new_lines = [B.clip(ln, 200) for ln in list(job.lines)[-min(new_count, NEW_LINES):]] if new_count else []
    step_title = job.steps.get(job.current_step or "", {}).get("title") if job.current_step else None
    common = dict(build_id=job.id, kind=job.kind, action=job.action, title=job.title, project=job.project,
                  state=job.state, step=step_title or job.current_step,
                  progress=round(job.progress, 3) if job.progress is not None else None,
                  progress_detail=B.clip(job.progress_detail, 200) if job.progress_detail else None,
                  eta_seconds=job.eta if not job.finished else None, elapsed_seconds=job.elapsed,
                  errors=errors, warnings=warnings, new_errors=max(0, errors - err0),
                  new_warnings=max(0, warnings - warn0), log_lines=job.line_count)
    full = verbose or job.finished or job.state == "needs_input"
    if not full:
        return BuildStatus(**common, view="delta", new_log_lines=new_lines, new_log_count=new_count,
                           next_steps=job_next_steps(job)[:1])
    groups = B.group_diags(job.diagnostics, job.project_path)
    steps = [StepInfo(id=s["id"], title=s.get("title") or s["id"], state=s.get("state") or "pending",
                      detail=B.clip(s["detail"], 200) if s.get("detail") else None, owner=s.get("owner"))
             for s in job.steps.values()]
    err = ErrorInfo(code=job.error.get("code"), message=B.clip_error(str(job.error.get("message") or "Failed."))) \
        if job.error else None
    return BuildStatus(
        **common, view="full", project_path=job.project_path,
        diagnostics=[DiagGroup(**g) for g in groups[:STATUS_DIAGS]], diagnostics_total=len(groups),
        steps=steps, pending_prompt=_prompt_model(job.pending_prompt), result=_result_model(job), error=err,
        log_file=job.info.get("logFile"), next_steps=job_next_steps(job))


async def wait_with_progress(state: AppState, job: Job, seconds: float, ctx: Context | None) -> Job:
    last = [-1.0]

    async def report(j: Job) -> None:
        if ctx is None or j.progress is None:
            return
        pct = round(j.progress * 100, 1)
        if pct > last[0]:  # progress must increase
            last[0] = pct
            await ctx.report_progress(pct, 100, message=j.progress_detail or j.current_step or j.state)

    return await state.jobs.wait(job, min(max(seconds, 0), MAX_WAIT), report)


async def start_and_wait(state: AppState, ctx: Context | None, *, kind: str, group: str, action: str,
                         flags: dict[str, Any], title: str, prefix: str, wait_seconds: float,
                         project_path: str | None = None, project: str | None = None, finisher=None) -> BuildStatus:
    job = await state.jobs.start(kind, group, action, flags, title=title, prefix=prefix,
                                 project_path=project_path, project=project, finisher=finisher)
    await wait_with_progress(state, job, wait_seconds, ctx)
    return job_status(job)
