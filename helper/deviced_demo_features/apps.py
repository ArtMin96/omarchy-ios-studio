"""Demo: apps.list, app.launch, app.kill, app.uninstall, app.install, processes.list."""
from __future__ import annotations

import time
from pathlib import Path

import demo_art
from common import demo_delay
from deviced_demo_core import APPS, DIR, FILE_SHARING, TEAM, event, installed, pids, running


def apps_list() -> list:
    out = []
    for name, bid, hue, shape, dev, _, mb in APPS:
        if bid not in installed:
            continue
        out.append({"bundleId": bid, "name": name, "version": "1.%d.0" % (len(name) % 7), "build": str(10 + len(name)),
                    "developer": dev, "removable": True, "hidden": False, "system": False, "executable": name.replace(" ", ""),
                    "path": "/private/var/containers/Bundle/Application/DEMO/" + name.replace(" ", "") + ".app",
                    "icon": str(demo_art.icon(DIR / "icons" / (bid + ".png"), hue, shape)), "running": bid in running,
                    "pid": pids[bid] if bid in running else None, "size": mb * 1_000_000,
                    "fileSharing": bid in FILE_SHARING, "debuggable": dev})
    return sorted(out, key=lambda a: a["name"].lower())


def register(reg):
    reg.command("apps.list", lambda ctx, a: apps_list())

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
        return {"bundleId": a.get("bundleId")}

    @reg.command("app.install")
    def install(ctx, a):
        for pct in (0, 35, 70, 100):
            event("installProgress", {"path": a.get("path"), "percent": pct, "status": "done" if pct == 100 else ("uploading" if pct == 0 else "installing")})
            time.sleep(demo_delay(0.5))
        name = Path(str(a.get("path") or "App.ipa")).stem
        bid = next((b for n, b, *_ in APPS if n == name), f"XTL-{TEAM}.com.example." + name.lower())
        installed.add(bid)
        return {"path": a.get("path"), "bundleId": bid, "packageBundleId": bid.split(f"XTL-{TEAM}.")[-1], "seconds": 2.0}

    @reg.command("processes.list")
    def processes(ctx, a):
        return [{"pid": pids.get(b, 812), "name": n.replace(" ", ""), "path": "", "isApp": True, "bundleId": b}
                for n, b, *_ in APPS if b in running]
