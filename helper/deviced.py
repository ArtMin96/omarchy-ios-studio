"""iosstudio-deviced: the long-running iPhone daemon (JSON lines over stdin/stdout).

Runs under the pymobiledevice3 venv python (common.pymd3_python()). Protocol: docs/deviced-protocol.md.
One tunnel to the active device stays open; it reconnects with backoff. Requests run concurrently
as asyncio tasks, so a slow request never blocks events or other requests.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import platform
import posixpath
import re
import signal
import sys
import threading
import time
import traceback
from collections import deque
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))

import common  # noqa: E402
from common import HelperError, fix  # noqa: E402
import device_crash  # noqa: E402
import device_link as link  # noqa: E402
from device_models import marketing_name  # noqa: E402

VERSION = "1"
SCREENS_DIR = common.CACHE_DIR / "screens"
ICONS_DIR = common.CACHE_DIR / "icons"
CRASH_DIR = common.CACHE_DIR / "crashes"
DEVICE_CACHE = common.CACHE_DIR / "devices.json"
LAST_DEVICE = common.STATE_DIR / "last-device"
DEVICE_STATE = common.STATE_DIR / "device.json"   # read by Setup while the daemon isn't running

BACKOFF = [3, 5, 10, 20, 30]
HEALTH_EVERY_S = 60
TUNNEL_WAIT_S = 25
DISCOVERY_EVERY_S = 3

SYSLOG_FLUSH_S = 0.1
SYSLOG_BATCH = 200
SYSLOG_BUFFER = 1000
SYSLOG_MSG_MAX = 8192
LEVELS = {"debug": 0, "info": 1, "default": 2, "error": 3, "fault": 4}
# os_trace raw level byte -> our name
RAW_LEVEL = {0x00: "default", 0x01: "info", 0x02: "debug", 0x03: "default", 0x10: "error", 0x11: "fault"}

PAIR_HELP_WIFI = ("On your iPhone, open Settings › Developer › Paired Macs and tap this computer "
                  "under “Other Devices”.")


# ---------------------------------------------------------------- output

_out_lock = threading.Lock()
# The protocol owns a private copy of stdout; fd 1 itself is pointed at stderr so that nothing a
# library prints (pymobiledevice3's rich progress bars redirect sys.stdout while installing) can
# ever corrupt the JSON lines.
_OUT = os.fdopen(os.dup(1), "w", buffering=1, encoding="utf-8")
os.dup2(2, 1)
sys.stdout = sys.stderr
ONCE = False   # --once mode: events go to stderr, only the single reply goes to stdout


def send(obj: dict) -> None:
    line = json.dumps(obj, separators=(",", ":"), default=str)
    with _out_lock:
        try:
            _OUT.write(line + "\n")
            _OUT.flush()
        except (BrokenPipeError, ValueError, OSError):
            os._exit(0)


def emit(name: str, data) -> None:
    if ONCE:
        if name in ("connection", "installProgress", "pair"):
            dlog(f"{name}: {json.dumps(data, default=str)[:300]}")
        return
    send({"event": name, "data": data})


def dlog(text: str, level: str = "debug") -> None:
    print(f"[{time.strftime('%H:%M:%S')}] deviced: {text}", file=sys.stderr, flush=True)


class _LogBridge(logging.Handler):
    def emit(self, record):  # noqa: D401 - logging API
        if record.levelno >= logging.WARNING:
            dlog(f"{record.name}: {record.getMessage()}")


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


def safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", s)[:200]


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


# ---------------------------------------------------------------- device session

class Session:
    """The active device: one tunnel, reconnect with backoff, shared DVT channels."""

    def __init__(self, daemon: "Daemon", udid: str):
        self.d = daemon
        self.pmd3 = daemon.pmd3
        self.udid = udid
        self.rsd = None
        self.where: dict = {}
        self.state = "searching"
        self.connected = asyncio.Event()
        self.wake = asyncio.Event()
        self.dropped = asyncio.Event()
        self.closing = False
        self.task: asyncio.Task | None = None
        self.info: dict = {}
        self.attempt = 0
        # DVT (instruments) channels, opened lazily on the current tunnel
        self.dvt_stack: contextlib.AsyncExitStack | None = None
        self.dvt = None
        self.dvt_lock = asyncio.Lock()
        self.shot = None
        self.loc = None
        self.location: dict | None = None

    # -- lifecycle
    def start(self):
        self.task = asyncio.create_task(self._run(), name=f"session-{self.udid}")

    async def close(self):
        self.closing = True
        self.dropped.set()
        self.wake.set()
        if self.task:
            self.task.cancel()
            with contextlib.suppress(BaseException):
                await self.task

    def drop(self, why: str = ""):
        if self.rsd is not None:
            dlog(f"dropping tunnel: {why}")
            self.dropped.set()

    def reconnect_now(self):
        self.attempt = 0
        self.dropped.set()
        self.wake.set()

    def set_state(self, state: str, message: str, detail: str = "", retry_in: float = 0):
        self.state = state
        emit("connection", {"udid": self.udid, "state": state, "transport": self.where.get("transport"),
                            "message": message, "detail": detail[-1500:], "retryIn": retry_in,
                            "since": time.time()})
        self.d.publish_devices()
        self.d.write_state(self)

    def pick_transport(self) -> str | None:
        if self.udid in self.d.usb:
            return "usb"
        if self.udid in self.d.wifi:
            return "wifi"
        return None

    async def _sleep_or_wake(self, seconds: float):
        self.wake.clear()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self.wake.wait(), timeout=seconds)

    async def _run(self):
        while not self.closing:
            transport = self.pick_transport()
            if transport is None:
                if self.udid in self.d.lockdown:
                    self.set_state("not-paired", "Plug in your iPhone, or pair it once for Wi-Fi")
                else:
                    self.set_state("not-paired", "Not paired — pair your iPhone once")
                await self._sleep_or_wake(DISCOVERY_EVERY_S * 2)
                continue
            self.where = {"transport": transport}
            self.set_state("connecting", "Connecting over USB…" if transport == "usb" else "Looking for your iPhone on Wi-Fi…")
            try:
                async with link.rsd_for(self.pmd3, transport, self.udid) as (rsd, where):
                    if rsd.udid and rsd.udid != self.udid:
                        raise RuntimeError(f"reached {rsd.udid}, not {self.udid}")
                    self.rsd, self.where = rsd, where
                    self.attempt = 0
                    self.dropped.clear()
                    self.info = self.static_info()
                    self.d.remember(self.udid, self.info)
                    self.connected.set()
                    self.set_state("connected", "Connected over USB" if transport == "usb" else "Connected over Wi-Fi")
                    self.d.on_connected(self)
                    await self._hold()
            except asyncio.CancelledError:
                raise
            except BaseException as exc:  # noqa: BLE001
                e = classify(exc)
                dlog(f"connect/hold over {transport} failed: {type(exc).__name__}: {exc}")
                self._after_drop()
                if self.closing:
                    break
                delay = BACKOFF[min(self.attempt, len(BACKOFF) - 1)]
                self.attempt += 1
                if e.code == "not-trusted":
                    self.set_state("untrusted", "Not trusted — tap Trust on your iPhone", e.detail or "", delay)
                elif e.code == "locked":
                    self.set_state("locked", "Locked — unlock your iPhone", e.detail or "", delay)
                elif e.code == "unreachable":
                    msg = ("Can't reach your iPhone over USB — check the cable" if transport == "usb" else
                           "Can't reach your iPhone — is it awake and on the same Wi-Fi?")
                    self.set_state("unreachable", msg, e.detail or "", delay)
                else:
                    self.set_state("error", e.message, e.detail or "", delay)
                await self._sleep_or_wake(delay)
                continue
            self._after_drop()
            if self.closing:
                break
            self.set_state("searching", "Reconnecting to your iPhone…")
            await self._sleep_or_wake(1)

    def _after_drop(self):
        was = self.rsd is not None
        self.rsd = None
        self.connected.clear()
        self.dvt = self.shot = self.loc = None
        stack, self.dvt_stack = self.dvt_stack, None
        if stack is not None:
            asyncio.create_task(self._close_stack(stack))
        if was:
            self.d.on_disconnected(self)

    @staticmethod
    async def _close_stack(stack):
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(stack.aclose(), 5)

    async def _hold(self):
        """Stay connected until dropped; refresh battery etc. periodically (doubles as health check)."""
        asyncio.create_task(self._push_info())
        while not self.closing:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.dropped.wait(), timeout=HEALTH_EVERY_S)
            if self.dropped.is_set() or self.closing:
                return
            try:
                await asyncio.wait_for(self._push_info(raise_=True), 20)
            except BaseException as exc:  # noqa: BLE001
                if is_connection_error(exc):
                    dlog(f"health check failed: {exc!r}")
                    return

    async def _push_info(self, raise_: bool = False):
        try:
            data = await self.dynamic_info()
            self.info.update(data)
            emit("deviceInfo", {"udid": self.udid, **data})
            self.d.write_state(self)
        except BaseException:
            if raise_:
                raise

    async def get_rsd(self, timeout: float = TUNNEL_WAIT_S):
        if self.rsd is not None:
            return self.rsd
        self.wake.set()  # skip backoff wait: someone needs the phone now
        try:
            await asyncio.wait_for(self.connected.wait(), timeout)
        except asyncio.TimeoutError:
            if self.state == "not-paired":
                raise err("not-paired", "Your iPhone isn't paired with this computer yet.", PAIR_FIX)
            if self.state == "untrusted":
                raise err("not-trusted", "Your iPhone doesn't trust this computer yet.", PAIR_FIX)
            if self.state == "locked":
                raise err("locked", "Your iPhone is locked. Unlock it and try again.")
            raise err("unreachable", "Can't reach your iPhone. Make sure it's awake and on the same "
                      "Wi-Fi, or plug it in.", fix("Retry", "device", action="reconnect"))
        return self.rsd

    # -- info
    def static_info(self) -> dict:
        lv = (self.rsd.all_values or {}) if self.rsd else {}
        pt = lv.get("ProductType") or getattr(self.rsd, "product_type", "") or ""
        return {
            "udid": self.udid,
            "name": lv.get("DeviceName", ""),
            "productType": pt,
            "model": marketing_name(pt),
            "hardwareModel": lv.get("HardwareModel", ""),
            "ios": lv.get("ProductVersion") or getattr(self.rsd, "product_version", "") or "",
            "build": lv.get("BuildVersion", ""),
            "cpu": lv.get("CPUArchitecture", ""),
            "deviceClass": lv.get("DeviceClass", ""),
            "color": lv.get("DeviceColor", ""),
            "serial": lv.get("SerialNumber", ""),
            "wifiAddress": lv.get("WiFiAddress", ""),
            "bluetoothAddress": lv.get("BluetoothAddress", ""),
            "timeZone": lv.get("TimeZone", ""),
            "activation": lv.get("ActivationState", ""),
            "passcodeSet": None,
            "transport": self.where.get("transport"),
            "address": self.where.get("address", ""),
            "wifiPaired": self.udid in self.d.wifi,
            "usbPaired": self.udid in self.d.lockdown,
            "pymobiledevice3": self.pmd3.version,
        }

    async def dynamic_info(self) -> dict:
        rsd = self.rsd
        if rsd is None:
            raise err("unreachable", "Not connected.")
        p = self.pmd3
        out: dict = {"battery": None, "storage": None, "developerMode": None, "ddiMounted": None}
        conn_err: list[BaseException] = []

        async def battery():
            async with p.DiagnosticsService(rsd) as d:
                out["battery"] = battery_view(await d.get_battery())

        async def storage():
            async with p.AfcService(rsd) as a:
                i = await a.get_device_info()
                total, free = int(i.get("FSTotalBytes", 0)), int(i.get("FSFreeBytes", 0))
                out["storage"] = {"total": total, "free": free, "used": max(0, total - free)}

        async def devmode():
            out["developerMode"] = bool(await rsd.get_developer_mode_status())

        async def ddi():
            async with p.MobileImageMounterService(lockdown=rsd) as m:
                out["ddiMounted"] = bool(await m.is_image_mounted(p.image_type_for_device(rsd)))

        async def guard(fn):
            try:
                await asyncio.wait_for(fn(), 15)
            except BaseException as exc:  # noqa: BLE001
                dlog(f"info {fn.__name__}: {exc!r}")
                if is_connection_error(exc):
                    conn_err.append(exc)

        await asyncio.gather(guard(battery), guard(storage), guard(devmode), guard(ddi))
        if len(conn_err) == 4:
            raise conn_err[0]
        return out

    # -- DVT channels
    async def ensure_ddi(self):
        rsd = await self.get_rsd()
        p = self.pmd3
        async with p.MobileImageMounterService(lockdown=rsd) as m:
            mounted = await m.is_image_mounted(p.image_type_for_device(rsd))
        if not mounted:
            dlog("mounting the developer disk image")
            try:
                await p.auto_mount(rsd)
            except BaseException as exc:  # noqa: BLE001
                e = classify(exc)
                if e.code == "failed":
                    raise err("ddi", "Couldn't prepare your iPhone for developer tools (the developer disk "
                              "image). Check your internet connection and that the iPhone is unlocked.",
                              fix("Try again", "device", action="ddi"), e.detail)
                raise e
            self.info["ddiMounted"] = True
            emit("deviceInfo", {"udid": self.udid, "ddiMounted": True})

    async def get_dvt(self):
        async with self.dvt_lock:
            if self.dvt is not None and self.dvt_stack is not None:
                return self.dvt
            await self.ensure_ddi()
            rsd = await self.get_rsd()
            stack = contextlib.AsyncExitStack()
            try:
                self.dvt = await stack.enter_async_context(self.pmd3.DvtProvider(rsd))
            except BaseException:
                await stack.aclose()
                raise
            self.dvt_stack = stack
            return self.dvt

    async def screenshot(self) -> bytes:
        for attempt in range(2):
            dvt = await self.get_dvt()
            try:
                async with self.dvt_lock:
                    if self.shot is None:
                        self.shot = await self.dvt_stack.enter_async_context(self.pmd3.Screenshot(dvt))
                    shot = self.shot
                return await asyncio.wait_for(shot.get_screenshot(), 15)
            except BaseException as exc:  # noqa: BLE001
                if isinstance(exc, asyncio.CancelledError):
                    raise
                # DVT channel broke: rebuild it once
                self.dvt = self.shot = self.loc = None
                stack, self.dvt_stack = self.dvt_stack, None
                if stack:
                    asyncio.create_task(self._close_stack(stack))
                if attempt or not is_connection_error(exc):
                    raise
        raise err("failed", "Couldn't take a screenshot.")

    async def location_set(self, lat: float, lon: float):
        dvt = await self.get_dvt()
        async with self.dvt_lock:
            if self.loc is None:
                self.loc = await self.dvt_stack.enter_async_context(self.pmd3.LocationSimulation(dvt))
            await self.loc.set(lat, lon)
        self.location = {"lat": lat, "lon": lon}

    async def location_clear(self):
        if self.loc is None:
            dvt = await self.get_dvt()
            async with self.dvt_lock:
                self.loc = await self.dvt_stack.enter_async_context(self.pmd3.LocationSimulation(dvt))
        async with self.dvt_lock:
            await self.loc.clear()
        self.location = None


# ---------------------------------------------------------------- syslog

class Syslog:
    def __init__(self, daemon: "Daemon"):
        self.d = daemon
        self.task: asyncio.Task | None = None
        self.flusher: asyncio.Task | None = None
        self.buf: deque = deque()
        self.filters: dict = {}
        self.dropped = 0
        self._last_drop_emit = 0.0
        self.udid = None
        self.reconnecting = False
        self.error = None
        self._exe = None   # resolved from filters.bundleId
        self._bid = None

    def state(self) -> dict:
        s = {"running": self.task is not None and not self.task.done(), "udid": self.udid,
             "filters": self.filters, "dropped": self.dropped, "reconnecting": self.reconnecting}
        if self.error:
            s["error"] = self.error
        return s

    def emit_state(self):
        emit("syslogState", self.state())

    @staticmethod
    def norm_filters(f: dict | None) -> dict:
        f = dict(f or {})
        out = {}
        if f.get("processes"):
            out["processes"] = [str(x) for x in f["processes"] if str(x)]
        if f.get("pids"):
            out["pids"] = [int(x) for x in f["pids"]]
        if f.get("minLevel") in LEVELS:
            out["minLevel"] = f["minLevel"]
        if f.get("text"):
            out["text"] = str(f["text"])
        if f.get("subsystem"):
            out["subsystem"] = str(f["subsystem"])
        if f.get("bundleId"):
            out["bundleId"] = str(f["bundleId"])
        return out

    async def start(self, session: Session, filters: dict):
        await self.stop(emit_state=False)
        self.filters = self.norm_filters(filters)
        self.udid = session.udid
        self.dropped = 0
        self.error = None
        self._exe = self._bid = None
        self.buf.clear()
        self.task = asyncio.create_task(self._run(session))
        self.flusher = asyncio.create_task(self._flush_loop())
        self.emit_state()

    async def set_filters(self, filters: dict):
        self.filters = self.norm_filters(filters)
        self._exe = self._bid = None
        if self.filters.get("bundleId") and self.d.session and self.d.session.rsd:
            await self._resolve_bundle(self.d.session)
        self.emit_state()

    async def stop(self, emit_state: bool = True):
        for t in (self.task, self.flusher):
            if t:
                t.cancel()
        for t in (self.task, self.flusher):
            if t:
                with contextlib.suppress(BaseException):
                    await t
        self.task = self.flusher = None
        if self.buf:
            self._flush_once(all_=True)
        self.reconnecting = False
        if emit_state:
            self.emit_state()

    async def _resolve_bundle(self, session: Session):
        bid = self.filters.get("bundleId")
        if not bid:
            return
        try:
            rsd = await session.get_rsd()
            apps = await session.pmd3.InstallationProxyService(lockdown=rsd).get_apps(application_type="Any")
            for k, v in apps.items():
                if k == bid or k.endswith("." + bid):
                    self._exe, self._bid = v.get("CFBundleExecutable") or None, k
                    return
        except BaseException as exc:  # noqa: BLE001
            dlog(f"syslog: can't resolve {bid}: {exc!r}")
        self._exe, self._bid = None, bid

    def _match(self, e) -> dict | None:
        f = self.filters
        proc = posixpath.basename(e.filename or "")
        lvl = RAW_LEVEL.get(int(e.level), "default")
        if "minLevel" in f and LEVELS[lvl] < LEVELS[f["minLevel"]]:
            return None
        if "pids" in f and e.pid not in f["pids"]:
            return None
        if "processes" in f and proc not in f["processes"]:
            return None
        msg = e.message or ""
        if f.get("bundleId"):
            exe, bid = self._exe, self._bid or f["bundleId"]
            if not ((exe and proc == exe) or (exe and exe in msg) or bid in msg):
                return None
        sub = e.label.subsystem if e.label else ""
        cat = e.label.category if e.label else ""
        if "subsystem" in f and not sub.startswith(f["subsystem"]):
            return None
        if "text" in f:
            t = f["text"].lower()
            if t not in msg.lower() and t not in proc.lower() and t not in sub.lower():
                return None
        if len(msg) > SYSLOG_MSG_MAX:
            msg = msg[:SYSLOG_MSG_MAX] + "…"
        try:
            ts = e.timestamp.timestamp()
        except Exception:  # noqa: BLE001
            ts = time.time()
        return {"ts": ts, "process": proc, "pid": e.pid, "level": lvl, "subsystem": sub,
                "category": cat, "message": msg, "image": posixpath.basename(e.image_name or "")}

    def _push(self, entry: dict):
        if len(self.buf) >= SYSLOG_BUFFER:
            self.buf.popleft()
            self.dropped += 1
            now = time.monotonic()
            if now - self._last_drop_emit > 1:
                self._last_drop_emit = now
                self.emit_state()
        self.buf.append(entry)

    def _flush_once(self, all_: bool = False):
        while self.buf:
            n = min(len(self.buf), SYSLOG_BATCH)
            batch = [self.buf.popleft() for _ in range(n)]
            emit("syslog", batch)
            if not all_:
                break

    async def _flush_loop(self):
        while True:
            await asyncio.sleep(SYSLOG_FLUSH_S)
            if self.buf:
                self._flush_once()

    async def _run(self, session: Session):
        first = True
        while True:
            try:
                rsd = await session.get_rsd(timeout=TUNNEL_WAIT_S if first else 3600)
                if self.filters.get("bundleId") and self._exe is None:
                    await self._resolve_bundle(session)
                if self.reconnecting or first:
                    self.reconnecting = False
                    self.error = None
                    self.emit_state()
                first = False
                pids = self.filters.get("pids") or []
                pid = pids[0] if len(pids) == 1 else -1
                n = 0
                dlog(f"syslog: streaming (pid filter {pid})")
                async for entry in session.pmd3.OsTraceService(lockdown=rsd).syslog(pid=pid):
                    if n == 0:
                        dlog("syslog: first entry")
                    item = self._match(entry)
                    if item is not None:
                        self._push(item)
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
                    self.reconnecting = True
                    self.error = None
                    self.emit_state()
                    await asyncio.sleep(2)
                    continue
                if first and e.code in ("not-paired", "not-trusted"):
                    self.error = e.to_json()
                    self.emit_state()
                    return
                self.reconnecting = True
                self.error = e.to_json()
                self.emit_state()
                await asyncio.sleep(5)


# ---------------------------------------------------------------- the daemon

class Daemon:
    def __init__(self, pmd3):
        self.pmd3 = pmd3
        self.usb: set[str] = set()
        self.wifi: dict[str, str] = {}
        self.lockdown: set[str] = set()
        self.cache: dict = common.load_json(DEVICE_CACHE, {}) or {}
        self.session: Session | None = None
        self.syslog = Syslog(self)
        self.mirror_task: asyncio.Task | None = None
        self.mirror_fps = 1.0
        self.pair_task: asyncio.Task | None = None
        self.pair_code_fut: asyncio.Future | None = None
        self.icon_task: asyncio.Task | None = None
        self.sizes_task: asyncio.Task | None = None
        self._last_devices = None
        self.stopping = asyncio.Event()

    # -- devices
    def remember(self, udid: str, info: dict):
        keep = {k: info.get(k) for k in ("name", "productType", "model", "ios", "build") if info.get(k)}
        if keep and self.cache.get(udid) != {**self.cache.get(udid, {}), **keep}:
            self.cache[udid] = {**self.cache.get(udid, {}), **keep, "seen": time.time()}
            with contextlib.suppress(OSError):
                common.save_json(DEVICE_CACHE, self.cache)

    def write_state(self, s: "Session"):
        """~/.local/state/ios-studio/device.json: what Setup needs when the daemon isn't running."""
        if ONCE and s.rsd is None and not s.info:
            return
        info = s.info or {}
        cached = self.cache.get(s.udid, {})
        old = common.load_json(DEVICE_STATE, {}) or {}
        same = old.get("udid") == s.udid
        def pick(key, ikey=None):
            v = info.get(ikey or key)
            if v is None or v == "":
                v = cached.get(key) or (old.get(key) if same else None)
            return v
        state = {
            "udid": s.udid,
            "name": pick("name"),
            "model": pick("model"),
            "productType": pick("productType"),
            "iosVersion": info.get("ios") or cached.get("ios") or (old.get("iosVersion") if same else None),
            "build": pick("build"),
            "developerMode": info.get("developerMode") if info.get("developerMode") is not None else (old.get("developerMode") if same else None),
            "ddiMounted": info.get("ddiMounted") if info.get("ddiMounted") is not None else (old.get("ddiMounted") if same else None),
            "connection": s.state,
            "transport": s.where.get("transport") if s.rsd is not None else None,
            "wifiPaired": s.udid in self.wifi,
            "usbPaired": s.udid in self.lockdown,
            "battery": (info.get("battery") or {}).get("level") if info.get("battery") else (old.get("battery") if same else None),
            "updatedAt": time.time(),
        }
        with contextlib.suppress(OSError):
            common.save_json(DEVICE_STATE, state)

    def device_list(self) -> list[dict]:
        udids = set(self.usb) | set(self.wifi) | set(self.cache)
        # lockdown records only matter for devices we've seen or that are plugged in
        udids |= {u for u in self.lockdown if u in self.usb}
        out = []
        for u in sorted(udids):
            c = self.cache.get(u, {})
            s = self.session if self.session and self.session.udid == u else None
            connected = bool(s and s.rsd is not None)
            out.append({
                "udid": u,
                "name": (s.info.get("name") if s and s.info.get("name") else c.get("name", "")),
                "productType": c.get("productType", ""),
                "model": c.get("model") or marketing_name(c.get("productType", "")),
                "ios": c.get("ios", ""),
                "build": c.get("build", ""),
                "transport": s.where.get("transport") if connected else None,
                "usb": u in self.usb,
                "wifiPaired": u in self.wifi,
                "usbPaired": u in self.lockdown,
                "state": s.state if s else ("available" if u in self.usb else "idle"),
                "active": s is not None,
            })
        return out

    def publish_devices(self):
        lst = self.device_list()
        key = json.dumps(lst, sort_keys=True)
        if key != self._last_devices:
            self._last_devices = key
            emit("devices", lst)

    async def discovery(self):
        while not self.stopping.is_set():
            try:
                usb = set(await link.usb_udids(self.pmd3))
            except BaseException:  # noqa: BLE001
                usb = set()
            new_usb = usb - self.usb
            self.usb = usb
            self.wifi = link.wifi_records(self.pmd3)
            self.lockdown = link.lockdown_records()
            s = self.session
            if s is None:
                cand = self.default_udid()
                if cand:
                    await self.select(cand)
            elif new_usb:
                if s.udid in new_usb:
                    if s.rsd is None or s.where.get("transport") != "usb":
                        s.reconnect_now()  # prefer the cable
                elif s.rsd is None:
                    await self.select(sorted(new_usb)[0])
            self.publish_devices()
            await asyncio.sleep(DISCOVERY_EVERY_S)

    def default_udid(self) -> str | None:
        last = ""
        with contextlib.suppress(OSError):
            last = LAST_DEVICE.read_text().strip()
        pref = (common.settings().get("defaultDevice") or "").strip()
        for cand in (pref, last):
            if cand and (cand in self.usb or cand in self.wifi):
                return cand
        if self.usb:
            return sorted(self.usb)[0]
        if self.wifi:
            return sorted(self.wifi)[0]
        return None

    async def select(self, udid: str):
        if self.session and self.session.udid == udid:
            return self.session
        old = self.session
        self.session = None
        if old:
            await self.mirror_stop()
            await self.syslog.stop()
            await old.close()
        s = Session(self, udid)
        self.session = s
        with contextlib.suppress(OSError):
            common.atomic_write(LAST_DEVICE, udid + "\n")
        s.start()
        self.publish_devices()
        return s

    def on_connected(self, s: Session):
        self.publish_devices()

    def on_disconnected(self, s: Session):
        self.publish_devices()

    def need_session(self, args: dict) -> Session:
        udid = args.get("udid")
        if udid and (not self.session or self.session.udid != udid):
            raise err("bad-args", "That iPhone isn't the selected one. Select it first.", fix("Select", "device"))
        if not self.session:
            raise err("no-device", "No iPhone found yet. Plug it in or pair it over Wi-Fi.", PAIR_FIX)
        return self.session

    async def rsd(self, args) -> tuple[Session, object]:
        s = self.need_session(args)
        return s, await s.get_rsd()

    # -- mirror
    async def mirror_stop(self):
        t, self.mirror_task = self.mirror_task, None
        if t:
            t.cancel()
            with contextlib.suppress(BaseException):
                await t
            emit("mirror", {"running": False, "fps": self.mirror_fps})

    async def _mirror(self, s: Session):
        seq = 0
        fails = 0
        safe = safe_name(s.udid)
        while True:
            t0 = time.monotonic()
            try:
                png = await s.screenshot()
                fails = 0
                path = SCREENS_DIR / f"mirror-{safe}-{seq % 2}.png"
                await asyncio.to_thread(write_bytes, path, png)
                w, h = png_size(png)
                seq += 1
                emit("frame", {"path": str(path), "seq": seq, "ts": time.time(), "width": w, "height": h})
            except asyncio.CancelledError:
                raise
            except BaseException as exc:  # noqa: BLE001
                fails += 1
                e = classify(exc)
                dlog(f"mirror: {exc!r}")
                if e.code == "unreachable" and fails < 30:
                    await asyncio.sleep(2)
                    continue
                self.mirror_task = None
                emit("mirror", {"running": False, "fps": self.mirror_fps, "error": e.to_json()})
                return
            delay = 1.0 / self.mirror_fps - (time.monotonic() - t0)
            if delay > 0:
                await asyncio.sleep(delay)

    # -- icons
    def icon_path(self, udid: str, bundle_id: str) -> Path:
        return ICONS_DIR / safe_name(udid) / f"{safe_name(bundle_id)}.png"

    async def fetch_icons(self, s: Session, bundle_ids: list[str]):
        try:
            rsd = await s.get_rsd()
            async with self.pmd3.IconService(rsd) as svc:
                for bid in bundle_ids:
                    path = self.icon_path(s.udid, bid)
                    if path.exists():
                        continue
                    try:
                        icon = await asyncio.wait_for(svc.fetch_icon(bundle_identifier=bid, width=60, height=60, scale=2), 10)
                    except BaseException as exc:  # noqa: BLE001
                        if is_connection_error(exc):
                            raise
                        continue
                    await asyncio.to_thread(write_bytes, path, icon.png_data)
                    emit("appIcon", {"udid": s.udid, "bundleId": bid, "path": str(path)})
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001
            dlog(f"icons: {exc!r}")

    async def fetch_sizes(self, s: Session, kind: str):
        """Sizes are slow to compute (~9 s over Wi-Fi), so they follow apps.list as an event."""
        try:
            rsd = await s.get_rsd()
            apps = await self.pmd3.InstallationProxyService(lockdown=rsd).get_apps(
                application_type=kind, calculate_sizes=True)
            sizes = {bid: int(i.get("StaticDiskUsage") or 0) + int(i.get("DynamicDiskUsage") or 0)
                     for bid, i in apps.items() if i.get("StaticDiskUsage") is not None}
            emit("appSizes", {"udid": s.udid, "sizes": sizes})
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001
            dlog(f"sizes: {exc!r}")

    # -------------------------------------------------------- commands

    async def c_daemon_ping(self, a):
        return {"pong": True, "ts": time.time()}

    async def c_daemon_shutdown(self, a):
        asyncio.get_running_loop().call_later(0.1, self.stopping.set)
        return {}

    async def c_devices_list(self, a):
        return self.device_list()

    async def c_device_select(self, a):
        udid = a.get("udid")
        if not udid:
            raise err("bad-args", "Which iPhone? (udid missing)")
        if udid not in self.usb and udid not in self.wifi and udid not in self.cache:
            raise err("not-found", "That iPhone isn't around right now.")
        await self.select(udid)
        return next((d for d in self.device_list() if d["udid"] == udid), {})

    async def c_device_reconnect(self, a):
        s = self.need_session(a)
        s.reconnect_now()
        return {}

    async def c_device_info(self, a):
        s, _ = await self.rsd(a)
        if a.get("fresh") or "battery" not in s.info:
            s.info.update(await s.dynamic_info())
        self.write_state(s)
        info = dict(s.info)
        info.update(transport=s.where.get("transport"), address=s.where.get("address", ""),
                    wifiPaired=s.udid in self.wifi, usbPaired=s.udid in self.lockdown,
                    location=s.location, locked=s.state == "locked")
        return info

    # apps
    async def c_apps_list(self, a):
        s, rsd = await self.rsd(a)
        kind = "Any" if a.get("system") else "User"
        apps = await self.pmd3.InstallationProxyService(lockdown=rsd).get_apps(
            application_type=kind, calculate_sizes=bool(a.get("sizes", False)))
        running: dict[str, int] = {}
        with contextlib.suppress(BaseException):
            async with self.pmd3.AppServiceService(rsd) as svc:
                for p in await asyncio.wait_for(svc.list_processes(), 10):
                    url = p.get("executableURL")
                    path = url.get("relative", "") if isinstance(url, dict) else str(url or "")
                    if ".app/" in path and "processIdentifier" in p:
                        running[path.split(".app/")[0].rstrip("/").removeprefix("file://") + ".app"] = int(p["processIdentifier"])
        out = []
        for bid, info in apps.items():
            path = info.get("Path", "")
            pid = None
            for rp, rpid in running.items():
                if path and (rp.endswith(path) or path.endswith(rp)):
                    pid = rpid
                    break
            signer = info.get("SignerIdentity", "") or ""
            icon = self.icon_path(s.udid, bid)
            size = None
            if info.get("StaticDiskUsage") is not None:
                size = int(info.get("StaticDiskUsage") or 0) + int(info.get("DynamicDiskUsage") or 0)
            out.append({
                "bundleId": bid,
                "name": info.get("CFBundleDisplayName") or info.get("CFBundleName") or bid,
                "version": info.get("CFBundleShortVersionString", ""),
                "build": info.get("CFBundleVersion", ""),
                "developer": signer.startswith(("Apple Development", "iPhone Developer")) or bool(info.get("ProfileValidated") and "Development" in signer),
                "signer": signer,
                "removable": info.get("ApplicationType") == "User" or bool(info.get("IsRemovable", False)),
                "hidden": "hidden" in (info.get("SBAppTags") or []),
                "system": info.get("ApplicationType") == "System",
                "executable": info.get("CFBundleExecutable", ""),
                "path": path,
                "size": size,
                "icon": str(icon) if icon.exists() else None,
                "running": pid is not None,
                "pid": pid,
            })
        out.sort(key=lambda x: (x["name"] or "").lower())
        if a.get("sizes") is None and (self.sizes_task is None or self.sizes_task.done()):
            self.sizes_task = asyncio.create_task(self.fetch_sizes(s, kind))
        if a.get("icons", True):
            missing = [x["bundleId"] for x in out if not x["icon"]]
            if missing and (self.icon_task is None or self.icon_task.done()):
                self.icon_task = asyncio.create_task(self.fetch_icons(s, missing))
        return out

    async def _app_pids(self, rsd, bundle_id: str) -> list[int]:
        apps = await self.pmd3.InstallationProxyService(lockdown=rsd).get_apps(
            application_type="Any", bundle_identifiers=[bundle_id])
        info = apps.get(bundle_id)
        if not info:
            raise err("not-found", "That app isn't installed on your iPhone.")
        app_path = (info.get("Path") or "").rstrip("/")
        exe = info.get("CFBundleExecutable") or ""
        pids = []
        async with self.pmd3.AppServiceService(rsd) as svc:
            for p in await svc.list_processes():
                url = p.get("executableURL")
                path = url.get("relative", "") if isinstance(url, dict) else str(url or "")
                path = path.removeprefix("file://")
                if (app_path and path.startswith(app_path + "/")) or (exe and path.endswith(f".app/{exe}")):
                    if "processIdentifier" in p:
                        pids.append(int(p["processIdentifier"]))
        return sorted(set(pids))

    async def c_app_launch(self, a):
        bid = a.get("bundleId")
        if not bid:
            raise err("bad-args", "Which app? (bundleId missing)")
        s, rsd = await self.rsd(a)
        await s.ensure_ddi()
        try:
            async with self.pmd3.AppServiceService(rsd) as svc:
                res = await svc.launch_application(bid, list(a.get("args") or []), True, False, dict(a.get("env") or {}))
        except self.pmd3.exceptions.CoreDeviceError as exc:
            code, reason = link.launch_failure(getattr(exc, "user_info", None))
            if code == "Locked":
                raise err("locked", "Your iPhone is locked. Unlock it, then press Launch again.", None, str(exc))
            low = f"{reason} {exc}".lower()
            if "untrusted" in low or "not been explicitly trusted" in low or "profile" in low and "trust" in low:
                raise err("untrusted-developer", "Your iPhone doesn't trust this developer yet. On the iPhone open "
                          "Settings › General › VPN & Device Management, tap your Apple Development profile and Trust.",
                          None, str(exc))
            raise err("launch-failed", f"The app didn't start{': ' + reason if reason else '.'}", None, str(exc))
        pid = None
        with contextlib.suppress(Exception):
            tok = res.get("processToken") if isinstance(res, dict) else None
            pid = (tok or {}).get("processIdentifier")
        return {"bundleId": bid, "pid": pid}

    async def c_app_kill(self, a):
        bid = a.get("bundleId")
        if not bid:
            raise err("bad-args", "Which app? (bundleId missing)")
        s, rsd = await self.rsd(a)
        pids = await self._app_pids(rsd, bid)
        async with self.pmd3.AppServiceService(rsd) as svc:
            for pid in pids:
                await svc.send_signal_to_process(pid, int(signal.SIGKILL))
        return {"bundleId": bid, "killed": pids}

    async def c_app_install(self, a):
        raw = str(a.get("path") or "").strip()
        if not raw:
            raise err("bad-args", "Which app file? (path missing)")
        path = Path(raw).expanduser()
        if not path.exists():
            raise err("not-found", "The app file to install wasn't found. Build the app again and retry.", None, str(path))
        if not (path.suffix == ".ipa" or (path.is_dir() and path.suffix == ".app")):
            raise err("bad-args", "Only a signed .ipa (or .app folder) can be installed.", None, str(path))
        try:
            bid = await asyncio.to_thread(link.package_bundle_id, path)
        except Exception as exc:  # noqa: BLE001
            raise err("bad-package", "That app file is damaged or isn't a signed iPhone app.", None, repr(exc))
        s, rsd = await self.rsd(a)
        sp = str(path)
        emit("installProgress", {"path": sp, "percent": 0, "status": "uploading"})
        last = [-1]

        def progress(pct, *_):
            p = int(pct)
            if p != last[0]:
                last[0] = p
                emit("installProgress", {"path": sp, "percent": p, "status": "installing"})

        t0 = time.monotonic()
        try:
            await self.pmd3.InstallationProxyService(lockdown=rsd).install_from_local(path, "Install", None, progress)
        except BaseException as exc:  # noqa: BLE001
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise link.install_error(exc, classify)
        installed = None
        with contextlib.suppress(BaseException):
            apps = await self.pmd3.InstallationProxyService(lockdown=rsd).get_apps(application_type="User")
            for k in sorted(apps):
                if bid and (k == bid or k.endswith("." + bid)):
                    installed = k
                    break
        emit("installProgress", {"path": sp, "percent": 100, "status": "done"})
        with contextlib.suppress(OSError):
            if installed:
                self.icon_path(s.udid, installed).unlink(missing_ok=True)
        return {"path": sp, "bundleId": installed or bid, "packageBundleId": bid,
                "seconds": round(time.monotonic() - t0, 1)}

    async def c_app_uninstall(self, a):
        bid = a.get("bundleId")
        if not bid:
            raise err("bad-args", "Which app? (bundleId missing)")
        s, rsd = await self.rsd(a)
        await self.pmd3.InstallationProxyService(lockdown=rsd).uninstall(bid)
        with contextlib.suppress(OSError):
            self.icon_path(s.udid, bid).unlink()
        return {"bundleId": bid}

    async def c_processes_list(self, a):
        s, rsd = await self.rsd(a)
        async with self.pmd3.AppServiceService(rsd) as svc:
            procs = await svc.list_processes()
        out = []
        for p in procs:
            url = p.get("executableURL")
            path = (url.get("relative", "") if isinstance(url, dict) else str(url or "")).removeprefix("file://")
            is_app = ".app/" in path
            if a.get("appsOnly") and not is_app:
                continue
            out.append({"pid": p.get("processIdentifier"), "name": posixpath.basename(path.rstrip("/")),
                        "path": path, "isApp": is_app})
        out.sort(key=lambda x: (x["name"] or "").lower())
        return out

    # screen
    async def c_screenshot(self, a):
        s, _ = await self.rsd(a)
        png = await s.screenshot()
        path = SCREENS_DIR / f"shot-{time.strftime('%Y%m%d-%H%M%S')}.png"
        await asyncio.to_thread(write_bytes, path, png)
        w, h = png_size(png)
        return {"path": str(path), "width": w, "height": h, "bytes": len(png), "ts": time.time()}

    async def c_mirror_start(self, a):
        s = self.need_session(a)
        try:
            fps = float(a.get("fps", 1.0))
        except (TypeError, ValueError):
            fps = 1.0
        self.mirror_fps = max(0.2, min(4.0, fps))
        if self.mirror_task is None or self.mirror_task.done():
            await s.get_rsd()
            self.mirror_task = asyncio.create_task(self._mirror(s))
        emit("mirror", {"running": True, "fps": self.mirror_fps})
        return {"running": True, "fps": self.mirror_fps}

    async def c_mirror_stop(self, a):
        await self.mirror_stop()
        return {"running": False}

    # location
    async def c_location_set(self, a):
        try:
            lat, lon = float(a["lat"]), float(a["lon"])
        except (KeyError, TypeError, ValueError):
            raise err("bad-args", "Enter a latitude and longitude (numbers).")
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise err("bad-args", "Latitude must be between -90 and 90, longitude between -180 and 180.")
        s, _ = await self.rsd(a)
        await s.location_set(lat, lon)
        emit("deviceInfo", {"udid": s.udid, "location": s.location})
        return {"lat": lat, "lon": lon}

    async def c_location_clear(self, a):
        s, _ = await self.rsd(a)
        await s.location_clear()
        emit("deviceInfo", {"udid": s.udid, "location": None})
        return {}

    # ddi / dev mode
    async def c_ddi_status(self, a):
        s, rsd = await self.rsd(a)
        async with self.pmd3.MobileImageMounterService(lockdown=rsd) as m:
            mounted = bool(await m.is_image_mounted(self.pmd3.image_type_for_device(rsd)))
        s.info["ddiMounted"] = mounted
        return {"mounted": mounted}

    async def c_ddi_mount(self, a):
        s, _ = await self.rsd(a)
        await s.ensure_ddi()
        return {"mounted": True}

    async def c_devmode_status(self, a):
        s, rsd = await self.rsd(a)
        enabled = bool(await rsd.get_developer_mode_status())
        s.info["developerMode"] = enabled
        return {"enabled": enabled}

    async def c_devmode_reveal(self, a):
        """Show the Developer Mode switch in Settings (needs lockdown: USB, or RSD if supported)."""
        s = self.need_session(a)
        if s.udid in self.usb:
            lockdown = await self.pmd3.create_using_usbmux(serial=s.udid, autopair=False)
            try:
                await self.pmd3.AmfiService(lockdown).reveal_developer_mode_option_in_ui()
            finally:
                with contextlib.suppress(Exception):
                    await lockdown.close()
            return {}
        rsd = await s.get_rsd()
        try:
            await self.pmd3.AmfiService(rsd).reveal_developer_mode_option_in_ui()
        except BaseException as exc:  # noqa: BLE001
            raise err("needs-usb", "To show the Developer Mode switch, plug your iPhone in with a cable once. "
                      "Or open Settings › Privacy & Security on the iPhone — the switch is at the bottom.",
                      None, repr(exc))
        return {}

    # profiles
    async def c_profiles_list(self, a):
        s, rsd = await self.rsd(a)
        async with self.pmd3.MisagentService(lockdown=rsd) as m:
            raw = await m.copy_all()
        out = []
        for p in raw:
            try:
                plist = p.plist if hasattr(p, "plist") else link.parse_profile(bytes(p))
                out.append(link.profile_summary(plist))
            except (KeyError, TypeError, ValueError) as exc:
                dlog(f"profile skipped: {exc!r}")
        out.sort(key=lambda x: x["expires"], reverse=True)
        return out

    # crashes
    async def c_crash_list(self, a):
        s, rsd = await self.rsd(a)
        want = (a.get("process") or "").strip()
        async with self.pmd3.CrashReportsManager(rsd) as c:
            if a.get("flush", True):
                with contextlib.suppress(BaseException):
                    await asyncio.wait_for(c.flush(), 20)
            paths = await c.ls("/", depth=2)
        out = []
        for p in paths:
            item = device_crash.classify(p)
            if not item:
                continue
            if want and item["process"] != want:
                continue
            out.append(item)
        out.sort(key=lambda x: x["ts"], reverse=True)
        return out

    async def c_crash_pull(self, a):
        name = a.get("name") or ""
        if not name or ".." in name.split("/"):
            raise err("bad-args", "Which crash report? (name missing)")
        s, rsd = await self.rsd(a)
        local = CRASH_DIR / safe_name(s.udid) / safe_name(posixpath.basename(name))
        if not local.exists():
            async with self.pmd3.CrashReportsManager(rsd) as c:
                try:
                    data = await c.afc.get_file_contents(name if name.startswith("/") else "/" + name)
                except BaseException as exc:  # noqa: BLE001
                    e = classify(exc)
                    if e.code == "failed":
                        raise err("not-found", "That crash report is no longer on your iPhone.", None, repr(exc))
                    raise e
            await asyncio.to_thread(write_bytes, local, data)
        text = await asyncio.to_thread(local.read_text, "utf-8", "replace")
        return {"path": str(local), "summary": device_crash.parse_ips(text)}

    # syslog
    async def c_syslog_start(self, a):
        s = self.need_session(a)
        await self.syslog.start(s, a.get("filters") or {})
        return {"running": True, "filters": self.syslog.filters}

    async def c_syslog_filter(self, a):
        await self.syslog.set_filters(a.get("filters") or {})
        return {"filters": self.syslog.filters}

    async def c_syslog_stop(self, a):
        await self.syslog.stop()
        return {"running": False}

    # pairing
    async def c_pair_status(self, a):
        recs = []
        for u in sorted(set(self.wifi) | self.lockdown):
            recs.append({"udid": u, "wifi": u in self.wifi, "usb": u in self.lockdown, "path": self.wifi.get(u, "")})
        return {"records": recs, "usb": sorted(self.usb)}

    async def c_pair_start(self, a):
        if self.pair_task and not self.pair_task.done():
            raise err("busy", "Pairing is already in progress.")
        method = a.get("method") or ("usb" if self.usb else "wifi")
        if method not in ("usb", "wifi"):
            raise err("bad-args", "Pairing method must be usb or wifi.")
        if method == "usb":
            udid = a.get("udid") or (sorted(self.usb)[0] if self.usb else None)
            if not udid or udid not in self.usb:
                raise err("unreachable", "Plug your iPhone in with a USB cable and unlock it, then try again.")
            self.pair_task = asyncio.create_task(self._pair_usb(udid))
        else:
            self.pair_task = asyncio.create_task(self._pair_wifi(float(a.get("timeout", 300))))
        return {"started": True, "method": method}

    async def c_pair_code(self, a):
        fut = self.pair_code_fut
        if not fut or fut.done():
            raise err("bad-args", "No pairing code was asked for.")
        code = re.sub(r"\D", "", str(a.get("code", "")))
        if len(code) != 6:
            raise err("bad-args", "The code has 6 digits.")
        fut.set_result(code)
        return {}

    async def c_pair_cancel(self, a):
        t = self.pair_task
        if t and not t.done():
            t.cancel()
            with contextlib.suppress(BaseException):
                await t
        return {}

    def _pair_event(self, phase, method, message, **kw):
        emit("pair", {"phase": phase, "method": method, "message": message, "code": kw.pop("code", None),
                      "udid": kw.pop("udid", None), **kw})

    async def _pair_usb(self, udid: str):
        p = self.pmd3
        m = "usb"
        try:
            self._pair_event("starting", m, "Talking to your iPhone over the cable…", udid=udid)
            lockdown = None
            try:
                lockdown = await p.create_using_usbmux(serial=udid, autopair=False)
                paired = bool(getattr(lockdown, "paired", False))
            except BaseException:  # noqa: BLE001
                paired = False
            if not paired:
                if lockdown is not None:
                    with contextlib.suppress(Exception):
                        await lockdown.close()
                self._pair_event("trust", m, "Unlock your iPhone and tap “Trust”, then enter your iPhone passcode.", udid=udid)
                lockdown = await p.create_using_usbmux(serial=udid, autopair=True, pair_timeout=120)
            self._pair_event("waiting", m, "Setting up Wi-Fi access…", udid=udid)
            path = link.record_path(p, udid)
            had = path.exists()
            try:
                svc = await p.tunnel_service.RemotePairingLockdownService.create(lockdown)
                identifier = getattr(svc, "identifier", "")
                try:
                    await svc.connect(autopair=not had)
                except p.exceptions.RemotePairingCompletedError:
                    pass
                finally:
                    with contextlib.suppress(Exception):
                        await svc.close()
            finally:
                with contextlib.suppress(Exception):
                    await lockdown.close()
            if path.exists():
                link.pin_record(path, identifier)
            self.wifi = link.wifi_records(p)
            self.lockdown = link.lockdown_records()
            self._pair_event("done", m, "Paired. Your iPhone now works over USB and Wi-Fi.", udid=udid,
                             name=self.cache.get(udid, {}).get("name", ""))
            await self.select(udid)
            self.session.reconnect_now()
        except asyncio.CancelledError:
            self._pair_event("cancelled", m, "Pairing cancelled.")
            raise
        except BaseException as exc:  # noqa: BLE001
            e = classify(exc)
            msg = e.message
            if type(exc).__name__ in ("PairingDialogResponsePendingError", "UserDeniedPairingError"):
                msg = "The iPhone didn't confirm “Trust”. Unlock it and try again."
            self._pair_event("failed", m, msg, detail=e.detail or repr(exc))

    async def _pair_wifi(self, timeout: float):
        ts = self.pmd3.tunnel_service
        m = "wifi"
        try:
            info = ts.PairableHostInfo(name=f"iOS Studio on {platform.node()}")
            self._pair_event("waiting", m, PAIR_HELP_WIFI + " Developer Mode must be on.")

            def on_pin(pin):
                self._pair_event("show-code", m, "Type this code on your iPhone", code=str(pin))

            def on_wait(elapsed):
                self._pair_event("waiting", m, PAIR_HELP_WIFI, elapsed=int(elapsed))

            result = await ts.serve_pairable_host(info, pin_callback=on_pin, timeout=timeout,
                                                  waiting_callback=on_wait)
            peer = result.peer_device
            udid = peer.udid or ""
            with contextlib.suppress(Exception):
                link.pin_record(Path(result.record_path), info.identifier)
            self.wifi = link.wifi_records(self.pmd3)
            if udid:
                self.cache.setdefault(udid, {}).update(name=peer.name or "", productType=peer.model or "",
                                                      model=marketing_name(peer.model or ""))
                with contextlib.suppress(OSError):
                    common.save_json(DEVICE_CACHE, self.cache)
            self._pair_event("done", m, "Paired over Wi-Fi.", udid=udid, name=peer.name)
            if udid:
                await self.select(udid)
                self.session.reconnect_now()
        except asyncio.CancelledError:
            self._pair_event("cancelled", m, "Pairing cancelled.")
            raise
        except (asyncio.TimeoutError, TimeoutError):
            self._pair_event("failed", m, "No iPhone started pairing. Open Settings › Developer › Paired Macs on "
                             "the iPhone while this window waits (both on the same Wi-Fi).")
        except BaseException as exc:  # noqa: BLE001
            e = classify(exc)
            self._pair_event("failed", m, e.message if e.code != "failed" else "Pairing didn't finish. Try again.",
                             detail=e.detail or repr(exc))

    # -------------------------------------------------------- dispatch

    async def handle(self, msg: dict):
        rid = msg.get("id")
        cmd = str(msg.get("cmd") or "")
        args = msg.get("args") or {}
        fn = getattr(self, "c_" + cmd.replace(".", "_").replace("-", "_"), None)
        if fn is None:
            send({"id": rid, "ok": False, "error": {"code": "bad-args", "message": f"Unknown command {cmd!r}."}})
            return
        try:
            data = await fn(args if isinstance(args, dict) else {})
            send({"id": rid, "ok": True, "data": data if data is not None else {}})
        except asyncio.CancelledError:
            send({"id": rid, "ok": False, "error": {"code": "cancelled", "message": "Cancelled."}})
        except BaseException as exc:  # noqa: BLE001
            e = classify(exc)
            if e.code == "failed" and not isinstance(exc, HelperError):
                dlog(f"{cmd} failed:\n{traceback.format_exc()}")
            if e.code == "unreachable" and not isinstance(exc, HelperError) and self.session:
                self.session.drop(f"{cmd}: {exc!r}")
            send({"id": rid, "ok": False, "error": e.to_json()})

    async def shutdown(self):
        await self.mirror_stop()
        await self.syslog.stop(emit_state=False)
        if self.pair_task:
            self.pair_task.cancel()
        if self.session:
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(self.session.close(), 5)


def stdin_reader(loop: asyncio.AbstractEventLoop, q: asyncio.Queue):
    for line in sys.stdin:
        line = line.strip()
        if line:
            loop.call_soon_threadsafe(q.put_nowait, line)
    loop.call_soon_threadsafe(q.put_nowait, None)


async def once(cmd: str, raw_args: str) -> int:
    """`iosstudio-deviced --once <cmd> '<json args>'`: open the tunnel, run one command, print one reply."""
    global ONCE, send
    ONCE = True
    logging.basicConfig(level=logging.ERROR, stream=sys.stderr)

    def reply(obj: dict) -> int:
        send(obj)
        return 0 if obj.get("ok") else 1

    try:
        args = json.loads(raw_args or "{}")
        if not isinstance(args, dict):
            raise ValueError("args must be a JSON object")
    except (json.JSONDecodeError, ValueError) as exc:
        return reply({"ok": False, "error": {"code": "bad-args", "message": "The arguments aren't valid JSON.", "detail": str(exc)}})
    try:
        pmd3 = link.load_pmd3()
    except link.ApiError as exc:
        return reply({"ok": False, "error": classify(exc).to_json()})
    d = Daemon(pmd3)
    if not hasattr(d, "c_" + cmd.replace(".", "_").replace("-", "_")):
        return reply({"ok": False, "error": {"code": "bad-args", "message": f"Unknown command {cmd!r}."}})
    d.usb = set(await link.usb_udids(pmd3))
    d.wifi = link.wifi_records(pmd3)
    d.lockdown = link.lockdown_records()
    udid = args.get("udid") or d.default_udid()
    if not udid:
        return reply({"ok": False, "error": err("no-device", "No paired iPhone was found.", PAIR_FIX).to_json()})
    if udid not in d.usb and udid not in d.wifi:
        return reply({"ok": False, "error": err("not-paired", "That iPhone isn't paired with this computer.", PAIR_FIX).to_json()})
    s = Session(d, udid)
    d.session = s
    s.start()
    out: dict = {}

    def capture(obj):
        out.update(obj)
    try:
        real_send = send
        send = capture  # handle() replies through send()
        try:
            await d.handle({"id": None, "cmd": cmd, "args": args})
        finally:
            send = real_send
    finally:
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(d.shutdown(), 6)
    out.pop("id", None)
    return reply(out or {"ok": False, "error": {"code": "failed", "message": "No reply."}})


async def amain() -> int:
    logging.basicConfig(level=logging.ERROR, stream=sys.stderr)
    logging.getLogger().addHandler(_LogBridge())
    q: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()
    threading.Thread(target=stdin_reader, args=(loop, q), daemon=True).start()
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, lambda: q.put_nowait(None))
    try:
        pmd3 = link.load_pmd3()
    except link.ApiError as exc:
        emit("ready", {"version": VERSION, "pymobiledevice3": None, "python": sys.executable, "pid": os.getpid(),
                       "error": classify(exc).to_json()})
        # Answer every request with the same error until stdin closes.
        while (line := await q.get()) is not None:
            with contextlib.suppress(json.JSONDecodeError):
                m = json.loads(line)
                send({"id": m.get("id"), "ok": False, "error": classify(exc).to_json()})
        return 0
    d = Daemon(pmd3)
    emit("ready", {"version": VERSION, "pymobiledevice3": pmd3.version, "python": sys.executable, "pid": os.getpid()})
    disc = asyncio.create_task(d.discovery())
    tasks: set[asyncio.Task] = set()

    async def pump():
        while True:
            line = await q.get()
            if line is None:
                d.stopping.set()
                return
            try:
                m = json.loads(line)
                if not isinstance(m, dict):
                    raise ValueError
            except (json.JSONDecodeError, ValueError):
                send({"event": "log", "data": {"level": "warn", "text": "ignored a malformed request line"}})
                continue
            t = asyncio.create_task(d.handle(m))
            tasks.add(t)
            t.add_done_callback(tasks.discard)

    pumper = asyncio.create_task(pump())
    await d.stopping.wait()
    pumper.cancel()
    disc.cancel()
    for t in list(tasks):
        t.cancel()
    try:
        await asyncio.wait_for(d.shutdown(), 6)
    except BaseException:  # noqa: BLE001
        pass
    return 0


def main() -> int:
    code = 0
    try:
        if len(sys.argv) >= 3 and sys.argv[1] == "--once":
            code = asyncio.run(once(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "{}"))
            return code
        return asyncio.run(amain())
    except KeyboardInterrupt:
        return 0
    finally:
        # Tunnel threads/sockets must never keep the process alive after the app quits.
        with contextlib.suppress(Exception):
            _OUT.flush()
        os._exit(code)


if __name__ == "__main__":
    main()
