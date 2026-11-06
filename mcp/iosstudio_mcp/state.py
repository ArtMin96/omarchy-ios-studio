"""Lifespan state shared by every tool module, and the helpers tools use to reach it.

    state = get_state(ctx)
    proj = await resolve_project(state, project_path)   # validated iOS Studio project or ToolError
    udid = await state.active_udid()                    # device client -> last-known device.json -> None

Device integration (filled in by tools/device.py's on_startup; the client connects lazily on the first
device tool call, so agents that never touch the phone open no tunnel; device tools call the phone with
`tools.device.dcall(state, cmd, args)`, which maps DeviceError to a ToolError naming the next tool):
    state.device          the device client object (helper/device_client.py), or None until first use
    state.install_app     async (udid, ipa_path, job) -> dict      used by ios_run after a Wi-Fi build
    state.launch_app      async (udid, bundle_id, args=None, env=None) -> dict   used by ios_run after install
    state.device_udid     async () -> str | None                    the active iPhone's udid
    state.device_status   async () -> dict | None                   live device.json-shaped dict (ios_status)
A tool module may define `async def on_startup(state)` / `async def on_shutdown(state)`; the server
calls them from the lifespan, in module order.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError

from . import helper
from .jobs import Job, JobManager

DEVICE_STATE = Path.home() / ".local/state/ios-studio/device.json"


@dataclass
class AppState:
    jobs: JobManager = field(default_factory=JobManager)
    # --- device slots (tools/device.py)
    device: Any = None
    install_app: Callable[[str, str, Job], Awaitable[dict]] | None = None
    launch_app: Callable[..., Awaitable[dict]] | None = None   # (udid, bundle_id, args=None, env=None)
    device_udid: Callable[[], Awaitable[str | None]] | None = None
    device_status: Callable[[], Awaitable[dict | None]] | None = None   # device.json-shaped dict
    # --- free-form per-module storage: "device" (event cache), "logs.sessions" (log ring buffers)
    extras: dict[str, Any] = field(default_factory=dict)
    # --- caches
    _projects: tuple[float, dict] | None = None

    async def projects(self, max_age: float = 10.0) -> dict:
        """`projects list` data, cached briefly (it scans the project folders)."""
        if self._projects and time.time() - self._projects[0] < max_age:
            return self._projects[1]
        data = await helper.call("projects", "list", timeout=40)
        self._projects = (time.time(), data)
        return data

    def invalidate_projects(self) -> None:
        self._projects = None

    async def active_udid(self) -> str | None:
        """udid of the active iPhone: the device client's answer, else the last-known device state."""
        if self.device_udid:
            try:
                u = await self.device_udid()
                if u:
                    return u
            except Exception:  # noqa: BLE001  fall back to the state file
                pass
        if helper.demo_mode():
            return "00008150-000000000000001C"
        st = last_device_state()
        return st.get("udid") if st else None


def last_device_state() -> dict | None:
    """The daemon's last-known device (~/.local/state/ios-studio/device.json), if any."""
    try:
        data = json.loads(DEVICE_STATE.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) and data.get("udid") else None


def get_state(ctx: Context) -> AppState:
    return ctx.request_context.lifespan_context


# ------------------------------------------------------------------ projects

async def resolve_project(state: AppState, project: str) -> dict:
    """Validate `project` (a path or a project name) and return its `projects list` entry.

    Accepts an absolute or ~ path to an iOS Studio project (Package.swift + xtool.yml), or the name of a
    listed project. Raises a ToolError that says which tool to call otherwise.
    """
    raw = (project or "").strip()
    if not raw:
        raise ToolError("project_path is required. Call ios_project_list to see projects.")
    data = await state.projects()
    items = data.get("projects") or []
    looks_like_path = "/" in raw or raw.startswith("~") or raw.startswith(".")
    if looks_like_path:
        p = helper.expand(raw)
        sp = str(p)
        for it in items:
            if it.get("path") == sp:
                return it
        if helper.demo_mode():
            raise ToolError(f"'{raw}' isn't one of the demo projects. Call ios_project_list.")
        if not p.is_dir():
            raise ToolError(f"No folder at {helper.tilde(sp)}. Call ios_project_list for valid paths.")
        if not helper.is_project_dir(p):
            if (p / "Package.swift").is_file():
                raise ToolError(f"{helper.tilde(sp)} is a Swift package without xtool.yml, so it isn't an iOS app yet. "
                                "Call ios_project_convert(project_path=...) to make it one.")
            raise ToolError(f"{helper.tilde(sp)} isn't an iOS Studio project (it needs Package.swift and xtool.yml). "
                            "Use ios_project_create for a new app, or ios_project_list.")
        if not helper.inside_home(p):
            raise ToolError("Projects must live inside your home folder (or a folder listed in IOSSTUDIO_MCP_ALLOW_PATHS).")
        # A valid project that's hidden or outside the scanned roots: ask the helper directly.
        return await helper.call("projects", "info", {"path": sp})
    matches = [it for it in items if str(it.get("name", "")).lower() == raw.lower()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        paths = ", ".join(helper.tilde(m.get("path")) or "" for m in matches)
        raise ToolError(f"Several projects are named '{raw}' ({paths}). Pass the full project_path.")
    names = ", ".join(str(it.get("name")) for it in items[:15]) or "none found"
    raise ToolError(f"No project named '{raw}'. Known projects: {names}. Pass a path or call ios_project_list.")


def existing_dir(raw: str, what: str = "folder") -> Path:
    """An existing directory inside $HOME (for add/convert)."""
    p = helper.expand(raw)
    if not p.is_dir():
        raise ToolError(f"No {what} at {helper.tilde(str(p))}.")
    if not helper.inside_home(p):
        raise ToolError(f"The {what} must be inside your home folder (or IOSSTUDIO_MCP_ALLOW_PATHS).")
    return p
