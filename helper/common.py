"""Shared helpers for iOS Studio's Python helper (stdlib only).

Protocol (see docs/development.md):
  one-shot   -> exactly one JSON object on stdout via ok()/fail()
  streaming  -> JSON lines via event(); finish with done()
  prompts    -> prompt() writes a prompt event and reads one JSON line answer from stdin
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable

HOME = Path.home()
REPO = Path(__file__).resolve().parent.parent
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config")) / "ios-studio"
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", HOME / ".local/state")) / "ios-studio"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", HOME / ".cache")) / "ios-studio"
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", HOME / ".local/share")) / "ios-studio"
VENV_DIR = DATA_DIR / "venv"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
LOCAL_BIN = HOME / ".local/bin"
XTOOL = LOCAL_BIN / "xtool"


class HelperError(Exception):
    """Raise for a handled, user-facing error."""

    def __init__(self, code: str, message: str, fix: dict | None = None, detail: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.fix = fix
        self.detail = detail

    def to_json(self) -> dict:
        err: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.fix:
            err["fix"] = self.fix
        if self.detail:
            err["detail"] = self.detail[-4000:]
        return err


def fix(label: str, page: str, step: str | None = None, action: str | None = None) -> dict:
    out = {"label": label, "page": page}
    if step:
        out["step"] = step
    if action:
        out["action"] = action
    return out


# ---------------------------------------------------------------- output

def _write(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, separators=(",", ":"), default=str) + "\n")
    sys.stdout.flush()


def ok(data: Any = None) -> int:
    _write({"ok": True, "data": data if data is not None else {}})
    return 0


def fail(code: str, message: str, fix: dict | None = None, detail: str | None = None) -> int:
    _write({"ok": False, "error": HelperError(code, message, fix, detail).to_json()})
    return 1


def event(name: str, **fields: Any) -> None:
    _write({"event": name, **fields})


def step(step_id: str, title: str, state: str, **extra: Any) -> None:
    event("step", id=step_id, title=title, state=state, **extra)


def progress(fraction: float | None = None, detail: str = "", eta: float | None = None) -> None:
    event("progress", fraction=fraction, detail=detail, eta=eta)


def log(text: str) -> None:
    event("log", text=text.rstrip("\n")[:4000])


def done(ok_: bool = True, data: Any = None, error: HelperError | None = None) -> int:
    if ok_:
        event("done", ok=True, data=data if data is not None else {})
        return 0
    event("done", ok=False, error=(error or HelperError("failed", "Something went wrong.")).to_json())
    return 1


_prompt_counter = 0


def prompt(kind: str, message: str, options: list[dict] | None = None, **extra: Any) -> str:
    """Ask the UI a question (streaming mode). Blocks until one JSON line answer arrives on stdin."""
    global _prompt_counter
    _prompt_counter += 1
    pid = f"p{_prompt_counter}"
    event("prompt", id=pid, kind=kind, message=message, options=options or [], **extra)
    while True:
        line = sys.stdin.readline()
        if not line:
            raise HelperError("cancelled", "The request was cancelled.")
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("prompt") == pid:
            if msg.get("cancel"):
                raise HelperError("cancelled", "The request was cancelled.")
            return str(msg.get("value", ""))


def read_stdin_json() -> dict:
    """For one-shot commands that receive secrets/payload as one JSON object on stdin."""
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HelperError("bad-input", "The app sent malformed input.", detail=str(exc))


# ---------------------------------------------------------------- processes

def which(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    cand = LOCAL_BIN / name
    return str(cand) if cand.exists() and os.access(cand, os.X_OK) else None


def run_cmd(argv: list[str], timeout: float = 60, input_text: str | None = None,
            env: dict | None = None, cwd: str | None = None) -> subprocess.CompletedProcess:
    """Run and capture. Never raises on non-zero; raises HelperError on missing binary/timeout."""
    full_env = os.environ.copy()
    full_env["PATH"] = f"{LOCAL_BIN}:{full_env.get('PATH', '/usr/bin')}"
    full_env.setdefault("NO_COLOR", "1")
    if env:
        full_env.update(env)
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                              input=input_text, env=full_env, cwd=cwd)
    except FileNotFoundError:
        raise HelperError("missing-tool", f"{Path(argv[0]).name} isn't installed yet.",
                          fix("Open Setup", "setup"))
    except subprocess.TimeoutExpired:
        raise HelperError("timeout", f"{Path(argv[0]).name} took too long to respond.")


def stream_cmd(argv: list[str], env: dict | None = None, cwd: str | None = None,
               on_line=None) -> int:
    """Run, forwarding each output line as a log event (or to on_line). Returns exit code."""
    full_env = os.environ.copy()
    full_env["PATH"] = f"{LOCAL_BIN}:{full_env.get('PATH', '/usr/bin')}"
    full_env.setdefault("NO_COLOR", "1")
    if env:
        full_env.update(env)
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                bufsize=1, env=full_env, cwd=cwd, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        raise HelperError("missing-tool", f"{Path(argv[0]).name} isn't installed yet.",
                          fix("Open Setup", "setup"))
    assert proc.stdout is not None
    for line in proc.stdout:
        if on_line:
            on_line(line.rstrip("\n"))
        else:
            log(line)
    return proc.wait()


def pymd3_python() -> str | None:
    """Python interpreter that has pymobiledevice3, in preference order: ours, then
    ~/pymobile3-venv (the venv path omarchy-apple-dev's scripts create and expect)."""
    for cand in (VENV_DIR / "bin/python", HOME / "pymobile3-venv/bin/python"):
        if cand.exists():
            return str(cand)
    return None


# ---------------------------------------------------------------- files

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


def settings() -> dict:
    return load_json(SETTINGS_FILE, {}) or {}


# Where people usually keep code. The defaults for "projectRoots" are whichever of these exist;
# new apps go to ~/Developer (Apple's convention), created on first use.
PROJECT_ROOT_CANDIDATES = ("Developer", "Projects", "projects", "Code", "code", "src", "dev", "Work")
NEW_APP_DIR = HOME / "Developer"


def default_project_roots() -> list[Path]:
    out, seen = [], set()
    for name in PROJECT_ROOT_CANDIDATES:
        p = HOME / name
        try:
            key = p.resolve()
        except OSError:
            continue
        if p.is_dir() and key not in seen:   # ~/Code and ~/code are one folder on case-insensitive filesystems
            seen.add(key)
            out.append(p)
    return out


def project_roots() -> list[Path]:
    """Configured project roots (Settings), else the detected defaults. Only existing folders."""
    roots = settings().get("projectRoots")
    if not roots:
        return default_project_roots()
    return [p for p in (Path(os.path.expanduser(str(r))) for r in roots) if p.is_dir()]


def new_app_dir() -> Path:
    """Where "New app" creates projects unless the user picks another folder."""
    roots = settings().get("projectRoots")
    if roots:
        return Path(os.path.expanduser(str(roots[0])))
    return NEW_APP_DIR


def cache_get(name: str, max_age: float | None = None) -> Any:
    """Return cached payload {"at": ts, "data": ...} or None."""
    entry = load_json(CACHE_DIR / f"{name}.json")
    if not isinstance(entry, dict):
        return None
    if max_age is not None and time.time() - float(entry.get("at", 0)) > max_age:
        return None
    return entry


def cache_put(name: str, data: Any) -> dict:
    entry = {"at": time.time(), "data": data}
    save_json(CACHE_DIR / f"{name}.json", entry)
    return entry


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


def parse_flags(args: Iterable[str]) -> dict[str, Any]:
    """--key value / --flag  ->  {"key": "value", "flag": True}; positional -> "_": [...]"""
    out: dict[str, Any] = {"_": []}
    args = list(args)
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("--"):
            key = a[2:].replace("-", "_")
            if i + 1 < len(args) and not args[i + 1].startswith("--"):
                out[key] = args[i + 1]
                i += 2
                continue
            out[key] = True
        else:
            out["_"].append(a)
        i += 1
    return out
