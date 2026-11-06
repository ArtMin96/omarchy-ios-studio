"""Tool modules. Each exposes `register(mcp)`; optional `on_startup(state)` / `on_shutdown(state)`.

Shared pieces for tool authors (device, logs and crash tools included):
    tool(mcp, ...)      use instead of @mcp.tool(...): same arguments; results go through to_result(), which
                        applies the output budget (see budget.py and docs/mcp.md "Output size")
    ann(...)            ToolAnnotations with all four hints set explicitly
    READ / READ_REMOTE  read-only presets (local / talks to Apple or the phone)
    job_status(job)     BuildStatus model for any job, with next_steps
    start_and_wait(...) start a helper stream as a job and long-poll it briefly
    wait_with_progress  job long-poll that forwards progress notifications to the client
    dry_run(...)        the ActionResult a destructive tool returns when confirm is false
    page_of(...)        one byte-budgeted page of a list for offset-paginated tools
    slim_schemas(mcp)   drop schema noise (titles, null defaults) from every tool's published schemas
"""
from __future__ import annotations

import functools
import inspect
import json
from typing import Annotated, Any

import logging

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ResourceError, ToolError
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel

from .. import budget as B
from .. import helper
from ..jobs import Job
from ..models import (ActionResult, BuildResult, BuildStatus, DiagGroup, ErrorInfo, PendingPrompt, PromptOption,
                      StepInfo, iso)
from ..state import AppState

log = logging.getLogger("iosstudio.tools")

# Order matters only for on_startup hooks. Missing modules (device/logs/crashes until they exist) are skipped.
MODULES = ["status", "projects", "build_settings", "project_check", "run_presets", "build", "account", "toolchain", "app", "device", "logs", "crashes", "screen", "network", "metrics", "files", "debug", "console", "ui_inspect"]

MAX_WAIT = 25  # seconds; stays under opencode's 30 s and Codex's 60 s default tool timeouts


def to_result(value: Any) -> Any:
    """Pydantic model -> CallToolResult under the output budget.

    structuredContent is the whole model without nulls and empty defaults, bulk fields included, so clients
    that hand the model structuredContent see everything. The text block is the readable rendering: compact
    JSON of the other fields, then the model's BULK fields (log lines, crash text) as plain lines. The
    budget counts both copies: if text + structuredContent exceed the call's ceiling (DEFAULT_BYTES, or the
    tool's max_bytes), bulk lines and then lists are cut and the result is marked truncated, with a note.
    Resource links the tool attached (spilled files) are appended as resource_link blocks."""
    if not isinstance(value, BaseModel):
        return value
    data, text = B.enforce(B.dump(value), getattr(type(value), "BULK", ()), B.ceiling(value))
    content: list = [TextContent(type="text", text=text)] + B.links(value)
    return CallToolResult(content=content, structured_content=data)


def tool(mcp: MCPServer, **kwargs: Any):
    """`@tool(mcp, annotations=...)`: register an async tool whose return annotation is a Pydantic model.
    The outputSchema still comes from that model; results go through to_result()."""
    def deco(fn):
        sig = inspect.signature(fn, eval_str=True)
        ret = sig.return_annotation

        @functools.wraps(fn)
        async def wrapper(*args: Any, **kw: Any) -> Any:
            try:
                return to_result(await fn(*args, **kw))
            except (ToolError, ResourceError) as exc:   # one line + next step, never a raw dump
                raise ToolError(B.clip_error(str(exc))) from None

        wrapper.__signature__ = sig.replace(return_annotation=Annotated[CallToolResult, ret])
        mcp.tool(**kwargs)(wrapper)
        return fn
    return deco


def page_of(items: list, offset: int, limit: int, to_model, max_bytes: int | None = None) -> tuple[list, int | None]:
    """(models, next_offset) for offset-paginated tools: at most `limit` items whose JSON fits room(), the
    share of the budget left for them once both copies (text and structuredContent) are counted."""
    room = B.room(max_bytes)
    out: list = []
    used = 0
    for x in items[offset:offset + limit]:
        m = to_model(x)
        b = B.nbytes(json.dumps(B.dump(m), separators=(",", ":"), ensure_ascii=False)) + 1
        if out and used + b > room:
            break
        out.append(m)
        used += b
    end = offset + len(out)
    return out, (end if end < len(items) else None)


def fit_page(model: Any, field: str, offset: int = 0, note=None) -> Any:
    """Drop items from the end of a paged list (model.<field>) until the whole result fits its budget, so
    to_result()'s enforce() never cuts that list: enforce would keep next_offset pointing past items the
    caller never saw. Keeps count, has_more, next_offset (when the model has it), omitted, truncated and
    the note in step; note(k, end) gives the note for k items ending at offset `end`."""
    items = list(getattr(model, field))
    limit = B.ceiling(model)
    bulk = getattr(type(model), "BULK", ())
    if len(items) <= 1 or B.result_size(B.dump(model), bulk) <= limit:
        return model
    total = int(getattr(model, "total", offset + len(items)))

    def apply(k: int) -> bool:
        end = offset + k
        setattr(model, field, items[:k])
        model.count = k
        if "next_offset" in type(model).model_fields:
            model.next_offset = end if end < total else None
        if "has_more" in type(model).model_fields:
            model.has_more = end < total
        model.omitted = total - k
        model.truncated = True
        model.note = note(k, end) if note else f"Showing {k} of {total}; call again with offset={end}."
        return B.result_size(B.dump(model), bulk) <= limit

    lo, hi = 1, len(items) - 1          # largest k that fits; k=1 is kept even if it doesn't
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if apply(mid):
            lo = mid
        else:
            hi = mid - 1
    apply(lo)
    return model


def _slim(schema: Any, output: bool) -> Any:
    """Schema noise off: `title` keywords, `default: null`, and in output schemas every default. `anyOf`
    [X, null] with a plain X becomes X with "type": [T, "null"]. Validation is unchanged."""
    if isinstance(schema, list):
        return [_slim(x, output) for x in schema]
    if not isinstance(schema, dict):
        return schema
    out: dict = {}
    for k, v in schema.items():
        if k in ("properties", "$defs", "definitions") and isinstance(v, dict):
            out[k] = {name: _slim(sub, output) for name, sub in v.items()}
        elif k == "title" and isinstance(v, str):
            continue
        elif k == "default" and (v is None or output):
            continue
        else:
            out[k] = _slim(v, output)
    any_of = out.get("anyOf")
    if isinstance(any_of, list) and len(any_of) == 2 and {"type": "null"} in any_of:
        other = next(x for x in any_of if x != {"type": "null"})
        t = other.get("type")
        if isinstance(t, str) and "$ref" not in other and "anyOf" not in other:
            merged = {**other, "type": [t, "null"]}
            if "enum" in merged:
                merged["enum"] = [*merged["enum"], None]
            out.pop("anyOf")
            out = {**merged, **out}
    return out


def _shallow(schema: dict, owner: str) -> dict:
    """The same top-level shape with nested models typed as plain objects: it accepts exactly what the full
    schema accepts at the top level and points to the tool that publishes the full one."""
    def flat(s: Any) -> Any:
        if isinstance(s, dict):
            if "$ref" in s:
                return {"type": "object"}
            return {k: ({n: flat(x) for n, x in v.items()} if k == "properties" else flat(v))
                    for k, v in s.items() if not (k == "description" and isinstance(v, str))}
        if isinstance(s, list):
            return [flat(x) for x in s]
        return s
    out = {k: flat(v) for k, v in schema.items() if k != "$defs"}
    out["description"] = f"Same result as {owner}; its outputSchema has the nested fields."
    return out


# Tools whose output model is shared publish the full schema once, on the tool named here (else the first).
CANONICAL = {"BuildStatus": "ios_build_status", "DeviceList": "ios_device_list"}


def slim_schemas(mcp: MCPServer) -> None:
    """Smaller tools/list: drop titles and null defaults everywhere and defaults in output schemas, and
    publish a shared output model's nested definitions once (see CANONICAL)."""
    tools = mcp._tool_manager.list_tools()
    by_model: dict[Any, list] = {}
    for t in tools:
        t.parameters = _slim(t.parameters, output=False)
        if t.fn_metadata.output_schema is not None:
            t.fn_metadata.output_schema = _slim(t.fn_metadata.output_schema, output=True)
            by_model.setdefault(t.fn_metadata.output_model, []).append(t)
    for model, group in by_model.items():
        if len(group) < 2 or not group[0].fn_metadata.output_schema.get("$defs"):
            continue
        name = getattr(model, "__name__", "")
        owner = CANONICAL.get(name) or group[0].name
        for t in group:
            if t.name != owner:
                t.fn_metadata.output_schema = _shallow(t.fn_metadata.output_schema, owner)
    for t in tools:
        t.__dict__.pop("output_schema", None)   # cached_property: re-read the slim one


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
