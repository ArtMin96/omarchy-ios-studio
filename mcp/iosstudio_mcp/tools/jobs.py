"""Generic job tools: every background job (builds, toolchain installs, renewals, package changes, installs,
file copies, camera tests, app data changes, profiles, screenshot matrices, recordings, new projects) is
polled, answered and cancelled here by its job_id. A feature's own tool, called again with job_id, gives
that feature's full result; these give the shared view (state, progress, prompt, error, next steps)."""
from __future__ import annotations

from typing import Annotated

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from ..core.job_status import job_status, wait_with_progress
from ..core.params import JobId, WaitSeconds
from ..core.registry import tool
from ..models import JobStatus
from ..state import get_state


def register(mcp: MCPServer) -> None:

    @tool(mcp, "Job status", kind="read")
    async def ios_job_status(
        ctx: Context,
        job_id: JobId,
        wait_seconds: WaitSeconds = 20,
        verbose: Annotated[bool, Field(description="Full view (steps, diagnostics) even while running")] = False,
    ) -> JobStatus:
        """State of any background job: running / needs_input / succeeded / failed / cancelled.

        While running it returns a small delta (view='delta'): step, progress, ETA, error and warning counts,
        and what is new since your last poll (new_log_lines, new_errors). When the job finishes or needs
        input, or with verbose=true, it returns the full view: steps, the top 5 grouped diagnostics, any
        prompt for the human, the result (a build's ipa path, installed, launched; a feature job's `detail`)
        and next_steps. `tool` names the feature tool that returns a feature job's full result.

        Long-polls: returns early when the job finishes or needs input, else after wait_seconds (≤25), with
        progress notifications. If state is needs_input, show pending_prompt to the user and answer with
        ios_job_respond.
        """
        state = get_state(ctx)
        job = state.jobs.get(job_id)
        await wait_with_progress(state, job, wait_seconds, ctx)
        return job_status(job, verbose=verbose)

    @tool(mcp, "Answer job prompt", kind="destructive", open_world=True)
    async def ios_job_respond(
        ctx: Context,
        job_id: JobId,
        prompt_id: Annotated[str, Field(description="pending_prompt.id from ios_job_status, e.g. 'p1'")],
        value: Annotated[str, Field(description="One of pending_prompt.options[].value, e.g. 'no'")],
        confirm: Annotated[bool, Field(description="Required (true) for a destructive option, only after the user "
                                                   "explicitly chose it")] = False,
    ) -> JobStatus:
        """Answer a question a running job is waiting on (state needs_input), e.g. "Replace your signing
        certificate?" during a build or "Install everything?" during a toolchain install.

        Always show the prompt to the user and send THEIR answer; never decide for them. Options marked
        destructive (like revoking a certificate, which stops apps signed elsewhere from opening) also
        need confirm=true. Returns the job status after the answer.
        """
        state = get_state(ctx)
        job = state.jobs.get(job_id)
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

    @tool(mcp, "Cancel job", kind="write", idempotent=True)
    async def ios_job_cancel(ctx: Context, job_id: JobId) -> JobStatus:
        """Stop a running job and everything it started, and wait until it has stopped. A build stops xtool
        and swift-build: cancelled before the install step, nothing is installed or launched; cancelled while
        an ios_run is installing over Wi-Fi, the iPhone may still finish that install but the app isn't
        launched (the status `error` says which). A package change puts Package.swift back; a file copy is
        stopped on the iPhone; a screenshot matrix keeps the shots taken so far. Returns the final status."""
        state = get_state(ctx)
        job = await state.jobs.cancel(state.jobs.get(job_id))
        return job_status(job)
