"""Where iOS Studio keeps things (stdlib only): the XDG folders, settings.json, project roots and the JSON cache."""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from iosstudio.core import fileio

APP = "ios-studio"


# The XDG base folders, read from the environment on every call (an empty variable means the default).
def config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def state_home() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")


def cache_home() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")


def data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")


# iOS Studio's own folder in each of them.
def config_dir() -> Path:
    return config_home() / APP


def state_dir() -> Path:
    return state_home() / APP


def cache_dir() -> Path:
    return cache_home() / APP


def data_dir() -> Path:
    return data_home() / APP


def sdk_bundles(config: Path | None = None, home: Path | None = None) -> list[Path]:
    """Where xtool's Darwin Swift SDK bundle can be, most likely first (SwiftPM uses $XDG_CONFIG_HOME/swiftpm)."""
    config, home = config or config_home(), home or Path.home()
    out: list[Path] = []
    for p in (config / "swiftpm/swift-sdks/darwin.artifactbundle", home / ".config/swiftpm/swift-sdks/darwin.artifactbundle",
              home / ".swiftpm/swift-sdks/darwin.artifactbundle"):
        if p not in out:
            out.append(p)
    return out


def sdk_bundle(config: Path | None = None, home: Path | None = None) -> Path | None:
    """The installed Darwin Swift SDK bundle, or None."""
    return next((p for p in sdk_bundles(config, home) if p.is_dir()), None)


def tilde(path: str | Path | None, home: str | Path | None = None) -> str:
    """`path` with the home folder shown as ~ (short, and keeps the user's home out of outputs)."""
    s, h = str(path or ""), str(home or Path.home()).rstrip("/")
    return "~" + s[len(h):] if s == h or s.startswith(h + "/") else s


# Snapshots taken at import, for the many call sites that use them; new code calls the functions above.
HOME = Path.home()
REPO = Path(__file__).resolve().parents[3]   # helper/iosstudio/core/appdirs.py -> the checkout
CONFIG_DIR = config_dir()
STATE_DIR = state_dir()
CACHE_DIR = cache_dir()
DATA_DIR = data_dir()
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
