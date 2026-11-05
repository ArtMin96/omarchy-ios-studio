"""Shared pieces of the real device daemon, importable by feature modules (helper/deviced_features/).

deviced.py runs as __main__, so feature modules must never `import deviced` (that would load a
second copy). Everything they need besides the Daemon/Session objects they're handed lives here:
event output (emit, dlog), errors (err, classify, is_connection_error), small file utilities, the
cache paths, and PortForward.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common  # noqa: E402
from common import HelperError, fix  # noqa: E402
import device_link as link  # noqa: E402

VERSION = "1"
SCREENS_DIR = common.CACHE_DIR / "screens"
ICONS_DIR = common.CACHE_DIR / "icons"
CRASH_DIR = common.CACHE_DIR / "crashes"
DEVICE_CACHE = common.CACHE_DIR / "devices.json"
LAST_DEVICE = common.STATE_DIR / "last-device"
DEVICE_STATE = common.STATE_DIR / "device.json"   # read by Setup while the daemon isn't running

TUNNEL_WAIT_S = 25

# ---------------------------------------------------------------- output

_out_lock = threading.Lock()
_OUT = None
ONCE = False   # --once mode: events go to stderr, only the single reply goes to stdout
BROKER = None  # --serve mode: device_broker.Broker; events go to the subscribed clients


def init_output() -> None:
    """The protocol owns a private copy of stdout; fd 1 itself is pointed at stderr so that nothing
    a library prints (pymobiledevice3's rich progress bars redirect sys.stdout while installing) can
    ever corrupt the JSON lines. deviced.py calls this once, at import."""
    global _OUT
    if _OUT is not None:
        return
    _OUT = os.fdopen(os.dup(1), "w", buffering=1, encoding="utf-8")
    os.dup2(2, 1)
    sys.stdout = sys.stderr


def close_output() -> None:
    with contextlib.suppress(Exception):
        _OUT.close()


def flush_output() -> None:
    with contextlib.suppress(Exception):
        _OUT.flush()


def send(obj: dict) -> None:
    line = json.dumps(obj, separators=(",", ":"), default=str)
    with _out_lock:
        try:
            _OUT.write(line + "\n")
            _OUT.flush()
        except (BrokenPipeError, ValueError, OSError, AttributeError):
            os._exit(0)


def emit(name: str, data) -> None:
    """Device-wide event to every subscribed client (thread-safe in --serve mode)."""
    if ONCE:
        if name in ("connection", "installProgress", "pair"):
            dlog(f"{name}: {json.dumps(data, default=str)[:300]}")
        return
    if BROKER is not None:
        BROKER.emit(name, data)
        return
    send({"event": name, "data": data})


def dlog(text: str, level: str = "debug") -> None:
    print(f"[{time.strftime('%H:%M:%S')}] deviced: {text}", file=sys.stderr, flush=True)


class LogBridge(logging.Handler):
    def emit(self, record):  # noqa: D401 - logging API
        if record.levelno >= logging.WARNING:
            dlog(f"{record.name}: {record.getMessage()}")


def current_client():
    """The broker client whose request is running, or None (--once mode, background work)."""
    if BROKER is None:
        return None
    import device_broker
    return device_broker.current_client()


# ---------------------------------------------------------------- errors

def err(code: str, message: str, fix_: dict | None = None, detail: str | None = None) -> HelperError:
    return HelperError(code, message, fix_, detail)


PAIR_FIX = fix("Pair iPhone", "device", action="pair")


def classify(exc: BaseException, pmd3=None) -> HelperError:
    """Plain-language HelperError for any exception from the phone."""
    if isinstance(exc, HelperError):
        return exc
    name = type(exc).__name__
    text = f"{name}: {exc}"
    low = text.lower()
    if isinstance(exc, link.UnreachableError):
        return err("unreachable", "Can't reach your iPhone. Make sure it's awake, unlocked and on "
                   "the same Wi-Fi (or plugged in).", fix("Retry", "device", action="reconnect"), text)
    if isinstance(exc, link.ApiError):
        return err("pmd3-api", "The iPhone tools need repair.", fix("Repair", "setup", step="pymobiledevice3"), text)
    if "locked" in low or name == "PasswordRequiredError":
        return err("locked", "Your iPhone is locked. Unlock it and try again.", None, text)
    if "developermode" in low.replace(" ", "") or name == "DeveloperModeIsNotEnabledError":
        return err("developer-mode-off", "Developer Mode is off on your iPhone.",
                   fix("Turn on Developer Mode", "device", action="devmode"), text)
    if name in ("NotPairedError", "PairingError", "InvalidHostIDError", "PairingDialogResponsePendingError",
                "UserDeniedPairingError") or isinstance(exc, link.NotPairedError):
        return err("not-trusted", "This computer isn't trusted by your iPhone yet.", PAIR_FIX, text)
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError, asyncio.IncompleteReadError)) \
            or name in ("ConnectionTerminatedError", "ConnectionFailedError", "StreamClosedError"):
        return err("unreachable", "The connection to your iPhone dropped. Trying again…",
                   fix("Retry", "device", action="reconnect"), text)
    return err("failed", "Something went wrong talking to your iPhone.", None, text)


def is_connection_error(exc: BaseException) -> bool:
    return classify(exc).code == "unreachable"


# ---------------------------------------------------------------- small utils

def png_size(data: bytes) -> tuple[int, int]:
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    return 0, 0


def write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


async def installed_apps(pmd3, rsd, **kw) -> dict:
    """InstallationProxyService.get_apps(**kw) on a connection of its own, closed again afterwards."""
    async with pmd3.InstallationProxyService(lockdown=rsd) as ip:
        return await ip.get_apps(**kw)


def safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", s)[:200]


def icon_path(udid: str, bundle_id: str) -> Path:
    return ICONS_DIR / safe_name(udid) / f"{safe_name(bundle_id)}.png"


def battery_view(b: dict | None) -> dict | None:
    if not b:
        return None
    health = None
    design, full = b.get("DesignCapacity"), b.get("NominalChargeCapacity") or b.get("AppleRawMaxCapacity")
    if design and full:
        with contextlib.suppress(Exception):
            health = max(0, min(100, round(100 * float(full) / float(design))))
    return {
        "level": b.get("CurrentCapacity"),
        "charging": bool(b.get("IsCharging")),
        "external": bool(b.get("ExternalConnected")),
        "cycleCount": b.get("CycleCount"),
        "health": health,
        "temperature": round(b["Temperature"] / 100, 1) if isinstance(b.get("Temperature"), (int, float)) else None,
    }


# ---------------------------------------------------------------- port forwarding

class PortForward:
    """A loopback TCP listener; each accepted connection is piped to a service on the phone through
    the daemon's own tunnel (never a second one). Made by Session.port_forward(); stop() closes the
    listener and every piped connection.

    `connect()` is awaited per accepted connection and returns a pymobiledevice3 ServiceConnection
    (anything with .reader, .writer and async close()). It resolves the session's *current* RSD,
    so a session-scoped forward keeps working for new connections after the tunnel reconnects.
    """

    def __init__(self, connect, name: str, host: str = "127.0.0.1", port: int = 0):
        self._connect = connect
        self.name = name
        self.host = host
        self.want_port = port
        self.server: asyncio.AbstractServer | None = None
        self._conns: set[asyncio.Task] = set()
        self.connections = 0

    @property
    def port(self) -> int | None:
        if self.server is None or not self.server.sockets:
            return None
        return self.server.sockets[0].getsockname()[1]

    def describe(self) -> dict:
        return {"name": self.name, "host": self.host, "port": self.port, "active": len(self._conns),
                "connections": self.connections}

    async def start(self) -> "PortForward":
        self.server = await asyncio.start_server(self._handle, self.host, self.want_port, backlog=64)
        dlog(f"forward {self.name}: listening on {self.host}:{self.port}")
        return self

    async def stop(self) -> None:
        srv, self.server = self.server, None
        if srv is not None:
            srv.close()
        tasks = list(self._conns)
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if srv is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(srv.wait_closed(), 3)

    @staticmethod
    async def _pipe(reader, writer) -> None:
        while True:
            data = await reader.read(65536)
            if not data:
                return
            writer.write(data)
            await writer.drain()

    async def _handle(self, lr: asyncio.StreamReader, lw: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        self._conns.add(task)
        self.connections += 1
        remote = None
        pipes: list[asyncio.Task] = []
        try:
            try:
                remote = await self._connect()
            except Exception as exc:  # noqa: BLE001
                dlog(f"forward {self.name}: can't reach the phone side: {exc!r}")
                return
            pipes = [asyncio.create_task(self._pipe(lr, remote.writer)),
                     asyncio.create_task(self._pipe(remote.reader, lw))]
            await asyncio.wait(pipes, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            pass
        finally:
            # one side ended, or stop() cancelled us: end the other pipe too (asyncio.wait doesn't)
            for p in pipes:
                p.cancel()
            await asyncio.gather(*pipes, return_exceptions=True)
            if remote is not None:
                with contextlib.suppress(BaseException):
                    await asyncio.wait_for(remote.close(), 3)
            with contextlib.suppress(Exception):
                lw.close()
            self._conns.discard(task)
