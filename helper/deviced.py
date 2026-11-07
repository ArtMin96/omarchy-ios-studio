"""iosstudio-deviced: the long-running iPhone daemon, shared by the GUI and other clients.

Runs under the pymobiledevice3 venv python (common.pymd3_python()). Protocol: docs/deviced-protocol.md.
`--serve`: the per-user broker (helper/device_broker.py) on $XDG_RUNTIME_DIR/ios-studio/deviced.sock
($IOSSTUDIO_RUNTIME_DIR overrides the base); clients reach it through bin/iosstudio-deviced's stdio
bridge or helper/device_client.py.
`--once <cmd> '<json>'`: standalone one-shot (used when no daemon is running).
One tunnel to the active device stays open; it reconnects with backoff. Requests run concurrently
as asyncio tasks, so a slow request never blocks events or other requests.

This file is the core: the session (tunnel, reconnect, shared DVT and service access), the device
list, and dispatch. Commands live in feature modules under helper/deviced_features/ (loaded by
deviced_kit.load_features); see docs/development.md#device-daemon-features.
"""
from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import os
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import deviced_core as core  # noqa: E402

core.init_output()

import common  # noqa: E402
import device_broker  # noqa: E402
import device_link as link  # noqa: E402
import deviced_kit as kit  # noqa: E402
from common import HelperError, fix  # noqa: E402
from device_models import marketing_name  # noqa: E402
from deviced_core import (  # noqa: E402
    DEVICE_CACHE,
    DEVICE_STATE,
    LAST_DEVICE,
    PAIR_FIX,
    TUNNEL_WAIT_S,
    VERSION,
    battery_view,
    classify,
    dlog,
    emit,
    err,
    is_connection_error,
    send,
)

BACKOFF = [3, 5, 10, 20, 30]
HEALTH_EVERY_S = 60
DISCOVERY_EVERY_S = 3


# ---------------------------------------------------------------- device session

class Session:
    """The active device: one tunnel, reconnect with backoff, and shared access to its services.

    Feature modules reach the phone only through this object (never a second tunnel):
      await s.get_rsd()                        the connected RemoteServiceDiscoveryService
      async with s.open_service(Cls, ...)      a lockdown-over-RSD service for one call
      await s.cached_service(key, Cls, ...)    the same, kept open until the tunnel drops
      async with s.house_arrest(bundle_id)     an app's container over AFC (VendContainer/VendDocuments)
      await s.get_dvt() / s.dvt_instrument()   the shared DVT connection and cached instruments
      async with s.dvt_channel(Cls)            an instrument channel on the shared DVT connection
      async with s.dedicated_dvt()             a DVT connection of its own (long-held instruments)
      await s.port_forward(...)                a loopback TCP port piped to a service on the phone
      s.userspace_udp()                        a UDP socket on the tunnel's stack (device-initiated media)
      s.data / s.tunnel_data                   per-session / per-tunnel feature state
    """

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
        self.instruments: dict = {}        # key -> instrument entered on dvt_stack
        self.services: dict = {}           # key -> service kept open on the current tunnel
        self._svc_locks: dict = {}
        self.data: dict = {}               # feature state for this device (survives reconnects)
        self.tunnel_data: dict = {}        # feature state for the current tunnel (cleared on drop)
        self.last_connection: dict | None = None

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
        self.last_connection = {"udid": self.udid, "state": state, "transport": self.where.get("transport"),
                                "message": message, "detail": detail[-1500:], "retryIn": retry_in,
                                "since": time.time()}
        emit("connection", self.last_connection)
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
                self._after_drop()   # close() cancels us here: still close the DVT stack and services
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
        self.dvt = None
        self.instruments = {}
        stack, self.dvt_stack = self.dvt_stack, None
        if stack is not None:
            asyncio.create_task(self._close_stack(stack))
        services, self.services = self.services, {}
        for svc in services.values():
            asyncio.create_task(self._close_service(svc))
        self.tunnel_data = {}
        # A closing session (device switch, shutdown) had its resources and hooks run already, and
        # a tunnel-drop now would release the next session's tunnel resources.
        if was and not self.closing:
            self.d.on_disconnected(self)

    @staticmethod
    async def _close_stack(stack):
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(stack.aclose(), 5)

    @staticmethod
    async def _close_service(svc):
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(svc.__aexit__(None, None, None), 5)

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

    @property
    def transport(self) -> str | None:
        return self.where.get("transport") if self.rsd is not None else None

    def require_usb(self, what: str = "This"):
        """Raise needs-usb unless the current tunnel runs over the cable (e.g. pcapd)."""
        if self.where.get("transport") != "usb":
            raise err("needs-usb", f"{what} needs your iPhone plugged in with a USB cable.")

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

    # -- lockdown-over-RSD services
    @staticmethod
    async def _make(factory, *args, **kw):
        obj = factory(*args, **kw)
        if inspect.isawaitable(obj):
            obj = await obj
        return obj

    @contextlib.asynccontextmanager
    async def open_service(self, factory, *args, **kw):
        """`async with s.open_service(AfcService) as afc:` one service connection for this call.

        factory(rsd, *args, **kw) is a pymobiledevice3 service class (AfcService, PcapdService,
        DiagnosticsService, ...) or a coroutine function returning one (HouseArrestService.create).
        The object is entered as an async context manager and closed on exit.
        """
        rsd = await self.get_rsd()
        obj = await self._make(factory, rsd, *args, **kw)
        async with obj as svc:
            yield svc

    async def cached_service(self, key: str, factory, *args, **kw):
        """Like open_service, but the entered service stays open and is shared until the tunnel
        drops (or forget_service(key)). Use for services called often (e.g. AFC browsing)."""
        if key in self.services:
            return self.services[key]
        lock = self._svc_locks.setdefault(key, asyncio.Lock())
        async with lock:
            if key in self.services:
                return self.services[key]
            rsd = await self.get_rsd()
            obj = await self._make(factory, rsd, *args, **kw)
            svc = await obj.__aenter__()
            if self.rsd is not rsd:   # the tunnel changed while we opened it
                await self._close_service(obj)
                raise err("unreachable", "The connection to your iPhone dropped. Trying again…",
                          fix("Retry", "device", action="reconnect"))
            self.services[key] = svc
            return svc

    async def forget_service(self, key: str):
        """Close a cached service (e.g. after it broke); the next cached_service() opens a new one."""
        svc = self.services.pop(key, None)
        if svc is not None:
            await self._close_service(svc)

    def house_arrest(self, bundle_id: str, documents: bool = False):
        """`async with s.house_arrest("com.example.app") as afc:` an app's container (VendContainer)
        or only its Documents (documents=True, VendDocuments) as an AfcService."""
        from pymobiledevice3.services.house_arrest import HouseArrestService
        return self.open_service(HouseArrestService.create, bundle_id, documents_only=documents)

    async def service_connection(self, name: str, checkin: bool = True):
        """A raw pymobiledevice3 ServiceConnection to an RSD service (caller closes it). checkin=True
        for `*.shim.remote` lockdown services, False for developer services such as debugproxy."""
        rsd = await self.get_rsd()
        if checkin:
            return await rsd.start_lockdown_service(name)
        return await rsd.start_lockdown_service_without_checkin(name)

    @contextlib.asynccontextmanager
    async def usb_lockdown(self, autopair: bool = False):
        """A plain lockdown client over usbmux (USB only), for the few services RSD doesn't offer."""
        if self.udid not in self.d.usb:
            raise err("needs-usb", "Plug your iPhone in with a USB cable for this.")
        lockdown = await self.pmd3.create_using_usbmux(serial=self.udid, autopair=autopair)
        try:
            yield lockdown
        finally:
            with contextlib.suppress(Exception):
                await lockdown.close()

    # -- port forwarding (debugserver, WebDriverAgent, ...)
    async def port_forward(self, *, service: str | None = None, port: int | None = None,
                           checkin: bool = False, local_port: int = 0, host: str = "127.0.0.1") -> core.PortForward:
        """Listen on host:local_port (0 = any free port) and pipe each connection through this
        tunnel to an RSD service by name (service="com.apple.internal.dt.remote.debugproxy") or to a
        TCP port on the phone (port=8100). Returns a started PortForward (.port, .stop()). Hold it
        with d.resources so it closes with the client / tunnel."""
        if (service is None) == (port is None):
            raise ValueError("pass service= or port=")
        rsd = await self.get_rsd()
        if service is not None:
            rsd.get_service_port(service)   # InvalidServiceError now rather than per connection

        async def connect():
            r = await self.get_rsd()
            if service is not None:
                return await (r.start_lockdown_service(service) if checkin
                              else r.start_lockdown_service_without_checkin(service))
            return await r.create_service_connection(int(port))
        return await core.PortForward(connect, service or f"port {port}", host, local_port).start()

    # -- userspace UDP (device-initiated media, e.g. DisplayService video)
    def udp_available(self) -> bool:
        return self.rsd is not None and bool(getattr(self.pmd3.userspace_tunnel, "USERSPACE_ACTIVE", False))

    def userspace_udp(self):
        """A UDP socket on the tunnel's userspace stack (.local_ip, .port, recv, sendto, close).
        pymobiledevice3's screen_stream.open_media_receiver() picks this up by itself."""
        if not self.udp_available():
            raise err("unreachable", "Not connected to your iPhone.")
        return self.pmd3.userspace_tunnel.UserspaceUdp()

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
        """The shared DVT (instruments) connection, opened lazily (mounting the DDI if needed)."""
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

    def reset_dvt(self):
        """Forget the shared DVT connection and its instruments (it broke); the next use reopens it."""
        self.dvt = None
        self.instruments = {}
        stack, self.dvt_stack = self.dvt_stack, None
        if stack:
            asyncio.create_task(self._close_stack(stack))

    async def dvt_instrument(self, key: str, factory, *args, **kw):
        """An instrument (Screenshot, LocationSimulation, Sysmontap, ...) opened once on the shared
        DVT connection and kept until it's reset or the tunnel drops. factory(dvt, *args, **kw)."""
        dvt = await self.get_dvt()
        async with self.dvt_lock:
            inst = self.instruments.get(key)
            if inst is None:
                if self.dvt is not dvt or self.dvt_stack is None:
                    raise err("unreachable", "The connection to your iPhone dropped. Trying again…",
                              fix("Retry", "device", action="reconnect"))
                inst = await self.dvt_stack.enter_async_context(factory(dvt, *args, **kw))
                self.instruments[key] = inst
            return inst

    @contextlib.asynccontextmanager
    async def dvt_channel(self, factory, *args, **kw):
        """`async with s.dvt_channel(ProcessControl) as pc:` a channel on the shared DVT connection
        for this call only."""
        dvt = await self.get_dvt()
        async with factory(dvt, *args, **kw) as inst:
            yield inst

    @contextlib.asynccontextmanager
    async def dedicated_dvt(self):
        """A DVT connection of its own over the same tunnel, for instruments held for a long time
        (condition inducer, sysmontap) so they never block or break the shared one."""
        await self.ensure_ddi()
        rsd = await self.get_rsd()
        async with self.pmd3.DvtProvider(rsd) as dvt:
            yield dvt

    async def dvt_retry(self, fn, fail: BaseException | None = None):
        """await fn() using the shared DVT connection; if it breaks with a connection error, rebuild
        the connection once and try again."""
        for attempt in range(2):
            await self.get_dvt()
            try:
                return await fn()
            except BaseException as exc:  # noqa: BLE001
                if isinstance(exc, asyncio.CancelledError):
                    raise
                self.reset_dvt()
                if attempt or not is_connection_error(exc):
                    raise
        raise fail or err("failed", "The developer tools connection to your iPhone failed.")


# ---------------------------------------------------------------- the daemon

class Daemon:
    def __init__(self, pmd3, resources: kit.Resources | None = None):
        self.pmd3 = pmd3
        self.usb: set[str] = set()
        self.wifi: dict[str, str] = {}
        self.lockdown: set[str] = set()
        self.cache: dict = common.load_json(DEVICE_CACHE, {}) or {}
        self.session: Session | None = None
        self.disc_task: asyncio.Task | None = None
        self._last_devices = None
        self.stopping = asyncio.Event()
        self.resources = resources or kit.Resources(log=dlog)
        self.registry = kit.FeatureRegistry(log=dlog)
        self._register_core(self.registry.module("core"))
        kit.load_features(self.registry, "deviced_features")
        if self.registry.failed:
            dlog(f"features that failed to load: {', '.join(sorted(self.registry.failed))}")

    def client(self):
        """The client whose request is running (key for d.resources), None in --once mode."""
        return core.current_client()

    # -- devices
    def remember(self, udid: str, info: dict):
        keep = {k: info.get(k) for k in ("name", "productType", "model", "ios", "build") if info.get(k)}
        if keep and self.cache.get(udid) != {**self.cache.get(udid, {}), **keep}:
            self.cache[udid] = {**self.cache.get(udid, {}), **keep, "seen": time.time()}
            with contextlib.suppress(OSError):
                common.save_json(DEVICE_CACHE, self.cache)

    def write_state(self, s: "Session"):
        """~/.local/state/ios-studio/device.json: what Setup needs when the daemon isn't running."""
        if core.ONCE and s.rsd is None and not s.info:
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
            await self.resources.release_all("session", ("tunnel", "session"))
            await self.registry.run_hooks("session_end", self, old)
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
        asyncio.create_task(self._tunnel_dropped(s))

    async def _tunnel_dropped(self, s: Session):
        await self.resources.release_all("tunnel", ("tunnel",))
        await self.registry.run_hooks("tunnel_drop", self, s)

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

    # -------------------------------------------------------- core commands

    def _register_core(self, reg):
        @reg.command("daemon.ping")
        async def c_daemon_ping(d, a):
            return {"pong": True, "ts": time.time()}

        @reg.command("daemon.shutdown")
        async def c_daemon_shutdown(d, a):
            asyncio.get_running_loop().call_later(0.1, d.stopping.set)
            return {}

        @reg.command("daemon.features")
        async def c_daemon_features(d, a):
            return {**d.registry.describe(), "resources": d.resources.describe()}

        @reg.command("devices.list")
        async def c_devices_list(d, a):
            return d.device_list()

        @reg.command("device.select")
        async def c_device_select(d, a):
            udid = a.get("udid")
            if not udid:
                raise err("bad-args", "Which iPhone? (udid missing)")
            if udid not in d.usb and udid not in d.wifi and udid not in d.cache:
                raise err("not-found", "That iPhone isn't around right now.")
            await d.select(udid)
            return next((x for x in d.device_list() if x["udid"] == udid), {})

        @reg.command("device.reconnect")
        async def c_device_reconnect(d, a):
            s = d.need_session(a)
            s.reconnect_now()
            return {}

        @reg.command("device.info")
        async def c_device_info(d, a):
            s, _ = await d.rsd(a)
            if a.get("fresh") or "battery" not in s.info:
                s.info.update(await s.dynamic_info())
            d.write_state(s)
            info = dict(s.info)
            info.update(transport=s.where.get("transport"), address=s.where.get("address", ""),
                        wifiPaired=s.udid in d.wifi, usbPaired=s.udid in d.lockdown)
            info.update(d.registry.info(d, s))
            info.update(locked=s.state == "locked")
            return info

        reg.commit()

    # -------------------------------------------------------- dispatch

    async def dispatch(self, cmd: str, args: dict):
        fn = self.registry.lookup(cmd)
        if fn is None:
            raise err("bad-args", f"Unknown command {cmd!r}.")
        return await fn(self, args if isinstance(args, dict) else {})

    def error_json(self, exc: BaseException, cmd: str) -> dict:
        e = classify(exc)
        if e.code == "failed" and not isinstance(exc, HelperError):
            dlog(f"{cmd} failed:\n{''.join(traceback.format_exception(exc))}")
        if e.code == "unreachable" and not isinstance(exc, HelperError) and self.session:
            self.session.drop(f"{cmd}: {exc!r}")
        return e.to_json()

    async def handle(self, msg: dict) -> dict:
        """--once mode: one request -> its reply object."""
        rid = msg.get("id")
        cmd = str(msg.get("cmd") or "")
        try:
            data = await self.dispatch(cmd, msg.get("args") or {})
            return {"id": rid, "ok": True, "data": data if data is not None else {}}
        except asyncio.CancelledError:
            return {"id": rid, "ok": False, "error": {"code": "cancelled", "message": "Cancelled."}}
        except BaseException as exc:  # noqa: BLE001
            return {"id": rid, "ok": False, "error": self.error_json(exc, cmd)}

    # -------------------------------------------------------- broker backend (device_broker.py)

    async def welcome(self) -> list:
        out = [("ready", {"version": VERSION, "pymobiledevice3": self.pmd3.version, "python": sys.executable,
                          "pid": os.getpid()}),
               ("devices", self.device_list())]
        s = self.session
        if s is not None:
            if s.last_connection:
                out.append(("connection", s.last_connection))
            if s.info:
                out.append(("deviceInfo", {"udid": s.udid, **s.info, **self.registry.info(self, s)}))
        out.extend(await self.registry.welcome(self))
        return out

    def _syslog(self):
        phone = self.registry.provided.get("syslog")
        if phone is None:
            raise err("failed", "The live log isn't available (its module failed to load; see deviced.log).")
        return phone

    def syslog_target(self, args: dict) -> str:
        self._syslog()
        return self.need_session(args).udid

    def syslog_open(self, pid: int):
        self._syslog().open(self.session, pid)

    async def syslog_close(self):
        phone = self.registry.provided.get("syslog")
        if phone is not None:
            await phone.close()

    async def syslog_resolve(self, bid: str):
        """bundleId filter -> (executable, installed bundle id). Never waits for the tunnel."""
        return await self._syslog().resolve(self.session, bid)

    async def client_gone(self, client):
        await self.registry.run_hooks("client_gone", self, client)

    async def shutdown(self):
        self.stopping.set()
        if self.disc_task:
            self.disc_task.cancel()
        await self.resources.release_all("shutdown")
        await self.registry.run_hooks("shutdown", self)
        if self.session:
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(self.session.close(), 5)


class BrokenBackend:
    """--serve when pymobiledevice3 can't be loaded: every request answers with the same error."""

    def __init__(self, exc: BaseException):
        self.e = classify(exc)

    async def welcome(self):
        return [("ready", {"version": VERSION, "pymobiledevice3": None, "python": sys.executable, "pid": os.getpid(),
                           "error": self.e.to_json()})]

    async def dispatch(self, cmd, args):
        if cmd == "daemon.ping":
            return {"pong": True, "ts": time.time()}
        raise self.e

    def error_json(self, exc, cmd):
        return classify(exc).to_json()

    def syslog_target(self, args):
        raise self.e

    def syslog_open(self, pid):
        pass

    async def syslog_close(self):
        pass

    async def syslog_resolve(self, bid):
        return None, bid

    async def shutdown(self):
        pass


async def once(cmd: str, raw_args: str) -> int:
    """`iosstudio-deviced --once <cmd> '<json args>'`: open the tunnel, run one command, print one reply."""
    core.ONCE = True
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
    if not d.registry.has(cmd):
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
    try:
        out = await d.handle({"id": None, "cmd": cmd, "args": args})
    finally:
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(d.shutdown(), 6)
    out.pop("id", None)
    return reply(out or {"ok": False, "error": {"code": "failed", "message": "No reply."}})


def _other_real_daemon() -> str | None:
    """With IOSSTUDIO_RUNTIME_DIR set (parallel worktrees, tests), a second real daemon would open a
    second tunnel and knock out the phone link of the user's own daemon. Refuse while that one is up."""
    if not os.environ.get("IOSSTUDIO_RUNTIME_DIR") or os.environ.get("IOSSTUDIO_ALLOW_SECOND_TUNNEL") == "1":
        return None
    import device_client  # the default base, as if IOSSTUDIO_RUNTIME_DIR weren't set
    base = device_client.xdg_runtime_dir() or f"/tmp/ios-studio-{os.getuid()}"
    default = Path(base) / "ios-studio" / "deviced.sock"
    if default != device_broker.paths(None)["sock"] and device_broker.socket_alive(default):
        return str(default)
    return None


async def serve() -> int:
    """`iosstudio-deviced --serve`: the shared daemon (normally started detached by the bridge)."""
    broker = device_broker.Broker(demo=None)
    if not broker.acquire():
        return 0   # another daemon is live; its socket serves everybody
    broker.redirect_stderr()
    core.close_output()   # nothing is written to the original stdout in this mode
    core.BROKER = broker
    logging.basicConfig(level=logging.ERROR, stream=sys.stderr)
    logging.getLogger().addHandler(core.LogBridge())

    async def make(_broker):
        other = _other_real_daemon()
        if other:
            dlog(f"refusing to open a second tunnel: the daemon on {other} owns the iPhone")
            return BrokenBackend(err("busy", "Another iOS Studio device service already owns the iPhone; a second "
                                     "one would break its connection. Use demo mode (IOSSTUDIO_DEMO=1) here.",
                                     None, other))
        try:
            pmd3 = await asyncio.to_thread(link.load_pmd3)
        except link.ApiError as exc:
            dlog(f"pymobiledevice3 unusable: {exc!r}")
            return BrokenBackend(exc)
        d = Daemon(pmd3, broker.resources)
        d.disc_task = asyncio.create_task(d.discovery())
        dlog(f"pymobiledevice3 {pmd3.version} loaded; features: {', '.join(d.registry.loaded)}")
        return d
    return await broker.run(make)


def main() -> int:
    code = 0
    try:
        if len(sys.argv) >= 3 and sys.argv[1] == "--once":
            code = asyncio.run(once(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "{}"))
            return code
        if len(sys.argv) >= 2 and sys.argv[1] == "--serve":
            code = asyncio.run(serve())
            return code
        print("usage: deviced.py --serve | --once <cmd> '<json args>'  (normally run by bin/iosstudio-deviced)",
              file=sys.stderr)
        code = 2
        return code
    except KeyboardInterrupt:
        return 0
    finally:
        # Tunnel threads/sockets must never keep the process alive after the app quits.
        core.flush_output()
        os._exit(code)


if __name__ == "__main__":
    main()
