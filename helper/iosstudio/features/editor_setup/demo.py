"""Demo mode (IOSSTUDIO_DEMO): made-up answers. Zed ready, Neovim (LazyVim) needs setup, VS Code not installed;
setup and --fix answer as if done but change nothing. HabitTracker has never been built (no .bsp yet)."""
from __future__ import annotations

import os
import time
from pathlib import Path

from iosstudio.core import output

from .commands import prefer_flag
from .editors import NVIM_PLUGIN_DETAIL, NVIM_PLUGIN_REL, next_step, nvim_plugin_text
from .env import EDITOR_USAGE, EDITORS, SETTING_KEY, Env
from .project import BSP_DETAIL, BSP_REL, BSP_TEXT, NOT_BUILT_NOTE, brief_of

DEMO_NO_BSP = {"HabitTracker"}


def _machine() -> dict:
    home = os.path.expanduser("~")
    return {"swift": "6.4", "xtool": home + "/.local/bin/xtool", "sourcekit_lsp": "/usr/bin/sourcekit-lsp",
            "preferred": "zed", "demo": True, "editors": [
                {"id": "vscode", "name": "VS Code", "installed": False, "binary": None, "state": "not-installed",
                 "extension": None, "configured": True, "files": [], "note": None, "notes": []},
                {"id": "zed", "name": "Zed", "installed": True, "binary": "/usr/bin/zeditor", "state": "ready",
                 "extension": home + "/.local/share/zed/extensions/installed/swift", "configured": True,
                 "files": [], "note": None, "notes": []},
                {"id": "neovim", "name": "Neovim", "installed": True, "binary": "/usr/bin/nvim",
                 "state": "needs-setup", "extension": None, "configured": False, "files": [], "lazyvim": True,
                 "note": f"Set up changes ~/.config/nvim/{NVIM_PLUGIN_REL}.", "notes": []}]}


def _setup(editor: str, confirm: bool, prefer: bool = True) -> dict:
    info = next(e for e in _machine()["editors"] if e["id"] == editor)
    acts: list[dict] = []
    if editor == "neovim":
        body = nvim_plugin_text(Env(Path("/"), Path("/"), Path("/"), "/usr/bin", probe_swift=False))
        acts.append({"path": os.path.expanduser("~/.config/nvim/" + NVIM_PLUGIN_REL), "scope": "user",
                     "kind": "write", "detail": NVIM_PLUGIN_DETAIL, "preview": body})
    settings = os.path.expanduser("~/.config/ios-studio/settings.json")
    if prefer and editor == "zed":
        acts.append({"path": settings, "scope": "user", "kind": "skip", "configured": True,
                     "detail": "Zed is already your editor."})
    elif prefer:
        acts.append({"path": settings, "scope": "user", "kind": "merge", "keys": [SETTING_KEY],
                     "detail": f"Remember {EDITORS[editor]} as your editor."})
    pending = any(a["kind"] in ("write", "merge") for a in acts)
    if confirm:
        for a in acts:
            if a["kind"] in ("write", "merge"):
                a["done"] = True
    state = info["state"] if not confirm or info["state"] == "not-installed" else "ready"
    nxt = None if not confirm and pending else next_step(editor)
    return {"editor": editor, "name": EDITORS[editor], "dryRun": not confirm, "actions": acts,
            "applied": confirm and pending, "state": state, "next": nxt, "notes": info["notes"], "demo": True}


def _project(path: str, fix: bool = False, confirm: bool = False) -> dict:
    name = path.rstrip("/").split("/")[-1] or "App"
    root = path.rstrip("/")
    has = name not in DEMO_NO_BSP
    out = {"project": path,
           "bsp": {"present": has, "stale": False, "reason": None, "path": root + "/" + BSP_REL, "kind": "bsp",
                   "built": has, "note": None if has else NOT_BUILT_NOTE},
           "ready": has, "hint": None if has else "Build once to enable completion", "preferred": "zed",
           "auto": {"at": time.time() - 3600, "wrote": [{"kind": "write", "path": root + "/" + BSP_REL}]}
           if name == "RecipeBox" else None, "pending": [], "created": name != "HabitTracker", "demo": True}
    if fix:
        act = {"path": root + "/" + BSP_REL, "scope": "project", "kind": "skip" if has else "write",
               "detail": "Already there." if has else BSP_DETAIL}
        if not has:
            act["preview"] = BSP_TEXT
            if confirm:
                act["done"] = True
        out["fix"] = {"dryRun": not confirm, "actions": [act], "applied": confirm and not has,
                      "next": "Nothing left to write for this project." if has else
                      ("Reopen the folder in your editor if it was open." if confirm else None)}
    return out


def demo_brief(path: str) -> dict:
    """The `editor` field of `projects info` in demo mode."""
    return brief_of(_project(path))


def demo(action: str, flags: dict) -> int:
    """Demo mode: made-up status; setup and --fix list their plan and write nothing."""
    path = flags.get("project") or flags.get("path")
    if action == "status":
        if not path or path is True:
            return output.ok(_machine())
        return output.ok(_project(str(path), bool(flags.get("fix")), bool(flags.get("confirm"))))
    if action == "setup":
        editor = str(flags.get("editor") or "")
        if editor not in EDITORS:
            return output.fail("usage", EDITOR_USAGE)
        return output.ok(_setup(editor, bool(flags.get("confirm")), prefer_flag(flags)))
    return output.fail("usage", f"Unknown command 'editor {action}'.")
