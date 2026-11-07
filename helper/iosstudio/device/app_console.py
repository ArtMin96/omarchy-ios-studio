"""App console: what an app launched by iOS Studio writes to stdout and stderr (Swift `print()`,
`FileHandle.standardError`, `NSLog` to stderr...). Shared by the real daemon
(features/console.py) and the demo one (demo_features/console.py). Stdlib only.

On a real iPhone the app's output never reaches the device log. CoreDevice can hand it to us
instead: open a stdio socket, pass its identifier when launching, and the app's stdin, stdout and
stderr are attached to it through a pseudoterminal. This module owns everything after the bytes
arrive:

* LineSplitter: bytes -> lines. Partial lines wait (and are flushed after a quiet second or at
  exit), CR LF and stray control characters are cleaned up, invalid UTF-8 becomes U+FFFD, and a
  line longer than MAX_LINE bytes keeps its first MAX_LINE bytes (`cut` = bytes left out).
* ConsoleSession: one launch of one app. A CursorRing of lines (RING_LINES / RING_BYTES), an
  ingest rate cap (RATE lines/s, burst BURST) with a `dropped` counter, and a status.
* register_commands(reg, backend): the console.* commands, `console` / `consoleState` events,
  cleanup on device switch and shutdown.

Backend interface (duck-typed):
    emit(name, data)                               device-wide event (thread-safe)
    err(code, message, fix=None, detail=None) -> Exception
    async launch(ctx, session, args) -> (result, readers)
        Launch the app with its stdio attached. result is app.launch's reply ({bundleId, pid}),
        readers a list of Reader. Raise ConsoleUnavailable when the console can't be attached:
        the app is then launched without it (the reply says why).
    async plain_launch(ctx, args) -> dict          app.launch without the console
Handlers get (ctx, args); ctx has .client() in both daemons.
"""
from __future__ import annotations

import asyncio
import contextlib
import inspect
import itertools
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from iosstudio.device.deviced_kit import CursorRing

MAX_LINE = 8192             # bytes kept of one line; the rest is counted in `cut`
MAX_PENDING = MAX_LINE      # a partial line is cut (not buffered further) past this
RING_LINES = 10_000         # lines kept per session
RING_BYTES = 2 << 20        # and bytes (text + ~48 bytes of overhead per line)
RATE = 2000                 # lines per second kept; more is dropped (and counted)
BURST = 4000                # lines that may arrive at once before the rate cap applies
PARTIAL_FLUSH_S = 1.0       # a partial line with no newline after this long is shown as is
TICK_S = 0.1                # event batching and partial-line flushing
EVENT_MAX = 1000            # lines per `console` event batch (the rest stay in the ring)
MAX_LIVE = 6                # sessions capturing at once; launching another stops the oldest
KEEP_ENDED = 4              # ended sessions kept for reading
READ_MAX = 5000             # entries per console.read

STREAMS = ("out", "stdout", "stderr", "meta")

# CSI / OSC escape sequences, then any C0 control character except tab, and DEL
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class ConsoleUnavailable(Exception):
    """The backend couldn't attach the console; the app is launched without it."""


def clean(raw: bytes) -> str:
    """One line's bytes -> display text: UTF-8 (invalid bytes -> U+FFFD), no escapes or controls.
    A carriage return inside a line (progress bars) keeps only what was drawn last."""
    text = raw.decode("utf-8", "replace")
    text = text.rstrip("\r")
    if "\r" in text:
        text = text.rsplit("\r", 1)[1] or text.replace("\r", "")
    if "\x1b" in text:
        text = _ANSI.sub("", text)
    return _CTRL.sub("", text) if _CTRL.search(text) else text


def _utf8_cut(raw: bytes, n: int) -> int:
    """Largest cut <= n that doesn't split a UTF-8 character."""
    if n >= len(raw):
        return len(raw)
    i = n
    while i > 0 and n - i < 3 and (raw[i] & 0xC0) == 0x80:
        i -= 1
    return i if i > 0 else n


class LineSplitter:
    """Bytes in, (text, cut) lines out. Thread-unsafe; one per stream."""

    def __init__(self, max_line: int = MAX_LINE):
        self.max_line = max(16, int(max_line))
        self._buf = bytearray()
        self._skip = 0            # > 0: dropping the rest of an over-long line (bytes dropped so far)
        self._kept: bytes = b""   # its kept head, emitted when its newline arrives
        self.last_data = 0.0

    @property
    def pending(self) -> bool:
        return bool(self._buf) or self._skip > 0

    def feed(self, data: bytes, now: float | None = None) -> list[tuple[str, int]]:
        if not data:
            return []
        self.last_data = time.monotonic() if now is None else now
        out: list[tuple[str, int]] = []
        start = 0
        n = len(data)
        while start < n:
            nl = data.find(b"\n", start)
            end = n if nl < 0 else nl
            chunk = data[start:end]
            if self._skip:
                self._skip += len(chunk)
            else:
                room = self.max_line - len(self._buf)
                if len(chunk) <= room:
                    self._buf += chunk
                else:
                    joined = bytes(self._buf) + chunk
                    cut = _utf8_cut(joined, self.max_line)
                    self._kept = joined[:cut]
                    self._skip = len(joined) - cut
                    self._buf.clear()
            if nl < 0:
                break
            out.append(self._take())
            start = nl + 1
        return out

    def _take(self) -> tuple[str, int]:
        if self._skip:
            line, cut = clean(self._kept), self._skip
            self._kept, self._skip = b"", 0
        else:
            line, cut = clean(bytes(self._buf)), 0
        self._buf.clear()
        return line, cut

    def flush(self) -> tuple[str, int] | None:
        """The partial line (no newline yet), or None."""
        if not self.pending:
            return None
        return self._take()


def _entry_size(e: dict) -> int:
    return len(e.get("text", "")) + 48


class ConsoleSession:
    def __init__(self, sid: str, bundle_id: str, name: str | None = None, rate: int = RATE, burst: int = BURST,
                 ring_lines: int = RING_LINES, ring_bytes: int = RING_BYTES):
        self.id = sid
        self.bundle_id = bundle_id
        self.name = name or bundle_id.rsplit(".", 1)[-1]
        self.pid: int | None = None
        self.ring = CursorRing(max_items=ring_lines, max_bytes=ring_bytes, size=_entry_size)
        self.splitters: dict[str, LineSplitter] = {}
        self.streams = "merged"
        self.status = "starting"          # starting | running | ended | stopped | lost | failed
        self.reason: str | None = None    # why it isn't running any more, in plain words
        self.error: dict | None = None
        self.started = round(time.time(), 3)
        self.ended: float | None = None
        self.bytes = 0
        self.dropped = 0
        self.cut_lines = 0
        self.rate = rate
        self.burst = burst
        self._tokens = float(burst)
        self._refill = time.monotonic()
        self._dropping = 0                # dropped since the last kept line (for the "N dropped" note)
        self._sent = 0                    # ring seq up to which `console` events went out
        self.tasks: list[asyncio.Task] = []
        self.closers: list[Callable[[], Any]] = []
        self.owner = ""

    # -- lines
    @property
    def live(self) -> bool:
        return self.status in ("starting", "running")

    def feed(self, stream: str, data: bytes, now: float | None = None) -> int:
        """Raw bytes from one stream. Returns the number of lines kept."""
        self.bytes += len(data)
        sp = self.splitters.get(stream)
        if sp is None:
            sp = self.splitters[stream] = LineSplitter()
        kept = 0
        for text, cut in sp.feed(data, now):
            kept += self.add(stream, text, cut)
        return kept

    def flush_partial(self, idle: float = PARTIAL_FLUSH_S, now: float | None = None) -> int:
        """Show partial lines that have waited `idle` seconds (0 = all of them, at exit)."""
        now = time.monotonic() if now is None else now
        kept = 0
        for stream, sp in self.splitters.items():
            if sp.pending and now - sp.last_data >= idle:
                line = sp.flush()
                if line is not None:
                    kept += self.add(stream, line[0], line[1])
        return kept

    def _allow(self) -> bool:
        now = time.monotonic()
        self._tokens = min(float(self.burst), self._tokens + (now - self._refill) * self.rate)
        self._refill = now
        if self._tokens >= 1:
            self._tokens -= 1
            return True
        return False

    def add(self, stream: str, text: str, cut: int = 0, ts: float | None = None) -> int:
        """One line; returns 1 if kept, 0 if the rate cap dropped it."""
        if stream != "meta" and not self._allow():
            self.dropped += 1
            self._dropping += 1
            return 0
        self.note_dropped()
        e: dict = {"ts": round(time.time() if ts is None else ts, 3), "stream": stream, "text": text}
        if cut:
            e["cut"] = cut
            self.cut_lines += 1
        self.ring.append(e)
        return 1

    def note_dropped(self) -> None:
        if self._dropping:
            n, self._dropping = self._dropping, 0
            self.ring.append({"ts": round(time.time(), 3), "stream": "meta",
                              "text": f"[iOS Studio] {n} line{'s' if n != 1 else ''} dropped: the app printed "
                                      f"more than {self.rate} lines a second."})

    def unsent(self, limit: int = EVENT_MAX) -> tuple[int, list[dict], int]:
        """(seq of the first line, lines, skipped) not yet sent as `console` events."""
        page = self.ring.read(self._sent, limit=limit)
        skipped = page["missed"]
        if page["entries"]:
            self._sent = page["nextCursor"]
        if page["hasMore"]:                   # more than `limit` arrived in one tick: skip to the end
            skipped += self.ring.seq - self._sent
            self._sent = self.ring.seq
        return page["cursor"], page["entries"], skipped

    # -- state
    def describe(self) -> dict:
        out = {"sessionId": self.id, "bundleId": self.bundle_id, "name": self.name, "pid": self.pid,
               "status": self.status, "running": self.live, "streams": self.streams,
               "started": self.started, "ended": self.ended, "lines": self.ring.seq,
               "bufferStart": self.ring.buffer_start, "bytes": self.bytes, "dropped": self.dropped,
               "cutLines": self.cut_lines, "owner": self.owner}
        if self.reason:
            out["reason"] = self.reason
        if self.error:
            out["error"] = self.error
        return out


async def _maybe_await(v):
    if inspect.isawaitable(v):
        return await v
    return v


@dataclass
class Reader:
    """One stream of the launched app: `read()` returns bytes, b"" at the end; `close()` frees it."""
    stream: str
    read: Callable[[], Awaitable[bytes]]
    close: Callable[[], Any] | None = None


def match_bundle(have: str, want: str) -> bool:
    """`want` may be the plain id of an app installed as XTL-<TEAM>.<id>."""
    return bool(have and want) and (have == want or have.endswith("." + want))


def register_commands(reg, backend) -> dict[str, ConsoleSession]:
    sessions: dict[str, ConsoleSession] = {}
    ids = itertools.count(1)
    ticker: dict[str, asyncio.Task | None] = {"t": None}
    E = backend.err

    def emit_state(sess: ConsoleSession) -> None:
        backend.emit("consoleState", sess.describe())

    def send_lines(sess: ConsoleSession) -> None:
        first, lines, skipped = sess.unsent()
        if lines or skipped:
            data = {"sessionId": sess.id, "bundleId": sess.bundle_id, "first": first, "lines": lines}
            if skipped:
                data["skipped"] = skipped
            backend.emit("console", data)

    async def tick() -> None:
        try:
            while any(s.live for s in sessions.values()):
                await asyncio.sleep(TICK_S)
                for s in list(sessions.values()):
                    if s.live:
                        s.flush_partial()
                        send_lines(s)
        finally:
            ticker["t"] = None

    def ensure_ticker() -> None:
        t = ticker["t"]
        if t is None or t.done():
            ticker["t"] = asyncio.create_task(tick())

    def prune() -> None:
        ended = sorted((s for s in sessions.values() if not s.live), key=lambda s: s.ended or 0)
        for s in ended[:-KEEP_ENDED] if KEEP_ENDED else ended:
            sessions.pop(s.id, None)

    async def close_io(sess: ConsoleSession) -> None:
        tasks, sess.tasks = sess.tasks, []
        me = asyncio.current_task()
        for t in tasks:
            if t is not me:
                t.cancel()
        for t in tasks:
            if t is not me:
                with contextlib.suppress(BaseException):
                    await t
        closers, sess.closers = sess.closers, []
        for c in closers:
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(_maybe_await(c()), 5)

    async def finish(sess: ConsoleSession, status: str, reason: str | None = None, error: dict | None = None):
        if not sess.live:
            return
        sess.status, sess.reason, sess.error = status, reason, error
        sess.ended = round(time.time(), 3)
        sess.flush_partial(0)
        sess.note_dropped()
        await close_io(sess)
        send_lines(sess)
        emit_state(sess)
        prune()

    async def pump(sess: ConsoleSession, rd: Reader, readers_left: list) -> None:
        try:
            while True:
                chunk = await rd.read()
                if not chunk:
                    break
                sess.feed(rd.stream, chunk)
                if sess.status == "starting":
                    sess.status = "running"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            readers_left.remove(rd)
            reg.log(f"console {sess.id}: {rd.stream} broke: {exc!r}")
            await finish(sess, "lost", "The connection to the iPhone broke, so the output stopped here. "
                         "The app may still be running.", {"code": "unreachable", "message": str(exc)[:300]})
            return
        readers_left.remove(rd)
        if not readers_left:
            await finish(sess, "ended", "The app exited.")

    def find(a: dict, live_only: bool = False) -> ConsoleSession:
        sid = str(a.get("sessionId") or "")
        if sid:
            s = sessions.get(sid)
            if s is None:
                raise E("not-found", "That console session has ended and was cleared (only the last "
                                     f"{KEEP_ENDED} are kept). Launch the app with the console again.")
            return s
        bid = str(a.get("bundleId") or "")
        pool = [s for s in sessions.values() if (not bid or match_bundle(s.bundle_id, bid))
                and (s.live or not live_only)]
        if not pool:
            what = f"for {bid}" if bid else "yet"
            raise E("not-found", f"No console output {what}. Launch the app with the console on "
                                 "(app.launch with console: true).")
        return max(pool, key=lambda s: (s.started, int(s.id[1:])))

    @reg.command("console.launch")
    async def launch(ctx, a):
        bid = str(a.get("bundleId") or "").strip()
        if not bid:
            raise E("bad-args", "Which app? (bundleId missing)")
        sess = ConsoleSession(f"c{next(ids)}", bid, a.get("name"))
        client = ctx.client() if hasattr(ctx, "client") else None
        sess.owner = getattr(client, "name", "") or ""
        try:
            result, readers = await backend.launch(ctx, sess, a)
        except ConsoleUnavailable as exc:
            reg.log(f"console unavailable for {bid}: {exc}")
            result = await backend.plain_launch(ctx, a)
            out = dict(result or {})
            out["console"] = {"error": {"code": "console-unavailable",
                                        "message": f"Launched without the console: {exc}"}}
            return out
        sess.pid = (result or {}).get("pid")
        if (result or {}).get("bundleId"):
            sess.bundle_id = result["bundleId"]
        # a relaunch replaces the app (terminateExisting): its old session ends by itself.
        live = sorted((s for s in sessions.values() if s.live and not match_bundle(s.bundle_id, sess.bundle_id)),
                      key=lambda s: s.started)
        for old in live[:max(0, len(live) - MAX_LIVE + 1)]:
            await finish(old, "stopped", f"Stopped to make room: at most {MAX_LIVE} apps' consoles are "
                                         "captured at once.")
        sessions[sess.id] = sess
        sess.status = "running"
        left = list(readers)
        for rd in readers:
            if rd.close is not None:
                sess.closers.append(rd.close)
        if len(readers) > 1:
            sess.streams = "split"
        elif readers:
            sess.streams = "merged" if readers[0].stream == "out" else "split"
        for rd in readers:
            sess.tasks.append(asyncio.create_task(pump(sess, rd, left)))
        if not readers:
            await finish(sess, "ended", "Nothing to read.")
        ensure_ticker()
        emit_state(sess)
        return {**dict(result or {}), "console": sess.describe()}

    @reg.command("console.read")
    async def read(ctx, a):
        sess = find(a)
        try:
            limit = max(1, min(int(a.get("limit") or 500), READ_MAX))
        except (TypeError, ValueError):
            limit = 500
        cur = a.get("cursor")
        try:
            cur = None if cur in (None, "") else max(0, int(cur))
        except (TypeError, ValueError):
            raise E("bad-args", "cursor must be a number.") from None
        try:
            wait = min(float(a.get("wait") or 0), 20.0) if sess.live else 0
        except (TypeError, ValueError):
            wait = 0
        page = await sess.ring.wait_read(cur, timeout=wait, limit=limit)
        return {**page, "session": sess.describe()}

    @reg.command("console.list")
    async def list_(ctx, a):
        out = sorted(sessions.values(), key=lambda s: (s.started, int(s.id[1:])), reverse=True)
        bid = str(a.get("bundleId") or "")
        return {"sessions": [s.describe() for s in out if not bid or match_bundle(s.bundle_id, bid)]}

    @reg.command("console.stop")
    async def stop(ctx, a):
        sess = find(a, live_only=not a.get("sessionId"))
        await finish(sess, "stopped", "Capture stopped.")
        return sess.describe()

    @reg.command("console.clear")
    async def clear(ctx, a):
        targets = [find(a)] if (a.get("sessionId") or a.get("bundleId")) else list(sessions.values())
        for s in targets:
            s.ring.clear()
            s._sent = s.ring.seq
            emit_state(s)
        return {"cleared": [s.id for s in targets]}

    @reg.welcome
    def welcome(ctx):
        return [("consoleState", s.describe()) for s in sessions.values()]

    @reg.on_tunnel_drop
    async def tunnel_dropped(ctx, session):
        # the stdio sockets went through the tunnel: they're dead even if no read has failed yet
        for s in list(sessions.values()):
            await finish(s, "lost", "The connection to the iPhone dropped, so the output stopped here. The app "
                         "may still be running; launch it again to see more.", {"code": "unreachable",
                                                                                "message": "Tunnel dropped"})

    @reg.on_session_end
    async def device_changed(ctx, session):
        for s in list(sessions.values()):
            await finish(s, "stopped", "The iPhone changed.")

    @reg.on_shutdown
    async def shutdown(ctx):
        for s in list(sessions.values()):
            await finish(s, "stopped", "The iPhone service stopped.")
        t = ticker["t"]
        if t is not None:
            t.cancel()
            with contextlib.suppress(BaseException):
                await t

    return sessions
