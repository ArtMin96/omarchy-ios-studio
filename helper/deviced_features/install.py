"""app.install (installProgress events), app.uninstall."""
from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path

import device_link as link
from deviced_core import classify, emit, err, icon_path


def register(reg):
    @reg.command("app.install")
    async def c_app_install(d, a):
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
        s, rsd = await d.rsd(a)
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
            await d.pmd3.InstallationProxyService(lockdown=rsd).install_from_local(path, "Install", None, progress)
        except BaseException as exc:  # noqa: BLE001
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise link.install_error(exc, classify)
        installed = None
        with contextlib.suppress(BaseException):
            apps = await d.pmd3.InstallationProxyService(lockdown=rsd).get_apps(application_type="User")
            for k in sorted(apps):
                if bid and (k == bid or k.endswith("." + bid)):
                    installed = k
                    break
        emit("installProgress", {"path": sp, "percent": 100, "status": "done"})
        with contextlib.suppress(OSError):
            if installed:
                icon_path(s.udid, installed).unlink(missing_ok=True)
        return {"path": sp, "bundleId": installed or bid, "packageBundleId": bid,
                "seconds": round(time.monotonic() - t0, 1)}

    @reg.command("app.uninstall")
    async def c_app_uninstall(d, a):
        bid = a.get("bundleId")
        if not bid:
            raise err("bad-args", "Which app? (bundleId missing)")
        s, rsd = await d.rsd(a)
        await d.pmd3.InstallationProxyService(lockdown=rsd).uninstall(bid)
        with contextlib.suppress(OSError):
            icon_path(s.udid, bid).unlink()
        return {"bundleId": bid}
