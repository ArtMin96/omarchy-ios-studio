"""App console: an app's stdout and stderr (Swift print()) for apps launched with the console on.

The device daemon keeps one ring buffer per launch (helper/app_console.py: 10,000 lines / 2 MB, lines
cut at 8 KB, at most 2,000 lines a second). An app launched by ios_app_launch(console=true),
ios_app_run_and_capture or ios_run has one; ios_app_console_read pages through it with a cursor.
Pure helpers (fmt_line, select_lines, page) are used by tests and by ios_app_run_and_capture.
"""
from __future__ import annotations

import asyncio
import time
from typing import Annotated, ClassVar, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from .. import budget as B
from ..models import Out
from ..state import AppState, get_state
from . import MAX_WAIT, READ_REMOTE, tool
from .device import Udid, dcall

FETCH = 5000          # entries per daemon read (its maximum)
TAG = {"stderr": "[stderr] ", "meta": "[studio] "}

Stream = Literal["all", "stdout", "stderr"]


# ------------------------------------------------------------------ models

class ConsoleRead(B.Budgeted, Out):
    BULK: ClassVar[tuple[str, ...]] = ("lines",)
    session_id: str
    bundle_id: str
    running: bool = Field(description="Still capturing (the app hasn't exited)")
    status: str = Field(description="running | ended (the app exited) | stopped | lost (connection broke)")
    reason: str | None = None
    pid: int | None = None
    streams: str = Field(description="merged: stdout and stderr arrive together (real iPhone); split: stderr "
                                     "lines are tagged [stderr]")
    count: int
    next_cursor: int = Field(description="Pass as cursor on the next read")
    has_more: bool = Field(description="More is buffered past next_cursor")
    buffer_start: int = Field(description="Oldest line number still buffered")
    missed: int = Field(0, description="Lines overwritten before you read them")
    dropped: int = Field(0, description="Lines the app printed faster than 2,000 a second (not kept)")
    skipped: int = Field(0, description="Older matching lines skipped by newest=true")
    total_lines: int
    lines: list[str] = Field(default_factory=list, description="'<n> <time> [stderr] <text>'; repeats fold into ×N")
    next_steps: list[str] = Field(default_factory=list)


class ConsoleSummary(Out):
    session_id: str | None = None
    captured: int = Field(0, description="Lines the app printed during the capture")
    stderr: int = 0
    dropped: int = 0
    streams: str | None = None
    lines_omitted: int = 0
    error: str | None = Field(None, description="Why there's no console output")


# ------------------------------------------------------------------ pure helpers

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


def stream_ok(e: dict, stream: str) -> bool:
    s = e.get("stream")
    if stream == "stderr":
        return s == "stderr"
    if stream == "stdout":
        return s in ("stdout", "out")
    return True


def page(entries: list[tuple[int, dict]], limit: int, room: int, newest: bool = False,
         cut: int = B.LINE_CUT) -> tuple[list[str], int, int]:
    """Matching (seq, entry) pairs -> (rendered lines within `room` bytes, entries shown, rows cut).
    Repeats fold into one line with ×N. newest=True keeps the end."""
    sel = entries[-limit:] if newest else entries[:limit]
    rows = B.fold_repeats(sel, key=lambda se: (se[1].get("stream"), se[1].get("text")))
    rendered = [fmt_line(seq, e, cut) + (f" ×{n}" if n > 1 else "") for (seq, e), n in rows]
    pg = B.take(rendered, len(rendered), room, from_end=newest)
    shown_rows = rows[pg.start:pg.start + len(pg.items)]
    return pg.items, sum(n for _, n in shown_rows), len(rendered) - len(pg.items)


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


# ------------------------------------------------------------------ daemon access

def _cursors(state: AppState) -> dict[str, int]:
    return state.extras.setdefault("console.cursors", {})


async def daemon_read(state: AppState, target: dict, cursor: int | None, limit: int = FETCH, wait: float = 0,
                      udid: str | None = None) -> dict:
    a = dict(target)
    a.update(limit=limit, wait=wait)
    if cursor is not None:
        a["cursor"] = cursor
    return await dcall(state, "console.read", a, udid=udid, timeout=wait + 20,
                       not_found="Launch the app with the console on: ios_app_launch(bundle_id, console=true) or "
                                 "ios_app_run_and_capture(bundle_id).") or {}


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
    _cursors(state)[sid] = int(pg.get("nextCursor") or 0)
    return (ConsoleSummary(session_id=sid, captured=captured,
                           stderr=sum(1 for e in entries if e.get("stream") == "stderr"),
                           dropped=int(sess.get("dropped") or 0), streams=sess.get("streams"),
                           lines_omitted=max(0, captured - len(entries) + left_out)), lines, entries)


# ------------------------------------------------------------------ tool

def register(mcp: MCPServer) -> None:

    @tool(mcp, annotations=READ_REMOTE("Read app console"))
    async def ios_app_console_read(
        ctx: Context,
        bundle_id: Annotated[str | None, Field(description="The app, e.g. 'com.example.myapp': its latest console "
                                                           "session")] = None,
        session_id: Annotated[str | None, Field(description="A console session id (console.session_id from "
                                                            "ios_app_run_and_capture / ios_app_launch)")] = None,
        cursor: Annotated[int | None, Field(ge=0, description="next_cursor from the previous read (0 = from the "
                                                              "start); omit to continue where the last read ended")] = None,
        limit: Annotated[int, Field(ge=1, le=2000, description="Max lines to return")] = 200,
        wait_seconds: Annotated[int, Field(ge=0, le=MAX_WAIT, description="Long-poll: if nothing new is "
                                                                         "buffered, wait up to this long")] = 0,
        grep: Annotated[str | None, Field(max_length=B.MAX_PATTERN, description="Case-insensitive regex; only "
                                                                              "matching lines")] = None,
        stream: Annotated[Stream, Field(description="stderr: only lines the app wrote to stderr (when the "
                                                    "session's streams are split)")] = "all",
        newest: Annotated[bool, Field(description="Skip to the newest `limit` matching lines")] = False,
        line_chars: Annotated[int, Field(ge=80, le=4000, description="Cut lines after this many characters")] = B.LINE_CUT,
        max_bytes: Annotated[int | None, Field(ge=1000, le=B.MAX_BYTES, description="Raise the size cap for text + "
                                                                                  "structuredContent together (default 12000 bytes)")] = None,
        udid: Udid = None,
    ) -> ConsoleRead:
        """Read what an app printed (Swift print(), stdout and stderr) after `cursor`, oldest first, as
        plain text after the JSON header: '<n> <time> [stderr] <text>'. Only apps launched with the console
        on have one: ios_app_run_and_capture and ios_run do it, ios_app_launch with console=true. Attaching to
        an app that was already running gives no output. Returns next_cursor, has_more, `missed` if the
        10,000-line buffer overwrote lines you hadn't read, and `dropped` if the app printed faster than
        2,000 lines a second. grep / stream / newest narrow this read. wait_seconds (≤25) waits for new
        lines. Results stay under 12 KB unless max_bytes raises it."""
        state = get_state(ctx)
        if not bundle_id and not session_id:
            raise ToolError("Pass bundle_id (its latest console session) or session_id.")
        target = {"sessionId": session_id} if session_id else {"bundleId": bundle_id.strip()}
        rx = B.compile_pattern(grep) if grep else None
        filtering = rx is not None or stream != "all"
        # resolve the session and the default cursor without reading lines
        head = await daemon_read(state, target, 10 ** 12, limit=1, udid=udid)
        sess = head.get("session") or {}
        sid = str(sess.get("sessionId") or session_id or "")
        target = {"sessionId": sid}
        total = int(sess.get("lines") or 0)
        start = int(sess.get("bufferStart") or 0)
        if newest and cursor is None:
            cur = max(start, total - (FETCH if filtering else limit))
        else:
            cur = _cursors(state).get(sid, 0) if cursor is None else cursor
        fetch = FETCH if filtering else limit
        wait = wait_seconds if cur >= total and sess.get("running") else 0
        pg = await daemon_read(state, target, cur, limit=fetch, wait=wait, udid=udid)
        sess = pg.get("session") or sess
        base = int(pg.get("cursor") or 0)
        examined = [(base + i, e) for i, e in enumerate(pg.get("entries") or []) if isinstance(e, dict)]
        missed = int(pg.get("missed") or 0)

        def passes(e: dict) -> bool:
            return stream_ok(e, stream) and (rx is None or bool(rx.search(str(e.get("text") or ""))))

        matches = [(n, e) for n, e in examined if passes(e)]
        lines, shown, cut_rows = page(matches, limit, B.room(max_bytes), newest, line_chars)
        end = int(pg.get("nextCursor") or base)
        if newest or (not cut_rows and len(matches) <= limit):
            nxt = end                                   # everything examined was looked at
        else:
            sel = matches[:limit]
            nxt = sel[shown - 1][0] + 1 if shown else base
        skipped = len(matches) - shown if newest else 0
        _cursors(state)[sid] = nxt
        total = int(sess.get("lines") or total)
        more = nxt < total
        running = bool(sess.get("running"))
        steps, note = [], None
        if more:
            steps.append(f"More is buffered: read again with cursor={nxt}.")
            if cut_rows:
                note = f"{cut_rows} more lines didn't fit the size cap; read on from cursor={nxt} (or pass max_bytes)."
            elif filtering and len(examined) >= fetch:
                note = f"Searched {len(examined)} lines; read on from cursor={nxt} to search further."
        elif running:
            steps.append(f"Caught up. Call again with cursor={nxt} and wait_seconds=10 for new lines.")
        if skipped:
            note = f"newest=true skipped {skipped} older matching lines."
        if not running:
            steps.append(f"Not capturing any more: {sess.get('reason') or sess.get('status')}. Relaunch with "
                         "ios_app_run_and_capture or ios_app_launch(console=true) for fresh output.")
        if missed:
            steps.append(f"{missed} lines were overwritten before you read them; read more often or use grep.")
        if total == 0 and running:
            steps.append("Nothing printed yet. print() output appears once the app writes a full line.")
        if sess.get("streams") == "merged" and stream == "stderr":
            steps.append("On this iPhone stdout and stderr arrive together, so stream='stderr' can't separate them.")
        return ConsoleRead(session_id=sid, bundle_id=str(sess.get("bundleId") or bundle_id or ""), running=running,
                           status=str(sess.get("status") or "unknown"), reason=sess.get("reason"),
                           pid=sess.get("pid"), streams=str(sess.get("streams") or "merged"), count=len(lines),
                           next_cursor=nxt, has_more=more, buffer_start=int(sess.get("bufferStart") or 0),
                           missed=missed, dropped=int(sess.get("dropped") or 0), skipped=skipped,
                           total_lines=total, lines=lines, next_steps=steps, truncated=bool(cut_rows or skipped),
                           omitted=cut_rows, note=note).with_budget(max_bytes)

