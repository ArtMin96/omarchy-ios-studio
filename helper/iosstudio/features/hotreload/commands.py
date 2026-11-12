"""`hotreload <action>` handlers: flags in, JSON out; the work is in the other modules of this package.

    hotreload enable  --project P [--dry-run] [--max-bytes N]
    hotreload disable --project P [--dry-run] [--max-bytes N]      stops a running session first (an error
                      after that carries sessionStopped: true)
    hotreload run     --project P [--udid U] [--preset ID] [--debounce-ms 150]     streaming (session.py)
    hotreload status  [--project P] [--wait-seconds N (≤25)] [--after-seq S]
                      -> {project, enabled, mode, session: snapshot | null, waited?, timedOut?} with --project,
                         else {sessions: [snapshot…] (running and recently stopped, without history),
                               projects: {path: {enabled, mode}} (every listed project, ≤ 200; no swift call)}
    hotreload stop    --project P       -> {stopped, session}
    hotreload pause   --project P       -> {paused: true,  session}
    hotreload resume  --project P       -> {paused: false, session}

Each handler is one function in ACTIONS. Demo mode (demo/hot_reload.py) calls the session commands with
demo=True: the same runtime files (in the demo runtime dir), a scripted session.
"""
from __future__ import annotations

import os
from pathlib import Path

from iosstudio.core import output
from iosstudio.core.output import HelperError
from iosstudio.features.packages.commands import fail, max_bytes_flag

# ================================================================ project setup

def cmd_enable(flags: dict) -> int:
    from iosstudio.features.hotreload import project_setup
    from iosstudio.features.packages import project
    try:
        root = project.project_root(flags)
        return output.ok(project_setup.enable(root, bool(flags.get("dry_run")), max_bytes_flag(flags)))
    except HelperError as exc:
        return fail(exc)


def cmd_disable(flags: dict) -> int:
    from iosstudio.features.hotreload import project_setup, state
    from iosstudio.features.packages import project
    stopped = False
    try:
        root = project.project_root(flags)
        dry = bool(flags.get("dry_run"))
        if not dry and state.session_pid(state.key(root)) is not None:
            stopped = bool(state.stop(state.key(root))["stopped"])
        return output.ok({**project_setup.disable(root, dry, max_bytes_flag(flags)), "sessionStopped": stopped})
    except HelperError as exc:
        if stopped:                  # the session is gone even though Package.swift kept its block
            exc.extra = {**(getattr(exc, "extra", None) or {}), "sessionStopped": True}  # type: ignore[attr-defined]
        return fail(exc)


# ================================================================ the session

def _path_flag(flags: dict) -> str:
    """--project for status/stop/pause/resume: an absolute real path, not checked (the folder may be gone)."""
    raw = flags.get("project") or flags.get("path")
    if not raw or raw is True:
        raise HelperError("usage", "Missing --project.")
    return os.path.realpath(os.path.expanduser(str(raw)))


def _str_flag(flags: dict, key: str) -> str | None:
    v = flags.get(key)
    return str(v) if v and v is not True else None


def _int_flag(flags: dict, key: str) -> int | None:
    v = flags.get(key)
    if v is None or v is True:
        return None
    try:
        return int(float(v))
    except ValueError:
        raise HelperError("usage", f"--{key.replace('_', '-')} must be a number.") from None


def cmd_run(flags: dict, demo: bool = False) -> int:
    from iosstudio.features.hotreload import session
    try:
        if demo:
            root = Path(_path_flag(flags))
        else:
            from iosstudio.features.packages import project
            root = project.project_root(flags)
        debounce = session.clamp_debounce(flags["debounce_ms"]) if flags.get("debounce_ms") not in (None, True) \
            else session.DEFAULT_DEBOUNCE_MS
    except HelperError as exc:
        return output.done(False, error=exc)
    cls = session.Session
    if demo:
        from iosstudio.demo.hot_reload import DemoSession
        cls = DemoSession
    return cls(root, udid=_str_flag(flags, "udid"), preset=_str_flag(flags, "preset"), debounce_ms=debounce).run()


def _setting(path: str, demo: bool) -> dict:
    """{enabled, mode, importedBy?} of the project (mode off when it can't be read). In stub mode, importedBy
    lists the files that still `import HotReload` (a bounded scan of Sources/, only for stub projects)."""
    if demo:
        from iosstudio.demo.hot_reload import demo_setting
        return demo_setting(path)
    from iosstudio.features.hotreload import project_setup
    try:
        st = project_setup.status(Path(path))
    except (OSError, HelperError):
        return {"enabled": False, "mode": "off"}
    out = {"enabled": st["enabled"], "mode": st["mode"]}
    if st["mode"] == "stub":
        out["importedBy"] = project_setup.imported_by(Path(path))
    return out


MAX_PROJECTS = 200


def project_settings(demo: bool = False) -> dict[str, dict]:
    """{path: {enabled, mode}} for the projects the Projects page lists (under the project roots and added by
    hand, minus hidden ones), at most MAX_PROJECTS. File reads only (Package.swift), no swift call."""
    if demo:
        from iosstudio.demo.demo import HOME, PROJECTS
        return {f"{HOME}/Developer/{p['name']}": _setting(f"{HOME}/Developer/{p['name']}", True) for p in PROJECTS}
    from iosstudio.features.projects import model, store
    st = store.load_state()
    found, _conv = model.scan()
    paths = [str(p) for p in found] + [a for a in st["added"] if isinstance(a, str)]
    hidden = set(st["hidden"])
    out: dict[str, dict] = {}
    for path in dict.fromkeys(paths):
        if path in hidden or len(out) >= MAX_PROJECTS:
            continue
        if (Path(path) / "Package.swift").is_file():
            out[path] = _setting(path, False)
    return out


def cmd_status(flags: dict, demo: bool = False) -> int:
    from iosstudio.features.hotreload import state
    if not (flags.get("project") or flags.get("path")):
        return output.ok({"sessions": state.sessions(), "projects": project_settings(demo)})
    path = _path_flag(flags)
    k = state.key(path)
    wait = flags.get("wait_seconds")
    out: dict = {"project": path, **_setting(path, demo)}
    if wait not in (None, True):
        try:
            seconds = float(wait)
        except ValueError:
            raise HelperError("usage", "--wait-seconds must be a number.") from None
        snap = state.wait(k, seconds, _int_flag(flags, "after_seq"))
        if snap is not None:
            out["waited"] = snap.pop("waited")
            out["timedOut"] = snap.pop("timedOut")
    else:
        snap = state.current(k)
    return output.ok({**out, "session": snap})


def cmd_stop(flags: dict, demo: bool = False) -> int:
    from iosstudio.features.hotreload import state
    return output.ok(state.stop(state.key(_path_flag(flags))))


def _set_paused(flags: dict, paused: bool) -> int:
    from iosstudio.features.hotreload import state
    path = _path_flag(flags)
    return output.ok(state.set_paused(state.key(path), path, paused))


def cmd_pause(flags: dict, demo: bool = False) -> int:
    return _set_paused(flags, True)


def cmd_resume(flags: dict, demo: bool = False) -> int:
    return _set_paused(flags, False)


ACTIONS = {
    "enable": cmd_enable,
    "disable": cmd_disable,
    "run": cmd_run,
    "status": cmd_status,
    "stop": cmd_stop,
    "pause": cmd_pause,
    "resume": cmd_resume,
}

GROUPS = {"hotreload": ACTIONS}
