"""Projects: resolving a project_path argument, and run presets.

    p = await resolve_project(state, project_path)    # validated iOS Studio project entry, or ToolError
    how = await resolve_preset(state, name, project_path=..., bundle_id=...)   # a run preset's args/env
    args, env = merge_launch(how, args, env)
"""
from __future__ import annotations

from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError

from .. import helper
from ..state import AppState


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


# ------------------------------------------------------------------ run presets (tools/run_presets.py)

def plain_bundle(bid: str | None) -> str:
    """com.example.app from XTL-<TEAM>.com.example.app (free teams' installed form)."""
    b = str(bid or "").strip()
    return b.split(".", 1)[1] if b.startswith("XTL-") and "." in b else b


async def project_for_bundle(state: AppState, bundle_id: str) -> dict:
    """The listed project whose bundle id is `bundle_id` (plain or XTL- form), or a ToolError asking for
    project_path."""
    want = plain_bundle(bundle_id)
    items = (await state.projects()).get("projects") or []
    hits = [p for p in items if want and plain_bundle(p.get("bundleId")) == want] or \
        [p for p in items if want and str(p.get("name") or "").lower() == want.lower()]   # a display name
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        paths = ", ".join(helper.tilde(p.get("path")) or "" for p in hits[:5])
        raise ToolError(f"Several projects build {want} ({paths}). Pass project_path to say whose presets to use.")
    raise ToolError(f"No listed project builds {want}, so its presets can't be found. Pass project_path "
                    "(ios_project_list shows projects).")


async def resolve_preset(state: AppState, preset: str, *, project_path: str | None = None,
                         bundle_id: str | None = None) -> dict:
    """{"name", "args", "env"} of a project's preset. The project is project_path, else the listed project
    that builds bundle_id. An unknown name raises the helper's unknown-preset error (it lists the names)."""
    if project_path:
        p = await resolve_project(state, project_path)
    elif bundle_id:
        p = await project_for_bundle(state, bundle_id)
    else:
        raise ToolError("preset needs the app's project: pass project_path or bundle_id.")
    data = await helper.call("projects", "presets", {"path": p["path"], "resolve": preset})
    data = data if isinstance(data, dict) else {}
    return {"name": str(data.get("name") or preset), "args": [str(x) for x in data.get("args") or []],
            "env": {str(k): str(v) for k, v in (data.get("env") or {}).items()}, "project": p}


def merge_launch(resolved: dict | None, args: list[str] | None, env: dict[str, str] | None
                 ) -> tuple[list[str] | None, dict[str, str] | None]:
    """The preset's args then the explicit ones; the explicit env overrides the preset's."""
    if not resolved:
        return args, env
    a = list(resolved.get("args") or []) + list(args or [])
    e = {**(resolved.get("env") or {}), **(env or {})}
    return a or None, e or None
