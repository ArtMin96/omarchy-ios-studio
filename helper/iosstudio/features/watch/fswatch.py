"""File watching: which paths count as a change, inotify (ctypes) with a polling fallback, and a content filter."""
from __future__ import annotations

import ctypes
import ctypes.util
import errno
import hashlib
import os
import re
import struct
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Protocol

POLL_INTERVAL = 1.0
POLL_MAX_FILES = 20_000  # the polling fallback stats at most this many files per pass
HASH_MAX = 4 * 1024 * 1024
PRIME_MAX_FILES = 5000

ROOT_FILES = {"Package.swift", "xtool.yml"}       # Package.resolved is left out: builds rewrite it themselves
ROOT_SUFFIXES = (".plist", ".entitlements")
SOURCE_DIR = "Sources"
IGNORED_DIRS = {".build", ".bsp", "xtool", ".git", ".swiftpm", "DerivedData", "node_modules", ".vscode", ".idea",
                ".zed", "build", "Pods"}
TEMP_SUFFIXES = (".swp", ".swo", ".swx", ".swpx", ".swn", "~", ".tmp", ".temp", ".bak", ".orig", ".crswap",
                 ".kate-swp", "___jb_tmp___", "___jb_old___", ".part")
TEMP_RX = re.compile(r"\.tmp[.\-_]?\w*$|\.sw[a-p]$|^#.*#$")


class FileWatcher(Protocol):
    """What the watcher loop needs from a file watcher (InotifyWatcher, PollWatcher, a test fake)."""
    mode: str

    def fileno(self) -> int | None: ...
    def read(self, now: float | None = None) -> list[str]: ...
    def close(self) -> None: ...


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


def file_digest(path: Path) -> str | None:
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
            self.known[rel] = file_digest(self.root / rel)

    def changed(self, rels: Iterable[str]) -> list[str]:
        out = []
        for rel in rels:
            d = file_digest(self.root / rel)
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

IN_CLOSE_WRITE = 0x8
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
        self.snap = scan_files(root, POLL_MAX_FILES)
        self.last = clock()

    def fileno(self) -> int | None:
        return None

    def read(self, now: float | None = None) -> list[str]:
        now = self.clock() if now is None else now
        if now - self.last < self.interval:
            return []
        self.last = now
        new = scan_files(self.root, POLL_MAX_FILES)
        changed = [k for k in new if self.snap.get(k) != new[k]] + [k for k in self.snap if k not in new]
        self.snap = new
        return sorted(changed)

    def close(self) -> None:
        pass


def make_watcher(root: Path, force_poll: bool = False) -> tuple[FileWatcher, str | None]:
    """(watcher, why it polls or None). IOSSTUDIO_WATCH_POLL=1 forces polling."""
    if force_poll or os.environ.get("IOSSTUDIO_WATCH_POLL") == "1":
        return PollWatcher(root), "forced"
    try:
        return InotifyWatcher(root), None
    except WatchLimit as exc:
        return PollWatcher(root), f"inotify limit reached ({exc.strerror}); checking files every second instead"
    except (OSError, AttributeError) as exc:
        return PollWatcher(root), f"inotify isn't available ({exc}); checking files every second instead"
