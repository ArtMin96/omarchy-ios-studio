"""ios_open_app: hand a step to the human in the iOS Studio window (sign-in, pairing, Setup)."""
from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from .. import helper
from ..models import ActionResult
from . import ann, tool

PAGES = ("home", "setup", "account", "device", "projects", "logs", "crashes", "toolchain", "settings")

HUMAN_STEP = {
    "account": "Ask the user to sign in with their Apple ID in the iOS Studio window (password and 2FA code are "
               "typed there, never through the agent). Then call ios_account_status.",
    "device": "Ask the user to connect the iPhone (cable, or pair over Wi-Fi), unlock it and tap Trust; "
              "Developer Mode may need enabling on the phone. Then call ios_status.",
    "setup": "Ask the user to finish the highlighted Setup step in the window (some steps need their password "
             "or an Xcode download). Then call ios_toolchain_check.",
}



def _session_env() -> dict[str, str]:
    """This process's env plus the Hyprland/Wayland session variables, found under XDG_RUNTIME_DIR when the
    agent started the server with a stripped environment."""
    env = dict(os.environ)
    runtime = Path(env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
    if runtime.is_dir():
        env.setdefault("XDG_RUNTIME_DIR", str(runtime))
        if not env.get("HYPRLAND_INSTANCE_SIGNATURE"):
            insts = sorted((d for d in (runtime / "hypr").glob("*") if (d / ".socket.sock").exists()),
                           key=lambda d: d.stat().st_mtime, reverse=True) if (runtime / "hypr").is_dir() else []
            if insts:
                env["HYPRLAND_INSTANCE_SIGNATURE"] = insts[0].name
        if not env.get("WAYLAND_DISPLAY"):
            socks = sorted(p.name for p in runtime.glob("wayland-*") if not p.name.endswith(".lock"))
            if socks:
                env["WAYLAND_DISPLAY"] = socks[0]
    return env


def register(mcp: MCPServer) -> None:

    @tool(mcp, annotations=ann("Open iOS Studio", read_only=False, idempotent=True))
    async def ios_open_app(
        page: Annotated[Literal[PAGES], Field(description="Page to show. 'account' for Apple ID sign-in, 'device' "
                                                          "for pairing/Developer Mode, 'setup' for manual setup "
                                                          "steps, 'logs'/'crashes' for the live log")] = "home",
    ) -> ActionResult:
        """Open (or focus) the iOS Studio window on a page, for steps only the human can do.

        Use it when a tool reports a human step: signing in to the Apple ID (password and 2FA are typed
        in the app, never through MCP), pairing the iPhone, enabling Developer Mode, or Setup steps that
        need the user's password or a download. Tell the user what to do there, then re-check with
        ios_status / ios_account_status. Returns immediately; it doesn't wait for the user.
        """
        if not helper.APP_LAUNCHER.exists():
            raise ToolError("The iOS Studio launcher (bin/ios-studio) is missing from this install.")
        env = _session_env()
        if not env.get("HYPRLAND_INSTANCE_SIGNATURE"):
            return ActionResult(done=False, message="No graphical session is visible to this MCP server, so the "
                                "window can't be opened from here. Ask the user to open iOS Studio and go to the "
                                f"'{page}' page.", next_steps=[HUMAN_STEP.get(page, "Ask the user to open iOS Studio.")])
        argv = [str(helper.APP_LAUNCHER)]
        demo = os.environ.get("IOSSTUDIO_DEMO")
        if demo:
            argv.append(f"--demo={demo if demo not in ('1', 'true', 'yes') else 'ready'}")
        argv.append(page)
        # stderr goes to a file, not a pipe: the launcher may outlive this call and must never block on it.
        with tempfile.TemporaryFile() as errf:
            proc = await asyncio.create_subprocess_exec(*argv, env=env, stdin=asyncio.subprocess.DEVNULL,
                                                        stdout=asyncio.subprocess.DEVNULL, stderr=errf,
                                                        start_new_session=True)
            # The launcher refuses at once when the open window is in the other mode (real vs demo).
            try:
                await asyncio.wait_for(proc.wait(), 1.5)
            except asyncio.TimeoutError:
                pass
            errf.seek(0)
            err = errf.read(2000)
        if proc.returncode not in (None, 0):
            why = err.decode(errors="replace").strip().removeprefix("ios-studio: ") or "The launcher failed."
            return ActionResult(done=False, message=why,
                                next_steps=["Ask the user to close the open iOS Studio window, then call ios_open_app "
                                            "again.", HUMAN_STEP.get(page, "Ask the user to open iOS Studio.")])
        return ActionResult(done=True, message=f"Opened iOS Studio on the {page} page.",
                            next_steps=[HUMAN_STEP.get(page, "Tell the user what to look at in the window.")])
