"""Runtime files of hot reload sessions, and what other processes do with them (status, stop, pause, resume).

One session per project, found through files in the runtime dir ($IOSSTUDIO_RUNTIME_DIR or $XDG_RUNTIME_DIR,
/ios-studio[-demo]/), key = buildinfo.project_key(path):

    hotreload-<key>.lock          flock held by the session while it lives (its pid inside): a free lock = no session
    hotreload-<key>.json          the latest snapshot plus `history` (newest HISTORY_MAX reloads); kept after it stops
    hotreload-<key>-control.json  {path, paused}: pause/resume, applied by the session on SIGUSR1

A snapshot whose session died without saying so (killed, crashed: the lock is free) is rewritten as stopped
with stopReason `session-died` by whoever reads it first. Stopped state files older than PRUNE_AFTER go at the
next session start.
"""
from __future__ import annotations

import fcntl
import os
import signal
import time
from pathlib import Path
from typing import IO, Any

from iosstudio.core import fileio, output
from iosstudio.core.output import HelperError
from iosstudio.features.hotreload.buildinfo import project_key
from iosstudio.features.watch.runtime import backoff, runtime_dir, save_json

HISTORY_MAX = 20          # reload summaries kept in the state file
STOPPED_KEEP = 600        # seconds a stopped session stays in `hotreload status` without --project
PRUNE_AFTER = 24 * 3600   # seconds after which a stopped session's files are deleted
STOP_WAIT = 5.0           # `hotreload stop` waits this long before killing the session
CONTROL_WAIT = 2.0        # pause/resume wait this long for the session to show the change
MAX_STATUS_WAIT = 25


def start_fix() -> dict:
    return output.fix("Start hot reload", "projects", action="hot-reload-start")


# ================================================================ files

def key(root: str | Path) -> str:
    return project_key(root)


def lock_path(k: str) -> Path:
    return runtime_dir() / f"hotreload-{k}.lock"


def state_path(k: str) -> Path:
    return runtime_dir() / f"hotreload-{k}.json"


def control_path(k: str) -> Path:
    return runtime_dir() / f"hotreload-{k}-control.json"


def read_state(k: str) -> dict | None:
    st = fileio.load_json(state_path(k))
    return st if isinstance(st, dict) else None


def write_state(k: str, snap: dict) -> None:
    save_json(state_path(k), snap)


def read_control(k: str) -> dict:
    c = fileio.load_json(control_path(k), {})
    return c if isinstance(c, dict) else {}


def write_control(k: str, path: str, paused: bool) -> None:
    save_json(control_path(k), {"path": path, "paused": paused, "time": time.time()})


def clear_control(k: str) -> None:
    control_path(k).unlink(missing_ok=True)


# ================================================================ the session's lock

def acquire(k: str, path: str) -> IO[str]:
    """Take the project's session lock (kept while the returned file is open) and write our pid into it.
    HelperError('busy') with the running session's pid when another session holds it."""
    fh = open(lock_path(k), "a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        pid = session_pid(k)
        raise HelperError("busy", f"Hot reload is already running for this project (process {pid}).",
                          output.fix("Stop it", "projects", action="hot-reload-stop"),
                          detail=f"pid {pid} {path}") from None
    fh.seek(0)
    fh.truncate()
    fh.write(f"{os.getpid()}\n")
    fh.flush()
    return fh


def release(fh: IO[str]) -> None:
    try:
        fh.seek(0)
        fh.truncate()
    except OSError:
        pass
    fh.close()


def session_pid(k: str) -> int | None:
    """The live session's pid (its flock is held), -1 if it just took the lock, None when no session runs.
    Never blocks."""
    try:
        fh = open(lock_path(k), "a+")
    except OSError:
        return None
    try:
        try:
            fcntl.flock(fh, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            fh.seek(0)
            try:
                return int(fh.read().split()[0])
            except (ValueError, IndexError):
                st = read_state(k) or {}
                return int(st["pid"]) if st.get("pid") else -1
        fcntl.flock(fh, fcntl.LOCK_UN)
        return None
    finally:
        fh.close()


# ================================================================ reading status

def mark_dead(k: str, st: dict) -> dict:
    """A session that ended without writing `stopped` (killed, crashed): record it as stopped."""
    now = time.time()
    st = {**st, "state": "stopped", "stopReason": "session-died", "stoppedAt": now, "updatedAt": now,
          "message": "Stopped: hot reload ended unexpectedly", "progress": None,
          "seq": int(st.get("seq") or 0) + 1, "pending": [], "pendingCount": 0}
    try:
        write_state(k, st)
    except OSError:
        pass
    return st


def current(k: str) -> dict | None:
    """The project's snapshot (with `running`), a dead session recorded as stopped; None if it never ran."""
    st = read_state(k)
    if st is None:
        return None
    pid = session_pid(k)
    if st.get("state") != "stopped" and pid is None:
        st = mark_dead(k, st)
    return {**st, "running": st.get("state") != "stopped" and pid is not None}


def sessions() -> list[dict]:
    """Every running session and those stopped within STOPPED_KEEP, newest first (without history)."""
    out = []
    now = time.time()
    for p in sorted(runtime_dir().glob("hotreload-*.json")):
        if p.name.endswith("-control.json"):
            continue
        st = current(p.stem.removeprefix("hotreload-"))
        if not st:
            continue
        if st.get("state") == "stopped" and now - float(st.get("stoppedAt") or 0) > STOPPED_KEEP:
            continue
        st.pop("history", None)
        out.append(st)
    return sorted(out, key=lambda s: -float(s.get("updatedAt") or 0))


def last_finished_seq(st: dict | None) -> int:
    """The newest reload seq that finished (ok, failed or unchanged), 0 if none."""
    hist = (st or {}).get("history") or []
    return max((int(h.get("seq") or 0) for h in hist if isinstance(h, dict)), default=0)


def wait(k: str, seconds: float, after_seq: int | None = None) -> dict | None:
    """Block until a reload with seq > after_seq (default: the newest finished one now) has finished, the session
    stops, or `seconds` (≤ MAX_STATUS_WAIT) pass. Returns current() plus `waited` and `timedOut`. Callers that just
    saved should pass the newest seq they saw (lastReload/lastError/history): the default can miss a reload that
    finished before the call."""
    seconds = max(0.0, min(float(seconds), MAX_STATUS_WAIT))
    start = time.monotonic()
    st = current(k)
    if st is None:
        return None
    after = last_finished_seq(st) if after_seq is None else int(after_seq)
    delays = backoff(0.1, 0.4)
    while True:
        if not st.get("running") or last_finished_seq(st) > after:
            return {**st, "waited": round(time.monotonic() - start, 1), "timedOut": False}
        left = seconds - (time.monotonic() - start)
        if left <= 0:
            return {**st, "waited": round(time.monotonic() - start, 1), "timedOut": True}
        time.sleep(min(next(delays), left))
        st = current(k) or st


# ================================================================ stop / pause / resume

def _wait_gone(k: str, pid: int, wait_s: float) -> bool:
    deadline = time.monotonic() + wait_s
    delays = backoff()
    while time.monotonic() < deadline:
        if session_pid(k) is None:
            return True
        try:
            os.waitpid(pid, os.WNOHANG)    # our own child (tests start the session from this process)
        except ChildProcessError:
            pass
        time.sleep(next(delays))
    return session_pid(k) is None


def _live_pid(k: str) -> int | None:
    pid = session_pid(k)
    deadline = time.monotonic() + 1          # a session that just took the lock writes its pid right after
    while pid is not None and pid <= 0 and time.monotonic() < deadline:
        time.sleep(0.05)
        pid = session_pid(k)
    return pid if pid and pid > 0 else None


def stop(k: str) -> dict:
    """SIGTERM the session and wait up to STOP_WAIT for it to end (then SIGKILL). {stopped, session}."""
    pid = _live_pid(k)
    if pid is None:
        return {"stopped": False, "session": current(k)}
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    if not _wait_gone(k, pid, STOP_WAIT):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        _wait_gone(k, pid, 2)
    st = read_state(k) or {}
    if st and st.get("state") != "stopped":
        st = mark_dead(k, st)
        st["stopReason"] = "stopped"
        st["message"] = "Stopped"
        write_state(k, st)
    return {"stopped": True, "session": current(k)}


def set_paused(k: str, path: str, paused: bool) -> dict:
    """Write the control file, SIGUSR1 the session, wait for its snapshot to show it. {paused, session}."""
    pid = _live_pid(k)
    if pid is None:
        raise HelperError("not-running", "Hot reload isn't running for this project.", start_fix())
    write_control(k, path, paused)
    try:
        os.kill(pid, signal.SIGUSR1)
    except ProcessLookupError:
        raise HelperError("not-running", "Hot reload isn't running for this project.", start_fix()) from None
    deadline = time.monotonic() + CONTROL_WAIT
    delays = backoff()
    st = current(k) or {}
    while time.monotonic() < deadline and bool(st.get("paused")) != paused and st.get("running"):
        time.sleep(next(delays))
        st = current(k) or st
    st.pop("history", None)
    return {"paused": bool(st.get("paused")), "session": st}


# ================================================================ pruning

def prune(now: float | None = None) -> int:
    """Delete the files of sessions stopped more than PRUNE_AFTER ago (and stray control files)."""
    now = time.time() if now is None else now
    removed = 0
    for p in runtime_dir().glob("hotreload-*.json"):
        if p.name.endswith("-control.json"):
            continue
        k = p.stem.removeprefix("hotreload-")
        st: Any = fileio.load_json(p)
        stale = not isinstance(st, dict) or (st.get("state") == "stopped"
                                             and now - float(st.get("stoppedAt") or 0) > PRUNE_AFTER)
        if stale and session_pid(k) is None:
            for f in (p, control_path(k), lock_path(k)):
                f.unlink(missing_ok=True)
            removed += 1
    return removed
