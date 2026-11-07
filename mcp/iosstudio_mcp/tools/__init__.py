"""Tool modules. Each exposes `register(mcp)`; optional `on_startup(state)` / `on_shutdown(state)`.

A tool module imports from core/ (registry, params, errors, job_status, budget) and backends/ (device,
projects, builds, console, crashes), never from another tool module. See docs/mcp.md "For tool authors".
"""
from __future__ import annotations

import importlib
import logging
from types import ModuleType

from mcp.server.mcpserver import MCPServer

log = logging.getLogger("iosstudio.tools")

# Order matters only for on_startup hooks. A module missing from this install is skipped.
MODULES = ["status", "projects", "build_settings", "project_check", "run_presets", "build", "account", "toolchain", "app", "device", "logs", "crashes", "screen", "network", "metrics", "files", "debug", "console", "ui_inspect", "screenshots", "editor_setup", "watch", "format", "packages", "app_data"]


def load() -> list[ModuleType]:
    mods = []
    for name in MODULES:
        full = f"{__name__}.{name}"
        try:
            mods.append(importlib.import_module(full))
        except ModuleNotFoundError as exc:
            if exc.name == full:   # optional module missing from this install
                log.debug("tool module %s not present", name)
                continue
            raise
    return mods


def register(mcp: MCPServer, modules: list[ModuleType]) -> None:
    for mod in modules:
        mod.register(mcp)
