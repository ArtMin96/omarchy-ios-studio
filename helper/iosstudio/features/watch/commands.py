"""The `watch` group's handlers: parse flags, call the lifecycle, print the result."""
from __future__ import annotations

import os
from pathlib import Path

from iosstudio.core import output
from iosstudio.core.output import HelperError

from . import lifecycle
from .runtime import DEFAULT_DEBOUNCE_MS, clamp_debounce


def _project(flags: dict) -> Path:
    raw = flags.get("project") or flags.get("path")
    if not raw or raw is True:
        raise HelperError("usage", "Missing --project.")
    p = Path(os.path.expanduser(str(raw))).resolve()
    if os.environ.get("IOSSTUDIO_DEMO"):
        return p
    if not p.is_dir():
        raise HelperError("not-found", "That project folder doesn't exist anymore.",
                          {**output.fix("Remove from list", "projects", action="remove"), "path": str(p)})
    if not (p / "Package.swift").is_file():
        raise HelperError("not-a-project", "That folder isn't an xtool app (it needs Package.swift and xtool.yml).")
    return p


def _flag_owner(flags: dict, default: int) -> int:
    v = flags.get("owner")
    try:
        return int(v) if v and v is not True else default
    except ValueError:
        raise HelperError("usage", "--owner must be a process id.")



def _flag_project_path(flags: dict) -> str | None:
    """--project for status/stop: an absolute path, not checked (the folder may be gone)."""
    raw = flags.get("project") or flags.get("path")
    return str(Path(os.path.expanduser(str(raw))).resolve()) if raw and raw is not True else None


def _flag_bool(flags: dict, key: str) -> bool | None:
    """--key / --key true|false / --no-key; None when not given."""
    if flags.get("no_" + key):
        return False
    v = flags.get(key)
    if v is None:
        return None
    if v is True:
        return True
    low = str(v).strip().lower()
    if low in ("1", "true", "yes", "on"):
        return True
    if low in ("0", "false", "no", "off"):
        return False
    raise HelperError("usage", f"--{key.replace('_', '-')} takes true or false.")



def cmd_serve(flags: dict) -> int:
    """The watcher process (started detached by `watch run`)."""
    return lifecycle.serve(_project(flags), _flag_owner(flags, os.getppid()), bool(flags.get("auto_run")),
                           clamp_debounce(flags.get("debounce_ms") or DEFAULT_DEBOUNCE_MS),
                           not flags.get("no_initial_build"))


def cmd_run(flags: dict) -> int:
    """Settings not given keep the running watcher's value (a new watcher gets the defaults)."""
    root = _project(flags)
    auto_run = _flag_bool(flags, "auto_run")
    debounce = clamp_debounce(flags["debounce_ms"]) if flags.get("debounce_ms") not in (None, True) else None
    initial = not flags.get("no_initial_build")
    if flags.get("detach"):
        owner = _flag_owner(flags, os.getppid())
        return output.ok(lifecycle.ensure(root, auto_run, debounce, owner, initial))
    return lifecycle.follow(root, auto_run, debounce, initial)


def cmd_status(flags: dict) -> int:
    project = _flag_project_path(flags)
    wait = flags.get("wait_seconds")
    st = lifecycle.wait_status(float(wait)) if wait and wait is not True else lifecycle.current_status()
    if project and st.get("path") and st.get("path") != project:
        st = {"watching": False, "state": "off", "message": f"Not watching this project (watching "
              f"{st.get('project')})" if st.get("watching") else "Not watching this project",
              "other": {"project": st.get("project"), "path": st.get("path"), "state": st.get("state")}}
    return output.ok(st)


def cmd_stop(flags: dict) -> int:
    return output.ok(lifecycle.stop(_flag_project_path(flags)))


GROUPS = {
    "watch": {
        "run": cmd_run,
        "serve": cmd_serve,
        "status": cmd_status,
        "stop": cmd_stop,
    },
}


def demo(action: str, flags: dict) -> int:
    """Demo mode: the same commands on the demo runtime dir; `serve` runs the scripted DemoWatcher."""
    handler = GROUPS["watch"].get(action)
    if handler is None:
        return output.fail("usage", f"Unknown command 'watch {action}'.") or 2
    try:
        return int(handler(flags) or 0)
    except HelperError as exc:
        return output.fail(exc.code, exc.message, exc.fix, exc.detail)
