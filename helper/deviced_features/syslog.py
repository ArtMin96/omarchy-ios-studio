"""The one os_trace stream to the phone behind syslog.start / syslog.filter / syslog.stop.

The commands themselves are per client and live in device_broker.SyslogHub; this module is the
phone side the broker drives through the daemon's syslog_* backend methods (provided as "syslog").
"""
from __future__ import annotations

import asyncio
import contextlib
import posixpath
import time

import deviced_core as core
from deviced_core import TUNNEL_WAIT_S, classify, dlog

SYSLOG_MSG_MAX = 8192
LEVELS = {"debug": 0, "info": 1, "default": 2, "error": 3, "fault": 4}
# os_trace raw level byte -> our name
RAW_LEVEL = {0x00: "default", 0x01: "info", 0x02: "debug", 0x03: "default", 0x10: "error", 0x11: "fault"}


class PhoneSyslog:
    """The one os_trace stream to the phone. Per-client filters live in device_broker.SyslogHub."""

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
        apps = await session.pmd3.InstallationProxyService(lockdown=session.rsd).get_apps(application_type="Any")
        for k, v in apps.items():
            if k == bid or k.endswith("." + bid):
                return v.get("CFBundleExecutable") or None, k
        return None, bid

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
                async for entry in session.pmd3.OsTraceService(lockdown=rsd).syslog(pid=pid):
                    if n == 0:
                        dlog("syslog: first entry")
                    item = self._entry(entry, hub.min_level())
                    if item is not None:
                        hub.feed(item)
                    n += 1
                    if n % 256 == 0:
                        await asyncio.sleep(0)  # let flushes and requests run
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
