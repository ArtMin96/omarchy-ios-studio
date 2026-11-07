"""apps.list (+ appIcon / appSizes events), app.launch, app.kill, processes.list."""
from __future__ import annotations

import asyncio
import contextlib
import posixpath
import signal
import time
import uuid

import device_link as link
from deviced_core import dlog, emit, err, icon_path, installed_apps, is_connection_error, write_bytes


async def fetch_icons(d, s, bundle_ids: list[str]):
    try:
        rsd = await s.get_rsd()
        async with d.pmd3.IconService(rsd) as svc:
            for bid in bundle_ids:
                path = icon_path(s.udid, bid)
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


async def fetch_sizes(d, s, kind: str):
    """Sizes are slow to compute (~9 s over Wi-Fi), so they follow apps.list as an event."""
    try:
        rsd = await s.get_rsd()
        apps = await installed_apps(d.pmd3, rsd,
            application_type=kind, calculate_sizes=True)
        sizes = {bid: int(i.get("StaticDiskUsage") or 0) + int(i.get("DynamicDiskUsage") or 0)
                 for bid, i in apps.items() if i.get("StaticDiskUsage") is not None}
        emit("appSizes", {"udid": s.udid, "sizes": sizes})
    except asyncio.CancelledError:
        raise
    except BaseException as exc:  # noqa: BLE001
        dlog(f"sizes: {exc!r}")


async def app_pids(d, rsd, bundle_id: str) -> list[int]:
    apps = await installed_apps(d.pmd3, rsd,
        application_type="Any", bundle_identifiers=[bundle_id])
    info = apps.get(bundle_id)
    if not info:
        raise err("not-found", "That app isn't installed on your iPhone.")
    app_path = (info.get("Path") or "").rstrip("/")
    exe = info.get("CFBundleExecutable") or ""
    pids = []
    async with d.pmd3.AppServiceService(rsd) as svc:
        for p in await svc.list_processes():
            url = p.get("executableURL")
            path = url.get("relative", "") if isinstance(url, dict) else str(url or "")
            path = path.removeprefix("file://")
            if (app_path and path.startswith(app_path + "/")) or (exe and path.endswith(f".app/{exe}")):
                if "processIdentifier" in p:
                    pids.append(int(p["processIdentifier"]))
    return sorted(set(pids))


def register(reg):
    tasks: dict[str, asyncio.Task | None] = {"icons": None, "sizes": None}

    def idle(name: str) -> bool:
        t = tasks[name]
        return t is None or t.done()

    @reg.command("apps.list")
    async def c_apps_list(d, a):
        s, rsd = await d.rsd(a)
        kind = "Any" if a.get("system") else "User"
        apps = await installed_apps(d.pmd3, rsd,
            application_type=kind, calculate_sizes=bool(a.get("sizes", False)))
        running: dict[str, int] = {}
        with contextlib.suppress(BaseException):
            async with d.pmd3.AppServiceService(rsd) as svc:
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
            icon = icon_path(s.udid, bid)
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
                # Files tab: App Store apps that share Documents, and your builds (whole container)
                "fileSharing": bool(info.get("UIFileSharingEnabled")),
                "debuggable": bool((info.get("Entitlements") or {}).get("get-task-allow")),
            })
        out.sort(key=lambda x: (x["name"] or "").lower())
        if a.get("sizes") is None and idle("sizes"):
            tasks["sizes"] = asyncio.create_task(fetch_sizes(d, s, kind))
        if a.get("icons", True):
            missing = [x["bundleId"] for x in out if not x["icon"]]
            if missing and idle("icons"):
                tasks["icons"] = asyncio.create_task(fetch_icons(d, s, missing))
        return out

    @reg.command("app.launch")
    async def c_app_launch(d, a):
        bid = a.get("bundleId")
        if not bid:
            raise err("bad-args", "Which app? (bundleId missing)")
        # console: true captures print() output (deviced_features/console.py), which calls back here
        # with the stdio socket's identifier in `_stdio`.
        stdio = a.get("_stdio") if isinstance(a.get("_stdio"), uuid.UUID) else None
        if a.get("console") and stdio is None and d.registry.has("console.launch"):
            return await d.registry.lookup("console.launch")(d, a)
        s, rsd = await d.rsd(a)
        await s.ensure_ddi()
        try:
            async with d.pmd3.AppServiceService(rsd) as svc:
                res = await svc.launch_application(bid, list(a.get("args") or []), True, False, dict(a.get("env") or {}),
                                                   **({"stdio_identifier": stdio} if stdio else {}))
        except d.pmd3.exceptions.CoreDeviceError as exc:
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

    @reg.command("app.kill")
    async def c_app_kill(d, a):
        bid = a.get("bundleId")
        if not bid:
            raise err("bad-args", "Which app? (bundleId missing)")
        s, rsd = await d.rsd(a)
        pids = await app_pids(d, rsd, bid)
        async with d.pmd3.AppServiceService(rsd) as svc:
            for pid in pids:
                await svc.send_signal_to_process(pid, int(signal.SIGKILL))
        if pids:    # app data waits for cfprefsd to save a just-killed app's settings (deviced_features/appdata.py)
            s.tunnel_data.setdefault("app.killed", {})[bid] = time.monotonic()
        return {"bundleId": bid, "killed": pids}

    @reg.command("processes.list")
    async def c_processes_list(d, a):
        s, rsd = await d.rsd(a)
        async with d.pmd3.AppServiceService(rsd) as svc:
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
