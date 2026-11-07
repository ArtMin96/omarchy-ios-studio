"""The device daemon's wire format (docs/deviced-protocol.md), in one place. Stdlib only.

One JSON object per line, UTF-8, in both directions:

    request   {"id": 7, "cmd": "apps.list", "args": {...}}
    reply     {"id": 7, "ok": true, "data": ...}  |  {"id": 7, "ok": false, "error": {code, message, fix?, detail?}}
    event     {"event": "devices", "data": ...}

Used by the daemons (deviced.py, deviced_demo.py), the broker (device_broker.py) and the client
(device_client.py). device_client.py is also loaded by file path outside the iosstudio package (the
MCP server does that), so this module must stay importable on its own as well: no imports from the
repo except the optional HelperError base below.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator, Callable, Iterable, Iterator

try:   # inside the iosstudio package: a DeviceError is a HelperError (same fields, same to_json)
    from iosstudio.core.output import HelperError as _ErrorBase
except ImportError:   # loaded standalone (the MCP server loads device_client.py by path)
    _ErrorBase = Exception

MAX_LINE = 16 * 1024 * 1024   # longest line either side reads (asyncio StreamReader limit)
DETAIL_MAX = 4000             # error details are cut to their last DETAIL_MAX characters


# ====================================================================== errors

class DeviceError(_ErrorBase):
    """The one error of the device protocol: .code .message .fix (dict | None) .detail (str | None).
    Inside the iosstudio package it subclasses core.output.HelperError, so `except HelperError`
    catches it and both serialize the same way."""

    def __init__(self, code: str, message: str, fix: dict | None = None, detail: str | None = None):
        Exception.__init__(self, message)
        self.code = code
        self.message = message
        self.fix = fix
        self.detail = detail

    @classmethod
    def from_json(cls, e: Any) -> "DeviceError":
        if not isinstance(e, dict):
            return cls("failed", str(e))
        return cls(str(e.get("code") or "failed"), str(e.get("message") or "Something went wrong."),
                   e.get("fix"), e.get("detail"))

    def to_json(self) -> dict:
        out: dict = {"code": self.code, "message": self.message}
        if self.fix:
            out["fix"] = self.fix
        if self.detail:
            out["detail"] = str(self.detail)[-DETAIL_MAX:]
        return out

    def __repr__(self) -> str:
        return f"DeviceError({self.code!r}, {self.message!r})"


def error_json(exc: BaseException, fallback: str = "Something went wrong.") -> dict:
    """Any exception -> a protocol error object. Handled errors (DeviceError, HelperError: anything
    with a code and to_json()) keep theirs; anything else is `failed` with the exception as detail."""
    if getattr(exc, "code", None) and callable(getattr(exc, "to_json", None)):
        return exc.to_json()
    if isinstance(exc, asyncio.CancelledError):
        return CANCELLED
    return DeviceError("failed", fallback, None, f"{type(exc).__name__}: {exc}").to_json()


CANCELLED = {"code": "cancelled", "message": "Cancelled."}


def parse_args(raw: str | None) -> dict:
    """`--once <cmd> '<json args>'`: the args object, or DeviceError("bad-args")."""
    try:
        args = json.loads(raw or "{}")
    except ValueError as exc:
        raise DeviceError("bad-args", "The arguments aren't valid JSON.", None, str(exc)) from None
    if not isinstance(args, dict):
        raise DeviceError("bad-args", "The arguments aren't valid JSON.", None, "args must be a JSON object")
    return args


# ====================================================================== messages

def request(rid: Any, cmd: str, args: dict | None = None) -> dict:
    return {"id": rid, "cmd": cmd, "args": args or {}}


def event(name: str, data: Any) -> dict:
    return {"event": name, "data": data}


def reply_ok(rid: Any, data: Any) -> dict:
    return {"id": rid, "ok": True, "data": data if data is not None else {}}


def reply_error(rid: Any, error: dict | BaseException) -> dict:
    return {"id": rid, "ok": False, "error": error if isinstance(error, dict) else error_json(error)}


def is_event(msg: dict) -> bool:
    return "event" in msg


# ====================================================================== framing

def encode(obj: dict) -> bytes:
    """One message as one line (compact JSON + newline)."""
    return (json.dumps(obj, separators=(",", ":"), default=str) + "\n").encode("utf-8")


def decode(raw: bytes | str) -> dict | None:
    """One line -> its message, or None for a blank, malformed or non-object line."""
    try:
        msg = json.loads(raw)
    except ValueError:
        return None
    return msg if isinstance(msg, dict) else None


def write_line(f, obj: dict) -> None:
    """Write one message to a binary file object (or a text one) and flush it."""
    line = encode(obj)
    try:
        f.write(line)
    except TypeError:   # a text stream (sys.stdout)
        f.write(line.decode("utf-8"))
    f.flush()


def read_lines(f: Iterable[bytes | str], on_malformed: Callable[[], None] | None = None) -> Iterator[dict]:
    """The messages of a line-iterable (a socket makefile, a pipe) until EOF; bad lines are skipped."""
    for raw in f:
        msg = decode(raw)
        if msg is None:
            if on_malformed is not None and raw.strip():
                on_malformed()
            continue
        yield msg


class LineTooLong(Exception):
    """A line longer than the reader's limit (MAX_LINE)."""


async def write_line_async(w: asyncio.StreamWriter, obj: dict, drain: bool = True) -> None:
    w.write(encode(obj))
    if drain:
        await w.drain()


async def read_lines_async(reader: asyncio.StreamReader,
                           on_malformed: Callable[[], None] | None = None) -> AsyncIterator[dict]:
    """The messages from an asyncio stream until EOF. Bad lines are skipped (on_malformed() is told);
    a line over the reader's limit raises LineTooLong."""
    while True:
        try:
            raw = await reader.readline()
        except (ValueError, asyncio.LimitOverrunError) as exc:
            raise LineTooLong(str(exc)) from None
        if not raw:
            return
        msg = decode(raw)
        if msg is None:
            if on_malformed is not None and raw.strip():
                on_malformed()
            continue
        yield msg
