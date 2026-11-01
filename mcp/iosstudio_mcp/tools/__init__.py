"""Tool modules. Each exposes `register(mcp)`; optional `on_startup(state)` / `on_shutdown(state)`.

Shared pieces for tool authors (device, logs and crash tools included):
    tool(mcp, ...)      use instead of @mcp.tool(...): same arguments, compact JSON text + structuredContent
    ann(...)            ToolAnnotations with all four hints set explicitly
    READ / READ_REMOTE  read-only presets (local / talks to Apple or the phone)
    job_status(job)     BuildStatus model for any job, with next_steps
    start_and_wait(...) start a helper stream as a job and long-poll it briefly
    wait_with_progress  job long-poll that forwards progress notifications to the client
    dry_run(...)        the ActionResult a destructive tool returns when confirm is false
"""
from __future__ import annotations

import functools
import inspect
import json
from typing import Annotated, Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel

from .. import helper
from ..jobs import Job
from ..models import (ActionResult, BuildResult, BuildStatus, Diagnostic, ErrorInfo, PendingPrompt, PromptOption,
                      StepInfo, iso)
from ..state import AppState

# Order matters only for on_startup hooks. Missing modules (device/logs/crashes until they exist) are skipped.
MODULES = ["status", "projects", "build", "account", "toolchain", "app", "device", "logs", "crashes"]

MAX_WAIT = 25  # seconds; stays under opencode's 30 s and Codex's 60 s default tool timeouts


def to_result(value: Any) -> Any:
    """Pydantic model -> CallToolResult with compact JSON text (no indentation, no nulls) and the same data
    as structuredContent. The SDK's default text block is indented JSON, which costs agents tokens."""
    if not isinstance(value, BaseModel):
        return value
    data = value.model_dump(mode="json", exclude_none=True)
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return CallToolResult(content=[TextContent(type="text", text=text)], structured_content=data)


def tool(mcp: MCPServer, **kwargs: Any):
    """`@tool(mcp, annotations=...)`: register an async tool whose return annotation is a Pydantic model.
    The outputSchema still comes from that model; results go through to_result()."""
    def deco(fn):
        sig = inspect.signature(fn, eval_str=True)
        ret = sig.return_annotation

        @functools.wraps(fn)
        async def wrapper(*args: Any, **kw: Any) -> Any:
            return to_result(await fn(*args, **kw))

        wrapper.__signature__ = sig.replace(return_annotation=Annotated[CallToolResult, ret])
        mcp.tool(**kwargs)(wrapper)
        return fn
    return deco


def ann(title: str, *, read_only: bool, destructive: bool = False, idempotent: bool = False,
        open_world: bool = False) -> ToolAnnotations:
    return ToolAnnotations(title=title, read_only_hint=read_only, destructive_hint=destructive,
                           idempotent_hint=idempotent, open_world_hint=open_world)


def READ(title: str) -> ToolAnnotations:
    return ann(title, read_only=True, idempotent=True)


def READ_REMOTE(title: str) -> ToolAnnotations:
    return ann(title, read_only=True, idempotent=True, open_world=True)


def dry_run(message: str, would: list[str], data: dict | None = None) -> ActionResult:
    return ActionResult(done=False, message=message + " Nothing was changed. Ask the user to confirm, then call "
                        "again with confirm=true.", would=would, data=data,
                        next_steps=["Ask the user whether to proceed; only then repeat the call with confirm=true."])


# ------------------------------------------------------------------ job status

def _prompt_model(p: dict | None) -> PendingPrompt | None:
    if not p:
        return None
    opts = [PromptOption(value=str(o.get("value")), label=o.get("label"), destructive=bool(o.get("destructive")))
            for o in (p.get("options") or []) if isinstance(o, dict)]
    return PendingPrompt(id=str(p.get("id")), kind=p.get("kind"), title=p.get("title"),
                         message=str(p.get("message", "")), options=opts, default=p.get("default"),
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
                       duration_seconds=r.get("duration"))


def _sorted_diags(diags: list[dict]) -> list[dict]:
    rank = {"error": 0, "warning": 1}
    return sorted(diags, key=lambda d: rank.get(str(d.get("severity")), 2))


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
        if errors:
            out.append(f"{errors} compiler error(s). Call ios_build_diagnostics(build_id='{bid}'), fix the files, "
                       "then rebuild with ios_build_start(action='compile').")
        nxt = helper.next_step_for(job.error or {})
        if nxt and not (errors and (job.error or {}).get("code") in ("build-failed", "compile-errors")):
            out.append(nxt)
        if not errors:
            out.append(f"ios_build_log(build_id='{bid}', grep='error|fail') shows what went wrong.")
    elif job.state == "succeeded":
        out.extend(job.next_steps)
        r = job.result or {}
        if job.kind == "build" and job.action == "compile":
            out.append("Compiled cleanly. Call ios_run(project_path) to build, install and launch it on the iPhone.")
        elif job.kind == "build" and job.action == "ipa" and r.get("ipa"):
            out.append(f"Signed .ipa at {r.get('ipa')}.")
        elif r.get("launched"):
            out.append("The app is running on the iPhone. To see what it does, call ios_app_run_and_capture("
                       f"bundle_id='{r.get('installedBundleId') or r.get('bundleId')}') (relaunches it and reports "
                       "logs and crashes), or ios_logs_start + ios_logs_read, ios_screenshot.")
        elif r.get("installed"):
            out.append("Installed on the iPhone.")
    elif job.state == "cancelled":
        out.append("Cancelled. Start again with ios_build_start when ready.")
    return out


def job_status(job: Job, diag_limit: int = 10) -> BuildStatus:
    errors, warnings = job.counts()
    diags = _sorted_diags(job.diagnostics)
    steps = [StepInfo(id=s["id"], title=s.get("title") or s["id"], state=s.get("state") or "pending",
                      detail=s.get("detail"), owner=s.get("owner")) for s in job.steps.values()]
    err = ErrorInfo(code=job.error.get("code"), message=str(job.error.get("message") or "Failed.")) \
        if job.error else None
    step_title = job.steps.get(job.current_step or "", {}).get("title") if job.current_step else None
    return BuildStatus(
        build_id=job.id, kind=job.kind, action=job.action, title=job.title, project=job.project,
        project_path=job.project_path, state=job.state, step=step_title or job.current_step,
        progress=round(job.progress, 3) if job.progress is not None else None,
        progress_detail=job.progress_detail or None,
        eta_seconds=job.eta if not job.finished else None, elapsed_seconds=job.elapsed,
        errors=errors, warnings=warnings,
        diagnostics=[Diagnostic.from_helper(d) for d in diags[:diag_limit]], diagnostics_total=len(diags),
        steps=steps, pending_prompt=_prompt_model(job.pending_prompt), result=_result_model(job), error=err,
        log_lines=job.line_count, log_file=job.info.get("logFile"), next_steps=job_next_steps(job))


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
