"""Registering tools: `@tool(mcp, "Title", kind=...)` instead of `@mcp.tool(...)`.

    tool(mcp, title, kind=...)  register an async tool; its annotations come from `kind` (see KINDS), its
                                Pydantic result goes through to_result() (output budget, see budget.py)
    page_of(...)                one byte-budgeted page of a list for offset-paginated tools
    fit_page(...)               trim a paged list until the whole result fits its budget
    dry_run(...)                the ActionResult a destructive tool returns when confirm is false
    slim_schemas(mcp)           drop schema noise (titles, null defaults) from every tool's published schemas
"""
from __future__ import annotations

import functools
import inspect
import json
from typing import Annotated, Any, Literal, get_args, get_origin

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ResourceError, ToolError
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel

from ..models import ActionResult
from . import budget as B

MAX_WAIT = 25  # seconds; stays under opencode's 30 s and Codex's 60 s default tool timeouts

Kind = Literal["read", "read_remote", "write", "destructive"]

# kind -> (readOnlyHint, destructiveHint, idempotentHint, openWorldHint). "remote" (open world) means the
# tool talks to the iPhone, Apple or the internet; local tools only touch this computer.
KINDS: dict[str, tuple[bool, bool, bool, bool]] = {
    "read": (True, False, True, False),
    "read_remote": (True, False, True, True),
    "write": (False, False, False, False),
    "destructive": (False, True, False, False),
}


def annotations(title: str, kind: Kind, *, idempotent: bool | None = None,
                open_world: bool | None = None) -> ToolAnnotations:
    """All four hints set explicitly, from `kind`; idempotent / open_world override the kind's default."""
    ro, destructive, idem, ow = KINDS[kind]
    return ToolAnnotations(title=title, read_only_hint=ro, destructive_hint=destructive,
                           idempotent_hint=idem if idempotent is None else idempotent,
                           open_world_hint=ow if open_world is None else open_world)


def to_result(value: Any) -> Any:
    """Pydantic model -> CallToolResult under the output budget.

    structuredContent is the whole model without nulls and empty defaults, bulk fields included, so clients
    that hand the model structuredContent see everything. The text block is the readable rendering: compact
    JSON of the other fields, then the model's BULK fields (log lines, crash text) as plain lines. The
    budget counts both copies: if text + structuredContent exceed the call's ceiling (DEFAULT_BYTES, or the
    tool's max_bytes), bulk lines and then lists are cut and the result is marked truncated, with a note.
    Resource links the tool attached (spilled files) are appended as resource_link blocks.
    Anything else (a CallToolResult the tool built itself) passes through."""
    if not isinstance(value, BaseModel):
        return value
    data, text = B.enforce(B.dump(value), getattr(type(value), "BULK", ()), B.ceiling(value))
    content: list = [TextContent(type="text", text=text)] + B.links(value)
    return CallToolResult(content=content, structured_content=data)


def _builds_own_result(ret: Any) -> bool:
    """`-> Annotated[CallToolResult, Model]`: the tool returns its own CallToolResult (e.g. with an image)."""
    return get_origin(ret) is Annotated and get_args(ret)[0] is CallToolResult


def tool(mcp: MCPServer, title: str, kind: Kind, *, idempotent: bool | None = None,
         open_world: bool | None = None, **kwargs: Any):
    """`@tool(mcp, "Title", kind="read")`: register an async tool whose return annotation is a Pydantic model
    (or `Annotated[CallToolResult, Model]` for a tool that builds its own result). The outputSchema still
    comes from that model; results go through to_result(); a ToolError or ResourceError leaves as one
    clipped line. kind: read (local, read-only), read_remote (read-only, talks to the iPhone/Apple/internet),
    write, destructive (deletes or overwrites something the user may want back)."""
    ann = annotations(title, kind, idempotent=idempotent, open_world=open_world)

    def deco(fn):
        sig = inspect.signature(fn, eval_str=True)
        ret = sig.return_annotation

        @functools.wraps(fn)
        async def wrapper(*args: Any, **kw: Any) -> Any:
            try:
                return to_result(await fn(*args, **kw))
            except (ToolError, ResourceError) as exc:   # one line + next step, never a raw dump
                raise ToolError(B.clip_error(str(exc))) from None

        if not _builds_own_result(ret):
            ret = Annotated[CallToolResult, ret]
        wrapper.__signature__ = sig.replace(return_annotation=ret)
        mcp.tool(annotations=ann, **kwargs)(wrapper)
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


def dry_run(message: str, would: list[str], data: dict | None = None) -> ActionResult:
    return ActionResult(done=False, message=message + " Nothing was changed. Ask the user to confirm, then call "
                        "again with confirm=true.", would=would, data=data,
                        next_steps=["Ask the user whether to proceed; only then repeat the call with confirm=true."])


# ------------------------------------------------------------------ smaller tools/list

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
