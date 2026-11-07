"""Output protocol of iOS Studio's helper (stdlib only): one JSON object or JSON lines on stdout.

  one-shot   -> exactly one JSON object on stdout via ok()/fail()
  streaming  -> JSON lines via event(); finish with done()
  prompts    -> prompt() writes a prompt event and reads one JSON line answer from stdin
HelperError is the handled, user-facing error every command raises; fix() builds its "fix" link.
"""
from __future__ import annotations

import json
import sys
import threading
from typing import Any


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

EXIT_USAGE = 2   # bad command line; callers read the JSON, the exit code is for shells and journals

_write_lock = threading.Lock()


def _write(obj: dict) -> None:
    """One JSON line on stdout. Locked so lines from worker threads (heartbeats, readers) never interleave."""
    line = json.dumps(obj, separators=(",", ":"), default=str) + "\n"
    with _write_lock:
        sys.stdout.write(line)
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
    """Final stream event. A failed run may still carry data (e.g. what a batch did before failing)."""
    if ok_:
        event("done", ok=True, data=data if data is not None else {})
        return 0
    extra = {"data": data} if data is not None else {}
    event("done", ok=False, error=(error or HelperError("failed", "Something went wrong.")).to_json(), **extra)
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
