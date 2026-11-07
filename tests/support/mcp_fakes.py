"""MCP tool-test helpers. Needs the MCP server's dependencies (tests/mcp/ skips without them)."""
from __future__ import annotations

import json

from iosstudio_mcp.core import budget as B


class FakeMCP:
    """Stands in for the server: `register(FakeMCP(), state)` collects the tool functions by name."""

    def __init__(self):
        self.tools = {}

    def tool(self, **kw):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


def size_of(res) -> int:
    """Bytes a tool result costs the agent: its text content plus its structured content as compact JSON."""
    text = "".join(c.text for c in res.content if getattr(c, "text", None))
    return B.nbytes(text) + B.nbytes(json.dumps(res.structured_content, ensure_ascii=False, separators=(",", ":")))
