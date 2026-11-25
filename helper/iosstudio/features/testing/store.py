"""Where test runs leave their traces: one log per run in $XDG_STATE_HOME/ios-studio/test-runs/ (pruned: the
newest LOG_KEEP, at most LOG_MAX_BYTES) and the last run of each project in last.json there (LAST_MAX projects)."""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import IO

from iosstudio.core import appdirs, fileio

LOG_KEEP, LOG_MAX_BYTES = 20, 50 * 1024 * 1024
LOG_FILE_MAX = 20 * 1024 * 1024      # one run's log stops growing here (a test printing in a loop)
LAST_MAX = 100


def runs_dir() -> Path:
    return appdirs.state_dir() / "test-runs"


def lock_file(root: Path) -> Path:
    """The per-project test-run lock (not the build lock: tests have their own scratch path)."""
    return runs_dir() / "locks" / f"{fileio.path_key(root)}.lock"


def _last_file() -> Path:
    return runs_dir() / "last.json"


def last_runs() -> dict:
    data = fileio.load_json(_last_file(), {})
    return data if isinstance(data, dict) else {}


def last_run(root: Path) -> dict | None:
    v = last_runs().get(str(root))
    return v if isinstance(v, dict) else None


def remember(root: Path, entry: dict) -> None:
    """Record a project's last run (newest last; the oldest projects beyond LAST_MAX are dropped)."""
    f = _last_file()
    try:
        with fileio.lock(f.with_name(".last.json.lock")):
            data = last_runs()
            data.pop(str(root), None)
            data[str(root)] = entry
            fileio.save_json(f, dict(list(data.items())[-LAST_MAX:]))
    except OSError:
        pass


def last_entry(result: dict, ok: bool) -> dict:
    return {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "ok": ok, "counts": result["counts"],
            "duration": result["duration"], "filter": result["filter"]}


class RunLog:
    """One run's log file: every output line, capped at LOG_FILE_MAX bytes. Best effort (a full disk never stops
    a run)."""

    def __init__(self, package: str):
        d = runs_dir()
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", package)[:60] or "package"
        stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"
        self.path: Path | None = d / f"{stamp}-{safe}.log"
        self.size = 0
        self.cut = False
        self.fh: IO[str] | None = None
        try:
            d.mkdir(parents=True, exist_ok=True)
            self.fh = open(self.path, "w", encoding="utf-8", errors="replace")
        except OSError:
            self.path = None

    def write(self, line: str) -> None:
        if self.fh is None or self.cut:
            return
        self.size += len(line) + 1
        try:
            if self.size > LOG_FILE_MAX:
                self.cut = True
                self.fh.write("… (log cut: it reached its size limit)\n")
                return
            self.fh.write(line + "\n")
        except OSError:
            self.cut = True

    def close(self) -> str | None:
        if self.fh is not None:
            try:
                self.fh.close()
            except OSError:
                pass
            self.fh = None
        if self.path is not None:
            fileio.prune(runs_dir().glob("*.log"), LOG_KEEP, LOG_MAX_BYTES, protect=[self.path])
        return str(self.path) if self.path is not None else None
