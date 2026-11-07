"""Shared pieces of the Files feature (camera roll, app containers, camera tests) for both device
daemons: features/files.py + cameratest.py (real phone) and their demo twins. Stdlib only,
no side effects on import.

* Transfers: cancellable transfers (media.pull, container.pull/push, cameratest.run) by id, with
  throttled `transfer` progress events; transfer.cancel and a client disconnect cancel them.
* Path rules: phone paths are POSIX, never contain `..`, and container paths stay inside the
  vended root (`/` for your own apps, `/Documents` for file-sharing App Store apps).
* Local files: unique names in the destination folder, HEIC -> JPEG and thumbnail conversion with
  whatever is installed (heif-convert from libheif, vips, ImageMagick, ffmpeg).
* Photos.sqlite: the camera roll index (dates, sizes, kinds), read only when its columns are there.
"""
from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import os
import posixpath
import re
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

PHOTO_EXT = {".heic", ".heif", ".jpg", ".jpeg", ".png", ".gif", ".dng", ".tif", ".tiff", ".webp"}
VIDEO_EXT = {".mov", ".mp4", ".m4v", ".hevc"}
HEIC_EXT = {".heic", ".heif"}
CHUNK = 1 << 20               # 1 MiB per AFC read/write
PROGRESS_EVERY = 0.25         # seconds between transfer events
COCOA_EPOCH = 978307200       # 2001-01-01 in Unix time (Core Data timestamps)
CAMERA_IN = "/Documents/CameraTestInput"
CAMERA_OUT = "/Documents/CameraTestOutput"
DEFAULT_TEST_ARGS = ["-cameraTestMode", "1"]


# ---------------------------------------------------------------- media kinds

def media_kind(name: str) -> str | None:
    ext = posixpath.splitext(name)[1].lower()
    if ext in PHOTO_EXT:
        return "photo"
    if ext in VIDEO_EXT:
        return "video"
    return None


def is_heic(name: str) -> bool:
    return posixpath.splitext(name)[1].lower() in HEIC_EXT


def iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts))


# ---------------------------------------------------------------- phone paths

class PathError(ValueError):
    pass


def clean_phone_path(path: str, root: str = "/") -> str:
    """Normalise a phone path ('Documents/a' -> '/Documents/a') and keep it inside `root`."""
    p = str(path or "/").replace("\\", "/")
    if "\x00" in p or ".." in p.split("/"):
        raise PathError("That path isn't allowed.")
    p = posixpath.normpath("/" + p.lstrip("/"))
    root = posixpath.normpath("/" + root.lstrip("/"))
    if root != "/" and p != root and not p.startswith(root + "/"):
        raise PathError(f"Only files inside {root} can be reached for this app.")
    return p


def clean_dcim_path(path: str) -> str:
    """A camera roll path, 'DCIM/100APPLE/IMG_0001.HEIC' (no leading slash), read-only."""
    p = str(path or "").replace("\\", "/").lstrip("/")
    if not p.startswith("DCIM/") or ".." in p.split("/") or "\x00" in p:
        raise PathError("That isn't a photo or video in the camera roll.")
    return posixpath.normpath(p)


# ---------------------------------------------------------------- local paths

def expand_dest(dest: str | None, default: Path) -> Path:
    raw = str(dest or "").strip()
    p = Path(os.path.expanduser(raw)) if raw else default
    if not p.is_absolute():
        raise PathError("Choose a full folder path for the files.")
    return p


def unique_path(folder: Path, name: str) -> Path:
    """folder/name, or folder/'name (2).ext' when that exists."""
    name = Path(name).name or "file"
    p = folder / name
    if not p.exists():
        return p
    stem, ext = os.path.splitext(name)
    for i in itertools.count(2):
        q = folder / f"{stem} ({i}){ext}"
        if not q.exists():
            return q
    raise AssertionError


def part_path(final: Path) -> Path:
    return final.with_name(f".{final.name}.part")


def local_tree(paths: list[str]) -> list[tuple[Path, str]]:
    """Local files and folders -> [(file, relative path under its top item)], folders recursed."""
    out: list[tuple[Path, str]] = []
    for raw in paths:
        p = Path(os.path.expanduser(str(raw).removeprefix("file://")))
        if p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.is_file() and not any(part.startswith(".") for part in f.relative_to(p).parts):
                    out.append((f, posixpath.join(p.name, *f.relative_to(p).parts)))
        elif p.is_file():
            out.append((p, p.name))
        else:
            raise PathError(f"{p} isn't there anymore.")
    return out


# ---------------------------------------------------------------- converters

def _run(argv: list[str], timeout: float = 60) -> bool:
    if not shutil.which(argv[0]):
        return False
    try:
        r = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return False
    return r.returncode == 0


def _ok_file(p: Path) -> bool:
    return p.is_file() and p.stat().st_size > 0


def converters() -> dict:
    return {name: bool(shutil.which(name)) for name in ("heif-convert", "vips", "vipsthumbnail", "magick", "ffmpeg")}


def to_jpeg(src: Path, dst: Path, quality: int = 92) -> bool:
    """Full-size JPEG of an image (HEIC included). Tries heif-convert, vips, ImageMagick, ffmpeg."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tries = [["heif-convert", "-q", str(quality), str(src), str(dst)],
             ["vips", "copy", str(src), f"{dst}[Q={quality}]"],
             ["magick", f"{src}[0]", "-quality", str(quality), str(dst)],
             ["ffmpeg", "-v", "error", "-y", "-i", str(src), "-frames:v", "1", "-q:v", "2", str(dst)]]
    for argv in tries:
        with contextlib.suppress(OSError):
            dst.unlink()
        if _run(argv, 120) and _ok_file(dst):
            # heif-convert writes '<name>-1.jpg' for multi-image files; normalise
            return True
        alt = dst.with_name(dst.stem + "-1" + dst.suffix)
        if _ok_file(alt):
            os.replace(alt, dst)
            return True
    return False


def thumbnail(src: Path, dst: Path, video: bool, size: int = 320) -> bool:
    """A small JPEG preview (Qt has no HEIF plugin, so HEIC never goes to the UI as-is)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if video:
        tries = [["ffmpeg", "-v", "error", "-y", "-ss", "0.5", "-i", str(src), "-frames:v", "1",
                  "-vf", f"scale={size}:-2", "-q:v", "4", str(dst)],
                 ["ffmpeg", "-v", "error", "-y", "-i", str(src), "-frames:v", "1", "-vf", f"scale={size}:-2",
                  "-q:v", "4", str(dst)]]
    else:
        tries = [["vipsthumbnail", str(src), "-s", f"{size}x{size}", "-o", f"{dst}[Q=80]"],
                 ["magick", f"{src}[0]", "-auto-orient", "-thumbnail", f"{size}x{size}", "-quality", "80", str(dst)],
                 ["ffmpeg", "-v", "error", "-y", "-i", str(src), "-frames:v", "1", "-vf", f"scale={size}:-2",
                  "-q:v", "4", str(dst)]]
    for argv in tries:
        if _run(argv, 60) and _ok_file(dst):
            return True
    if not video:   # last resort: a full conversion, then shrink if possible
        tmp = dst.with_name(dst.stem + ".full.jpg")
        if to_jpeg(src, tmp):
            ok = _run(["vipsthumbnail", str(tmp), "-s", f"{size}x{size}", "-o", f"{dst}[Q=80]"]) and _ok_file(dst)
            if not ok:
                os.replace(tmp, dst)
            with contextlib.suppress(OSError):
                tmp.unlink()
            return _ok_file(dst)
    return False


# ---------------------------------------------------------------- Photos.sqlite

REQUIRED = {"Z_PK", "ZDIRECTORY", "ZFILENAME", "ZDATECREATED", "ZKIND"}


def read_photos_db(db: Path) -> list[dict] | None:
    """Camera roll assets from a copy of PhotoData/Photos.sqlite, newest first (`skip` marks trashed and
    hidden ones), or None when the schema lacks the columns we need (it changes between iOS releases)."""
    try:
        con = sqlite3.connect(str(db), timeout=5)
    except sqlite3.Error:
        return None
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(ZASSET)")}
        if not REQUIRED <= cols:
            return None
        opt = [c for c in ("ZWIDTH", "ZHEIGHT", "ZDURATION", "ZTRASHEDSTATE", "ZHIDDEN") if c in cols]
        sel = ["a.Z_PK", "a.ZDIRECTORY", "a.ZFILENAME", "a.ZDATECREATED", "a.ZKIND"] + [f"a.{c}" for c in opt]
        join = ""
        extra = {r[1] for r in con.execute("PRAGMA table_info(ZADDITIONALASSETATTRIBUTES)")}
        if {"ZASSET", "ZORIGINALFILESIZE"} <= extra:
            sel.append("x.ZORIGINALFILESIZE")
            join = " LEFT JOIN ZADDITIONALASSETATTRIBUTES x ON x.ZASSET = a.Z_PK"
        q = f"SELECT {', '.join(sel)} FROM ZASSET a{join} WHERE a.ZDIRECTORY LIKE 'DCIM/%' ORDER BY a.ZDATECREATED DESC"
        out = []
        for row in con.execute(q):
            r = dict(zip([s.split(".", 1)[1] for s in sel], row))
            name, folder = r.get("ZFILENAME"), r.get("ZDIRECTORY")
            if not name or not folder:
                continue
            ts = float(r["ZDATECREATED"]) + COCOA_EPOCH if r.get("ZDATECREATED") is not None else None
            kind = "video" if r.get("ZKIND") == 1 else "photo"
            out.append({"path": f"{folder}/{name}", "kind": kind, "ts": ts,
                        "width": r.get("ZWIDTH") or None, "height": r.get("ZHEIGHT") or None,
                        "duration": round(float(r["ZDURATION"]), 2) if kind == "video" and r.get("ZDURATION") else None,
                        "size": int(r["ZORIGINALFILESIZE"]) if r.get("ZORIGINALFILESIZE") else None,
                        # Recently Deleted and Hidden stay in DCIM until purged: known, but not listed
                        "skip": bool(r.get("ZTRASHEDSTATE")) or bool(r.get("ZHIDDEN"))})
        return out
    except sqlite3.Error:
        return None
    finally:
        con.close()


def merge_index(db_items: list[dict] | None, dcim: list[str]) -> list[dict]:
    """Assets the DB knows that are really in DCIM (iCloud-only ones aren't), newest first, then DCIM
    files the DB doesn't know (or every DCIM file when there's no usable DB). Live Photo companions
    (the .MOV next to a known HEIC) are left out."""
    present = set(dcim)
    items: list[dict] = []
    known: set[str] = set()
    stems: set[str] = set()
    for it in db_items or []:
        if it["path"] in present:
            known.add(it["path"])
            stems.add(posixpath.splitext(it["path"])[0])
            if not it.get("skip"):
                items.append({k: v for k, v in it.items() if k != "skip"})   # rows stay cached: copy
    rest = [p for p in dcim if p not in known and not (db_items and posixpath.splitext(p)[0] in stems)]
    for p in sorted(rest, reverse=True):
        items.append({"path": p, "kind": media_kind(p) or "photo", "ts": None, "width": None, "height": None,
                      "duration": None, "size": None})
    for it in items:
        it["id"] = it["path"]
        it["name"] = posixpath.basename(it["path"])
        it["date"] = iso(it["ts"])
    return items


# ---------------------------------------------------------------- done.json

def read_done(path: Path) -> tuple[Any, str | None]:
    """(parsed done.json, raw text when it isn't valid JSON)."""
    try:
        text = path.read_text("utf-8", "replace")
    except OSError:
        return None, None
    try:
        return json.loads(text), None
    except ValueError:
        return None, text[:20000]


def split_args(text: str | list | None) -> list[str]:
    if isinstance(text, list):
        return [str(x) for x in text]
    import shlex
    try:
        return shlex.split(str(text or ""))
    except ValueError:
        return str(text or "").split()


def parse_env(env: Any) -> dict[str, str]:
    if isinstance(env, dict):
        return {str(k): str(v) for k, v in env.items() if str(k)}
    out = {}
    for part in re.split(r"[\n,]+", str(env or "")):
        k, sep, v = part.strip().partition("=")
        if sep and k.strip():
            out[k.strip()] = v.strip()
    return out


# ---------------------------------------------------------------- transfers

class Progress:
    """Throttled `transfer` events for one transfer: {id, kind, state, bytes, total, files, count,
    file, error?}."""

    def __init__(self, emit: Callable[[str, Any], None], tid: str, kind: str, total: int | None, count: int,
                 extra: dict | None = None):
        self.emit = emit
        self.tid, self.kind = tid, kind
        self.total, self.count = total, count
        self.bytes = 0
        self.files = 0
        self.file = ""
        self.extra = extra or {}
        self.started = time.monotonic()
        self._last = 0.0

    def data(self, state: str) -> dict:
        d = {"id": self.tid, "kind": self.kind, "state": state, "bytes": self.bytes, "total": self.total,
             "files": self.files, "count": self.count, "file": self.file,
             "seconds": round(time.monotonic() - self.started, 1)}
        d.update(self.extra)
        return d

    def send(self, state: str = "running", force: bool = False, **more) -> None:
        now = time.monotonic()
        if not force and state == "running" and now - self._last < PROGRESS_EVERY:
            return
        self._last = now
        d = self.data(state)
        d.update(more)
        self.emit("transfer", d)

    def start_file(self, name: str) -> None:
        self.file = name
        self.send(force=True)

    def add(self, n: int) -> None:
        self.bytes += n
        self.send()

    def file_done(self) -> None:
        self.files += 1
        self.send()


class Transfers:
    """Running transfers by id: the request task that does it and the client that asked."""

    _ids = itertools.count(1)

    def __init__(self):
        self.running: dict[str, tuple[asyncio.Task, Any, str]] = {}

    def new_id(self, prefix: str = "t") -> str:
        return f"{prefix}{int(time.time()) % 100000}-{next(self._ids)}"

    @contextlib.contextmanager
    def track(self, tid: str, client, kind: str):
        task = asyncio.current_task()
        if tid in self.running:
            raise ValueError(f"transfer {tid} is already running")
        self.running[tid] = (task, client, kind)
        try:
            yield
        finally:
            self.running.pop(tid, None)

    def cancel(self, tid: str) -> bool:
        hit = self.running.get(tid)
        if not hit:
            return False
        hit[0].cancel()
        return True

    def cancel_client(self, client) -> int:
        n = 0
        for tid, (task, owner, _) in list(self.running.items()):
            if owner is not None and owner is client:
                task.cancel()
                n += 1
        return n

    def describe(self) -> list[dict]:
        return [{"id": tid, "kind": kind} for tid, (_, _, kind) in self.running.items()]


# ---------------------------------------------------------------- syslog capture (camera test)

class LogCapture:
    """A pseudo broker client that subscribes to the daemon's own syslog hub (broker.SyslogHub)
    so a camera test can record its app's log through the one phone stream."""

    name = "cameratest"
    closed = False
    events = None

    def __init__(self, keep: int = 5000):
        from collections import deque
        self.lines: deque = deque(maxlen=keep)
        self.count = 0

    def send(self, obj: dict) -> None:
        if obj.get("event") == "syslog" and isinstance(obj.get("data"), list):
            for e in obj["data"]:
                self.count += 1
                self.lines.append(e)

    def wants(self, name: str) -> bool:
        return name == "syslog"

    def close(self) -> None:
        pass

    def describe(self) -> dict:
        return {"name": self.name}

    @staticmethod
    def fmt(e: dict) -> str:
        ts = time.strftime("%H:%M:%S", time.localtime(float(e.get("ts") or time.time())))
        return f"{ts} {e.get('level', 'default'):<7} {e.get('process', '')}: {e.get('message', '')}"
