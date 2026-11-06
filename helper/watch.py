"""Watch mode (compile on save): group `watch` (stdlib only). See docs/features/watch-mode.md.

    watch run    --project P [--auto-run] [--debounce-ms 600] [--no-initial-build]   streaming (JSON lines)
    watch run    --project P ... --detach [--owner PID]                                 one-shot (MCP)
    watch status [--project P] [--wait-seconds N]                                      one-shot
    watch stop   [--project P]                                                         one-shot
    watch serve  --project P ... --owner PID                                            the watcher itself

One watcher per user at a time (builds are heavy). It is its own process (`watch serve`), started detached
by whoever asks first (the app through `watch run`, the MCP server through `watch run --detach`), found
through files in the runtime dir ($IOSSTUDIO_RUNTIME_DIR or $XDG_RUNTIME_DIR, /ios-studio[-demo]/):

    watch.lock          flock held by the watcher while it lives (its pid inside): a free lock = no watcher
    watch.json          the latest status snapshot (kept for STOPPED_KEEP after it stops)
    watch-events.jsonl  every snapshot as a JSON line; `watch run` tails it for the app
    watch-owners.json   {pid: process start time}: who uses the watcher. It stops when none of them is alive
    watch-control.json  settings changed while running (auto-run, debounce), applied on SIGUSR1
    watch.log           the watcher's stderr

Each save (inotify on Sources/ and the root's Package.swift, xtool.yml, *.plist, *.entitlements; a 1 s
mtime scan when inotify isn't available) waits for the debounce, then runs the same `build compile` as the
Build button (history, logs and diagnostics included). A change during a build cancels it and starts one
more after it; content that didn't change (a save without edits, a regenerated identical xtool.yml) is
ignored. With auto-run a clean compile is followed by Run (build, install, launch) when an iPhone is
connected and an Apple ID is signed in; otherwise the snapshot's `run` says why it was skipped.

Snapshot (the `watch` event, watch.json, `watch status` data):
    {event: "watch", seq, state: watching|changed|compiling|ok|failed|stopped, reason, message, project,
     path, pid, mode: inotify|poll, modeReason?, autoRun, debounceMs, startedAt, updatedAt, owners,
     changed: [rel path ≤ 5], changedCount, buildId (running or last), logFile, progress?: {fraction, detail},
     builds, superseded,
     lastBuild?: {id, result: ok|failed, duration, time, errors, warnings, errorCode?, errorMessage?, logFile,
                  diagnostics: [≤ 5 {severity, category, file, line, col, message}] (errors first)},
     run?: {state: checking|building|installing|launching|launched|failed|skipped, message, buildId? (the run's
            own build), forBuild (the clean compile it follows), bundleId?},
     stoppedAt?, stopReason?, replaced?: {path, project}}
`watch run` (streaming) prints the current snapshot, then each new one, and a final
{"event": "done", "ok": true, "data": snapshot} once the watcher stops or this follower detaches.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import errno
import fcntl
import hashlib
import json
import os
import re
import select
import signal
import struct
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

import common
from common import HelperError

HELPER_MAIN = Path(__file__).resolve().parent / "main.py"
DEFAULT_DEBOUNCE_MS = 600
MIN_DEBOUNCE_MS, MAX_DEBOUNCE_MS = 100, 10_000
MAX_DELAY = 4.0          # seconds: saves that never pause still compile at least this often
OWNER_CHECK = 1.0        # seconds between owner liveness checks
CANCEL_GRACE = 5.0       # a cancelled build gets this long to clean up before its process group is killed
STOP_WAIT = 12.0         # `watch stop` waits this long for the watcher to exit before killing it
START_WAIT = 8.0         # a starter waits this long for a new watcher's first snapshot
STOPPED_KEEP = 600       # a stopped watcher's status stays readable this long
BUSY_RETRY = 2.0         # another build of the project holds its lock: try again after this
POLL_INTERVAL = 1.0
FIRST_DIAGS = 5
CHANGED_SHOWN = 5
SUPERSEDE_LIMIT = 2      # after this many builds in a row were cancelled by saves, the next one is let finish
EVENTS_MAX = 1_000_000   # bytes; the events file is cut back to the latest snapshot beyond this
PROGRESS_EVERY = 1.0     # seconds between progress snapshots
HASH_MAX = 4 * 1024 * 1024
PRIME_MAX_FILES = 5000
MAX_STATUS_WAIT = 25

ROOT_FILES = {"Package.swift", "xtool.yml"}       # Package.resolved is left out: builds rewrite it themselves
ROOT_SUFFIXES = (".plist", ".entitlements")
SOURCE_DIR = "Sources"
IGNORED_DIRS = {".build", ".bsp", "xtool", ".git", ".swiftpm", "DerivedData", "node_modules", ".vscode", ".idea",
                ".zed", "build", "Pods"}
TEMP_SUFFIXES = (".swp", ".swo", ".swx", ".swpx", ".swn", "~", ".tmp", ".temp", ".bak", ".orig", ".crswap",
                 ".kate-swp", "___jb_tmp___", "___jb_old___", ".part")
TEMP_RX = re.compile(r"\.tmp[.\-_]?\w*$|\.sw[a-p]$|^#.*#$")
STATES = ("watching", "changed", "compiling", "ok", "failed", "stopped")


# ================================================================ files

def runtime_dir() -> Path:
    """device_client.runtime_base() (IOSSTUDIO_RUNTIME_DIR, XDG_RUNTIME_DIR, /run/user/<uid>, /tmp/...), then
    ios-studio (ios-studio-demo in demo mode), like the device daemon."""
    import device_client
    d = device_client.runtime_base() / ("ios-studio-demo" if os.environ.get("IOSSTUDIO_DEMO") else "ios-studio")
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


def _p(name: str) -> Path:
    return runtime_dir() / name


def lock_path() -> Path:
    return _p("watch.lock")


def status_path() -> Path:
    return _p("watch.json")


def events_path() -> Path:
    return _p("watch-events.jsonl")


def owners_path() -> Path:
    return _p("watch-owners.json")


def control_path() -> Path:
    return _p("watch-control.json")


@contextmanager
def _flock(name: str):
    with open(_p(name), "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield


def _load(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def _save(path: Path, data: Any) -> None:
    common.atomic_write(path, json.dumps(data, separators=(",", ":"), default=str), mode=0o600)


def read_status() -> dict | None:
    st = _load(status_path())
    return st if isinstance(st, dict) else None


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
    o = _load(owners_path(), {})
    return {str(k): str(v) for k, v in o.items()} if isinstance(o, dict) else {}


def add_owner(pid: int) -> None:
    start = proc_start(pid)
    if start is None:
        return
    with _flock(".watch-owners.lock"):
        o = read_owners()
        o[str(pid)] = start
        _save(owners_path(), o)


def remove_owner(pid: int) -> None:
    with _flock(".watch-owners.lock"):
        o = read_owners()
        if o.pop(str(pid), None) is not None:
            _save(owners_path(), o)


def live_owners() -> dict[str, str]:
    return {p: s for p, s in read_owners().items() if p.isdigit() and pid_alive(int(p), s)}


def _python() -> list[str]:
    """This interpreter with the flags bin/iosstudio-helper gave it (-I, -X pycache_prefix=...)."""
    argv = [sys.executable] + (["-I"] if sys.flags.isolated else [])
    for k, v in getattr(sys, "_xoptions", {}).items():
        argv += ["-X", k if v is True else f"{k}={v}"]
    return argv


def _pdeathsig() -> None:  # pragma: no cover - runs in the child
    try:
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
    except Exception:
        pass


def _killpg(pgid: int | None, sig: int = signal.SIGKILL) -> None:
    if not pgid or pgid <= 1:
        return
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


# ================================================================ what counts as a change

def ignored_name(name: str) -> bool:
    """Editor temp, swap and backup files: Vim's .swp/~/4913, Zed's hidden .tmpXXXX, JetBrains' ___jb_*___,
    Emacs' #file# and .#file, any hidden name."""
    if not name or name.startswith(".") or name.startswith("#") or name.isdigit():
        return True
    return name.endswith(TEMP_SUFFIXES) or bool(TEMP_RX.search(name))


def relevant(rel: str) -> bool:
    """A project-relative file path whose change should trigger a compile."""
    parts = [x for x in rel.split("/") if x]
    if not parts or any(ignored_name(x) or x in IGNORED_DIRS for x in parts):
        return False
    if len(parts) == 1:
        return parts[0] in ROOT_FILES or parts[0].endswith(ROOT_SUFFIXES)
    return parts[0] == SOURCE_DIR


def relevant_dir(rel: str) -> bool:
    parts = [x for x in rel.split("/") if x]
    return bool(parts) and parts[0] == SOURCE_DIR and not any(ignored_name(x) or x in IGNORED_DIRS for x in parts)


def scan_files(root: Path, limit: int | None = None) -> dict[str, tuple[int, int]]:
    """{rel path: (mtime_ns, size)} of every relevant file (root files + Sources/ tree)."""
    out: dict[str, tuple[int, int]] = {}
    try:
        for e in os.scandir(root):
            if e.is_file(follow_symlinks=True) and relevant(e.name):
                st = e.stat()
                out[e.name] = (st.st_mtime_ns, st.st_size)
    except OSError:
        return out
    stack = [SOURCE_DIR]
    while stack:
        rel = stack.pop()
        try:
            entries = list(os.scandir(root / rel))
        except OSError:
            continue
        for e in entries:
            r = f"{rel}/{e.name}"
            try:
                if e.is_dir(follow_symlinks=False):
                    if relevant_dir(r):
                        stack.append(r)
                elif relevant(r):
                    st = e.stat()
                    out[r] = (st.st_mtime_ns, st.st_size)
            except OSError:
                continue
            if limit and len(out) >= limit:
                return out
    return out


def _digest(path: Path) -> str | None:
    try:
        st = path.stat()
        if st.st_size > HASH_MAX:
            return f"big:{st.st_size}:{st.st_mtime_ns}"
        return hashlib.sha1(path.read_bytes()).hexdigest()
    except IsADirectoryError:
        return "dir"
    except OSError:
        return None


class ContentFilter:
    """Drops changes whose content is what it was (a save without edits, an identical regenerated file)."""

    def __init__(self, root: Path):
        self.root = root
        self.known: dict[str, str | None] = {}

    def prime(self) -> None:
        for rel in scan_files(self.root, PRIME_MAX_FILES):
            self.known[rel] = _digest(self.root / rel)

    def changed(self, rels) -> list[str]:
        out = []
        for rel in rels:
            d = _digest(self.root / rel)
            if d == "dir":   # a new or removed folder counts: what's in it changed
                out.append(rel)
                continue
            if rel in self.known and self.known[rel] == d:
                continue
            if rel not in self.known and d is None:   # created and gone again (a temp file we didn't ignore)
                continue
            self.known[rel] = d
            out.append(rel)
        return out


# ================================================================ watchers

IN_MODIFY, IN_ATTRIB, IN_CLOSE_WRITE = 0x2, 0x4, 0x8
IN_MOVED_FROM, IN_MOVED_TO, IN_CREATE, IN_DELETE = 0x40, 0x80, 0x100, 0x200
IN_DELETE_SELF, IN_MOVE_SELF = 0x400, 0x800
IN_Q_OVERFLOW, IN_IGNORED, IN_ONLYDIR, IN_ISDIR = 0x4000, 0x8000, 0x01000000, 0x40000000
IN_NONBLOCK, IN_CLOEXEC = 0o4000, 0o2000000
DIR_MASK = (IN_CLOSE_WRITE | IN_MOVED_FROM | IN_MOVED_TO | IN_CREATE | IN_DELETE | IN_DELETE_SELF | IN_MOVE_SELF
            | IN_ONLYDIR)
FILE_EVENTS = IN_CLOSE_WRITE | IN_MOVED_TO | IN_MOVED_FROM | IN_DELETE
EVENT_HEAD = struct.Struct("iIII")


class WatchLimit(OSError):
    pass


class InotifyWatcher:
    """Linux inotify through ctypes, recursive over Sources/ (new folders are added as they appear) plus the
    project root. Watches folders, not files, so an editor's atomic save (write a temp file, rename it over
    the original: Vim, Neovim, Zed, JetBrains) shows up as IN_MOVED_TO of the real name."""

    mode = "inotify"

    def __init__(self, root: Path):
        self.root = root
        self.libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
        fd = self.libc.inotify_init1(IN_NONBLOCK | IN_CLOEXEC)
        if fd < 0:
            e = ctypes.get_errno()
            raise (WatchLimit if e == errno.EMFILE else OSError)(e, os.strerror(e))
        self.fd = fd
        self.wds: dict[int, str] = {}
        self.paths: dict[str, int] = {}
        try:
            if not self._add(""):
                raise OSError(errno.ENOENT, f"{root} is gone")
            self._add_tree(SOURCE_DIR)
        except BaseException:
            self.close()
            raise

    def _add(self, rel: str) -> bool:
        path = self.root / rel if rel else self.root
        wd = self.libc.inotify_add_watch(self.fd, os.fsencode(str(path)), DIR_MASK)
        if wd < 0:
            e = ctypes.get_errno()
            if e == errno.ENOSPC:
                raise WatchLimit(e, "the inotify watch limit (fs.inotify.max_user_watches) is reached")
            if e in (errno.ENOENT, errno.ENOTDIR, errno.EACCES):
                return False
            raise OSError(e, os.strerror(e))
        self.wds[wd] = rel
        self.paths[rel] = wd
        return True

    def _add_tree(self, rel: str) -> list[str]:
        """Watch `rel` and the folders under it; returns the relevant files already in them (a folder that
        was created, then filled before its watch existed)."""
        found: list[str] = []
        stack = [rel]
        while stack:
            r = stack.pop()
            if r not in self.paths and not self._add(r):
                continue
            try:
                entries = list(os.scandir(self.root / r))
            except OSError:
                continue
            for e in entries:
                sub = f"{r}/{e.name}"
                try:
                    if e.is_dir(follow_symlinks=False):
                        if relevant_dir(sub):
                            stack.append(sub)
                    elif relevant(sub):
                        found.append(sub)
                except OSError:
                    continue
        return found

    def _forget(self, rel: str) -> None:
        for r in [r for r in self.paths if r == rel or r.startswith(rel + "/")]:
            wd = self.paths.pop(r)
            self.wds.pop(wd, None)
            try:
                self.libc.inotify_rm_watch(self.fd, wd)
            except Exception:  # noqa: BLE001
                pass

    def fileno(self) -> int | None:
        return self.fd

    def read(self, now: float | None = None) -> list[str]:
        data = b""
        while True:
            try:
                chunk = os.read(self.fd, 65536)
            except BlockingIOError:
                break
            except InterruptedError:
                continue
            if not chunk:
                break
            data += chunk
        changes: list[str] = []
        off = 0
        while off + EVENT_HEAD.size <= len(data):
            wd, mask, _cookie, ln = EVENT_HEAD.unpack_from(data, off)
            off += EVENT_HEAD.size
            name = data[off:off + ln].split(b"\0", 1)[0].decode(errors="replace")
            off += ln
            if mask & IN_Q_OVERFLOW:   # events were lost: rewatch and treat it as a change
                self._add_tree(SOURCE_DIR)
                changes.append(SOURCE_DIR)
                continue
            base = self.wds.get(wd)
            if base is None:
                continue
            if mask & IN_IGNORED:
                self.wds.pop(wd, None)
                if self.paths.get(base) == wd:
                    self.paths.pop(base, None)
                continue
            if not name:
                continue
            rel = f"{base}/{name}" if base else name
            if mask & IN_ISDIR:
                if not relevant_dir(rel):
                    continue
                if mask & (IN_CREATE | IN_MOVED_TO):
                    files = self._add_tree(rel)
                    if files or mask & IN_MOVED_TO:
                        changes.append(rel)
                elif mask & (IN_DELETE | IN_MOVED_FROM):
                    self._forget(rel)
                    changes.append(rel)
                continue
            if mask & FILE_EVENTS and relevant(rel):
                changes.append(rel)
        return list(dict.fromkeys(changes))

    def close(self) -> None:
        if getattr(self, "fd", -1) >= 0:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = -1


class PollWatcher:
    """Fallback: compare mtimes and sizes of every relevant file once per interval."""

    mode = "poll"

    def __init__(self, root: Path, interval: float = POLL_INTERVAL, clock: Callable[[], float] = time.monotonic):
        self.root = root
        self.interval = interval
        self.clock = clock
        self.snap = scan_files(root)
        self.last = clock()

    def fileno(self) -> int | None:
        return None

    def read(self, now: float | None = None) -> list[str]:
        now = self.clock() if now is None else now
        if now - self.last < self.interval:
            return []
        self.last = now
        new = scan_files(self.root)
        changed = [k for k in new if self.snap.get(k) != new[k]] + [k for k in self.snap if k not in new]
        self.snap = new
        return sorted(changed)

    def close(self) -> None:
        pass


def make_watcher(root: Path, force_poll: bool = False) -> tuple[Any, str | None]:
    """(watcher, why it polls or None). IOSSTUDIO_WATCH_POLL=1 forces polling."""
    if force_poll or os.environ.get("IOSSTUDIO_WATCH_POLL") == "1":
        return PollWatcher(root), "forced"
    try:
        return InotifyWatcher(root), None
    except WatchLimit as exc:
        return PollWatcher(root), f"inotify limit reached ({exc.strerror}); checking files every second instead"
    except (OSError, AttributeError) as exc:
        return PollWatcher(root), f"inotify isn't available ({exc}); checking files every second instead"


# ================================================================ scheduling (pure; driven by a clock)

class Scheduler:
    """Debounce and supersede, without I/O. Call changed() on file changes, tick() to learn whether a build
    should start now, finished() when it ended. Never two builds at once: a change during a build asks for
    it to be cancelled (changed() returns True) and one more build runs after it."""

    def __init__(self, debounce: float, max_delay: float = MAX_DELAY, supersede_limit: int = SUPERSEDE_LIMIT):
        self.debounce = debounce
        self.supersede_limit = supersede_limit
        self.streak = 0          # builds cancelled by saves in a row
        self.protected = False   # the running build finishes whatever is saved meanwhile
        self.max_delay = max(max_delay, debounce)
        self.pending = False
        self.first: float | None = None
        self.due_at: float | None = None
        self.building = False
        self.cancelling = False
        self.files: list[str] = []
        self.superseded = 0

    def changed(self, files, now: float) -> bool:
        for f in files:
            if f not in self.files:
                self.files.append(f)
        if not self.pending:
            self.pending = True
            self.first = now
        first = self.first if self.first is not None else now
        self.due_at = min(now + self.debounce, first + self.max_delay)
        if self.building and not self.cancelling and not self.protected:
            self.cancelling = True
            self.superseded += 1
            return True
        return False

    def request(self, now: float, delay: float = 0.0) -> None:
        """Ask for a build without a file change (the first build, a retry after `busy`)."""
        if not self.pending:
            self.pending = True
            self.first = now
        self.due_at = now + delay if self.due_at is None else min(self.due_at, now + delay)

    def due_in(self, now: float) -> float | None:
        if not self.pending or self.building or self.due_at is None:
            return None
        return max(0.0, self.due_at - now)

    def tick(self, now: float) -> list[str] | None:
        """The changed files when a build should start now (and it is marked running), else None."""
        if not self.pending or self.building or self.due_at is None or now < self.due_at:
            return None
        files, self.files = self.files, []
        self.pending, self.first, self.due_at = False, None, None
        self.building = True
        # Saves that keep coming faster than a build takes would cancel every build: after
        # supersede_limit cancelled builds in a row, this one finishes, so there's always a result.
        self.protected = self.streak >= self.supersede_limit
        return files

    def finished(self, superseded: bool | None = None) -> None:
        """The build ended; superseded: it was cancelled by a save (default: a cancel was asked)."""
        was = self.cancelling if superseded is None else superseded
        self.streak = self.streak + 1 if was else 0
        self.building = False
        self.cancelling = False
        self.protected = False


# ================================================================ builds (helper children)

class HelperBuild:
    """One `build <kind>` helper process, the same command the Build/Run buttons use: it writes the build
    history, the log and the diagnostics. Its own session; it dies with the watcher (PDEATHSIG)."""

    def __init__(self, kind: str, path: str, build_id: str, extra: list[str] | None = None,
                 argv: list[str] | None = None):
        self.kind = kind
        self.build_id = build_id
        self.argv = argv or [*_python(), str(HELPER_MAIN), "build", kind, "--path", path,
                             "--build-id", build_id, *(extra or [])]
        self.proc = subprocess.Popen(self.argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, start_new_session=True, preexec_fn=_pdeathsig,
                                     close_fds=True)
        os.set_blocking(self.proc.stdout.fileno(), False)
        self.buf = b""
        self.eof = False
        self.done: dict | None = None
        self.log_file: str | None = None
        self.cancel_at: float | None = None
        self.started = time.monotonic()

    @property
    def pgid(self) -> int:
        return self.proc.pid

    def fileno(self) -> int | None:
        return None if self.eof else self.proc.stdout.fileno()

    def read_events(self) -> list[dict]:
        out: list[dict] = []
        while not self.eof:
            try:
                chunk = os.read(self.proc.stdout.fileno(), 65536)
            except BlockingIOError:
                break
            except InterruptedError:
                continue
            if not chunk:
                self.eof = True
                break
            self.buf += chunk
        *lines, self.buf = self.buf.split(b"\n")
        if self.eof and self.buf:
            lines.append(self.buf)
            self.buf = b""
        for ln in lines:
            try:
                ev = json.loads(ln.decode(errors="replace"))
            except ValueError:
                continue
            if not isinstance(ev, dict):
                continue
            if ev.get("event") == "build" and ev.get("logFile"):
                self.log_file = ev["logFile"]
            if ev.get("event") == "done":
                self.done = ev
            out.append(ev)
        return out

    def finished(self) -> bool:
        if not self.eof:
            return False
        return self.proc.poll() is not None or self._reap()

    def _reap(self) -> bool:
        try:
            self.proc.wait(timeout=0.2)
            return True
        except subprocess.TimeoutExpired:
            return False

    def cancel(self) -> None:
        """SIGTERM: the helper cancels xtool, kills its process tree and records the build as cancelled."""
        if self.cancel_at is None:
            self.cancel_at = time.monotonic()
            try:
                self.proc.send_signal(signal.SIGTERM)
            except ProcessLookupError:
                pass

    def enforce(self) -> None:
        """A cancelled build that didn't end within CANCEL_GRACE: kill its whole group."""
        if self.cancel_at is not None and time.monotonic() - self.cancel_at > CANCEL_GRACE:
            _killpg(self.pgid)

    def kill(self) -> None:
        _killpg(self.pgid)
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass

    def close(self) -> None:
        try:
            self.proc.stdout.close()
        except OSError:
            pass

    def result(self) -> dict:
        """{ok, data | error}: the done event, or a synthetic error when the helper died without one."""
        if self.done:
            return self.done
        rc = self.proc.poll()
        code = "cancelled" if self.cancel_at is not None else "build-failed"
        return {"event": "done", "ok": False, "error": {"code": code, "message": "Build cancelled." if
                                                        code == "cancelled" else f"The build helper exited ({rc})."}}


def short_diags(diags: list, n: int = FIRST_DIAGS) -> list[dict]:
    """Errors first, then warnings: ≤ n of {severity, category, file, line, col, message}."""
    ds = [d for d in diags or [] if isinstance(d, dict) and d.get("severity") in ("error", "warning")]
    ds = [d for d in ds if d.get("severity") == "error"] + [d for d in ds if d.get("severity") != "error"]
    out = []
    for d in ds[:n]:
        out.append({k: v for k, v in {
            "severity": d.get("severity"), "category": d.get("category") or ("compiler" if d.get("file") else "other"),
            "file": d.get("file"), "line": d.get("line"), "col": d.get("col"),
            "message": " ".join(str(d.get("message") or "").split())[:300]}.items() if v is not None})
    return out


def summarize(done: dict, build_id: str, log_file: str | None) -> dict:
    """A finished build's done event -> lastBuild."""
    if done.get("ok"):
        b = done.get("data") or {}
        err = None
    else:
        err = done.get("error") or {}
        b = err.get("build") or {}
    diags = b.get("diagnostics") or []
    errors = b.get("errors")
    if errors is None:
        errors = sum(1 for d in diags if isinstance(d, dict) and d.get("severity") == "error")
    warnings = b.get("warnings")
    if warnings is None:
        warnings = sum(1 for d in diags if isinstance(d, dict) and d.get("severity") == "warning")
    out = {"id": build_id, "result": "ok" if done.get("ok") else "failed", "duration": b.get("duration"),
           "time": b.get("time") or time.time(), "errors": errors, "warnings": warnings,
           "logFile": b.get("logFile") or log_file, "diagnostics": short_diags(diags)}
    if err:
        out["errorCode"] = err.get("code")
        out["errorMessage"] = str(err.get("message") or "")[:400]
    return out


# ================================================================ the watcher

def _next_build_number() -> int:
    """w-<n> ids keep counting across watchers: one more than the highest in the build history."""
    try:
        import projects
        hist = projects._history()
    except Exception:  # noqa: BLE001
        return 1
    best = 0
    for e in hist:
        m = re.fullmatch(r"w-(\d+)", str(e.get("buildId") or "")) if isinstance(e, dict) else None
        if m:
            best = max(best, int(m.group(1)))
    return best + 1


def _device_for_run() -> tuple[str | None, str | None]:
    """(udid, None) when an iPhone is connected (the device daemon's last-known state is 'connected' and the
    daemon is running), else (None, why not)."""
    st = common.load_json(common.STATE_DIR / "device.json", None)
    if not isinstance(st, dict) or not st.get("udid"):
        return None, "no iPhone has been connected yet"
    if st.get("connection") != "connected":
        return None, f"{st.get('name') or 'the iPhone'} isn't connected right now"
    if not (runtime_dir() / "deviced.sock").exists():
        return None, "the iPhone service isn't running (open iOS Studio or the iPhone page)"
    return str(st["udid"]), None


class Watcher:
    """The watcher process: file events -> Scheduler -> HelperBuild, snapshots to watch.json and
    watch-events.jsonl. `build_factory(kind, build_id, extra)` makes the builds (tests pass a fake)."""

    def __init__(self, root: Path, auto_run: bool = False, debounce_ms: int = DEFAULT_DEBOUNCE_MS,
                 initial_build: bool = True, build_factory: Callable[..., Any] | None = None,
                 watcher: Any = None, clock: Callable[[], float] = time.monotonic, owner_check: bool = True,
                 device_check: Callable[[], tuple[str | None, str | None]] | None = None,
                 auth_check: Callable[[], bool] | None = None, run_steps: Any = None):
        self.root = root
        self.auto_run = auto_run
        self.debounce_ms = debounce_ms
        self.initial_build = initial_build
        self.build_factory = build_factory or (lambda kind, bid, extra: HelperBuild(
            kind, str(root), bid, [*extra, "--trigger", "watch"]))
        self.watcher = watcher
        self.mode_reason: str | None = None
        self.clock = clock
        self.owner_check = owner_check
        self.check_project = True
        self.device_check = device_check or _device_for_run
        self.auth_check = auth_check
        self.run_steps = run_steps            # install/launch over the device daemon (tests replace it)
        self.sched = Scheduler(debounce_ms / 1000.0)
        self.filter = ContentFilter(root)
        self.build: Any = None
        self.build_files: list[str] = []
        self.run_job: RunJob | None = None
        self.stop_reason: str | None = None
        self.stop_message: str | None = None
        self.reload = False
        self.lock = threading.RLock()
        self.seq = 0
        self.number = 0
        self.last_owner_check = 0.0
        self.last_progress = 0.0
        self.snap: dict = {}

    # ---- snapshots
    def publish(self, state: str | None = None, reason: str = "", message: str | None = None, **fields: Any) -> dict:
        with self.lock:
            self.seq += 1
            if state:
                self.snap["state"] = state
            if message is not None:
                self.snap["message"] = message
            self.snap.update(fields)
            for k in [k for k, v in self.snap.items() if v is None]:
                self.snap.pop(k)
            self.snap.update(event="watch", seq=self.seq, reason=reason, updatedAt=time.time(),
                             autoRun=self.auto_run, debounceMs=self.debounce_ms,
                             builds=self.number, superseded=self.sched.superseded)
            snap = dict(self.snap)
            _save(status_path(), snap)
            line = json.dumps(snap, separators=(",", ":"), default=str) + "\n"
            ev = events_path()
            try:
                if ev.exists() and ev.stat().st_size > EVENTS_MAX:
                    ev.write_text(line)
                else:
                    with open(ev, "a") as fh:
                        fh.write(line)
            except OSError:
                pass
            return snap

    def _new_id(self) -> str:
        if not self.number:
            self.number = _next_build_number() - 1
        self.number += 1
        return f"w-{self.number}"

    # ---- builds
    def start_compile(self, files: list[str]) -> None:
        bid = self._new_id()
        self.build_files = files
        try:
            self.build = self.build_factory("compile", bid, [])
        except OSError as exc:
            self.sched.finished()
            self.publish("failed", "build-started", f"Couldn't start the build: {exc}")
            return
        self.publish("compiling", "build-started",
                     f"Compiling ({len(files)} changed file{'s' if len(files) != 1 else ''})" if files
                     else "Compiling", buildId=bid, logFile=None, progress=None,
                     changed=files[:CHANGED_SHOWN], changedCount=len(files))

    def on_build_events(self, events: list[dict]) -> None:
        for ev in events:
            if ev.get("event") == "build" and ev.get("logFile"):
                self.publish(reason="build-log", logFile=ev["logFile"])
            elif ev.get("event") == "progress" and self.clock() - self.last_progress >= PROGRESS_EVERY:
                self.last_progress = self.clock()
                frac = ev.get("fraction")
                self.publish(reason="progress", progress={"fraction": frac, "detail": str(ev.get("detail") or "")[:200]})

    def on_build_done(self) -> None:
        b, self.build = self.build, None
        done = b.result()
        getattr(b, "close", lambda: None)()
        code = (done.get("error") or {}).get("code") if not done.get("ok") else None
        self.sched.finished(superseded=code == "cancelled" and self.sched.cancelling)
        if code == "cancelled":
            if self.stop_reason:
                return
            self.publish("changed", "superseded", "Files changed during the build; compiling again",
                         progress=None)
            return
        if code == "busy":
            self.sched.request(self.clock(), BUSY_RETRY)
            self.publish("changed", "busy", "Another build of this project is running; compiling after it",
                         progress=None)
            return
        last = summarize(done, b.build_id, b.log_file)
        if last["result"] == "ok":
            w = last["warnings"]
            msg = "Compiled cleanly" + (f", {w} warning{'s' if w != 1 else ''}" if w else "")
            self.publish("ok", "build-finished", msg, lastBuild=last, progress=None, logFile=last["logFile"])
            if self.auto_run and not self.sched.pending:
                self.maybe_run(last["id"])
        else:
            n = last["errors"]
            msg = (f"{n} error{'s' if n != 1 else ''}" if n else (last.get("errorMessage") or "The build failed"))
            self.publish("failed", "build-finished", msg, lastBuild=last, progress=None, logFile=last["logFile"])

    # ---- auto-run
    def maybe_run(self, for_build: str | None = None) -> None:
        """Auto-run after the clean compile `for_build` (its id goes in run.forBuild)."""
        if self.run_job and self.run_job.alive() and self.run_job.phase in ("installing", "launching"):
            self.publish(reason="run", run={"state": "skipped", "message": "The previous run is still installing, "
                                            "so this clean build wasn't run.", "time": time.time(),
                                            "forBuild": for_build})
            return
        self.run_job = RunJob(self, self._new_id(), for_build)
        self.run_job.start()

    def run_blocks(self) -> bool:
        """A run that is still checking or building holds the project: compiles wait for it."""
        return bool(self.run_job and self.run_job.alive() and self.run_job.phase in ("checking", "building"))

    # ---- main loop
    def on_changes(self, files: list[str]) -> None:
        now = self.clock()
        cancel = self.sched.changed(files, now)
        if self.run_job and self.run_job.alive() and self.run_job.phase in ("checking", "building"):
            self.run_job.cancel("A newer change came in before the run started; compiling that first.")
        if cancel and self.build:
            self.build.cancel()
            self.publish(reason="superseding", message="Files changed during the build; restarting it",
                         changed=self.sched.files[:CHANGED_SHOWN], changedCount=len(self.sched.files))
        elif self.build and self.sched.protected:
            self.publish(reason="changed", message="Files changed; this build finishes first (the last "
                         f"{self.sched.supersede_limit} were restarted), then it compiles again",
                         changed=self.sched.files[:CHANGED_SHOWN], changedCount=len(self.sched.files))
        elif not self.build:
            n = len(self.sched.files)
            self.publish("changed", "changed", f"{n} file{'s' if n != 1 else ''} changed",
                         changed=self.sched.files[:CHANGED_SHOWN], changedCount=n)

    def check_owners(self) -> None:
        now = self.clock()
        if not self.owner_check or now - self.last_owner_check < OWNER_CHECK:
            return
        self.last_owner_check = now
        owners = live_owners()
        if not owners:
            self.stop_reason = "no-owner"
            self.stop_message = "Stopped: the app or MCP session that used the watcher has ended"
            return
        if len(owners) != self.snap.get("owners"):
            self.publish(reason="owners", owners=len(owners))
        if self.check_project and not (self.root / "Package.swift").is_file():
            self.stop_reason = "project-gone"
            self.stop_message = "Stopped: the project folder or its Package.swift is gone"

    def apply_control(self) -> None:
        c = _load(control_path(), {}) or {}
        if c.get("path") not in (None, str(self.root)):
            return
        if "autoRun" in c:
            self.auto_run = bool(c["autoRun"])
        if c.get("debounceMs"):
            self.debounce_ms = clamp_debounce(c["debounceMs"])
            self.sched.debounce = self.debounce_ms / 1000.0
            self.sched.max_delay = max(MAX_DELAY, self.sched.debounce)
        self.publish(reason="settings", message=("Watching; auto-run on" if self.auto_run else "Watching")
                     if self.snap.get("state") in ("watching", None) else None)

    def step(self, timeout: float = 0.5) -> None:
        """One turn of the loop (tests call it directly)."""
        now = self.clock()
        d = self.sched.due_in(now)
        if d is not None:
            timeout = min(timeout, d)
        fds = [f for f in (self.watcher.fileno() if self.watcher else None,
                           self.build.fileno() if self.build else None) if f is not None]
        try:
            if fds:
                select.select(fds, [], [], max(timeout, 0))
            elif timeout > 0:
                time.sleep(min(timeout, 0.2))
        except InterruptedError:
            pass
        if self.reload:
            self.reload = False
            self.apply_control()
        raw = self.watcher.read(self.clock()) if self.watcher else []
        changes = self.filter.changed(raw) if raw else []
        if changes:
            self.on_changes(changes)
        if self.build:
            self.on_build_events(self.build.read_events())
            self.build.enforce()
            if self.build.finished():
                self.on_build_events(self.build.read_events())
                self.on_build_done()
        if not self.build and not self.run_blocks():
            files = self.sched.tick(self.clock())
            if files is not None:
                self.start_compile(files)
        self.check_owners()

    def setup(self) -> None:
        if self.watcher is None:
            self.watcher, self.mode_reason = make_watcher(self.root)
        self.filter.prime()
        name = self.root.name
        self.snap = {"project": name, "path": str(self.root), "pid": os.getpid(), "mode": self.watcher.mode,
                     "modeReason": self.mode_reason, "startedAt": time.time(), "owners": len(live_owners())}
        self.publish("watching", "started", "Watching for changes" + (" (checking files every second)"
                                                                       if self.watcher.mode == "poll" else ""))
        if self.initial_build:
            self.sched.request(self.clock())

    def shutdown(self) -> None:
        if self.build:
            self.build.cancel()
        if self.run_job and self.run_job.alive():
            self.run_job.cancel("The watcher stopped.")
        deadline = time.monotonic() + CANCEL_GRACE
        while self.build and not self.build.finished() and time.monotonic() < deadline:
            self.build.read_events()
            time.sleep(0.1)
        if self.build:
            self.build.kill()
            self.build = None
        if self.run_job:
            self.run_job.join(CANCEL_GRACE + 1)
        if self.watcher:
            self.watcher.close()
        reason = self.stop_reason or "stopped"
        self.publish("stopped", "stopped", self.stop_message or "Stopped", stopReason=reason, stoppedAt=time.time(),
                     progress=None, owners=0)
        try:
            owners_path().unlink()
        except OSError:
            pass

    def loop(self) -> None:
        self.setup()
        try:
            while not self.stop_reason:
                self.step()
        finally:
            self.shutdown()


class RunJob(threading.Thread):
    """Auto-run after a clean compile: check the iPhone and Apple ID, `build run`, then (Wi-Fi) install and
    launch through the device daemon. phase: checking | building | installing | launching | done."""

    def __init__(self, w: Watcher, build_id: str, for_build: str | None = None):
        super().__init__(daemon=True, name="watch-run")
        self.w = w
        self.build_id = build_id      # the run's own build (w-N, in the history)
        self.for_build = for_build    # the clean compile it follows
        self.phase = "checking"
        self.cancelled: str | None = None
        self.build: Any = None
        self.once: subprocess.Popen | None = None

    def alive(self) -> bool:
        return self.is_alive()

    def report(self, state: str, message: str, **extra: Any) -> None:
        self.w.publish(reason="run", run={"state": state, "message": message, "buildId": self.build_id,
                                          "forBuild": self.for_build, "time": time.time(), **extra})

    def cancel(self, why: str) -> None:
        self.cancelled = why
        if self.build:
            self.build.cancel()
        if self.once and self.once.poll() is None:
            _killpg(self.once.pid, signal.SIGTERM)

    def run(self) -> None:  # noqa: C901
        try:
            self._run()
        except Exception as exc:  # noqa: BLE001  a failed run never takes the watcher down
            self.report("failed", f"Auto-run failed: {exc}")
        finally:
            self.phase = "done"

    def _run(self) -> None:
        self.report("checking", "Checking the iPhone and Apple ID")
        udid, why = self.w.device_check()
        if not udid:
            self.report("skipped", f"Not run: {why}.")
            return
        auth = self.w.auth_check
        if auth is None:
            import projects
            auth = projects._auth_ok
        if not auth():
            self.report("skipped", "Not run: sign in with your Apple ID first (Account page).")
            return
        if self.cancelled:
            self.report("skipped", self.cancelled)
            return
        self.phase = "building"
        self.report("building", "Building, signing and installing on the iPhone")
        extra = ["--udid", udid]
        preset = _selected_preset(self.w.root)
        if preset:
            extra += ["--preset", preset]
        self.build = self.w.build_factory("run", self.build_id, extra)
        while not self.build.finished():
            if self.cancelled:          # also a cancel that came before self.build was set
                self.build.cancel()
            fd = self.build.fileno()
            if fd is not None:
                try:
                    select.select([fd], [], [], 0.5)
                except InterruptedError:
                    pass
            else:
                time.sleep(0.1)
            self.build.read_events()
            self.build.enforce()
        self.build.read_events()
        done = self.build.result()
        if self.cancelled or (done.get("error") or {}).get("code") == "cancelled":
            self.report("skipped", self.cancelled or "The run was cancelled.")
            return
        if not done.get("ok"):
            err = done.get("error") or {}
            self.report("failed", str(err.get("message") or "The run failed.")[:400], errorCode=err.get("code"))
            return
        data = done.get("data") or {}
        steps = self.w.run_steps or DeviceSteps(self)
        bundle = data.get("bundleId")
        if data.get("needsInstall") and data.get("ipa"):
            self.phase = "installing"
            self.report("installing", "Installing on the iPhone over Wi-Fi")
            res = steps.install(udid, data["ipa"])
            if self.cancelled:
                self.report("skipped", self.cancelled)
                return
            if not res.get("ok"):
                self.report("failed", "Built and signed, but installing failed: " +
                            str((res.get("error") or {}).get("message") or "the iPhone wasn't reachable")[:300])
                return
            bundle = (res.get("data") or {}).get("bundleId") or bundle
            try:
                import projects
                projects._record_signing(str(self.w.root), udid, bundle, "wifi", data.get("profile"))
            except Exception:  # noqa: BLE001
                pass
        self.phase = "launching"
        launch = data.get("launch") or {}
        res = steps.launch(udid, bundle, launch.get("args") or [], launch.get("env") or {})
        if not res.get("ok"):
            self.report("failed", "Installed, but it didn't open: " +
                        str((res.get("error") or {}).get("message") or "launch failed")[:300], bundleId=bundle)
            return
        self.report("launched", "Running on the iPhone", bundleId=(res.get("data") or {}).get("bundleId") or bundle)


def _selected_preset(root: Path) -> str | None:
    """The run preset selected for the project in the app (what its Run button uses)."""
    try:
        import projects
        name = (projects._state()["meta"].get(str(root)) or {}).get("runPreset")
    except Exception:  # noqa: BLE001
        return None
    return name if isinstance(name, str) and name.strip() else None


class DeviceSteps:
    """Install and launch through `bin/iosstudio-deviced --once` (the shared device daemon), killable."""

    def __init__(self, job: RunJob):
        self.job = job

    def _once(self, cmd: str, args: dict, timeout: float) -> dict:
        exe = common.REPO / "bin/iosstudio-deviced"
        try:
            p = subprocess.Popen([str(exe), "--once", cmd, json.dumps(args)], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                 start_new_session=True, preexec_fn=_pdeathsig)
        except OSError as exc:
            return {"ok": False, "error": {"message": str(exc)}}
        self.job.once = p
        try:
            out, _ = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _killpg(p.pid)
            p.wait()
            return {"ok": False, "error": {"message": "timed out"}}
        finally:
            self.job.once = None
        for line in reversed(out.strip().splitlines()):
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict) and "ok" in obj and "event" not in obj:
                return obj
        return {"ok": False, "error": {"message": "the iPhone service gave no answer"}}

    def install(self, udid: str, ipa: str) -> dict:
        return self._once("app.install", {"udid": udid, "path": ipa}, 600)

    def launch(self, udid: str, bundle: str | None, args: list, env: dict) -> dict:
        if not bundle:
            return {"ok": False, "error": {"message": "no bundle id"}}
        apps = self._once("apps.list", {"udid": udid, "icons": False, "sizes": False}, 60)
        items = apps.get("data") if apps.get("ok") and isinstance(apps.get("data"), list) else []
        plain = bundle.split(".", 1)[-1] if bundle.startswith("XTL-") else bundle
        real = next((a.get("bundleId") for a in items if a.get("bundleId") == bundle), None) or \
            next((a.get("bundleId") for a in items if str(a.get("bundleId") or "").endswith("." + plain)), bundle)
        a: dict = {"udid": udid, "bundleId": real}
        if args:
            a["args"] = args
        if env:
            a["env"] = env
        return self._once("app.launch", a, 60)


# ================================================================ starting, stopping, status

def clamp_debounce(v: Any) -> int:
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        raise HelperError("usage", "--debounce-ms must be a number of milliseconds.")
    return max(MIN_DEBOUNCE_MS, min(MAX_DEBOUNCE_MS, n))


def _project(flags: dict) -> Path:
    raw = flags.get("project") or flags.get("path")
    if not raw or raw is True:
        raise HelperError("usage", "Missing --project.")
    p = Path(os.path.expanduser(str(raw))).resolve()
    if os.environ.get("IOSSTUDIO_DEMO"):
        return p
    if not p.is_dir():
        raise HelperError("not-found", "That project folder doesn't exist anymore.",
                          {**common.fix("Remove from list", "projects", action="remove"), "path": str(p)})
    if not (p / "Package.swift").is_file():
        raise HelperError("not-a-project", "That folder isn't an xtool app (it needs Package.swift and xtool.yml).")
    return p


def _flag_owner(flags: dict, default: int) -> int:
    v = flags.get("owner")
    try:
        return int(v) if v and v is not True else default
    except ValueError:
        raise HelperError("usage", "--owner must be a process id.")


def current_status() -> dict:
    """The status, with a dead watcher detected (its lock is free) and recorded as stopped."""
    st = read_status()
    pid = watcher_pid()
    if st and st.get("state") != "stopped" and pid is None:
        with _flock(".watch-start.lock"):
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
    _save(status_path(), st)
    try:
        with open(events_path(), "a") as fh:
            fh.write(json.dumps(st, separators=(",", ":"), default=str) + "\n")
        owners_path().unlink()
    except OSError:
        pass
    return st


def _spawn(root: Path, auto_run: bool, debounce_ms: int, initial: bool, owner: int) -> int:
    argv = [*_python(), str(HELPER_MAIN), "watch", "serve", "--project", str(root), "--debounce-ms",
            str(debounce_ms), "--owner", str(owner)]
    if auto_run:
        argv.append("--auto-run")
    if not initial:
        argv.append("--no-initial-build")
    log = open(_p("watch.log"), "a")
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
    while time.monotonic() < deadline:
        if watcher_pid() is None:
            return True
        try:
            os.waitpid(pid, os.WNOHANG)   # our own child (a follower that started it)
        except ChildProcessError:
            pass
        time.sleep(0.1)
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
        _save(status_path(), st)
        return st
    return read_status()


def ensure(root: Path, auto_run: bool | None, debounce_ms: int | None, owner: int, initial: bool = True) -> dict:
    """Make sure the one watcher watches `root` and `owner` is among its owners. auto_run / debounce_ms None
    keep a running watcher's value (a new one gets False / DEFAULT_DEBOUNCE_MS); a given one changes it live.
    Another project's watcher is stopped first (reported as `replaced`). Returns the snapshot plus
    `attached` (an existing watcher was reused) and `replaced`."""
    with _flock(".watch-start.lock"):
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
                _save(control_path(), {"path": str(root), "autoRun": auto_run, "debounceMs": debounce_ms})
                try:
                    os.kill(pid, signal.SIGUSR1)
                except ProcessLookupError:
                    pass
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    s2 = read_status() or {}
                    if bool(s2.get("autoRun")) == auto_run and int(s2.get("debounceMs") or 0) == debounce_ms:
                        break
                    time.sleep(0.05)
            return {**(read_status() or st), "watching": True, "attached": True}
        if pid is not None:
            old = _stop_locked("replaced")
            if old:
                replaced = {"path": old.get("path"), "project": old.get("project")}
        elif st and st.get("state") != "stopped":
            _mark_dead(st)
        _save(owners_path(), {})
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
            time.sleep(0.05)
        tail = ""
        try:
            tail = _p("watch.log").read_text()[-1500:]
        except OSError:
            pass
        raise HelperError("watch-failed", "The watcher didn't start.", detail=tail)


def stop(project: str | None = None) -> dict:
    with _flock(".watch-start.lock"):
        st = read_status() or {}
        if watcher_pid() is None and st.get("state") in (None, "stopped"):
            return {"stopped": False, "watching": False, "message": "Not watching", **({"last": st} if st else {})}
        if project and st.get("path") != project:
            return {"stopped": False, "watching": True, "message": f"Watching {st.get('project')}, not that project",
                    "project": st.get("project"), "path": st.get("path")}
        final = _stop_locked("stopped") or {}
        return {**final, "stopped": True, "watching": False}


def wait_status(project: str | None, seconds: float) -> dict:
    """Block until a build that finished after this call (and nothing newer pending), the watcher stops,
    or `seconds` pass. Returns the status with `waited` seconds and `timedOut`."""
    seconds = max(0.0, min(float(seconds), MAX_STATUS_WAIT))
    start = time.monotonic()
    st = current_status()
    if not st.get("watching") or seconds <= 0:
        return {**st, "waited": 0.0, "timedOut": False}
    first_id = (st.get("lastBuild") or {}).get("id")
    while time.monotonic() - start < seconds:
        time.sleep(0.2)
        st = current_status()
        if not st.get("watching"):
            break
        last = (st.get("lastBuild") or {}).get("id")
        if last != first_id and st.get("state") in ("ok", "failed", "watching"):
            break
    else:
        return {**st, "waited": round(time.monotonic() - start, 1), "timedOut": True}
    return {**st, "waited": round(time.monotonic() - start, 1), "timedOut": False}


# ================================================================ commands

def cmd_serve(flags: dict) -> int:
    """The watcher process (started detached by `watch run`)."""
    root = _project(flags)
    owner = _flag_owner(flags, os.getppid())
    fh = open(lock_path(), "a+")
    deadline = time.monotonic() + 3
    while True:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.monotonic() > deadline:
                print("another watcher holds the lock", file=sys.stderr)
                return 1
            time.sleep(0.1)
    fh.seek(0)
    fh.truncate()
    fh.write(f"{os.getpid()}\n")
    fh.flush()
    add_owner(owner)
    demo = bool(os.environ.get("IOSSTUDIO_DEMO"))
    w: Watcher = DemoWatcher(root, bool(flags.get("auto_run"))) if demo else Watcher(
        root, auto_run=bool(flags.get("auto_run")), debounce_ms=clamp_debounce(flags.get("debounce_ms") or
                                                                               DEFAULT_DEBOUNCE_MS),
        initial_build=not flags.get("no_initial_build"))
    if demo:
        w.debounce_ms = clamp_debounce(flags.get("debounce_ms") or DEFAULT_DEBOUNCE_MS)

    def on_stop(signum, _frame):
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


def cmd_run(flags: dict) -> int:
    """Settings not given keep the running watcher's value (a new watcher gets the defaults)."""
    root = _project(flags)
    auto_run = _flag_bool(flags, "auto_run")
    debounce = clamp_debounce(flags["debounce_ms"]) if flags.get("debounce_ms") not in (None, True) else None
    initial = not flags.get("no_initial_build")
    if flags.get("detach"):
        owner = _flag_owner(flags, os.getppid())
        return common.ok(ensure(root, auto_run, debounce, owner, initial))
    return follow(root, auto_run, debounce, initial)


def follow(root: Path, auto_run: bool | None, debounce: int | None, initial: bool) -> int:
    """Streaming: start or join the watcher with this process as an owner and print its snapshots."""
    me = os.getpid()
    try:
        snap = ensure(root, auto_run, debounce, me, initial)
    except HelperError as exc:
        common.done(False, error=exc)
        return 1
    leave = {"why": None}

    def on_signal(signum, _frame):
        leave["why"] = "detached"

    for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(s, on_signal)
    for k in ("attached", "replaced"):
        if snap.get(k):
            common.event("watch-start", **{k: snap[k]})
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
                    common._write(ev)
                    if ev.get("state") == "stopped":
                        final = ev
                        leave["why"] = "stopped"
                        break
            if leave["why"]:
                break
            if watcher_pid() is None:
                final = current_status()
                if final.get("state") == "stopped" and int(final.get("seq") or 0) > seq:
                    common._write(final)
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
        return common.done(True, {**(read_status() or {}), "detached": True})
    return common.done(True, final or read_status() or {})


def cmd_status(flags: dict) -> int:
    raw = flags.get("project") or flags.get("path")
    project = str(Path(os.path.expanduser(str(raw))).resolve()) if raw and raw is not True else None
    wait = flags.get("wait_seconds")
    st = wait_status(project, float(wait)) if wait and wait is not True else current_status()
    if project and st.get("path") and st.get("path") != project:
        st = {"watching": False, "state": "off", "message": f"Not watching this project (watching "
              f"{st.get('project')})" if st.get("watching") else "Not watching this project",
              "other": {"project": st.get("project"), "path": st.get("path"), "state": st.get("state")}}
    return common.ok(st)


def cmd_stop(flags: dict) -> int:
    raw = flags.get("project") or flags.get("path")
    project = str(Path(os.path.expanduser(str(raw))).resolve()) if raw and raw is not True else None
    return common.ok(stop(project))


GROUPS = {
    "watch": {
        "run": cmd_run,
        "serve": cmd_serve,
        "status": cmd_status,
        "stop": cmd_stop,
    },
}


# ================================================================ demo

DEMO_ERROR = {"severity": "error", "category": "compiler", "line": 42, "col": 17,
              "message": "cannot find 'habbits' in scope"}


class DemoWatcher(Watcher):
    """Demo mode: no files are watched and nothing is built. A scripted cycle repeats: watching -> changed
    -> compiling -> failed (1 error) -> watching -> changed -> compiling -> ok (auto-run: launched)."""

    SCRIPT = [(2.0, "changed"), (0.6, "compiling"), (3.0, "failed"), (3.0, "changed"), (0.6, "compiling"),
              (3.0, "ok"), (6.0, "changed")]

    def __init__(self, root: Path, auto_run: bool = False, speed: float = 1.0):
        super().__init__(root, auto_run=auto_run, initial_build=False, watcher=None)
        self.speed = speed
        self.check_project = False   # demo projects are made up
        self.i = 0
        self.next_at = 0.0
        self.t0 = 0.0

    def setup(self) -> None:
        self.snap = {"project": self.root.name, "path": str(self.root), "pid": os.getpid(), "mode": "inotify",
                     "startedAt": time.time(), "owners": len(live_owners())}
        self.number = 0
        self.publish("watching", "started", "Watching for changes")
        self.next_at = self.clock() + self.SCRIPT[0][0] * self.speed

    def step(self, timeout: float = 0.5) -> None:
        if self.reload:
            self.reload = False
            self.apply_control()
        now = self.clock()
        if now < self.next_at:
            time.sleep(min(0.2, self.next_at - now))
            self.check_owners()
            return
        _, what = self.SCRIPT[self.i % len(self.SCRIPT)]
        f = f"Sources/{self.root.name}/ContentView.swift"
        if what == "changed":
            self.publish("changed", "changed", "1 file changed", changed=[f], changedCount=1)
        elif what == "compiling":
            self.number += 1
            self.t0 = time.time()
            self.publish("compiling", "build-started", "Compiling (1 changed file)", buildId=f"w-{self.number}",
                         progress={"fraction": 0.4, "detail": "Compiling"})
        else:
            bad = what == "failed"
            diag = [{**DEMO_ERROR, "file": str(self.root / f)}] if bad else []
            last = {"id": f"w-{self.number}", "result": "failed" if bad else "ok", "duration": 3.1,
                    "time": self.t0, "errors": 1 if bad else 0, "warnings": 0, "diagnostics": diag,
                    "logFile": str(runtime_dir() / "demo-build.log")}
            if bad:
                last.update(errorCode="build-failed", errorMessage="The build failed.")
            self.publish(what, "build-finished", "1 error" if bad else "Compiled cleanly", lastBuild=last,
                         progress=None)
            if not bad and self.auto_run:
                self.publish(reason="run", run={"state": "launched", "message": "Running on the iPhone (demo)",
                                                "buildId": f"w-{self.number}", "forBuild": f"w-{self.number}",
                                                "time": time.time(), "bundleId": "com.example.habits"})
        self.i += 1
        self.next_at = self.clock() + self.SCRIPT[self.i % len(self.SCRIPT)][0] * self.speed
        self.check_owners()

    def shutdown(self) -> None:
        self.publish("stopped", "stopped", self.stop_message or "Stopped", stopReason=self.stop_reason or "stopped",
                     stoppedAt=time.time(), progress=None, owners=0)
        try:
            owners_path().unlink()
        except OSError:
            pass


def demo(action: str, flags: dict) -> int:
    """Demo mode: the same commands on the demo runtime dir; `serve` runs the scripted DemoWatcher."""
    handler = GROUPS["watch"].get(action)
    if handler is None:
        return common.fail("usage", f"Unknown command 'watch {action}'.") or 2
    try:
        return int(handler(flags) or 0)
    except HelperError as exc:
        return common.fail(exc.code, exc.message, exc.fix, exc.detail)
