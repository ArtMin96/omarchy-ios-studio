"""The watcher's lifecycle: start or join it (detached `watch serve`), stop it, read its status, follow its events."""
from __future__ import annotations

import fcntl
import json
import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

from iosstudio.core import fileio, output
from iosstudio.core.output import HelperError

from .runtime import (
    DEFAULT_DEBOUNCE_MS,
    HELPER_MAIN,
    add_owner,
    backoff,
    control_path,
    events_path,
    lock_path,
    owners_path,
    python_argv,
    read_status,
    remove_owner,
    runtime_file,
    save_json,
    status_path,
    watcher_pid,
)

STOP_WAIT = 12.0         # `watch stop` waits this long for the watcher to exit before killing it
START_WAIT = 8.0         # a starter waits this long for a new watcher's first snapshot
STOPPED_KEEP = 600       # a stopped watcher's status stays readable this long
MAX_STATUS_WAIT = 25
WATCH_LOG_MAX = 1 << 20   # bytes; watch.log (every watcher's stderr) is cut back to its tail beyond this


def current_status() -> dict:
    """The status, with a dead watcher detected (its lock is free) and recorded as stopped."""
    st = read_status()
    pid = watcher_pid()
    if st and st.get("state") != "stopped" and pid is None:
        with fileio.lock(runtime_file(".watch-start.lock")):
            st = read_status() or st
            if st.get("state") != "stopped" and watcher_pid() is None:
                st = _mark_dead(st)
    if not st or (st.get("state") == "stopped" and time.time() - float(st.get("stoppedAt") or 0) > STOPPED_KEEP):
        return {"watching": False, "state": "off", "message": "Not watching"}
    return {**st, "watching": st.get("state") != "stopped" and pid is not None}


def _mark_dead(st: dict) -> dict:
    """A watcher that died without cleaning up (killed, crashed): stop what it left and record it."""
    st = {**st, "state": "stopped", "stopReason": "watcher-died", "stoppedAt": time.time(),
          "message": "Stopped: the watcher process ended unexpectedly", "progress": None,
          "seq": int(st.get("seq") or 0) + 1, "updatedAt": time.time(), "owners": 0}
    st = {k: v for k, v in st.items() if v is not None}
    save_json(status_path(), st)
    try:
        with open(events_path(), "a") as fh:
            fh.write(json.dumps(st, separators=(",", ":"), default=str) + "\n")
        owners_path().unlink()
    except OSError:
        pass
    return st


def _trim_log(path: Path, keep: int = 64 * 1024) -> None:
    try:
        if path.stat().st_size > WATCH_LOG_MAX:
            fileio.atomic_write(path, fileio.tail_text(path, keep))
    except OSError:
        pass


def _spawn(root: Path, auto_run: bool, debounce_ms: int, initial: bool, owner: int) -> int:
    argv = [*python_argv(), *HELPER_MAIN, "watch", "serve", "--project", str(root), "--debounce-ms",
            str(debounce_ms), "--owner", str(owner)]
    if auto_run:
        argv.append("--auto-run")
    if not initial:
        argv.append("--no-initial-build")
    _trim_log(runtime_file("watch.log"))
    log = open(runtime_file("watch.log"), "a")
    try:
        p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=log,
                             start_new_session=True, close_fds=True, cwd="/")
    finally:
        log.close()
    return p.pid


def _signal_and_wait(pid: int, sig: int, wait: float) -> bool:
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        return True
    deadline = time.monotonic() + wait
    delays = backoff()
    while time.monotonic() < deadline:
        if watcher_pid() is None:
            return True
        try:
            os.waitpid(pid, os.WNOHANG)   # our own child (a follower that started it)
        except ChildProcessError:
            pass
        time.sleep(next(delays))
    return watcher_pid() is None


def _stop_locked(reason: str = "stopped") -> dict | None:
    """Stop the running watcher (caller holds the start lock). Returns its final snapshot or None."""
    pid = watcher_pid()
    if pid is None:
        st = read_status()
        if st and st.get("state") != "stopped":
            return _mark_dead(st)
        return None
    deadline = time.monotonic() + 1   # a watcher that just took the lock writes its pid right after
    while pid is not None and pid <= 0 and time.monotonic() < deadline:
        time.sleep(0.05)
        pid = watcher_pid()
    if pid is None:
        return read_status()
    if pid <= 0:
        raise HelperError("watch-busy", "A watcher is starting; try again in a moment.")
    if not _signal_and_wait(pid, signal.SIGTERM, STOP_WAIT):
        st = read_status() or {}
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        _signal_and_wait(pid, 0, 3)
        st = _mark_dead(st)
        st["stopReason"] = reason
        save_json(status_path(), st)
        return st
    return read_status()


def ensure(root: Path, auto_run: bool | None, debounce_ms: int | None, owner: int, initial: bool = True) -> dict:
    """Make sure the one watcher watches `root` and `owner` is among its owners. auto_run / debounce_ms None
    keep a running watcher's value (a new one gets False / DEFAULT_DEBOUNCE_MS); a given one changes it live.
    Another project's watcher is stopped first (reported as `replaced`). Returns the snapshot plus
    `attached` (an existing watcher was reused) and `replaced`."""
    with fileio.lock(runtime_file(".watch-start.lock")):
        pid = watcher_pid()
        st = read_status() or {}
        replaced = None
        if pid is not None and st.get("path") == str(root) and st.get("state") != "stopped":
            add_owner(owner)
            if auto_run is None:
                auto_run = bool(st.get("autoRun"))
            if debounce_ms is None:
                debounce_ms = int(st.get("debounceMs") or DEFAULT_DEBOUNCE_MS)
            if bool(st.get("autoRun")) != auto_run or int(st.get("debounceMs") or 0) != debounce_ms:
                save_json(control_path(), {"path": str(root), "autoRun": auto_run, "debounceMs": debounce_ms})
                try:
                    os.kill(pid, signal.SIGUSR1)
                except ProcessLookupError:
                    pass
                deadline = time.monotonic() + 2
                delays = backoff()
                while time.monotonic() < deadline:
                    s2 = read_status() or {}
                    if bool(s2.get("autoRun")) == auto_run and int(s2.get("debounceMs") or 0) == debounce_ms:
                        break
                    time.sleep(next(delays))
            return {**(read_status() or st), "watching": True, "attached": True}
        if pid is not None:
            old = _stop_locked("replaced")
            if old:
                replaced = {"path": old.get("path"), "project": old.get("project")}
        elif st and st.get("state") != "stopped":
            _mark_dead(st)
        save_json(owners_path(), {})
        add_owner(owner)
        try:
            events_path().write_text("")
        except OSError:
            pass
        try:
            control_path().unlink()
        except OSError:
            pass
        child = _spawn(root, bool(auto_run), debounce_ms or DEFAULT_DEBOUNCE_MS, initial, owner)
        deadline = time.monotonic() + START_WAIT
        delays = backoff()
        while time.monotonic() < deadline:
            s2 = read_status() or {}
            if s2.get("pid") == child and s2.get("state"):
                out = {**s2, "watching": s2.get("state") != "stopped", "attached": False}
                if replaced:
                    out["replaced"] = replaced
                return out
            try:
                done_pid, _ = os.waitpid(child, os.WNOHANG)
            except ChildProcessError:
                done_pid = 0
            if done_pid == child:
                break
            time.sleep(next(delays))
        tail = fileio.tail_text(runtime_file("watch.log"), 1500)   # the log only grows: never read all of it
        raise HelperError("watch-failed", "The watcher didn't start.", detail=tail)


def stop(project: str | None = None) -> dict:
    with fileio.lock(runtime_file(".watch-start.lock")):
        st = read_status() or {}
        if watcher_pid() is None and st.get("state") in (None, "stopped"):
            return {"stopped": False, "watching": False, "message": "Not watching", **({"last": st} if st else {})}
        if project and st.get("path") != project:
            return {"stopped": False, "watching": True, "message": f"Watching {st.get('project')}, not that project",
                    "project": st.get("project"), "path": st.get("path")}
        final = _stop_locked("stopped") or {}
        return {**final, "stopped": True, "watching": False}


def wait_status(seconds: float) -> dict:
    """Block until a build that finished after this call (and nothing newer pending), the watcher stops,
    or `seconds` pass. Returns the status with `waited` seconds and `timedOut`."""
    seconds = max(0.0, min(float(seconds), MAX_STATUS_WAIT))
    start = time.monotonic()
    st = current_status()
    if not st.get("watching") or seconds <= 0:
        return {**st, "waited": 0.0, "timedOut": False}
    first_id = (st.get("lastBuild") or {}).get("id")
    delays = backoff(0.1, 0.5)
    while time.monotonic() - start < seconds:
        time.sleep(min(next(delays), max(0.0, seconds - (time.monotonic() - start))))
        st = current_status()
        if not st.get("watching"):
            break
        last = (st.get("lastBuild") or {}).get("id")
        if last != first_id and st.get("state") in ("ok", "failed", "watching"):
            break
    else:
        return {**st, "waited": round(time.monotonic() - start, 1), "timedOut": True}
    return {**st, "waited": round(time.monotonic() - start, 1), "timedOut": False}



def follow(root: Path, auto_run: bool | None, debounce: int | None, initial: bool) -> int:
    """Streaming: start or join the watcher with this process as an owner and print its snapshots."""
    me = os.getpid()
    try:
        snap = ensure(root, auto_run, debounce, me, initial)
    except HelperError as exc:
        output.done(False, error=exc)
        return 1
    leave = {"why": None}

    def on_signal(signum, _frame):
        leave["why"] = "detached"

    for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(s, on_signal)
    for k in ("attached", "replaced"):
        if snap.get(k):
            output.event("watch-start", **{k: snap[k]})
    seq = -1
    offset = 0
    buf = b""
    stdin_open = True
    final = None
    try:
        while not leave["why"]:
            try:
                size = events_path().stat().st_size
            except OSError:
                size = 0
            if size < offset:
                offset = 0
                buf = b""
            if size > offset:
                with open(events_path(), "rb") as fh:
                    fh.seek(offset)
                    data = fh.read(size - offset)
                offset += len(data)
                buf += data
                *lines, buf = buf.split(b"\n")
                for ln in lines:
                    try:
                        ev = json.loads(ln)
                    except ValueError:
                        continue
                    if not isinstance(ev, dict) or int(ev.get("seq") or 0) <= seq:
                        continue
                    if ev.get("pid") != snap.get("pid"):
                        continue
                    seq = int(ev.get("seq") or 0)
                    output._write(ev)
                    if ev.get("state") == "stopped":
                        final = ev
                        leave["why"] = "stopped"
                        break
            if leave["why"]:
                break
            if watcher_pid() is None:
                final = current_status()
                if final.get("state") == "stopped" and int(final.get("seq") or 0) > seq:
                    output._write(final)
                leave["why"] = "stopped"
                break
            fds = [sys.stdin.fileno()] if stdin_open else []
            try:
                ready, _, _ = select.select(fds, [], [], 0.25)
            except (InterruptedError, ValueError, OSError):
                ready = []
            if ready:
                try:
                    chunk = os.read(sys.stdin.fileno(), 4096)
                except OSError:
                    chunk = b""
                if not chunk:
                    stdin_open = False
                for ln in chunk.splitlines():
                    try:
                        msg = json.loads(ln or b"{}")
                    except ValueError:
                        continue
                    if not isinstance(msg, dict):
                        continue
                    if msg.get("cmd") == "stop":
                        final = stop(str(root))
                        leave["why"] = "stopped"
                    elif msg.get("cancel") or msg.get("cmd") in ("quit", "detach"):
                        leave["why"] = "detached"
    finally:
        try:
            remove_owner(me)
        except OSError:
            pass
    if leave["why"] == "detached":
        return output.done(True, {**(read_status() or {}), "detached": True})
    return output.done(True, final or read_status() or {})



def serve(root: Path, owner: int, auto_run: bool, debounce_ms: int, initial: bool) -> int:
    """Run the watcher in this process (`watch serve`, started detached by ensure()) while holding watch.lock."""
    fh = open(lock_path(), "a+")
    deadline = time.monotonic() + 3
    delays = backoff()
    while True:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.monotonic() > deadline:
                print("another watcher holds the lock", file=sys.stderr)
                fh.close()
                return 1
            time.sleep(next(delays))
    fh.seek(0)
    fh.truncate()
    fh.write(f"{os.getpid()}\n")
    fh.flush()
    add_owner(owner)
    from .watcher import DemoWatcher, Watcher
    if os.environ.get("IOSSTUDIO_DEMO"):
        w: Watcher = DemoWatcher(root, auto_run)
        w.debounce_ms = debounce_ms
    else:
        w = Watcher(root, auto_run=auto_run, debounce_ms=debounce_ms, initial_build=initial)

    def on_stop(_signum, _frame):
        w.stop_reason = w.stop_reason or "stopped"

    def on_reload(_signum, _frame):
        w.reload = True

    for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(s, on_stop)
    signal.signal(signal.SIGUSR1, on_reload)
    try:
        w.loop()
    finally:
        fh.seek(0)
        fh.truncate()
        fh.close()
    return 0
