"""apps.list (+ apps / appIcons / appSizes events), app.launch, app.kill, processes.list.

The app list itself is cached per device in deviced_features/app_index.py (INDEX); this module adds
what changes all the time (running/pid) or lives elsewhere (icon files, sizes) to each reply."""
from __future__ import annotations

import asyncio
import contextlib
import posixpath
import signal
import time
import uuid

import device_link as link
import deviced_core as core
from deviced_core import dlog, emit, err, icon_path, is_connection_error, write_bytes
from deviced_features.app_index import INDEX, bundle_dir, compose, device_fetcher, fetch_icons, size_fetcher


async def running_pids(d, rsd) -> dict[str, int]:
    """{app bundle dir: pid} of the running apps (empty if the process list isn't available)."""
    out: dict[str, int] = {}
    try:
        async with d.pmd3.AppServiceService(rsd) as svc:
            procs = await asyncio.wait_for(svc.list_processes(), 10)
    except asyncio.CancelledError:
        raise
    except BaseException as exc:  # noqa: BLE001
        dlog(f"apps: process list: {exc!r}")
        return out
    for p in procs:
        url = p.get("executableURL")
        path = url.get("relative", "") if isinstance(url, dict) else str(url or "")
        if ".app/" in path and "processIdentifier" in p:
            out[bundle_dir(path)] = int(p["processIdentifier"])
    return out


class _Icons:
    def __init__(self, svc):
        self.svc = svc

    async def fetch(self, bid: str) -> bytes | None:
        icon = await self.svc.fetch_icon(bundle_identifier=bid, width=60, height=60, scale=2)
        return getattr(icon, "png_data", None)


def _lazy_fetcher(d, s):
    """A Fetch that resolves the session's current RSD when it runs (background refreshes outlive the request)."""
    async def fetch(kind, bundle_ids=None):
        return await device_fetcher(d.pmd3, await s.get_rsd())(kind, bundle_ids)
    return fetch


async def fetch_sizes(d, s, kind: str) -> dict:
    """Sizes are slow to compute (~9 s over Wi-Fi), so they follow apps.list as an event."""
    try:
        sizes = await size_fetcher(d.pmd3, await s.get_rsd())(kind)
    except asyncio.CancelledError:
        raise
    except BaseException as exc:  # noqa: BLE001
        dlog(f"sizes: {exc!r}")
        return {}
    INDEX.put_sizes(s.udid, kind, sizes)
    emit("appSizes", {"udid": s.udid, "sizes": sizes})
    return sizes


async def app_pids(d, s, rsd, bundle_id: str) -> list[int]:
    info = await INDEX.lookup(s.udid, bundle_id, device_fetcher(d.pmd3, rsd))
    if not info or info["bundleId"] != bundle_id:
        raise err("not-found", "That app isn't installed on your iPhone.")
    app_dir = bundle_dir(info.get("path") or "")
    exe = info.get("executable") or ""
    pids = []
    async with d.pmd3.AppServiceService(rsd) as svc:
        for p in await svc.list_processes():
            url = p.get("executableURL")
            path = url.get("relative", "") if isinstance(url, dict) else str(url or "")
            path = path.removeprefix("file://")
            if (app_dir and bundle_dir(path) == app_dir) or (exe and path.endswith(f".app/{exe}")):
                if "processIdentifier" in p:
                    pids.append(int(p["processIdentifier"]))
    return sorted(set(pids))


def register(reg):
    @reg.command("apps.list")
    async def c_apps_list(d, a):
        s, rsd = await d.rsd(a)
        udid = s.udid
        kind = "Any" if a.get("system") else "User"
        fetch = _lazy_fetcher(d, s)

        async def changed(_udid, k, apps):
            running = await running_pids(d, await s.get_rsd())
            emit("apps", {"udid": udid, "system": k == "Any",
                          "apps": compose(apps, running, INDEX.sizes(udid, k), lambda bid: icon_path(udid, bid))})

        (apps, stale, fetched_at), running = await asyncio.gather(
            INDEX.list(udid, kind, fetch, force=bool(a.get("force")), sync=core.ONCE, on_change=changed),
            running_pids(d, rsd))
        sizes = INDEX.sizes(udid, kind)
        if a.get("sizes") and sizes is None:
            if core.ONCE:     # no background work in --once mode: the reply waits
                sizes = await fetch_sizes(d, s, kind)
            else:
                INDEX.start(udid, f"sizes:{kind}", fetch_sizes(d, s, kind))
        out = compose(apps, running, sizes, lambda bid: icon_path(udid, bid))
        if a.get("icons", True) and not core.ONCE:
            missing = [x["bundleId"] for x in out if not x["icon"]]
            if missing:
                @contextlib.asynccontextmanager
                async def open_icons():
                    async with d.pmd3.IconService(await s.get_rsd()) as svc:
                        yield _Icons(svc)

                INDEX.start(udid, "icons", fetch_icons(
                    udid, missing, open_icons, lambda bid: icon_path(udid, bid), write_bytes, emit,
                    fatal=is_connection_error, log=dlog))
        return {"apps": out, "stale": stale, "fetchedAt": round(fetched_at, 3)}

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
        pids = await app_pids(d, s, rsd, bid)
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
