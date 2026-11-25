"""Unit tests on this computer: ios_test_run runs a project's test targets with `swift test` on the Linux host
(helper `test run`, helper/iosstudio/features/testing/) as a job of kind "test".

Only the test targets and the local targets they depend on are built (in .build/host-tests, apart from xtool's
build), so tested code must be Foundation-only: UIKit and SwiftUI don't exist on the host. The job keeps counts
of the per-test events, never the events themselves (core/jobs.py); failing tests come from the helper's final
result (at most 50), compile errors from its diagnostics. The result is cut to the budget; the whole run is in
the helper's log file (full_output_path)."""
from __future__ import annotations

import json
from typing import Annotated, Literal

from mcp.server.mcpserver import Context, MCPServer
from pydantic import Field

from .. import helper
from ..backends.projects import resolve_project
from ..core import budget as B
from ..core.job_status import wait_with_progress
from ..core.jobs import Job
from ..core.params import MaxBytes, OptJobId, ProjectPath, WaitSeconds
from ..core.registry import tool
from ..models import DiagGroup, ErrorInfo, Out
from ..state import AppState, get_state

KIND = "test"
TOOL = "ios_test_run"
FAILURES_CAP = 20           # failing tests in a result
MESSAGE_CUT = 200           # characters of a failure message
DIAGS_CAP = 20              # grouped compile diagnostics in a result
FILTER_MAX = 200

Filter = Annotated[str | None, Field(max_length=FILTER_MAX, description="Regex over test ids, e.g. 'MathTests' or "
                                                                        "'AppCoreTests.MathTests/testAdd'")]


class TestCounts(Out):
    total: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0


class TestRun(B.Budgeted, Out):
    job_id: str
    state: Literal["running", "succeeded", "failed", "cancelled"]
    project: str | None = None
    project_path: str | None = None
    filter: str | None = None
    busy: bool | None = Field(None, description="true: a run of this project was already going; this is that run")
    step: str | None = Field(None, description="While running: prepare, build or run")
    elapsed_seconds: float = 0
    counts: TestCounts | None = Field(None, description="While running: the tests finished so far")
    duration_seconds: float | None = None
    failures: list[str] = Field(default_factory=list, description="'id — file:line — message' per failing test")
    failures_total: int | None = None
    crashed: str | None = Field(None, description="The test that crashed the test process; later tests didn't run")
    no_tests_matched: bool | None = Field(None, description="true: filter matched no test")
    diagnostics: list[DiagGroup] = Field(default_factory=list, description="Compile errors (build_failed), grouped")
    diagnostics_total: int | None = None
    error: ErrorInfo | None = None
    next_steps: list[str] = Field(default_factory=list)


def _int(v) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0


def _counts(raw) -> TestCounts | None:
    if not isinstance(raw, dict):
        return None
    c = TestCounts(**{k: _int(raw.get(k)) for k in ("passed", "failed", "skipped")})
    c.total = _int(raw.get("total")) or c.passed + c.failed + c.skipped
    return c


def _failure_line(f: dict, root: str | None) -> str:
    name = str(f.get("id") or f.get("name") or "?")
    where = B.rel_path(str(f["file"]), root) + (f":{f['line']}" if f.get("line") else "") if f.get("file") else None
    msg = B.clip(" ".join(str(f.get("message") or "failed").split()), MESSAGE_CUT)
    return " — ".join(x for x in (name, where, msg) if x)


ERROR_STEPS = {
    "no_tests": "This project has no test target. Create a project from the 'App with unit tests' template "
                "(ios_project_templates) or add a Tests/<Name>Tests target to Package.swift that depends on a "
                "Foundation-only library target, then call ios_test_run again.",
    "host_unsupported": "The tested code imports UIKit/SwiftUI, which don't exist on this computer. Move the logic "
                        "into a Foundation-only library target (like AppCore in the 'App with unit tests' template), "
                        "test that, and check UI code on the iPhone (ios_run, ios_ui_tree).",
    "toolchain": "No Swift toolchain for this computer: call ios_status, which names the setup step that installs it.",
    "busy": "A test run of this project started in the iOS Studio app is still going: wait for it, then call "
            "ios_test_run again.",
    "timeout": "The run went over its time limit (a test that hangs?). Read the log at full_output_path, then run "
               "part of it with filter=... .",
    "cancelled": "Cancelled. Call ios_test_run again to start a new run.",
}


def _view(job: Job, max_bytes: int | None, busy: bool = False) -> TestRun:
    r = job.result if isinstance(job.result, dict) else {}
    root = job.project_path
    state = "running" if job.state in ("running", "needs_input") else job.state
    out = TestRun(job_id=job.id, state=state, project=job.project, project_path=helper.tilde(root) if root else None,
                  filter=job.data.get("filter"), busy=busy or None, elapsed_seconds=job.elapsed)
    if state == "running":
        out.step = job.current_step
        out.counts = _counts(job.data.get("tests"))
        out.next_steps = [f"Still running ({job.current_step or 'starting'}). Call ios_test_run(job_id='{job.id}', "
                          "wait_seconds=20) again; ios_job_cancel stops it."]
        if busy:
            out.note = (f"A test run of {job.project} is already going ({job.id}); this is that run, no new one "
                        "was started.")
        return out.with_budget(max_bytes)

    room = B.room(max_bytes)
    notes: list[str] = []
    out.counts = _counts(r.get("counts")) if r else None
    out.duration_seconds = r.get("duration") if isinstance(r.get("duration"), (int, float)) else None
    out.crashed = str(r["crashed"]) if r.get("crashed") else None
    out.no_tests_matched = True if r.get("no_tests_matched") else None
    if r.get("filter"):
        out.filter = str(r["filter"])
    log_path = r.get("log_path") or job.info.get("logFile")
    out.full_output_path = helper.tilde(str(log_path)) if log_path else None

    err = job.error or {}
    code = str(err.get("code") or "") or None
    if job.error:
        out.error = ErrorInfo(code=code, message=B.clip_error(str(err.get("message") or "The test run failed.")))

    compile_diags = [d for d in job.diagnostics if B.diag_category(d) != "test"]
    groups = B.group_diags(compile_diags, root) if code == "build_failed" or (job.state == "failed" and not r) else []
    fail_room = int(room * (0.55 if groups else 0.9))

    failures = [f for f in (r.get("failures") or []) if isinstance(f, dict)]
    total_failed = max(len(failures), out.counts.failed if out.counts else 0)
    if failures:
        page = B.take([_failure_line(f, root) for f in failures], FAILURES_CAP, fail_room)
        out.failures, out.failures_total = page.items, total_failed
        if len(page.items) < total_failed:
            out.omitted += total_failed - len(page.items)
            notes.append(f"Showing {len(page.items)} of {total_failed} failing tests"
                         + ("; the log at full_output_path has all of them" if out.full_output_path else "")
                         + "; rerun fewer with filter=..., or pass max_bytes (≤48000).")
    if groups:
        used, left = 0, room - sum(B.nbytes(x) + 1 for x in out.failures)
        for g in groups[:DIAGS_CAP]:
            m = DiagGroup(**g)
            b = B.nbytes(json.dumps(B.dump(m), separators=(",", ":"), ensure_ascii=False)) + 1
            if out.diagnostics and used + b > left:
                break
            out.diagnostics.append(m)
            used += b
        out.diagnostics_total = len(groups)
        if len(out.diagnostics) < len(groups):
            out.omitted += len(groups) - len(out.diagnostics)
            notes.append(f"Showing {len(out.diagnostics)} of {len(groups)} diagnostic groups; "
                         f"ios_build_diagnostics(job_id='{job.id}') pages through all of them.")
    out.truncated = out.omitted > 0
    out.note = " ".join(notes) or None
    out.next_steps = _next_steps(job, out, code, err)
    return out.with_budget(max_bytes)


def _next_steps(job: Job, out: TestRun, code: str | None, err: dict) -> list[str]:
    again = f"ios_test_run(project_path='{out.project_path or job.project}'" + \
            (f", filter='{out.filter}'" if out.filter else "") + ")"
    if job.state == "succeeded":
        if out.no_tests_matched:
            return [f"filter '{out.filter}' matched no test. It is a regex over ids like "
                    "'AppCoreTests.MathTests/testAdd': try the suite name, or call without filter."]
        n = out.counts.total if out.counts else 0
        return [f"All {n} test{'s' if n != 1 else ''} passed. UI code isn't covered here: check it on the iPhone "
                "(ios_run, ios_ui_tree)."]
    steps: list[str] = []
    if code == "tests_failed" or (out.failures and code is None):
        steps.append(f"Fix the failing tests or the code they test at the file:line shown, then call {again} again.")
    elif code == "build_failed":
        steps.append(f"Fix the compile errors in diagnostics (in the tests or the code they test), then call {again}.")
    elif code in ERROR_STEPS:
        steps.append(ERROR_STEPS[code])
    else:
        nxt = helper.next_step_for(err)
        steps.append(nxt or (f"Read the log at {out.full_output_path}, then call {again}." if out.full_output_path
                             else f"Call {again} again; ios_status checks the setup."))
    if out.crashed:
        steps.append(f"{out.crashed} crashed the test process, so the tests after it didn't run: fix the crash "
                     "first (its message and file:line are in failures).")
    return steps


async def _start(state: AppState, ctx: Context, project_path: str, filter: str | None,
                 wait_seconds: int, max_bytes: int | None) -> TestRun:
    p = await resolve_project(state, project_path)
    path = p["path"]
    for j in reversed(list(state.jobs.jobs.values())):
        if j.kind == KIND and j.project_path == path and not j.finished:
            return _view(j, max_bytes, busy=True)
    job = await state.jobs.start(KIND, "test", "run", {"project": path, "filter": filter or None},
                                 title=f"Unit tests {p.get('name')}", prefix=KIND, project_path=path,
                                 project=p.get("name"))
    job.tool = TOOL
    job.data["filter"] = filter or None
    await wait_with_progress(state, job, wait_seconds, ctx)
    return _view(job, max_bytes)


def register(mcp: MCPServer) -> None:

    @tool(mcp, "Run unit tests", kind="read")
    async def ios_test_run(
        ctx: Context,
        project_path: ProjectPath,
        filter: Filter = None,
        job_id: OptJobId = None,
        wait_seconds: WaitSeconds = 20,
        max_bytes: MaxBytes = None,
    ) -> TestRun:
        """Run a project's unit tests on this computer (swift test, no iPhone); tested code must be
        Foundation-only, not UIKit/SwiftUI. A job: state='running' + job_id, call again with job_id. Returns
        counts, failing tests (file:line), compile errors, the log as full_output_path."""
        state = get_state(ctx)
        if job_id:
            job = state.jobs.get(job_id, KIND)
            await wait_with_progress(state, job, wait_seconds, ctx)
            return _view(job, max_bytes)
        return await _start(state, ctx, project_path, (filter or "").strip() or None, wait_seconds, max_bytes)
