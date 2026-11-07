"""Where iOS Studio keeps things (stdlib only): the XDG folders, settings.json, project roots and the JSON cache."""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from iosstudio.core import fileio

HOME = Path.home()
REPO = Path(__file__).resolve().parents[3]   # helper/iosstudio/core/appdirs.py -> the checkout
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config")) / "ios-studio"
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", HOME / ".local/state")) / "ios-studio"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", HOME / ".cache")) / "ios-studio"
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", HOME / ".local/share")) / "ios-studio"
VENV_DIR = DATA_DIR / "venv"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
LOCAL_BIN = HOME / ".local/bin"
XTOOL = LOCAL_BIN / "xtool"


def settings() -> dict:
    return fileio.load_json(SETTINGS_FILE, {}) or {}


# Where people usually keep code. The defaults for "projectRoots" are whichever of these exist;
# new apps go to ~/Developer (Apple's convention), created on first use.
PROJECT_ROOT_CANDIDATES = ("Developer", "Projects", "projects", "Code", "code", "src", "dev", "Work")
NEW_APP_DIR = HOME / "Developer"


def default_project_roots() -> list[Path]:
    out, seen = [], set()
    for name in PROJECT_ROOT_CANDIDATES:
        p = HOME / name
        try:
            key = p.resolve()
        except OSError:
            continue
        if p.is_dir() and key not in seen:   # ~/Code and ~/code are one folder on case-insensitive filesystems
            seen.add(key)
            out.append(p)
    return out


def project_roots() -> list[Path]:
    """Configured project roots (Settings), else the detected defaults. Only existing folders."""
    roots = settings().get("projectRoots")
    if not roots:
        return default_project_roots()
    return [p for p in (Path(os.path.expanduser(str(r))) for r in roots) if p.is_dir()]


def new_app_dir() -> Path:
    """Where "New app" creates projects unless the user picks another folder."""
    roots = settings().get("projectRoots")
    if roots:
        return Path(os.path.expanduser(str(roots[0])))
    return NEW_APP_DIR


def cache_get(name: str, max_age: float | None = None) -> Any:
    """Return cached payload {"at": ts, "data": ...} or None."""
    entry = fileio.load_json(CACHE_DIR / f"{name}.json")
    if not isinstance(entry, dict):
        return None
    if max_age is not None and time.time() - float(entry.get("at", 0)) > max_age:
        return None
    return entry


def cache_put(name: str, data: Any) -> dict:
    entry = {"at": time.time(), "data": data}
    fileio.save_json(CACHE_DIR / f"{name}.json", entry)
    return entry
