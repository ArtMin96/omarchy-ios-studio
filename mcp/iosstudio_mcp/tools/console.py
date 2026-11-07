"""App console: an app's stdout and stderr (Swift print()) for apps launched with the console on.

The device daemon keeps one ring buffer per launch (helper/iosstudio/device/features/console/core.py: 10,000 lines / 2 MB, lines
cut at 8 KB, at most 2,000 lines a second). An app launched by ios_app_launch(console=true),
ios_app_run_and_capture or ios_run has one; ios_app_console_read pages through it with a cursor.
Pure helpers (fmt_line, select_lines, page) are used by tests and by ios_app_run_and_capture.
"""
from __future__ import annotations

from typing import Annotated, ClassVar, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from ..backends.console import FETCH, cursors, daemon_read, fmt_line
from ..core import budget as B
from ..core.params import MaxBytes, Udid
from ..core.registry import MAX_WAIT, tool
from ..models import Out
from ..state import get_state

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


# ------------------------------------------------------------------ pure helpers

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


# ------------------------------------------------------------------ tool

def register(mcp: MCPServer) -> None:

    @tool(mcp, "Read app console", kind="read_remote")
    async def ios_app_console_read(
        ctx: Context,
        bundle_id: Annotated[str | None, Field(description="The app: its latest console session")] = None,
        session_id: Annotated[str | None, Field(description="console.session_id from a launch")] = None,
        cursor: Annotated[int | None, Field(ge=0, description="next_cursor of the last read (0 = start; omit: "
                                                              "continue)")] = None,
        limit: Annotated[int, Field(ge=1, le=2000, description="Max lines")] = 200,
        wait_seconds: Annotated[int, Field(ge=0, le=MAX_WAIT, description="If nothing new, wait up to this "
                                                                         "long")] = 0,
        grep: Annotated[str | None, Field(max_length=B.MAX_PATTERN, description="Case-insensitive regex")] = None,
        stream: Annotated[Stream, Field(description="stderr: only stderr lines (split streams)")] = "all",
        newest: Annotated[bool, Field(description="Skip to the newest `limit` lines")] = False,
        line_chars: Annotated[int, Field(ge=80, le=4000, description="Cut lines after this many chars")] = B.LINE_CUT,
        max_bytes: MaxBytes = None,
        udid: Udid = None,
    ) -> ConsoleRead:
        """Read what an app printed (print(), stdout/stderr) after `cursor`, oldest first, as lines
        '<n> <time> [stderr] <text>'. Only apps launched with the console on (ios_run, ios_app_run_and_capture,
        ios_app_launch(console=true)). Returns next_cursor and has_more; `missed` counts lines the 10,000-line
        buffer overwrote."""
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
            cur = cursors(state).get(sid, 0) if cursor is None else cursor
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
        cursors(state)[sid] = nxt
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

