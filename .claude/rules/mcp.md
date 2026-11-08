---
paths:
  - "mcp/**"
---
# MCP server rules (mcp/iosstudio_mcp)

Full explanation: [docs/mcp.md#for-tool-authors](../../docs/mcp.md#for-tool-authors). Checklist:
[CONTRIBUTING.md#add-an-mcp-tool](../../CONTRIBUTING.md#add-an-mcp-tool).

## Every tool

- Lives in its area's module in `tools/` (a new module goes in `MODULES` in `tools/__init__.py`). A tool
  module imports only `core/` and `backends/`, never another tool module (`tests/mcp/test_mcp_core.py`).
- Registered with `@tool(mcp, "Title", kind=...)`, never `@mcp.tool()`. `kind`: `read` (local),
  `read_remote` (iPhone, Apple, internet), `write` (can be changed back), `destructive` (takes `confirm`
  and dry-runs without it). Reads and writes are separate tools.
- Returns a Pydantic model from `models.py`. `tools/list` publishes no `outputSchema`; field names,
  `next_steps` and `note` tell the agent what to do next.
- Shared parameters come from `core/params.py` (`ProjectPath`, `BundleId`, `Udid`, `JobId`, `WaitSeconds`,
  `MaxBytes`, …). A parameter a second module needs moves there. Background work is named `job_id`.
- Description at most 400 characters (what, when, job/dry-run/needs-the-human); parameter descriptions are
  short phrases; details go in `mcp/iosstudio_mcp/guide.md`. `tests/mcp/test_mcp_slim.py` checks the size.

## Time

- Every call answers within 25 s (`MAX_WAIT`). Step timeouts go through `deadline.clamp()` /
  `deadline.wait_time()`; `helper.call()` and `dcall()` already do.
- Work that is normally longer is a job in `state.jobs` (`core/jobs.py`): `state.jobs.run(fn, kind, title=...,
  tool="ios_x")` (or `start(...)` for helper streams), then `follow_job()` and `still_running(job)` from
  `core/job_status.py`. Work a second call should join uses `shared_job()` + `settle()`. Don't keep task
  registries in `state.extras`; pass `on_cancel` so cancelling a job stops the daemon side too.
- No blocking I/O in a handler: `asyncio.to_thread` and bounded reads.

## Output

- Fits the budget (12 KB default, `core/budget.py`): lists paged (`page_of`, `fit_page`, `paginate`
  cursors), long text clipped (`take`, `clip`), big results saved with `spill()` to the cache and returned as
  a path. No image bytes unless the caller asks (`include_image=true`).
- Errors are one line with a next step: phone calls go through `await dcall(state, "cmd", {...},
  hints={"code": "Next step."})`; add common codes to `core/errors.py`. Don't catch and re-raise to reword.
- Files the server stores are pruned; jobs finish or get cancelled when the server exits.

## Tests and docs

- Add `tests/mcp/test_mcp_<area>.py` with `support.mcp_harness.ToolCase`: `FakeDaemon` speaks the real
  daemon protocol to the real `DeviceClient`, `FakeHelper` is a real executable the real `helper.call()` runs,
  and tools run through the real `@tool` wrapper. Use an `asyncio.Event` as a reply delay, not sleeps.
- Cover: the happy path, a device/helper error and its next step, the 25 s path (job), paging/budget.
- The tool works in demo mode (`IOSSTUDIO_DEMO=1 bin/iosstudio-mcp`); update the tool table in `docs/mcp.md`
  and the server `INSTRUCTIONS` (under 15 lines) or `guide.md` when the workflow changes.
