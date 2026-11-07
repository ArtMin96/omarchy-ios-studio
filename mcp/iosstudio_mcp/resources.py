"""Resources (optional extras; everything here is also reachable through tools)."""
from __future__ import annotations

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ResourceError

from . import helper
from .core import budget as B
from .state import get_state, last_device_state

MAX_TEXT = 100_000   # resources are capped too, with a pointer to the tool that pages through the rest


def _tail(text: str, hint: str, cap: int = MAX_TEXT) -> str:
    raw = text.encode()
    if len(raw) <= cap:
        return text
    tail = raw[-cap:].decode(errors="ignore")
    tail = tail[tail.find("\n") + 1:] or tail
    return f"…[first {(len(raw) - cap) // 1000} KB cut; read earlier lines with {hint}]\n" + tail


def _head(text: str, hint: str, cap: int = MAX_TEXT) -> str:
    raw = text.encode()
    if len(raw) <= cap:
        return text
    return raw[:cap].decode(errors="ignore") + f"\n…[cut at {cap // 1000} KB; {hint}]\n"


def register(mcp: MCPServer) -> None:

    @mcp.resource("iosstudio://builds/{build_id}/log", name="build-log", mime_type="text/plain")
    async def build_log(build_id: str, ctx: Context) -> str:
        """The in-memory output of a build or job started in this session: its last 100 KB (ios_build_log
        pages through all of it)."""
        state = get_state(ctx)
        job = state.jobs.jobs.get(build_id)
        if job is None:
            raise ResourceError(f"Unknown build_id '{build_id}'.")
        return _tail("\n".join(job.lines), f"ios_build_log(build_id='{build_id}', before=...)")

    @mcp.resource("iosstudio://cache/{kind}/{name}", name="saved-output", mime_type="text/plain")
    def saved_output(kind: str, name: str) -> str:
        """A large output a tool saved to ~/.cache/ios-studio/mcp (its full_output_path): first 100 KB."""
        p = B.cache_file(kind, name)
        if p is None:
            raise ResourceError(f"No saved output {kind}/{name}; it may have been replaced by newer ones.")
        return _head(p.read_text(errors="replace"), f"the whole file is {helper.tilde(str(p))}")

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
            return _head(p.read_text(), f"the whole guide is {p}")
        except OSError:
            raise ResourceError("The troubleshooting guide isn't in this install.")

    @mcp.resource("iosstudio://docs/mcp", name="mcp-guide", mime_type="text/markdown")
    def mcp_guide() -> str:
        """How this MCP server works: tools, jobs, the safety model and demo mode."""
        p = helper.REPO / "docs" / "mcp.md"
        try:
            return _head(p.read_text(), f"the whole page is {p}")
        except OSError:
            raise ResourceError("docs/mcp.md isn't in this install.")
