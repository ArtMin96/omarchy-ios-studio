"""Shared pieces every tool module uses; nothing here knows about a particular tool.

    registry.py   tool(mcp, title, kind=...), to_result, page_of, fit_page, dry_run, slim_schemas, MAX_WAIT
    params.py     the parameter types tools share (Udid, BundleId, ProjectPath, MaxBytes, WaitSeconds, …)
    errors.py     error code -> one-line message + next step (device errors and feature hints)
    job_status.py BuildStatus views of helper jobs and the long-poll helpers
    budget.py     output size rules (see docs/mcp.md "Output size")

Tool modules import from core/ and backends/ only, never from each other.
"""
