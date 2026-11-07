"""The iPhone, through the shared device daemon (helper/device_client.py).

The MCP server is one more client of the per-user device broker that the iOS Studio window also uses,
so both share one tunnel to the phone. The client connects lazily, on the first tool that needs the
phone; ios_status only attaches when a daemon is already running.

    await dcall(state, "cmd", {...}, hints={code: next step})   one request; DeviceError -> ToolError
    hub(state)                       event cache: connection state, device info, listeners
    apps / match_app / resolve_app   installed apps; either bundle id form (XTL-<TEAM>.… or plain)
    launch(state, udid, bundle_id)   open an app (optionally with its console captured)
    start_install / wait_install / install_status   app.install as a job of this server (kind 'install')
    package_for(state, path, project_path)          the .ipa/.app to install
    attach(state) / close(state)     fill the AppState device slots / close the client (tools/device.py)
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from pathlib import Path
from typing import Any, Literal

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from .. import helper
from ..core import deadline
from ..core.errors import device_error
from ..core.jobs import Job
from ..core.registry import MAX_WAIT
from ..models import AppInstallResult
from ..state import AppState
from .builds import history
from .projects import resolve_project

log = logging.getLogger("iosstudio.device")

CONNECT_TIMEOUT = 40.0   # starting the daemon; inside a tool call it is cut to the call's budget

# Device-wide events the MCP client wants. Not 'frame' (mirror images) or icons: nobody here reads them.
EVENTS = ["ready", "devices", "connection", "deviceInfo", "installProgress", "transfer", "cameratest"]


def hub(state: AppState) -> dict:
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
    return mod.DeviceClient(name=name, events=events, connect_timeout=CONNECT_TIMEOUT)


async def client(state: AppState):
    """The server's DeviceClient, connected (spawning the daemon if needed)."""
    h = hub(state)
    async with h["lock"]:
        if state.device is None:
            dev = new_client("mcp", EVENTS)

            def on_event(name: str, data: Any) -> None:
                if name == "connection" and isinstance(data, dict):
                    h["connection"] = data
                elif name == "deviceInfo" and isinstance(data, dict):
                    h["info"].update({k: v for k, v in data.items()})
                for cb in list(h["listeners"]):
                    cb(name, data)

            dev.on_event(on_event)
            state.device = dev
        if not state.device.connected:
            try:
                await asyncio.wait_for(state.device.connect(), deadline.clamp(CONNECT_TIMEOUT, "connecting to the "
                                                                              "iPhone service"))
            except asyncio.TimeoutError:
                dev, state.device = state.device, None     # half-open (maybe mid-hello): start over next time
                with contextlib.suppress(Exception):
                    await dev.close()
                raise ToolError("The iPhone service is still starting [timeout]. Next: retry in a few seconds; if it "
                                "keeps happening, run ios_toolchain_check (pymobiledevice3 step).") from None
            except Exception as exc:  # noqa: BLE001
                raise device_error(exc) from None
            log.info("connected to the device daemon (pid %s)", (state.device.hello or {}).get("pid"))
        return state.device


async def dcall(state: AppState, cmd: str, args: dict | None = None, *, timeout: float = 60.0,
                udid: str | None = None, hints: dict[str, str] | None = None) -> Any:
    """One device request. DeviceError -> ToolError with the next step (hints: the caller's own next step
    per error code, see core/errors.py). Inside a tool call `timeout` is cut to what is left of the call's
    budget (core/deadline.py); in a background job it applies as given."""
    dev = await client(state)
    a = dict(args or {})
    if udid:
        a["udid"] = udid
    try:
        return await dev.request(cmd, a, timeout=deadline.clamp(timeout, f"the iPhone answered {cmd}"))
    except Exception as exc:  # noqa: BLE001
        if exc.__class__.__name__ == "DeviceError":
            raise device_error(exc, hints) from None
        raise


async def apps(state: AppState, udid: str | None = None, system: bool = False) -> list[dict]:
    data = await dcall(state, "apps.list", {"system": system, "icons": False, "sizes": False}, udid=udid, timeout=40)
    if isinstance(data, dict):  # {apps, stale, fetchedAt}
        data = data.get("apps")
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
    h = hub(state)

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
    h["listeners"].append(listener)
    try:
        return await dcall(state, "app.install", {"path": path}, udid=udid, timeout=600) or {}
    finally:
        h["listeners"].remove(listener)


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
                                                   "the same tool again with job_id")
    job_id: str | None = Field(None, description="The install's job: pass back as job_id to wait for it")
    progress: float | None = Field(None, description="0-1 while installing")
    progress_detail: str | None = None
    elapsed_seconds: float | None = None


def start_install(state: AppState, udid: str | None, pkg: str, project: dict | None = None,
                  tool: str = "ios_app_install") -> Job:
    """Start app.install as a job of this server (it keeps going if the tool call returns or the client
    gives up; installs over Wi-Fi can take minutes), recording the install on the project when it ends."""

    def prog(job: Job, pct: float, status: str) -> None:
        job.data["percent"], job.data["status"] = pct, status
        job.report(pct / 100, f"{status or 'installing'} {pct:.0f}%")

    async def run(job: Job) -> dict:
        u = udid or await state.active_udid()
        res = await install_ipa(state, udid, pkg, lambda pct, st: prog(job, pct, st))
        if project:
            try:
                await helper.call("projects", "record-install", {"path": project["path"], "udid": u,
                                                                 "via": "wifi", "ipa": pkg})
            except ToolError:
                pass
        return res

    return state.jobs.run(run, "install", title=f"Install {Path(pkg).name}", tool=tool, data={"pkg": pkg})


def get_install(state: AppState, job_id: str) -> Job:
    return state.jobs.get(job_id, "install")


async def wait_install(state: AppState, job: Job, seconds: float, on_progress=None) -> None:
    """Wait up to `seconds` (≤ MAX_WAIT) for the install, calling on_progress(percent, status) as it moves."""
    async def report(j: Job) -> None:
        if on_progress and j.data.get("percent") is not None:
            await on_progress(j.data["percent"], j.data.get("status") or "")
    await state.jobs.wait(job, max(0.0, min(seconds, MAX_WAIT)), report)


def install_status(job: Job, next_step: str | None = None) -> AppInstallStatus:
    """The install's result, or state='running'; raises the install's error if it failed."""
    pkg = job.data.get("pkg") or ""
    if not job.finished:
        pct = job.data.get("percent")
        return AppInstallStatus(
            installed=False, state="running", job_id=job.id, path=helper.tilde(pkg),
            progress=round(pct / 100, 3) if pct is not None else None,
            progress_detail=f"{job.data.get('status') or 'installing'} {pct:.0f}%" if pct is not None else "starting",
            elapsed_seconds=job.elapsed,
            next_steps=[next_step or f"Still installing. Call ios_app_install(job_id='{job.id}', "
                                     "wait_seconds=20) again to wait for it."])
    if job.state != "succeeded":
        if isinstance(job.exc, ToolError) or job.state == "cancelled":
            raise ToolError((job.error or {}).get("message") or "The install was cancelled.")
        raise ToolError(f"Installing {helper.tilde(pkg)} failed: {(job.error or {}).get('message') or job.state}")
    res = job.result or {}
    bid = res.get("bundleId")
    return AppInstallStatus(installed=True, state="succeeded", job_id=job.id, bundle_id=bid,
                            package_bundle_id=res.get("packageBundleId"), path=helper.tilde(pkg),
                            seconds=res.get("seconds"), elapsed_seconds=job.elapsed,
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
    h = hub(state)
    conn = h.get("connection") or {}
    info = h.get("info") or {}
    if conn.get("udid") not in (None, d.get("udid")):
        conn = {}
    return {"udid": d.get("udid"), "name": d.get("name"), "model": d.get("model"), "iosVersion": d.get("ios"),
            "connection": conn.get("state") or d.get("state"), "transport": d.get("transport"),
            "developerMode": info.get("developerMode") if info.get("udid") in (None, d.get("udid")) else None,
            "updatedAt": time.time()}


def attach(state: AppState) -> None:
    """Fill the AppState device slots (ios_run's install/launch, ios_status) with this client."""
    state.install_app = lambda udid, ipa, job: _install_for_job(state, udid, ipa, job)
    state.launch_app = lambda udid, bundle_id, args=None, env=None: launch(state, udid, bundle_id, args, env,
                                                                            console=True)
    state.device_udid = lambda: _device_udid(state)
    state.device_status = lambda: _device_status(state)


async def close(state: AppState) -> None:
    dev, state.device = state.device, None
    if dev is not None:
        try:
            await asyncio.wait_for(dev.close(), 5)
        except Exception:  # noqa: BLE001
            pass


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
        ipa = next((b.get("ipa") for b in await history(proj["path"], 20) if b.get("ipa") and b.get("result") == "ok"),
                   None)
    if not ipa:
        raise ToolError(f"{proj.get('name')} has no signed build yet. Run ios_build_start(project_path, action='ipa') "
                        "or ios_run(project_path).")
    return _check_package(ipa), proj
