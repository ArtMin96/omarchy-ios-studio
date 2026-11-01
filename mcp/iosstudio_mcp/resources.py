"""Resources (optional extras; everything here is also reachable through tools)."""
from __future__ import annotations

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ResourceError

from . import helper
from .state import get_state, last_device_state

MAX_TEXT = 400_000


def register(mcp: MCPServer) -> None:

    @mcp.resource("iosstudio://builds/{build_id}/log", name="build-log", mime_type="text/plain")
    async def build_log(build_id: str, ctx: Context) -> str:
        """Full in-memory output of a build or job started in this session (same as ios_build_log)."""
        state = get_state(ctx)
        job = state.jobs.jobs.get(build_id)
        if job is None:
            raise ResourceError(f"Unknown build_id '{build_id}'.")
        text = "\n".join(job.lines)
        return text[-MAX_TEXT:]

    @mcp.resource("iosstudio://device/state", name="device-state", mime_type="application/json")
    def device_state() -> str:
        """The last known iPhone state written by the device daemon (udid, model, iOS, connection)."""
        import json
        return json.dumps(last_device_state() or {}, indent=2)

    @mcp.resource("iosstudio://docs/troubleshooting", name="troubleshooting", mime_type="text/markdown")
    def troubleshooting() -> str:
        """iOS Studio's troubleshooting guide (signing, pairing, builds, toolchain)."""
        p = helper.REPO / "docs" / "troubleshooting.md"
        try:
            return p.read_text()[:MAX_TEXT]
        except OSError:
            raise ResourceError("The troubleshooting guide isn't in this install.")

    @mcp.resource("iosstudio://docs/mcp", name="mcp-guide", mime_type="text/markdown")
    def mcp_guide() -> str:
        """How this MCP server works: tools, jobs, the safety model and demo mode."""
        p = helper.REPO / "docs" / "mcp.md"
        try:
            return p.read_text()[:MAX_TEXT]
        except OSError:
            raise ResourceError("docs/mcp.md isn't in this install.")
