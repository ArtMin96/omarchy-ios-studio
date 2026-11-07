"""Files for the helper (stdlib only): atomic writes, JSON load/save, tails of big logs, pruning old outputs."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Iterable


def atomic_write(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return default


def save_json(path: Path, data: Any) -> None:
    atomic_write(path, json.dumps(data, indent=2, default=str) + "\n")


def tail_text(path: Path, max_bytes: int) -> str:
    """The last `max_bytes` of a (possibly huge) text file without reading the rest; '' if it's missing."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            return fh.read(max_bytes).decode("utf-8", "replace")
    except OSError:
        return ""


def dir_size(path: Path) -> int:
    total = 0
    try:
        for root, _dirs, files in os.walk(path, followlinks=False):
            for f in files:
                try:
                    total += os.lstat(os.path.join(root, f)).st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def prune(paths: Iterable[Path], keep: int, max_bytes: int | None = None, min_keep: int = 3,
          protect: Iterable[Path] = (), also: Iterable[str] = ()) -> int:
    """Delete the oldest of `paths` (files or folders, by mtime) beyond the newest `keep`, and beyond
    `max_bytes` in total. The newest `min_keep` always stay (a path an MCP tool just returned is still
    there when the agent reads it), and so does anything in `protect` (in use). `also`: suffixes of
    companion files deleted with a file (".jpg" for a recording's thumbnail). Best effort; returns the
    number of entries deleted."""
    entries = []
    for path in paths:
        try:
            entries.append((path.lstat().st_mtime, path))
        except OSError:
            pass
    entries.sort(key=lambda e: e[0], reverse=True)
    keep_set = {os.path.abspath(p) for p in protect}
    total, removed = 0, 0
    for i, (_, path) in enumerate(entries):
        is_dir = path.is_dir() and not path.is_symlink()
        size = dir_size(path) if is_dir else _size(path)
        total += size
        if i < max(1, min_keep) or os.path.abspath(path) in keep_set:
            continue
        if i < keep and (max_bytes is None or total <= max_bytes):
            continue
        try:
            if is_dir:
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
                for suffix in also:
                    path.with_suffix(suffix).unlink(missing_ok=True)
            total -= size
            removed += 1
        except OSError:
            pass
    return removed


def _size(path: Path) -> int:
    try:
        return path.lstat().st_size
    except OSError:
        return 0
