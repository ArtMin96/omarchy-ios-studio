"""iOS Studio MCP server (stdio). Run with `bin/iosstudio-mcp` or `python -m iosstudio_mcp`.

stdout carries the MCP protocol only; all logging goes to stderr (IOSSTUDIO_MCP_LOG=DEBUG for more).
"""
from __future__ import annotations

import logging
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from mcp.server.mcpserver import MCPServer

from . import __version__, prompts, resources, tools
from .core.registry import slim_schemas
from .state import AppState

log = logging.getLogger("iosstudio")

INSTRUCTIONS = """\
iOS Studio builds, signs, installs and debugs iPhone apps on this Linux computer (Swift + xtool, no Mac).

- Start with ios_status: it reports toolchain, Apple ID, iPhone and projects, and names the tool that fixes each problem.
- Projects are SwiftPM packages with an xtool.yml (ios_project_list); pass the path or project name as project_path.
- Build: ios_build_start(action='compile') needs no Apple ID or iPhone; ios_run builds, installs and launches. Then ios_build_diagnostics.
- Every call answers within 25 s. Longer work returns state='running' + job_id: call the same tool again with job_id (or ios_job_status). state='needs_input': show pending_prompt to the user, send THEIR answer with ios_job_respond.
- Destructive tools take confirm=false by default and return a dry run: ask the user, then repeat with confirm=true.
- Human-only steps (never ask for passwords or 2FA): Apple ID sign-in, Xcode .xip download, pairing/Trust, Developer Mode, polkit dialogs. Explain them and use ios_open_app(page=...).
- Results are kept small (~12 KB); a cut result has truncated=true and a `note` on how to get more; next_steps says what to call next.
- udid parameters default to the active iPhone.
- Read the resource iosstudio://guide for how the tools fit together (watch mode, hot reload, packages, presets, screen/UI, app data, debugger, performance).
"""


TOOL_MODULES = tools.load()


@asynccontextmanager
async def lifespan(server: MCPServer) -> AsyncIterator[AppState]:
    state = AppState()
    started = []
    try:
        for mod in TOOL_MODULES:
            hook = getattr(mod, "on_startup", None)
            if hook:
                try:
                    await hook(state)
                    started.append(mod)
                except Exception:  # noqa: BLE001  one module's startup must not take the server down
                    log.exception("startup hook of %s failed", mod.__name__)
            else:
                started.append(mod)
        yield state
    finally:
        await state.jobs.shutdown()
        for mod in reversed(started):
            hook = getattr(mod, "on_shutdown", None)
            if hook:
                try:
                    await hook(state)
                except Exception:  # noqa: BLE001
                    log.exception("shutdown hook of %s failed", mod.__name__)


def create_server() -> MCPServer:
    mcp = MCPServer("iosstudio", title="iOS Studio", version=__version__, instructions=INSTRUCTIONS,
                    website_url="https://github.com/ArtMin96/omarchy-ios-studio", lifespan=lifespan,
                    log_level=os.environ.get("IOSSTUDIO_MCP_LOG", "INFO").upper())
    tools.register(mcp, TOOL_MODULES)
    prompts.register(mcp)
    resources.register(mcp)
    slim_schemas(mcp)
    return mcp


mcp = create_server()


def main() -> None:
    logging.basicConfig(stream=sys.stderr, level=os.environ.get("IOSSTUDIO_MCP_LOG", "INFO").upper(),
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    log.info("iOS Studio MCP %s starting (demo=%s)", __version__, bool(os.environ.get("IOSSTUDIO_DEMO")))
    mcp.run("stdio")


if __name__ == "__main__":
    main()
