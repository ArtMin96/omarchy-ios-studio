"""The watcher's runtime files (lock, status, events, owners, control) and the process helpers around them."""
from __future__ import annotations

import fcntl
import json
import os
import signal
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from iosstudio.core import appdirs, fileio
from iosstudio.core.output import HelperError

HELPER_MAIN = [str(appdirs.REPO / "helper" / "run.py"), "iosstudio"]   # the helper, after python_argv()
DEFAULT_DEBOUNCE_MS = 600
MIN_DEBOUNCE_MS, MAX_DEBOUNCE_MS = 100, 10_000
MAX_DELAY = 4.0          # seconds: saves that never pause still compile at least this often
CANCEL_GRACE = 5.0       # a cancelled build gets this long to clean up before its process group is killed


# ================================================================ files

def runtime_dir() -> Path:
    """device_client.runtime_dir(): ios-studio (ios-studio-demo in demo mode) under the runtime base shared with
    the device daemon ($IOSSTUDIO_RUNTIME_DIR, $XDG_RUNTIME_DIR, /run/user/<uid>, /tmp/...). Created on use."""
    from iosstudio.device import client as device_client
    d = device_client.runtime_dir()
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


def runtime_file(name: str) -> Path:
    return runtime_dir() / name


def lock_path() -> Path:
    return runtime_file("watch.lock")


def status_path() -> Path:
    return runtime_file("watch.json")


def events_path() -> Path:
    return runtime_file("watch-events.jsonl")


def owners_path() -> Path:
    return runtime_file("watch-owners.json")


def control_path() -> Path:
    return runtime_file("watch-control.json")


def save_json(path: Path, data: Any) -> None:
    fileio.atomic_write(path, json.dumps(data, separators=(",", ":"), default=str), mode=0o600)


def read_status() -> dict | None:
    st = fileio.load_json(status_path())
    return st if isinstance(st, dict) else None


def clamp_debounce(v: Any) -> int:
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        raise HelperError("usage", "--debounce-ms must be a number of milliseconds.")
    return max(MIN_DEBOUNCE_MS, min(MAX_DEBOUNCE_MS, n))


# ================================================================ processes and owners

def proc_start(pid: int) -> str | None:
    """The process's start time (field 22 of /proc/<pid>/stat): a pid plus this identifies one process,
    even after the pid is reused."""
    try:
        with open(f"/proc/{int(pid)}/stat") as fh:
            stat = fh.read()
        fields = stat.rsplit(")", 1)[1].split()
        if fields[0] == "Z":   # a zombie is dead for our purposes
            return None
        return fields[19]
    except (OSError, IndexError, ValueError):
        return None


def pid_alive(pid: int, start: str | None = None) -> bool:
    st = proc_start(pid)
    return st is not None and (start is None or st == start)


def watcher_pid() -> int | None:
    """The live watcher's pid (its flock on watch.lock is held), else None. Never blocks."""
    try:
        fh = open(lock_path(), "a+")
    except OSError:
        return None
    try:
        try:
            fcntl.flock(fh, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            fh.seek(0)
            raw = fh.read().strip()
            try:
                return int(raw.split()[0])
            except (ValueError, IndexError):
                st = read_status() or {}
                return int(st["pid"]) if st.get("pid") else -1
        fcntl.flock(fh, fcntl.LOCK_UN)
        return None
    finally:
        fh.close()


def read_owners() -> dict[str, str]:
    o = fileio.load_json(owners_path(), {})
    return {str(k): str(v) for k, v in o.items()} if isinstance(o, dict) else {}


def add_owner(pid: int) -> None:
    start = proc_start(pid)
    if start is None:
        return
    with fileio.lock(runtime_file(".watch-owners.lock")):
        o = read_owners()
        o[str(pid)] = start
        save_json(owners_path(), o)


def remove_owner(pid: int) -> None:
    with fileio.lock(runtime_file(".watch-owners.lock")):
        o = read_owners()
        if o.pop(str(pid), None) is not None:
            save_json(owners_path(), o)


def live_owners() -> dict[str, str]:
    return {p: s for p, s in read_owners().items() if p.isdigit() and pid_alive(int(p), s)}


def python_argv() -> list[str]:
    """This interpreter with the flags bin/iosstudio-helper gave it (-I, -X pycache_prefix=...)."""
    argv = [sys.executable] + (["-I"] if sys.flags.isolated else [])
    for k, v in getattr(sys, "_xoptions", {}).items():
        argv += ["-X", k if v is True else f"{k}={v}"]
    return argv


def backoff(first: float = 0.05, cap: float = 0.25) -> Iterator[float]:
    """Sleep intervals for a deadline poll: quick at first (the answer usually comes fast), then fewer wakeups."""
    d = first
    while True:
        yield d
        d = min(cap, d * 1.5)


def killpg(pgid: int | None, sig: int = signal.SIGKILL) -> None:
    if not pgid or pgid <= 1:
        return
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass
