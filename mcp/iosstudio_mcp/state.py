"""Lifespan state shared by every tool module, and the helpers tools use to reach it.

    state = get_state(ctx)
    udid = await state.active_udid()                    # device client -> last-known device.json -> None

Projects are resolved with backends/projects.py (resolve_project).

Device integration (filled in by backends/device.py attach(), called from tools/device.py's on_startup;
the client connects lazily on the first device tool call, so agents that never touch the phone open no
tunnel; tools call the phone with `backends.device.dcall(state, cmd, args)`, which maps DeviceError to a
ToolError naming the next tool):
    state.device          the device client object (helper/iosstudio/device/device_client.py), or None until first use
    state.install_app     async (udid, ipa_path, job) -> dict      used by ios_run after a Wi-Fi build
    state.launch_app      async (udid, bundle_id, args=None, env=None) -> dict   used by ios_run after install
    state.device_udid     async () -> str | None                    the active iPhone's udid
    state.device_status   async () -> dict | None                   live device.json-shaped dict (ios_status)
A tool module may define `async def on_startup(state)` / `async def on_shutdown(state)`; the server
calls them from the lifespan, in module order.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from mcp.server.mcpserver import Context

from . import helper
from .core.jobs import Job, JobManager

DEVICE_STATE = helper.appdirs.state_dir() / "device.json"


@dataclass
class AppState:
    jobs: JobManager = field(default_factory=JobManager)
    # --- device slots (backends/device.py attach())
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
        st = await asyncio.to_thread(last_device_state)
        return st.get("udid") if st else None


def last_device_state() -> dict | None:
    """The daemon's last-known device (~/.local/state/ios-studio/device.json), if any. Blocking (a small
    file): async code calls it with asyncio.to_thread."""
    try:
        data = json.loads(DEVICE_STATE.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) and data.get("udid") else None


def get_state(ctx: Context) -> AppState:
    return ctx.request_context.lifespan_context
