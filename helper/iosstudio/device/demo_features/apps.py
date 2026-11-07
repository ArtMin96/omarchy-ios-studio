"""Demo: apps.list, app.launch, app.kill, app.uninstall, app.install, processes.list.

apps.list follows the real daemon's contract (features/app_index.py does the caching): served
from the cache with `stale`/`fetchedAt`, refreshed in the background (`apps` event when it changed),
icons drawn in the background and sent in batched `appIcons` events, sizes only with `sizes: true`.
"""
from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path

from iosstudio.core.subproc import demo_delay
from iosstudio.demo import demo_art
from iosstudio.device.deviced_demo_core import APPS, DIR, FILE_SHARING, TEAM, UDID, event, installed, pids, running
from iosstudio.device.features.app_index import AppIndex, bundle_dir, compose, fetch_icons

INDEX = AppIndex(DIR)
_BY_ID = {bid: (name, hue, shape, dev, mb) for name, bid, hue, shape, dev, _, mb in APPS}


def _path(name: str) -> str:
    return "/private/var/containers/Bundle/Application/DEMO/" + name.replace(" ", "") + ".app"


def _info(name: str, dev: bool, bid: str) -> dict:
    return {"CFBundleIdentifier": bid, "CFBundleDisplayName": name, "CFBundleShortVersionString": "1.%d.0" % (len(name) % 7),
            "CFBundleVersion": str(10 + len(name)), "CFBundleExecutable": name.replace(" ", ""), "Path": _path(name),
            "ApplicationType": "User", "SignerIdentity": "Apple Development: Alex (A1B2C3D4E5)" if dev
            else "Apple iPhone OS Application Signing", "UIFileSharingEnabled": bid in FILE_SHARING,
            "Entitlements": {"get-task-allow": dev}}


async def _fetch(kind: str, bundle_ids: list[str] | None = None) -> dict:
    await asyncio.sleep(demo_delay(0.4))
    return {bid: _info(name, dev, bid) for name, bid, _h, _s, dev, _r, _mb in APPS
            if bid in installed and (bundle_ids is None or bid in bundle_ids)}


def _icon_path(bid: str) -> Path:
    return DIR / "icons" / (bid + ".png")


class _Art:
    async def fetch(self, bid: str) -> bytes | None:
        _name, hue, shape, _dev, _mb = _BY_ID[bid]
        await asyncio.sleep(demo_delay(0.05))
        tmp = DIR / "icons" / f".{bid}.draw.png"
        data = await asyncio.to_thread(lambda: demo_art.icon(tmp, hue, shape).read_bytes())
        tmp.unlink(missing_ok=True)
        return data


@contextlib.asynccontextmanager
async def _open_art():
    yield _Art()


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


async def _sizes() -> None:
    await asyncio.sleep(demo_delay(2.0))
    sizes = {bid: mb * 1_000_000 for bid, (_n, _h, _s, _d, mb) in _BY_ID.items() if bid in installed}
    INDEX.put_sizes(UDID, "User", sizes)
    event("appSizes", {"udid": UDID, "sizes": sizes})


def _running() -> dict[str, int]:
    return {bundle_dir(_path(_BY_ID[b][0])): pids[b] for b in running if b in _BY_ID}


async def apps_list(a: dict) -> dict:
    async def changed(udid, kind, apps):
        event("apps", {"udid": udid, "system": kind == "Any", "apps": compose(apps, _running(), INDEX.sizes(udid, kind), _icon_path)})

    apps, stale, fetched_at = await INDEX.list(UDID, "User", _fetch, force=bool(a.get("force")), on_change=changed)
    sizes = INDEX.sizes(UDID, "User")
    if a.get("sizes") and sizes is None:
        INDEX.start(UDID, "sizes:User", _sizes())
    out = compose(apps, _running(), sizes, _icon_path)
    if a.get("icons", True):
        missing = [x["bundleId"] for x in out if not x["icon"]]
        if missing:
            INDEX.start(UDID, "icons", fetch_icons(UDID, missing, _open_art, _icon_path, _write, event))
    return {"apps": out, "stale": stale, "fetchedAt": round(fetched_at, 3)}


def register(reg):
    @reg.command("apps.list")
    async def list_apps(ctx, a):
        return await apps_list(a)

    @reg.on_shutdown
    def _shutdown(ctx):
        INDEX.cancel()

    @reg.command("app.launch")
    async def launch(ctx, a):
        if a.get("console") and ctx.registry.has("console.launch"):   # print() output: console.py
            return await ctx.registry.lookup("console.launch")(ctx, a)
        running.add(a.get("bundleId"))
        pids[a.get("bundleId")] = 812
        return {"bundleId": a.get("bundleId"), "pid": 812}

    @reg.command("app.kill")
    def kill(ctx, a):
        running.discard(a.get("bundleId"))
        return {"bundleId": a.get("bundleId"), "killed": [812]}

    @reg.command("app.uninstall")
    def uninstall(ctx, a):
        installed.discard(a.get("bundleId"))
        INDEX.invalidate(UDID)
        return {"bundleId": a.get("bundleId")}

    @reg.command("app.install")
    def install(ctx, a):
        for pct in (0, 35, 70, 100):
            event("installProgress", {"path": a.get("path"), "percent": pct, "status": "done" if pct == 100 else ("uploading" if pct == 0 else "installing")})
            time.sleep(demo_delay(0.5))
        name = Path(str(a.get("path") or "App.ipa")).stem
        bid = next((b for n, b, *_ in APPS if n == name), f"XTL-{TEAM}.com.example." + name.lower())
        installed.add(bid)
        INDEX.invalidate(UDID)
        return {"path": a.get("path"), "bundleId": bid, "packageBundleId": bid.split(f"XTL-{TEAM}.")[-1], "seconds": 2.0}

    @reg.command("processes.list")
    def processes(ctx, a):
        return [{"pid": pids.get(b, 812), "name": n.replace(" ", ""), "path": "", "isApp": True, "bundleId": b}
                for n, b, *_ in APPS if b in running]
