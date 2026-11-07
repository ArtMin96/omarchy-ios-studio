"""Resources (optional extras; everything here is also reachable through tools)."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ResourceError

from . import helper
from .core import budget as B
from .state import get_state, last_device_state

MAX_TEXT = 100_000   # resources are capped too, with a pointer to the tool that pages through the rest
GUIDE = Path(__file__).with_name("guide.md")   # the long version of the server's instructions


def _tail(text: str, hint: str, cap: int = MAX_TEXT) -> str:
    raw = text.encode()
    if len(raw) <= cap:
        return text
    tail = raw[-cap:].decode(errors="ignore")
    tail = tail[tail.find("\n") + 1:] or tail
    return f"…[first {(len(raw) - cap) // 1000} KB cut; read earlier lines with {hint}]\n" + tail


def _read_head(p: Path, hint: str, cap: int = MAX_TEXT) -> str:
    """The first `cap` bytes of a file as text, reading no more than that (blocking: run it in a thread)."""
    with open(p, "rb") as f:
        raw = f.read(cap + 1)
    if len(raw) <= cap:
        return raw.decode(errors="replace")
    return raw[:cap].decode(errors="ignore") + f"\n…[cut at {cap // 1000} KB; {hint}]\n"


def register(mcp: MCPServer) -> None:

    @mcp.resource("iosstudio://guide", name="guide", mime_type="text/markdown")
    async def guide() -> str:
        """How the iOS Studio tools fit together: builds, watch mode, packages, presets, jobs, the iPhone, screen
        and UI, app data, debugger, performance, free-account limits and human-only steps."""
        try:
            return await asyncio.to_thread(_read_head, GUIDE, f"the whole guide is {GUIDE}")
        except OSError:
            raise ResourceError("The guide isn't in this install.")

    @mcp.resource("iosstudio://jobs/{job_id}/log", name="job-log", mime_type="text/plain")
    async def job_log(job_id: str, ctx: Context) -> str:
        """The in-memory output of a build or other job started in this session: its last 100 KB
        (ios_build_log pages through a build's whole log)."""
        state = get_state(ctx)
        job = state.jobs.jobs.get(job_id)
        if job is None:
            raise ResourceError(f"Unknown job_id '{job_id}'.")
        return _tail("\n".join(job.lines), f"ios_build_log(job_id='{job_id}', before=...)")

    @mcp.resource("iosstudio://cache/{kind}/{name}", name="saved-output", mime_type="text/plain")
    async def saved_output(kind: str, name: str) -> str:
        """A large output a tool saved to ~/.cache/ios-studio/mcp (its full_output_path): first 100 KB."""
        p = await asyncio.to_thread(B.cache_file, kind, name)
        if p is None:
            raise ResourceError(f"No saved output {kind}/{name}; it may have been replaced by newer ones.")
        try:
            return await asyncio.to_thread(_read_head, p, f"the whole file is {helper.tilde(str(p))}")
        except OSError:
            raise ResourceError(f"Couldn't read saved output {kind}/{name}.")

    @mcp.resource("iosstudio://device/state", name="device-state", mime_type="application/json")
    async def device_state() -> str:
        """The last known iPhone state written by the device daemon (udid, model, iOS, connection)."""
        return json.dumps(await asyncio.to_thread(last_device_state) or {}, indent=2)

    @mcp.resource("iosstudio://docs/troubleshooting", name="troubleshooting", mime_type="text/markdown")
    async def troubleshooting() -> str:
        """iOS Studio's troubleshooting guide (signing, pairing, builds, toolchain)."""
        p = helper.REPO / "docs" / "troubleshooting.md"
        try:
            return await asyncio.to_thread(_read_head, p, f"the whole guide is {p}")
        except OSError:
            raise ResourceError("The troubleshooting guide isn't in this install.")

    @mcp.resource("iosstudio://docs/mcp", name="mcp-guide", mime_type="text/markdown")
    async def mcp_guide() -> str:
        """How this MCP server works: tools, jobs, the safety model and demo mode."""
        p = helper.REPO / "docs" / "mcp.md"
        try:
            return await asyncio.to_thread(_read_head, p, f"the whole page is {p}")
        except OSError:
            raise ResourceError("docs/mcp.md isn't in this install.")
