"""The one os_trace stream to the phone behind syslog.start / syslog.filter / syslog.stop.

The commands themselves are per client and live in broker.SyslogHub; this module is the
phone side the broker drives through the daemon's syslog_* backend methods (provided as "syslog").

The event loop only reads the framed records off the tunnel and batches the raw bytes; decoding
(pymobiledevice3's parse_syslog_entry), the level cut and every client's filter run on the hub's
worker thread (broker.SyslogWorker), so a chatty phone log never delays other requests.
"""
from __future__ import annotations

import asyncio
import contextlib
import plistlib
import posixpath
import struct
import time

from iosstudio.device.features.apps.index import device_fetcher
from iosstudio.device.features.apps.lookup import find_app
from iosstudio.device.real import core
from iosstudio.device.real.core import TUNNEL_WAIT_S, classify, dlog

SYSLOG_MSG_MAX = 8192
BATCH_MAX = 256          # raw records per batch handed to the worker
BATCH_WAIT_S = 0.05      # a partial batch goes out after this long
LEVELS = {"debug": 0, "info": 1, "default": 2, "error": 3, "fault": 4}
# os_trace raw level byte -> our name
RAW_LEVEL = {0x00: "default", 0x01: "info", 0x02: "debug", 0x03: "default", 0x10: "error", 0x11: "fault"}


class PhoneSyslog:
    """The one os_trace stream to the phone. Per-client filters live in broker.SyslogHub."""

    def __init__(self):
        self.task: asyncio.Task | None = None

    def open(self, session, pid: int):
        self.task = asyncio.create_task(self._run(session, pid))

    async def close(self):
        t, self.task = self.task, None
        if t:
            t.cancel()
            with contextlib.suppress(BaseException):
                await t

    @staticmethod
    async def resolve(session, bid: str):
        """bundleId filter -> (executable, installed bundle id). Never waits for the tunnel."""
        if session is None or session.rsd is None:
            return None, None
        app = await find_app(session.udid, bid, device_fetcher(session.pmd3, session.rsd))
        if app is None:
            return None, bid
        return app.get("executable") or None, app["bundleId"]

    @staticmethod
    def _entry(e, min_level: int) -> dict | None:
        lvl = RAW_LEVEL.get(int(e.level), "default")
        if LEVELS[lvl] < min_level:
            return None   # no subscriber wants this level: skip building it
        msg = e.message or ""
        if len(msg) > SYSLOG_MSG_MAX:
            msg = msg[:SYSLOG_MSG_MAX] + "…"
        try:
            ts = e.timestamp.timestamp()
        except Exception:  # noqa: BLE001
            ts = time.time()
        return {"ts": ts, "process": posixpath.basename(e.filename or ""), "pid": e.pid, "level": lvl,
                "subsystem": e.label.subsystem if e.label else "", "category": e.label.category if e.label else "",
                "message": msg, "image": posixpath.basename(e.image_name or "")}

    async def _run(self, session, pid: int):
        hub = core.BROKER.syslog
        decode = raw_decoder()
        first = True
        while True:
            try:
                rsd = await session.get_rsd(timeout=TUNNEL_WAIT_S if first else 3600)
                await hub.resolve_missing()
                if hub.reconnecting or first:
                    hub.stream_state(False, None)
                first = False
                n = 0
                dlog(f"syslog: streaming (pid filter {pid})")
                # Closed on every way out (stream end, error, cancel), not left to the garbage collector.
                async with session.pmd3.OsTraceService(lockdown=rsd) as ost:
                    if decode is not None and hasattr(ost, "service"):
                        records, parse = raw_records(ost, pid), decode
                    else:   # an unexpected pymobiledevice3: its own parser, still filtered on the worker
                        records, parse = ost.syslog(pid=pid), self._entry
                    batcher = Batcher(lambda b: hub.feed_batch(b, parse))
                    try:
                        async with contextlib.aclosing(records) as entries:
                            async for rec in entries:
                                if n == 0:
                                    dlog("syslog: first entry")
                                n += 1
                                batcher.add(rec)
                                if n % BATCH_MAX == 0:
                                    await asyncio.sleep(0)  # let flushes and requests run
                    finally:
                        batcher.flush()
                raise ConnectionError("the log stream ended")
            except asyncio.CancelledError:
                raise
            except BaseException as exc:  # noqa: BLE001
                e = classify(exc)
                dlog(f"syslog: {exc!r}")
                if e.code == "unreachable":
                    session.drop("syslog stream broke")
                    hub.stream_state(True, None)
                    await asyncio.sleep(2)
                    continue
                if first and e.code in ("not-paired", "not-trusted"):
                    hub.stream_failed(e.to_json())
                    return
                hub.stream_state(True, e.to_json())
                await asyncio.sleep(5)


class Batcher:
    """Collects records on the loop and hands them on in batches: when BATCH_MAX are waiting or
    BATCH_WAIT_S after the first one, whichever comes first."""

    def __init__(self, sink, size: int = BATCH_MAX, wait: float = BATCH_WAIT_S):
        self.sink = sink
        self.size = size
        self.wait = wait
        self.items: list = []
        self._timer: asyncio.TimerHandle | None = None

    def add(self, item) -> None:
        self.items.append(item)
        if len(self.items) >= self.size:
            self.flush()
        elif self._timer is None:
            self._timer = asyncio.get_running_loop().call_later(self.wait, self.flush)

    def flush(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if self.items:
            items, self.items = self.items, []
            self.sink(items)


_HDR = struct.Struct("<9xI8x16x18xI4xIxB6x8x8x16xHH2x4xII4x")   # pid, seconds, µs, level, sizes
HEADER_SIZE = _HDR.size                                          # 129


def fast_entry(raw: bytes, min_level: int) -> dict | None:
    """One raw os_trace record -> our entry dict, straight from the bytes (several times faster than
    building pymobiledevice3's SyslogEntry first). Layout: pymobiledevice3 _SyslogRecordHeader."""
    pid, sec, usec, level, image_size, msg_size, sub_size, cat_size = _HDR.unpack_from(raw)
    lvl = RAW_LEVEL.get(level, "default")
    if LEVELS[lvl] < min_level:
        return None
    off = HEADER_SIZE
    end = raw.find(b"\0", off)
    if end < 0:
        raise ValueError("no filename terminator")
    filename = raw[off:end].decode("utf-8", "replace")
    off = end + 1
    image = raw[off:off + image_size - 1].decode("utf-8", "replace") if image_size else ""
    off += image_size
    if msg_size > SYSLOG_MSG_MAX + 1:
        msg = raw[off:off + SYSLOG_MSG_MAX].decode("utf-8", "replace") + "…"
    else:
        msg = raw[off:off + msg_size - 1].decode("utf-8", "replace") if msg_size else ""
    off += msg_size
    subsystem = category = ""
    if sub_size > 0 and cat_size > 0:
        subsystem = raw[off:off + sub_size - 1].decode("utf-8", "replace")
        category = raw[off + sub_size:off + sub_size + cat_size - 1].decode("utf-8", "replace")
    return {"ts": sec + usec / 1e6, "process": posixpath.basename(filename), "pid": pid, "level": lvl,
            "subsystem": subsystem, "category": category, "message": msg, "image": posixpath.basename(image)}


def same_entry(a: dict | None, b: dict | None) -> bool:
    """Equal entries; timestamps within a millisecond (pymobiledevice3 goes through datetime)."""
    if a is None or b is None:
        return a is b
    return abs(a["ts"] - b["ts"]) < 1e-3 and {**a, "ts": 0} == {**b, "ts": 0}


def raw_decoder():
    """parse(raw, min_level) -> entry | None for the worker thread, or None when this
    pymobiledevice3 has no parse_syslog_entry (the stream then uses OsTraceService.syslog()).
    The first record is decoded both ways: fast_entry() is used only when it agrees with
    pymobiledevice3's own parser (a changed record layout falls back to the slow, safe path)."""
    try:
        from pymobiledevice3.services.os_trace import parse_syslog_entry
    except Exception:  # noqa: BLE001
        return None

    def slow(raw: bytes, min_level: int) -> dict | None:
        return PhoneSyslog._entry(parse_syslog_entry(raw), min_level)

    chosen: list = []

    def parse(raw: bytes, min_level: int) -> dict | None:
        if chosen:
            return chosen[0](raw, min_level)
        ref = slow(raw, 0)
        try:
            ok = same_entry(fast_entry(raw, 0), ref)
        except Exception:  # noqa: BLE001
            ok = False
        if not ok:
            dlog("syslog: record layout differs from the fast decoder's; using pymobiledevice3's parser")
        chosen.append(fast_entry if ok else slow)
        return ref if ref is not None and LEVELS[ref["level"]] >= min_level else None
    return parse


async def raw_records(ost, pid: int):
    """The os_trace_relay activity stream as raw record bytes (OsTraceService.syslog() without the
    decoding, which runs on the worker instead)."""
    from pymobiledevice3.services.os_trace import (
        OS_TRACE_RELAY_MESSAGE_FILTER_ALL,
        OS_TRACE_RELAY_STREAM_FLAGS_DEFAULT,
    )
    await ost.connect()
    svc = ost.service
    await svc.send_plist({"Request": "StartActivity", "MessageFilter": OS_TRACE_RELAY_MESSAGE_FILTER_ALL,
                          "Pid": pid, "StreamFlags": OS_TRACE_RELAY_STREAM_FLAGS_DEFAULT})
    (length_length,) = struct.unpack("<I", await svc.recvall(4))
    length = int.from_bytes(await svc.recvall(length_length), "little")
    response = plistlib.loads(await svc.recvall(length))
    if response.get("Status") != "RequestSuccessful":
        raise core.err("failed", "The iPhone refused to start its log stream.", None, str(response)[:300])
    while True:
        magic = await svc.recvall(1)
        if magic != b"\x02":
            raise ConnectionError(f"malformed log stream (chunk marker {magic.hex()})")
        (length,) = struct.unpack("<I", await svc.recvall(4))
        yield await svc.recvall(length)


def register(reg):
    phone = PhoneSyslog()
    reg.provide("syslog", phone)

    @reg.on_session_end
    async def device_changed(d, s):
        if core.BROKER is not None and not d.stopping.is_set():
            await core.BROKER.syslog.stop_all()

    @reg.on_shutdown
    async def stop(d):
        await phone.close()
