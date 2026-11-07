"""Registering tools: `@tool(mcp, "Title", kind=...)` instead of `@mcp.tool(...)`.

    tool(mcp, title, kind=...)  register an async tool; its annotations come from `kind` (see KINDS), its
                                Pydantic result goes through to_result() (output budget, see budget.py)
    page_of(...)                one byte-budgeted page of a list for offset-paginated tools
    fit_page(...)               trim a paged list until the whole result fits its budget
    dry_run(...)                the ActionResult a destructive tool returns when confirm is false
    slim_schemas(mcp)           a smaller tools/list: folded descriptions, slim input schemas, no outputSchema
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
from .deadline import MAX_WAIT, call_budget

__all__ = ["MAX_WAIT", "tool", "to_result", "page_of", "fit_page", "dry_run", "slim_schemas", "annotations"]

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
    if not isinstance(value, BaseModel) or isinstance(value, CallToolResult):   # (CallToolResult is a model too)
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
    clipped line. The call runs inside a MAX_WAIT time budget (core/deadline.py). kind: read (local, read-only), read_remote (read-only, talks to the iPhone/Apple/internet),
    write, destructive (deletes or overwrites something the user may want back)."""
    ann = annotations(title, kind, idempotent=idempotent, open_world=open_world)

    def deco(fn):
        sig = inspect.signature(fn, eval_str=True)
        ret = sig.return_annotation

        @functools.wraps(fn)
        async def wrapper(*args: Any, **kw: Any) -> Any:
            try:
                with call_budget():          # every step of the call fits in MAX_WAIT (core/deadline.py)
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

def _slim(schema: Any, required: frozenset = frozenset()) -> Any:
    """Schema noise off: `title` keywords and `default: null`; `anyOf` [X, null] with a plain X becomes X (an
    optional parameter is simply left out, so null isn't advertised; the server still accepts it), and long
    whitespace in descriptions is folded. What a call accepts is unchanged."""
    if isinstance(schema, list):
        return [_slim(x) for x in schema]
    if not isinstance(schema, dict):
        return schema
    out: dict = {}
    for k, v in schema.items():
        if k == "properties" and isinstance(v, dict):
            req = frozenset(schema.get("required") or ())
            out[k] = {name: _slim_param(sub, name in req) for name, sub in v.items()}
        elif k in ("$defs", "definitions") and isinstance(v, dict):
            out[k] = {name: _slim(sub) for name, sub in v.items()}
        elif k == "title" and isinstance(v, str):
            continue
        elif k == "default" and v is None:
            continue
        elif k == "description" and isinstance(v, str):
            out[k] = " ".join(v.split())
        else:
            out[k] = _slim(v)
    return out


def _slim_param(schema: Any, required: bool) -> Any:
    out = _slim(schema)
    any_of = out.get("anyOf") if isinstance(out, dict) else None
    if not required and isinstance(any_of, list) and len(any_of) == 2 and {"type": "null"} in any_of:
        other = next(x for x in any_of if x != {"type": "null"})
        out = {**{k: v for k, v in out.items() if k != "anyOf"}, **other}
    return out


def slim_schemas(mcp: MCPServer) -> None:
    """Smaller tools/list (docs/mcp.md, "For tool authors"): descriptions folded to one line, input schemas
    without noise (see _slim), and no published outputSchema. A model never sees an outputSchema (function
    calling sends name, description and input schema only) and reads the result itself, so the schemas only
    cost every client bytes; results are still checked against the tool's Pydantic model on the server."""
    for t in mcp._tool_manager.list_tools():
        t.description = " ".join((t.description or "").split())
        t.parameters = _slim(t.parameters)
        t.__dict__["output_schema"] = None      # Tool.output_schema is a cached_property: published as none
