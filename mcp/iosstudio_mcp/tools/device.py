"""iPhone and app tools, through the shared device daemon (helper/device_client.py).

The MCP server is one more client of the per-user device broker that the iOS Studio window also
uses, so both share one tunnel to the phone. The client connects lazily, on the first tool that
needs the phone; ios_status only attaches when a daemon is already running. This module also fills
the AppState device slots (install_app, launch_app, device_udid, device_status) used by ios_run,
renewals and ios_status.

Other modules call the phone with `await dcall(state, "cmd", {...})`, which turns a DeviceError into
a ToolError naming the next tool to call.
"""
from __future__ import annotations

import asyncio
import base64
import itertools
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, ImageContent, TextContent
from pydantic import Field

from .. import budget as B
from .. import helper
from ..jobs import Job
from ..models import (
    ActionResult,
    AppBrief,
    AppInstallResult,
    AppLaunchResult,
    AppList,
    DeviceDetails,
    DeviceEntry,
    DeviceList,
    DevicePrepare,
    LocationResult,
    ProcessInfo,
    ProcessList,
    ScreenshotInfo,
)
from ..state import AppState, get_state, resolve_project
from . import MAX_WAIT, READ_REMOTE, ann, dry_run, fit_page, page_of, tool
from .run_presets import PresetName, merge_launch, resolve_preset

log = logging.getLogger("iosstudio.device")

Udid = Annotated[str | None, Field(description="iPhone udid; default: the active iPhone (see ios_device_list)")]
BundleId = Annotated[str, Field(description="Bundle id, e.g. 'com.example.myapp'. The installed "
                                            "'XTL-<TEAM>.com.example.myapp' form also works")]

# Device-wide events the MCP client wants. Not 'frame' (mirror images) or icons: nobody here reads them.
EVENTS = ["ready", "devices", "connection", "deviceInfo", "installProgress", "transfer", "cameratest"]


# ------------------------------------------------------------------ errors

def _next_for(code: str, fix: dict | None) -> str | None:
    pair = ("Pairing is a human step: ask the user to connect the iPhone by cable, unlock it and tap Trust "
            "(or pair over Wi-Fi in iOS Studio): ios_open_app(page='device'). Then ios_device_list.")
    table = {
        "no-device": pair, "not-paired": pair, "not-trusted": pair, "device-not-found": pair,
        "unreachable": "Ask the user to wake and unlock the iPhone and keep it on the same Wi-Fi (or plug it in), "
                       "then ios_device_reconnect and retry.",
        "locked": "Ask the user to unlock the iPhone, then retry.",
        "developer-mode-off": "Call ios_device_prepare: it tells the user how to turn on Developer Mode.",
        "ddi": "Call ios_device_prepare(mount=true); mounting the developer disk image needs internet.",
        "busy": "Another operation (e.g. pairing in the iOS Studio window) is running. Wait a moment and retry.",
        "pmd3-api": "The iPhone tools need an update: ios_toolchain_check, then ios_toolchain_install.",
        "needs-setup": "The iPhone tools aren't installed: ios_toolchain_check, then ios_toolchain_install.",
        "app-limit": "Free Apple IDs allow 3 of your own apps per iPhone. Show ios_app_list(scope='own') to the "
                     "user and, only with their OK, remove one with ios_app_uninstall.",
        "device-not-registered": "Install once over USB so the iPhone is registered: plug it in and run "
                                 "ios_run(project_path, via='usb').",
        "no-space": "The iPhone is out of storage. Ask the user to free some space.",
        "incompatible": "The app's minimum iOS or architecture doesn't match this iPhone (ios_device_info shows its "
                        "iOS version).",
        "bad-signature": "Re-sign the app: ios_run(project_path) or ios_project_renew.",
        "bad-package": "The file isn't a valid signed .ipa or .app. Build one with ios_build_start(action='ipa').",
        "timeout": "The iPhone was slow to answer. Make sure it's awake and unlocked, then retry "
                   "(ios_device_list shows its connection).",
        "disconnected": "The device service restarted. Retry the call.",
        "daemon-unavailable": "The iPhone service couldn't start. Run ios_toolchain_check (pymobiledevice3 step), "
                              "or ask the user to open iOS Studio: ios_open_app(page='device').",
        "socket-path-too-long": "Set IOSSTUDIO_RUNTIME_DIR to a shorter folder (socket paths max out at 107 bytes), "
                                "then restart the MCP server.",
        # UI inspection (ui.* in docs/deviced-protocol.md)
        "no-element": "No element has that label or id. Use one of the close labels above, or call ios_ui_tree "
                      "(filter=...) to see what's on screen now.",
        "ambiguous-label": "Several elements match. Press one by element_id (listed above), or pass the full "
                           "label with match='exact'.",
        "stale-element": "The screen changed since that id was read. Call ios_ui_tree again and use a fresh id.",
        "press-failed": "The iPhone didn't activate that element (some controls ignore accessibility presses). "
                        "Try its parent cell or a nearby button from ios_ui_tree, and check with ios_screenshot.",
        "bad-args": "Check the arguments named in the message (ios_ui_press takes exactly one of element_id or label).",
        "ax-unavailable": "The iPhone's accessibility inspection service isn't available. Make sure the iPhone is "
                          "unlocked with Developer Mode on (ios_device_prepare), then ios_device_reconnect and retry.",
        "wrong-app": "That app isn't the one in front (or isn't installed or running). Launch it with "
                     "ios_app_launch(bundle_id) first (ios_app_list shows what's installed), then retry.",
        "no-app-in-front": "No app was detected in front. Open the app with ios_app_launch(bundle_id) first, or pass "
                           "bundle_id, then retry.",
    }
    if code in table:
        return table[code]
    page = (fix or {}).get("page")
    if page == "device":
        return "Ask the user to check the iPhone in iOS Studio: ios_open_app(page='device')."
    return helper.NEXT_BY_PAGE.get(page) if page else None


def device_tool_error(exc: Exception, not_found: str | None = None) -> ToolError:
    code = str(getattr(exc, "code", "failed") or "failed")
    msg = str(getattr(exc, "message", None) or exc).strip()
    nxt = not_found if code == "not-found" and not_found else _next_for(code, getattr(exc, "fix", None))
    detail = getattr(exc, "detail", None)
    out = f"{msg} [{code}]"
    if detail and code in ("failed", "install-failed", "ddi", "bad-package", "daemon-unavailable"):
        out += f" Detail: {str(detail).strip()[:300]}"
    elif detail and code in ("no-element", "ambiguous-label", "wrong-app", "press-failed", "ax-unavailable"):
        if isinstance(detail, (list, tuple)):
            detail = "; ".join(str(x) for x in detail)
        out += f" Detail: {str(detail).strip()[:250]}"
    if nxt:
        out += f" Next: {nxt}"
    return ToolError(out)


# ------------------------------------------------------------------ the shared client

def _hub(state: AppState) -> dict:
    return state.extras.setdefault("device", {"lock": asyncio.Lock(), "connection": None, "info": {},
                                              "listeners": []})


def _socket_exists() -> bool:
    _ensure_runtime_dir()
    try:
        return helper.device_client_module().socket_path().exists()
    except Exception:  # noqa: BLE001
        return False


def _ensure_runtime_dir() -> None:
    """The daemon's socket lives in $XDG_RUNTIME_DIR. Agents often start MCP servers with a trimmed
    environment; without this the server would start a second daemon under /tmp instead of sharing the
    one the iOS Studio window uses."""
    if not os.environ.get("XDG_RUNTIME_DIR"):
        run = Path(f"/run/user/{os.getuid()}")
        if run.is_dir() and os.access(run, os.W_OK):
            os.environ["XDG_RUNTIME_DIR"] = str(run)


def new_client(name: str, events: list[str] | None):
    _ensure_runtime_dir()
    mod = helper.device_client_module()
    return mod.DeviceClient(name=name, events=events, connect_timeout=40.0)


async def client(state: AppState):
    """The server's DeviceClient, connected (spawning the daemon if needed)."""
    hub = _hub(state)
    async with hub["lock"]:
        if state.device is None:
            dev = new_client("mcp", EVENTS)

            def on_event(name: str, data: Any) -> None:
                if name == "connection" and isinstance(data, dict):
                    hub["connection"] = data
                elif name == "deviceInfo" and isinstance(data, dict):
                    hub["info"].update({k: v for k, v in data.items()})
                for cb in list(hub["listeners"]):
                    cb(name, data)

            dev.on_event(on_event)
            state.device = dev
        if not state.device.connected:
            try:
                await state.device.connect()
            except Exception as exc:  # noqa: BLE001
                raise device_tool_error(exc) from None
            log.info("connected to the device daemon (pid %s)", (state.device.hello or {}).get("pid"))
        return state.device


async def dcall(state: AppState, cmd: str, args: dict | None = None, *, timeout: float = 60.0,
                udid: str | None = None, not_found: str | None = None) -> Any:
    """One device request. DeviceError -> ToolError with the next step."""
    dev = await client(state)
    a = dict(args or {})
    if udid:
        a["udid"] = udid
    try:
        return await dev.request(cmd, a, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        if exc.__class__.__name__ == "DeviceError":
            raise device_tool_error(exc, not_found) from None
        raise


async def apps(state: AppState, udid: str | None = None, system: bool = False) -> list[dict]:
    data = await dcall(state, "apps.list", {"system": system, "icons": False, "sizes": False}, udid=udid, timeout=40)
    return data if isinstance(data, list) else []


def match_app(items: list[dict], want: str) -> dict | None:
    """Free teams install com.example.app as XTL-<TEAM>.com.example.app; match either form (as the GUI does)."""
    want = want.strip()
    for a in items:
        if a.get("bundleId") == want:
            return a
    plain = want.split(".", 1)[-1] if want.startswith("XTL-") else want
    for a in items:
        b = str(a.get("bundleId") or "")
        if b == plain or b.endswith("." + plain):
            return a
    low = want.lower()
    for a in items:   # last resort: the app's display name
        if str(a.get("name", "")).lower() == low:
            return a
    return None


async def resolve_app(state: AppState, bundle_id: str, udid: str | None = None) -> dict:
    items = await apps(state, udid)
    a = match_app(items, bundle_id)
    if a is None:
        own = ", ".join(str(x.get("bundleId")) for x in items if x.get("developer"))[:400] or "none"
        raise ToolError(f"No app '{bundle_id}' is installed on the iPhone. Your own apps there: {own}. "
                        "Install it with ios_run(project_path) or ios_app_install(path).")
    return a


# ------------------------------------------------------------------ slots for ios_run / ios_status

async def install_ipa(state: AppState, udid: str | None, path: str, on_progress=None) -> dict:
    """app.install with installProgress forwarded to on_progress(percent, status)."""
    hub = _hub(state)

    def listener(name: str, data: Any) -> None:
        if name == "installProgress" and isinstance(data, dict) and on_progress and \
                (not data.get("path") or data.get("path") == path):
            try:
                r = on_progress(float(data.get("percent") or 0), str(data.get("status") or ""))
                if asyncio.iscoroutine(r):
                    asyncio.ensure_future(r)
            except Exception:  # noqa: BLE001
                pass

    await client(state)
    hub["listeners"].append(listener)
    try:
        return await dcall(state, "app.install", {"path": path}, udid=udid, timeout=600) or {}
    finally:
        hub["listeners"].remove(listener)


async def _install_for_job(state: AppState, udid: str, ipa: str, job: Job) -> dict:
    def prog(pct: float, status: str) -> None:
        job.set_step("install", "Installing", "running", detail=f"{status or 'installing'} {pct:.0f}%", owner="mcp")
        job.progress_detail = f"Installing on the iPhone: {pct:.0f}%"
        job.touch()
    res = await install_ipa(state, udid, ipa, prog)
    job.add_line(f"Installed {res.get('bundleId') or ipa} on the iPhone in {res.get('seconds') or '?'} s")
    if res.get("bundleId") and job.result is not None:
        job.result["installedBundleId"] = res["bundleId"]
    return res


class AppInstallStatus(AppInstallResult):
    """AppInstallResult plus the state of the background install (ios_app_install, ios_app_run_and_capture)."""
    state: Literal["running", "succeeded"] = Field("succeeded", description="running: still installing; call "
                                                   "the same tool again with install_id")
    install_id: str | None = Field(None, description="Pass back as install_id to wait for this install")
    progress: float | None = Field(None, description="0-1 while installing")
    progress_detail: str | None = None
    elapsed_seconds: float | None = None


@dataclass
class InstallJob:
    """One app.install running in this server, so a tool can return before it ends (installs over Wi-Fi
    can take minutes) and a later call can wait for it."""
    id: str
    pkg: str
    started: float = field(default_factory=time.time)
    task: asyncio.Task | None = None
    percent: float | None = None
    status: str = ""
    result: dict | None = None
    error: BaseException | None = None
    changed: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def finished(self) -> bool:
        return self.task is not None and self.task.done()


_install_ids = itertools.count(1)
INSTALLS_KEPT = 10


def _installs(state: AppState) -> dict[str, InstallJob]:
    return state.extras.setdefault("device.installs", {})


def start_install(state: AppState, udid: str | None, pkg: str, project: dict | None = None) -> InstallJob:
    """Start app.install as a background task of this server (it keeps going if the tool call returns or the
    client gives up), recording the install on the project when it ends."""
    jobs = _installs(state)
    for k in [k for k, j in jobs.items() if j.finished][:max(0, len(jobs) - INSTALLS_KEPT)]:
        jobs.pop(k, None)
    job = InstallJob(id=f"i{int(time.time()) % 100000}-{next(_install_ids)}", pkg=pkg)

    def prog(pct: float, status: str) -> None:
        job.percent, job.status = pct, status
        job.changed.set()

    async def run() -> None:
        try:
            u = udid or await state.active_udid()
            job.result = await install_ipa(state, udid, pkg, prog)
            if project:
                try:
                    await helper.call("projects", "record-install", {"path": project["path"], "udid": u,
                                                                     "via": "wifi", "ipa": pkg})
                except ToolError:
                    pass
        except BaseException as exc:  # noqa: BLE001 - kept for the status
            job.error = exc
            if isinstance(exc, asyncio.CancelledError):
                raise
        finally:
            job.changed.set()

    jobs[job.id] = job
    job.task = asyncio.create_task(run(), name=f"install-{job.id}")
    return job


def get_install(state: AppState, install_id: str) -> InstallJob:
    job = _installs(state).get(install_id.strip())
    if job is None:
        known = ", ".join(_installs(state)) or "none"
        raise ToolError(f"Unknown install_id '{install_id}' (known here: {known}). Ids live only while this MCP "
                        "server runs; start the install again.")
    return job


async def wait_install(job: InstallJob, seconds: float, on_progress=None) -> None:
    """Wait up to `seconds` (≤ MAX_WAIT) for the install, calling on_progress(percent, status) as it moves."""
    deadline = time.monotonic() + max(0.0, min(seconds, MAX_WAIT))
    while not job.finished:
        left = deadline - time.monotonic()
        if left <= 0:
            break
        job.changed.clear()
        try:
            await asyncio.wait_for(job.changed.wait(), min(left, 5.0))
        except asyncio.TimeoutError:
            pass
        if on_progress and job.percent is not None:
            try:
                await on_progress(job.percent, job.status)
            except Exception:  # noqa: BLE001
                pass


def install_status(job: InstallJob, next_step: str | None = None) -> AppInstallStatus:
    """The install's result, or state='running'; raises the install's error if it failed."""
    if not job.finished:
        pct = job.percent
        return AppInstallStatus(
            installed=False, state="running", install_id=job.id, path=helper.tilde(job.pkg),
            progress=round(pct / 100, 3) if pct is not None else None,
            progress_detail=f"{job.status or 'installing'} {pct:.0f}%" if pct is not None else "starting",
            elapsed_seconds=round(time.time() - job.started, 1),
            next_steps=[next_step or f"Still installing. Call ios_app_install(install_id='{job.id}', "
                                     "wait_seconds=20) again to wait for it."])
    if job.error is not None:
        if isinstance(job.error, ToolError):
            raise ToolError(str(job.error))
        raise ToolError(f"Installing {helper.tilde(job.pkg)} failed: {job.error}")
    res = job.result or {}
    bid = res.get("bundleId")
    return AppInstallStatus(installed=True, state="succeeded", install_id=job.id, bundle_id=bid,
                            package_bundle_id=res.get("packageBundleId"), path=helper.tilde(job.pkg),
                            seconds=res.get("seconds"), elapsed_seconds=round(time.time() - job.started, 1),
                            next_steps=[f"Launch it and see what happens: ios_app_run_and_capture(bundle_id='{bid}')."
                                        if bid else "Launch it with ios_app_launch."])


async def launch(state: AppState, udid: str | None, bundle_id: str, args: list[str] | None = None,
                 env: dict[str, str] | None = None, console: bool = False) -> dict:
    app = await resolve_app(state, bundle_id, udid)
    a: dict = {"bundleId": app["bundleId"]}
    if console:
        a["console"] = True    # capture stdout/stderr for ios_app_console_read (tools/console.py)
    if args:
        a["args"] = args
    if env:
        a["env"] = env
    res = await dcall(state, "app.launch", a, udid=udid, timeout=60) or {}
    res.setdefault("bundleId", app["bundleId"])
    return res


async def _active(state: AppState, connect: bool) -> dict | None:
    if state.device is None or not state.device.connected:
        if not connect and not _socket_exists():
            return None
    devs = await dcall(state, "devices.list", timeout=10)
    return next((d for d in devs or [] if d.get("active")), None)


async def _device_udid(state: AppState) -> str | None:
    d = await _active(state, connect=False)
    return d.get("udid") if d else None


async def _device_status(state: AppState) -> dict | None:
    d = await _active(state, connect=False)
    if not d:
        return None
    hub = _hub(state)
    conn = hub.get("connection") or {}
    info = hub.get("info") or {}
    if conn.get("udid") not in (None, d.get("udid")):
        conn = {}
    return {"udid": d.get("udid"), "name": d.get("name"), "model": d.get("model"), "iosVersion": d.get("ios"),
            "connection": conn.get("state") or d.get("state"), "transport": d.get("transport"),
            "developerMode": info.get("developerMode") if info.get("udid") in (None, d.get("udid")) else None,
            "updatedAt": time.time()}


async def on_startup(state: AppState) -> None:
    state.install_app = lambda udid, ipa, job: _install_for_job(state, udid, ipa, job)
    state.launch_app = lambda udid, bundle_id, args=None, env=None: launch(state, udid, bundle_id, args, env,
                                                                            console=True)
    state.device_udid = lambda: _device_udid(state)
    state.device_status = lambda: _device_status(state)


async def on_shutdown(state: AppState) -> None:
    dev, state.device = state.device, None
    if dev is not None:
        try:
            await asyncio.wait_for(dev.close(), 5)
        except Exception:  # noqa: BLE001
            pass


# ------------------------------------------------------------------ formatting

def _entry(d: dict) -> DeviceEntry:
    return DeviceEntry(udid=str(d.get("udid")), name=d.get("name") or None, model=d.get("model") or None,
                       ios_version=d.get("ios") or None, state=d.get("state"), transport=d.get("transport"),
                       usb_paired=bool(d.get("usbPaired")), wifi_paired=bool(d.get("wifiPaired")),
                       active=bool(d.get("active")))


def _gb(v: Any) -> float | None:
    try:
        return round(float(v) / 1e9, 1)
    except (TypeError, ValueError):
        return None


def _app(a: dict) -> AppBrief:
    return AppBrief(name=str(a.get("name") or a.get("bundleId")), bundle_id=str(a.get("bundleId")),
                    version=a.get("version") or None, developer=bool(a.get("developer")),
                    running=bool(a.get("running")), pid=a.get("pid"))


def _check_package(raw: str) -> str:
    p = helper.expand(raw)
    if p.suffix.lower() not in (".ipa", ".app"):
        raise ToolError(f"{helper.tilde(str(p))} isn't an .ipa or .app. Build a signed one with "
                        "ios_build_start(action='ipa'), or pass project_path.")
    if helper.demo_mode():
        return str(p)
    if not p.exists():
        raise ToolError(f"No file at {helper.tilde(str(p))}.")
    if p.suffix.lower() == ".app" and not p.is_dir():
        raise ToolError(f"{helper.tilde(str(p))} should be an .app folder.")
    if not helper.inside_home(p):
        raise ToolError("The app must be inside your home folder (or a folder in IOSSTUDIO_MCP_ALLOW_PATHS).")
    return str(p)


async def package_for(state: AppState, path: str | None, project_path: str | None) -> tuple[str, dict | None]:
    """(.ipa/.app path, project entry) from an explicit path or a project's last signed build."""
    if path:
        return _check_package(path), None
    if not project_path:
        raise ToolError("Pass path (a signed .ipa or .app) or project_path.")
    proj = await resolve_project(state, project_path)
    info = proj if proj.get("ipa") else await helper.call("projects", "info", {"path": proj["path"]})
    ipa = info.get("ipa")
    if not ipa:   # a build from this session, else the newest build in the history that produced one
        job = state.jobs.latest_for(proj["path"])
        ipa = (job.result or {}).get("ipa") if job and job.state == "succeeded" else None
    if not ipa:
        from .build import _history
        ipa = next((b.get("ipa") for b in await _history(proj["path"], 20) if b.get("ipa") and b.get("result") == "ok"),
                   None)
    if not ipa:
        raise ToolError(f"{proj.get('name')} has no signed build yet. Run ios_build_start(project_path, action='ipa') "
                        "or ios_run(project_path).")
    return _check_package(ipa), proj


# ------------------------------------------------------------------ screenshots

# ------------------------------------------------------------------ tools

def register(mcp: MCPServer) -> None:

    # -------------------------------------------------------------- devices

    @tool(mcp, annotations=READ_REMOTE("List iPhones"))
    async def ios_device_list(ctx: Context) -> DeviceList:
        """iPhones this computer knows (paired over USB and/or Wi-Fi), which one is active, and its
        connection state (connected / locked / unreachable / not-paired…).

        Use it when a device tool fails or before choosing a udid. Every other device tool defaults to
        the active iPhone; switch with ios_device_select. Pairing a new phone is a human step
        (ios_open_app(page='device')).
        """
        state = get_state(ctx)
        devs = await dcall(state, "devices.list", timeout=15) or []
        conn = _hub(state).get("connection") or {}
        active = next((d for d in devs if d.get("active")), None)
        nxt = []
        if not devs:
            nxt.append("No iPhone is paired. Human step: ask the user to plug it in, unlock it and tap Trust "
                       "(ios_open_app(page='device')).")
        elif active is None:
            nxt.append("No iPhone is active: ios_device_select(udid).")
        elif (conn.get("state") or active.get("state")) not in ("connected",):
            nxt.append(f"The iPhone is {conn.get('state') or active.get('state')}: {conn.get('message') or ''} "
                       "Ask the user to wake/unlock it, then ios_device_reconnect.".strip())
        else:
            nxt.append("Connected. ios_app_list shows installed apps; ios_app_run_and_capture launches one and "
                       "reports logs and crashes.")
        return DeviceList(devices=[_entry(d) for d in devs], active_udid=active.get("udid") if active else None,
                          connection=conn.get("message") if conn else None, next_steps=nxt)

    @tool(mcp, annotations=READ_REMOTE("iPhone details"))
    async def ios_device_info(
        ctx: Context,
        udid: Udid = None,
        fresh: Annotated[bool, Field(description="Re-read battery, storage and disk-image state from the phone "
                                                 "(slower)")] = False,
    ) -> DeviceDetails:
        """Details of the iPhone: model, iOS version, battery, free storage, Developer Mode, developer disk
        image (DDI), lock state and pairing. Use before installing (iOS version / space) or when launches
        fail (Developer Mode off or DDI not mounted: call ios_device_prepare)."""
        state = get_state(ctx)
        d = await dcall(state, "device.info", {"fresh": fresh}, udid=udid, timeout=45) or {}
        bat = d.get("battery") or {}
        sto = d.get("storage") or {}
        conn = _hub(state).get("connection") or {}
        nxt = []
        if d.get("developerMode") is False or d.get("ddiMounted") is False:
            nxt.append("Not ready to launch apps: call ios_device_prepare.")
        if d.get("locked"):
            nxt.append("The iPhone is locked; ask the user to unlock it.")
        return DeviceDetails(udid=str(d.get("udid")), name=d.get("name"), model=d.get("model"),
                             product_type=d.get("productType"), ios_version=d.get("ios"), build=d.get("build"),
                             transport=d.get("transport"),
                             connection=conn.get("state") if conn.get("udid") == d.get("udid") else None,
                             battery_percent=bat.get("level"), charging=bat.get("charging"),
                             storage_free_gb=_gb(sto.get("free")), storage_total_gb=_gb(sto.get("total")),
                             developer_mode=d.get("developerMode"), ddi_mounted=d.get("ddiMounted"),
                             locked=d.get("locked"), wifi_paired=d.get("wifiPaired"), usb_paired=d.get("usbPaired"),
                             next_steps=nxt)

    @tool(mcp, annotations=ann("Select iPhone", read_only=False, idempotent=True, open_world=True))
    async def ios_device_select(
        ctx: Context,
        udid: Annotated[str, Field(description="udid from ios_device_list")],
    ) -> DeviceList:
        """Make another paired iPhone the active one; device tools, ios_run and the iOS Studio window
        then use it. Shared with the window (last choice wins). Returns the updated device list."""
        state = get_state(ctx)
        await dcall(state, "device.select", {"udid": udid}, timeout=30,
                    not_found="Call ios_device_list for valid udids.")
        return await ios_device_list(ctx)

    @tool(mcp, annotations=ann("Reconnect iPhone", read_only=False, idempotent=True, open_world=True))
    async def ios_device_reconnect(ctx: Context, udid: Udid = None) -> DeviceList:
        """Drop and reopen the connection to the iPhone now (skips the retry back-off). Use after the user
        woke/unlocked the phone or joined the same Wi-Fi, or when calls time out. Returns the device list
        a few seconds later; poll ios_device_list if it still says connecting."""
        state = get_state(ctx)
        await dcall(state, "device.reconnect", udid=udid, timeout=20)
        for _ in range(16):
            await asyncio.sleep(0.5)
            if (_hub(state).get("connection") or {}).get("state") in ("connected", "locked", "not-paired",
                                                                        "untrusted", "error"):
                break
        return await ios_device_list(ctx)

    @tool(mcp, annotations=ann("Prepare iPhone for development", read_only=False, idempotent=True, open_world=True))
    async def ios_device_prepare(
        ctx: Context,
        udid: Udid = None,
        mount: Annotated[bool, Field(description="Mount the developer disk image if it isn't (downloads it from "
                                                 "Apple; needs internet). false = only report")] = True,
        reveal_developer_mode: Annotated[bool, Field(description="If Developer Mode is off, make its switch appear "
                                                                 "in the phone's Settings (USB only)")] = True,
    ) -> DevicePrepare:
        """Check and fix what launching apps, logs and location need: Developer Mode on, and the developer
        disk image (DDI) mounted. Mounts the DDI itself; Developer Mode can only be turned on by the human
        on the phone, so `human_steps` lists exactly what to tell them. Call it when a launch fails with
        developer-mode-off or ddi, or before the first run on a new iPhone."""
        state = get_state(ctx)
        actions, human, nxt = [], [], []
        dm = (await dcall(state, "devmode.status", udid=udid, timeout=30) or {}).get("enabled")
        if dm is False and reveal_developer_mode:
            try:
                await dcall(state, "devmode.reveal", udid=udid, timeout=30)
                actions.append("Made the Developer Mode switch visible in Settings.")
            except ToolError as exc:
                actions.append(f"Couldn't reveal the Developer Mode switch ({str(exc)[:120]}); it may need a USB cable.")
        if dm is False:
            human += ["On the iPhone: Settings > Privacy & Security > Developer Mode, turn it on.",
                      "The iPhone restarts; after it boots, unlock it and tap Turn On, then enter the passcode.",
                      "Then call ios_device_prepare again."]
        ddi = None
        if dm is not False:
            ddi = (await dcall(state, "ddi.status", udid=udid, timeout=30) or {}).get("mounted")
            if ddi is False and mount:
                await ctx.report_progress(10, 100, message="Mounting the developer disk image")
                ddi = bool((await dcall(state, "ddi.mount", udid=udid, timeout=300) or {}).get("mounted"))
                actions.append("Mounted the developer disk image." if ddi else "Tried to mount the disk image.")
            elif ddi is False:
                nxt.append("Call again with mount=true to mount the developer disk image (needs internet).")
        ready = dm is True and ddi is True
        if ready:
            nxt.append("Ready: ios_app_launch, ios_app_run_and_capture and ios_logs_start will work.")
        return DevicePrepare(ready=ready, developer_mode=dm, ddi_mounted=ddi, actions=actions, human_steps=human,
                             next_steps=nxt)

    # -------------------------------------------------------------- apps

    @tool(mcp, annotations=READ_REMOTE("List installed apps"))
    async def ios_app_list(
        ctx: Context,
        scope: Annotated[Literal["own", "all"], Field(description="own: development-signed apps (yours, e.g. built "
                                                                  "here); all: every user app")] = "own",
        include_system: Annotated[bool, Field(description="With scope='all', include Apple's system apps")] = False,
        query: Annotated[str | None, Field(description="Case-insensitive filter on name or bundle id")] = None,
        offset: Annotated[int, Field(ge=0, description="Skip this many apps (pagination)")] = 0,
        limit: Annotated[int, Field(ge=1, le=200, description="Max apps to return")] = 50,
        udid: Udid = None,
    ) -> AppList:
        """Apps installed on the iPhone: name, bundle id AS INSTALLED, version, whether it's your
        development build, and whether it's running (pid). Free teams install com.example.app as
        XTL-<TEAM>.com.example.app; the app tools accept either form. Free Apple IDs allow at most 3 own
        apps per iPhone (`own_apps`)."""
        state = get_state(ctx)
        items = await apps(state, udid, system=include_system and scope == "all")
        own = sum(1 for a in items if a.get("developer"))
        if scope == "own":
            items = [a for a in items if a.get("developer")]
        if query:
            q = query.lower()
            items = [a for a in items if q in str(a.get("name", "")).lower() or q in str(a.get("bundleId", "")).lower()]
        page, nxt_off = page_of(items, offset, limit, _app)
        nxt = []
        note = f"Showing {len(page)} of {len(items)}; call again with offset={nxt_off}, or narrow with query." \
            if nxt_off is not None else None
        if scope == "own" and not items and not query:
            nxt.append("None of your apps are installed. ios_run(project_path) builds and installs one.")
        return fit_page(AppList(scope=scope, total=len(items), count=len(page), offset=offset,
                                has_more=nxt_off is not None, next_offset=nxt_off, apps=page, own_apps=own,
                                next_steps=nxt, note=note, truncated=nxt_off is not None,
                                omitted=len(items) - len(page)), "apps", offset,
                        lambda k, end: f"Showing {k} of {len(items)}; call again with offset={end}, or narrow with query.")

    @tool(mcp, annotations=ann("Install app", read_only=False, destructive=False, idempotent=False, open_world=True))
    async def ios_app_install(
        ctx: Context,
        path: Annotated[str | None, Field(description="A signed .ipa file or .app folder, e.g. "
                                                      "'~/Projects/MyApp/xtool/MyApp.ipa'")] = None,
        project_path: Annotated[str | None, Field(description="Instead of path: install this project's last signed "
                                                              "build")] = None,
        install_id: Annotated[str | None, Field(description="Wait for an install started earlier (from a result with "
                                                            "state='running') instead of starting one")] = None,
        wait_seconds: Annotated[float, Field(ge=0, le=MAX_WAIT, description="How long to wait for the install "
                                                                           "before returning state='running'")] = 20,
        udid: Udid = None,
    ) -> AppInstallStatus:
        """Install an already signed .ipa/.app on the iPhone over the current connection (Wi-Fi or USB);
        ~10 s for a typical app, with progress notifications. Replaces an installed copy and keeps its data.
        Runs in the background: if it takes longer than wait_seconds (≤25) the result has state='running'
        and an install_id; call again with install_id to keep waiting. To build AND install, use ios_run
        instead. Fails with app-limit when a free Apple ID already has 3 apps on the phone. Then
        ios_app_launch or ios_app_run_and_capture."""
        state = get_state(ctx)
        last = [-1.0]

        async def prog(pct: float, status: str) -> None:
            if pct > last[0]:
                last[0] = pct
                await ctx.report_progress(pct, 100, message=f"{status} {pct:.0f}%")

        if install_id:
            job = get_install(state, install_id)
        else:
            pkg, proj = await package_for(state, path, project_path)
            job = start_install(state, udid, pkg, proj)
        await wait_install(job, wait_seconds, prog)
        return install_status(job)

    @tool(mcp, annotations=ann("Launch app", read_only=False, destructive=False, idempotent=False, open_world=True))
    async def ios_app_launch(
        ctx: Context,
        bundle_id: BundleId,
        args: Annotated[list[str] | None, Field(description="Launch arguments, e.g. ['-UITestMode', 'YES']")] = None,
        env: Annotated[dict[str, str] | None, Field(description="Environment variables for the process")] = None,
        console: Annotated[bool, Field(description="Also capture what the app prints (stdout/stderr, Swift "
                                                   "print()) for ios_app_console_read. Restarts it if running")] = False,
        udid: Udid = None,
        preset: PresetName = None,
        project_path: Annotated[str | None, Field(description="With preset: the app's project (name or path). "
                                                              "Default: the listed project that builds bundle_id")] = None,
    ) -> AppLaunchResult:
        """Open an installed app on the iPhone and return its pid. A running copy is ended and started
        again, so the launch arguments and environment always apply (unsaved state in it is lost). The
        phone must be unlocked. console=true also captures its print() output (read it with
        ios_app_console_read). To also capture its logs and catch a crash, prefer ios_app_run_and_capture.
        preset='<name>' launches with a run preset of the app's project (ios_project_presets): its args come
        first, then `args`, and `env` overrides its environment."""
        state = get_state(ctx)
        how = await resolve_preset(state, preset, project_path=project_path, bundle_id=bundle_id) if preset else None
        args, env = merge_launch(how, args, env)
        res = await launch(state, udid, bundle_id, args, env, console=console)
        bid = res.get("bundleId") or bundle_id
        con = (res.get("console") or {}) if console else {}
        con_err = None
        if console and not con.get("sessionId"):
            e = con.get("error") or {}
            con_err = B.clip(e.get("message") if isinstance(e, dict) else str(e or "Not captured."), B.ERROR_CUT)
        steps = ["ios_logs_start(bundle_id=...) to follow its log; ios_crash_list(bundle_id=...) "
                 "if it closes by itself; ios_screenshot to see the screen."]
        if con.get("sessionId"):
            steps.insert(0, f"ios_app_console_read(session_id='{con['sessionId']}', wait_seconds=5) for what it prints.")
        return AppLaunchResult(bundle_id=bid, pid=res.get("pid"), console_session_id=con.get("sessionId"),
                               console_error=con_err, preset=how["name"] if how else None,
                               args=[B.clip(x, 300) for x in args or []][:64] if how else [],
                               env={k: B.clip(v, 300) for k, v in (env or {}).items()} if how else {},
                               next_steps=steps)

    @tool(mcp, annotations=ann("Stop app", read_only=False, destructive=False, idempotent=True, open_world=True))
    async def ios_app_kill(ctx: Context, bundle_id: BundleId, udid: Udid = None) -> ActionResult:
        """Terminate a running app on the iPhone (like swiping it away; unsaved state in the app is lost).
        Does nothing if it isn't running."""
        state = get_state(ctx)
        app = await resolve_app(state, bundle_id, udid)
        res = await dcall(state, "app.kill", {"bundleId": app["bundleId"]}, udid=udid, timeout=30) or {}
        killed = res.get("killed") or []
        return ActionResult(done=True, message=f"Stopped {app.get('name')} (pid {', '.join(map(str, killed))})."
                            if killed else f"{app.get('name')} wasn't running.", data={"bundle_id": app["bundleId"]})

    @tool(mcp, annotations=ann("Uninstall app", read_only=False, destructive=True, idempotent=True, open_world=True))
    async def ios_app_uninstall(
        ctx: Context,
        bundle_id: BundleId,
        confirm: Annotated[bool, Field(description="false = dry run. true only after the user agreed to delete the "
                                                   "app and its data")] = False,
        udid: Udid = None,
    ) -> ActionResult:
        """Delete an app AND ALL ITS DATA from the iPhone. Cannot be undone. Call with confirm=false first:
        it changes nothing and says exactly what would be removed. Ask the user, and only if they agree
        call again with confirm=true. Typical reason: a free Apple ID hit its 3-app limit."""
        state = get_state(ctx)
        app = await resolve_app(state, bundle_id, udid)
        what = f"{app.get('name')} ({app['bundleId']}, version {app.get('version') or '?'})"
        if not confirm:
            return dry_run(f"This would uninstall {what} from the iPhone.",
                           [f"Uninstall {what}", "Delete all of its data and documents on the iPhone"],
                           {"bundle_id": app["bundleId"]})
        await dcall(state, "app.uninstall", {"bundleId": app["bundleId"]}, udid=udid, timeout=60)
        return ActionResult(done=True, message=f"Uninstalled {what}.", data={"bundle_id": app["bundleId"]})

    @tool(mcp, annotations=READ_REMOTE("List processes"))
    async def ios_process_list(
        ctx: Context,
        apps_only: Annotated[bool, Field(description="Only app processes (not system daemons)")] = True,
        query: Annotated[str | None, Field(description="Case-insensitive filter on name or bundle id")] = None,
        limit: Annotated[int, Field(ge=1, le=200, description="Max processes to return")] = 50,
        udid: Udid = None,
    ) -> ProcessList:
        """Processes running on the iPhone: pid, name and bundle id. Use it to check whether an app is still
        alive, or to find a pid for ios_logs_start(pid=...)."""
        state = get_state(ctx)
        items = await dcall(state, "processes.list", {"appsOnly": apps_only}, udid=udid, timeout=40) or []
        if query:
            q = query.lower()
            items = [p for p in items if q in str(p.get("name", "")).lower() or q in str(p.get("bundleId") or "").lower()]
        items.sort(key=lambda p: str(p.get("name", "")).lower())
        out, _ = page_of(items, 0, limit, lambda p: ProcessInfo(pid=int(p.get("pid") or 0), name=str(p.get("name")),
                                                                bundle_id=p.get("bundleId"), is_app=bool(p.get("isApp"))))
        cut = len(items) - len(out)
        return ProcessList(total=len(items), count=len(out), processes=out, truncated=cut > 0, omitted=cut,
                           note=f"Showing {len(out)} of {len(items)} (sorted by name); narrow with query"
                                + (" or apps_only=true." if not apps_only else ".") if cut else None)

    @mcp.tool(annotations=ann("Screenshot", read_only=True, idempotent=False, open_world=True))
    async def ios_screenshot(
        ctx: Context,
        max_dimension: Annotated[int, Field(ge=256, le=B.IMG_MAX_DIM, description="Downscale so the long edge is "
                                                                                 "at most this many pixels")] = B.IMG_MAX_DIM,
        format: Annotated[Literal["jpeg", "png"], Field(description="jpeg is ~10x smaller; png is sent only if it "
                                                                    "fits 350 KB, else jpeg")] = "jpeg",
        quality: Annotated[int, Field(ge=30, le=95, description="Starting JPEG quality; steps down to 65 and 50 "
                                                                "if the image is over 350 KB")] = 80,
        inline: Annotated[bool, Field(description="false: return only the saved file's path, no image")] = True,
        udid: Udid = None,
    ) -> Annotated[CallToolResult, ScreenshotInfo]:
        """Capture the iPhone's screen and return it as an image, plus the saved file path, so you can see
        what the app shows. The image is at most 1568 px on the long edge and 350 KB (JPEG, quality stepping
        down from 80 until it fits); width, height and bytes are reported. inline=false returns only the
        path. The phone must be unlocked with the screen on. If your client can't show images, open the path."""
        state = get_state(ctx)
        try:
            shot = await dcall(state, "screenshot", udid=udid, timeout=45) or {}
        except ToolError as exc:
            raise ToolError(B.clip_error(str(exc))) from None
        src = Path(str(shot.get("path") or ""))
        if not src.is_file():
            raise ToolError("The iPhone returned no screenshot file. Retry with the phone unlocked.")
        steps = [quality] + [q for q in (65, 50) if q < quality]
        img = await asyncio.to_thread(B.fit_image, src, max_dimension, B.IMG_MAX_BYTES, format, steps)
        w, h = img.width or int(shot.get("width") or 0), img.height or int(shot.get("height") or 0)
        out = src.with_suffix(".jpg" if img.format == "jpeg" else ".png")
        if out != src:
            out.write_bytes(img.data)
        send = inline and img.fits
        notes = list(img.notes)
        if inline and not img.fits:
            notes.append(f"The image is {len(img.data) // 1000} KB, over the {B.IMG_MAX_BYTES // 1000} KB cap, so it "
                         "isn't inline; open the path or call again with a smaller max_dimension.")
        info = ScreenshotInfo(path=helper.tilde(str(out)), original=helper.tilde(str(src)), width=w, height=h,
                              original_width=img.original_width or None, original_height=img.original_height or None,
                              format=img.format, bytes=len(img.data), quality=img.quality, inline=send,
                              note=" ".join(notes) or None)
        text = (f"Screenshot {w}x{h} {img.format.upper()} ({len(img.data) // 1000} KB) saved to {info.path}; "
                f"full-resolution PNG: {info.original}." + (f" {info.note}" if info.note else ""))
        content: list = [TextContent(type="text", text=text)]
        if send:
            content.insert(0, ImageContent(type="image", data=base64.b64encode(img.data).decode(),
                                           mime_type=f"image/{img.format}"))
        return CallToolResult(content=content, structured_content=B.dump(info))

    # -------------------------------------------------------------- location

    LASTS = ("It lasts until ios_location_clear, a phone restart, or the iOS Studio device service stopping "
             "(about 2 minutes after both this server and the iOS Studio window have closed). It applies to the "
             "whole phone and every app.")

    @tool(mcp, annotations=ann("Simulate location", read_only=False, idempotent=True, open_world=True))
    async def ios_location_set(
        ctx: Context,
        lat: Annotated[float, Field(ge=-90, le=90, description="Latitude, e.g. 38.7223")],
        lon: Annotated[float, Field(ge=-180, le=180, description="Longitude, e.g. -9.1393")],
        udid: Udid = None,
    ) -> LocationResult:
        """Make the iPhone report a simulated GPS position (for testing location features). Needs Developer
        Mode and the developer disk image (mounted automatically). Temporary: it lasts only while the
        iOS Studio device service runs, see the result message. Undo with ios_location_clear."""
        state = get_state(ctx)
        res = await dcall(state, "location.set", {"lat": lat, "lon": lon}, udid=udid, timeout=90) or {}
        return LocationResult(active=True, lat=res.get("lat", lat), lon=res.get("lon", lon),
                              message="The iPhone now reports this location. " + LASTS)

    @tool(mcp, annotations=ann("Clear simulated location", read_only=False, idempotent=True, open_world=True))
    async def ios_location_clear(ctx: Context, udid: Udid = None) -> LocationResult:
        """Stop simulating a location; the iPhone goes back to its real position."""
        state = get_state(ctx)
        await dcall(state, "location.clear", udid=udid, timeout=60)
        return LocationResult(active=False, message="The iPhone uses its real location again.")
