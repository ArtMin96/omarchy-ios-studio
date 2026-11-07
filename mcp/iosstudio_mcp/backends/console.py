"""App console (an app's stdout/stderr, Swift print()) as kept by the device daemon (helper/app_console.py).

    await daemon_read(state, {"sessionId": …}, cursor)   one console.read
    await capture_summary(state, launch_result, udid, max_lines, room)   after a run-and-capture
    fmt_line / select_lines                              rendering ('<n> <time> [stderr] <text>')
"""
from __future__ import annotations

import asyncio
import time

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from ..core import budget as B
from ..core import deadline
from ..models import Out
from ..state import AppState
from .device import dcall

FETCH = 5000          # entries per daemon read (its maximum)
TAG = {"stderr": "[stderr] ", "meta": "[studio] "}
NOT_FOUND = {"not-found": "Launch the app with the console on: ios_app_launch(bundle_id, console=true) or "
                          "ios_app_run_and_capture(bundle_id)."}


class ConsoleSummary(Out):
    session_id: str | None = None
    captured: int = Field(0, description="Lines the app printed during the capture")
    stderr: int = 0
    dropped: int = 0
    streams: str | None = None
    lines_omitted: int = 0
    error: str | None = Field(None, description="Why there's no console output")


def fmt_line(seq: int | None, e: dict, cut: int = B.LINE_CUT) -> str:
    try:
        ts = float(e.get("ts"))
        t = time.strftime("%H:%M:%S", time.localtime(ts)) + f".{int((ts % 1) * 1000):03d}"
    except (TypeError, ValueError):
        t = "--:--:--"
    text = str(e.get("text") or "")
    if len(text) > cut:
        text = text[:cut] + f" …[+{len(text) - cut}]"
    if e.get("cut"):
        text += f" …[line cut, +{e['cut']} bytes]"
    head = f"{seq} " if seq is not None else ""
    return f"{head}{t} {TAG.get(str(e.get('stream')), '')}{text}"


def select_lines(entries: list[dict], max_lines: int, room: int, cut: int = B.LINE_CUT) -> tuple[list[str], int]:
    """For a capture summary: every line if it fits, else the stderr lines (up to a third) plus the latest
    ones, in time order. Returns (rendered lines, entries left out)."""
    rows = B.fold_repeats(list(enumerate(entries)), key=lambda ie: (ie[1].get("stream"), ie[1].get("text")))
    keep: set[int] = set()
    errs = [i for i, ((_, e), _) in enumerate(rows) if e.get("stream") == "stderr"]
    keep.update(errs[:max(1, max_lines // 3)])
    for i in range(len(rows) - 1, -1, -1):
        if len(keep) >= max_lines:
            break
        keep.add(i)
    chosen = sorted(keep)
    rendered = [fmt_line(None, rows[i][0][1], cut) + (f" ×{rows[i][1]}" if rows[i][1] > 1 else "") for i in chosen]
    pg = B.take(rendered, len(rendered), room, from_end=True)
    shown = sum(rows[i][1] for i in chosen[pg.start:pg.start + len(pg.items)])
    return pg.items, len(entries) - shown


def cursors(state: AppState) -> dict[str, int]:
    return state.extras.setdefault("console.cursors", {})


async def daemon_read(state: AppState, target: dict, cursor: int | None, limit: int = FETCH, wait: float = 0,
                      udid: str | None = None) -> dict:
    a = dict(target)
    wait = max(0.0, deadline.wait_time(wait + 2) - 2)   # 2 s for the reply, within the call's budget
    a.update(limit=limit, wait=wait)
    if cursor is not None:
        a["cursor"] = cursor
    return await dcall(state, "console.read", a, udid=udid, timeout=wait + 20,
                       hints=NOT_FOUND) or {}


async def read_all(state: AppState, session_id: str, udid: str | None = None, newest: int = FETCH) -> dict:
    """The newest `newest` lines of a session (for a capture summary)."""
    head = await daemon_read(state, {"sessionId": session_id}, 10 ** 12, limit=1, udid=udid)
    total = int((head.get("session") or {}).get("lines") or 0)
    return await daemon_read(state, {"sessionId": session_id}, max(0, total - newest), limit=newest, udid=udid)


async def capture_summary(state: AppState, launch_result: dict, udid: str | None, max_lines: int,
                          room: int) -> tuple[ConsoleSummary, list[str], list[dict]]:
    """After a capture: (summary, rendered lines within `room`, every line read) for run-and-capture."""
    con = (launch_result or {}).get("console") or {}
    if not con.get("sessionId"):
        err = con.get("error") or {}
        msg = err.get("message") if isinstance(err, dict) else str(err)
        return ConsoleSummary(error=B.clip(msg or "The console wasn't captured.", B.ERROR_CUT)), [], []
    sid = con["sessionId"]
    await asyncio.sleep(0.3)   # the daemon batches lines every 0.1 s; a partial line waits ~1 s
    try:
        pg = await read_all(state, sid, udid)
    except ToolError as exc:
        return ConsoleSummary(session_id=sid, error=B.clip(str(exc), B.ERROR_CUT)), [], []
    entries = [e for e in pg.get("entries") or [] if isinstance(e, dict)]
    sess = pg.get("session") or {}
    lines, left_out = select_lines(entries, max_lines, room) if entries else ([], 0)
    captured = int(sess.get("lines") or len(entries))
    cursors(state)[sid] = int(pg.get("nextCursor") or 0)
    return (ConsoleSummary(session_id=sid, captured=captured,
                           stderr=sum(1 for e in entries if e.get("stream") == "stderr"),
                           dropped=int(sess.get("dropped") or 0), streams=sess.get("streams"),
                           lines_omitted=max(0, captured - len(entries) + left_out)), lines, entries)
