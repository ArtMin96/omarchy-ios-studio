"""app.install (installProgress events), app.uninstall. Both invalidate the app list cache (index.INDEX)
and the files feature's cached container connections of that app.

app.install does what pymobiledevice3's InstallationProxyService.install_from_local does (an AFC upload to
/PublicStaging, then installation_proxy "Install"), in steps it can bound: each upload chunk and each
installer message must arrive within a stall limit, so an installer that stops answering fails after
INSTALL_STALL_S with a plain error instead of hanging until the client gives up. Both connections are
opened per call through Session.open_service (bounded open and close) and closed on every way out,
cancellation included; the staged package is removed best effort within CLEANUP_S.

Like Xcode, the app is closed (app.kill) before the installer runs: iOS can't end an app that's in the
foreground for an update ("termination resistance"), and the installer then sends nothing until the app
leaves the screen. If the installer is silent for WAITING_NOTICE_S anyway, installProgress carries a
`message` saying it's waiting for the app to close.
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import os
import time
import uuid
import zipfile
from pathlib import Path

from iosstudio.core.output import fix
from iosstudio.device import link
from iosstudio.device.features.apps.index import INDEX, bundle_matches, device_fetcher
from iosstudio.device.real.core import classify, dlog, emit, err, icon_path

STAGING_DIR = "/PublicStaging/pymobiledevice3"   # where pymobiledevice3 stages packages too
UPLOAD_CHUNK = 1024 * 1024
UPLOAD_STALL_S = 60.0     # one 1 MiB chunk (or the staging folder, the installer request) taking longer: stuck
INSTALL_STALL_S = 120.0   # no word from the installer for this long after the upload: it's stuck
WAITING_NOTICE_S = 20.0   # ... and for this long: tell the user it waits for the app to close
CLOSE_APP_S = 15.0        # closing the running app first (best effort)
CLEANUP_S = 10.0          # removing the staged package; left behind if the phone doesn't answer

STALLED_MESSAGE = ("Your iPhone stopped answering while it installed {app}. Close {app} on the iPhone if it's "
                   "open, keep the iPhone unlocked and awake, then try again. If it keeps happening, restart the iPhone.")


class InstallStalled(Exception):
    """The phone stopped answering mid-install (the step and how long are in the message)."""


class AppInstallError(Exception):
    """The installer reported an error (its text is matched by link.install_error)."""


async def forget_containers(d, s, *bundle_ids: str) -> None:
    """Drop the files feature's cached container connections and file-sharing info of these apps: they go
    stale when the app is relaunched, killed, reinstalled or removed."""
    containers = d.registry.provided.get("containers")
    if containers is not None:
        await containers.forget(s, *bundle_ids)


def package_bytes(path: Path) -> bytes:
    """The .ipa's bytes; an .app folder is zipped into one (Payload/<name>.app/...) in memory."""
    if not path.is_dir():
        return path.read_bytes()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _dirs, files in os.walk(path):
            for name in files:
                full = Path(root) / name
                z.write(full, f"Payload/{path.name}/{full.relative_to(path).as_posix()}")
    return buf.getvalue()


async def _step(aw, limit: float, what: str):
    try:
        return await asyncio.wait_for(aw, limit)
    except TimeoutError:
        raise InstallStalled(f"no answer for {limit:g} s while {what}") from None


async def upload(afc, remote: str, data: bytes) -> None:
    await _step(afc.makedirs(STAGING_DIR), UPLOAD_STALL_S, "creating the staging folder")
    h = await _step(afc.fopen(remote, "w"), UPLOAD_STALL_S, "opening the staged file")
    try:
        for off in range(0, len(data), UPLOAD_CHUNK):
            await _step(afc.fwrite(h, data[off:off + UPLOAD_CHUNK]), UPLOAD_STALL_S,
                        f"uploading (MiB {off // UPLOAD_CHUNK + 1} of {len(data) / UPLOAD_CHUNK:.1f})")
    finally:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(afc.fclose(h), CLEANUP_S)


async def _next_message(ip, stage: str, on_waiting):
    """The installer's next message. After WAITING_NOTICE_S of silence on_waiting() is called (once per
    silence); after INSTALL_STALL_S it's a stall. The read is never cut off mid-message by the notice."""
    recv = asyncio.ensure_future(ip.service.recv_plist())
    try:
        done, _ = await asyncio.wait({recv}, timeout=min(WAITING_NOTICE_S, INSTALL_STALL_S))
        if not done and WAITING_NOTICE_S < INSTALL_STALL_S:
            on_waiting()
            done, _ = await asyncio.wait({recv}, timeout=INSTALL_STALL_S - WAITING_NOTICE_S)
        if not done:
            raise InstallStalled(f"no answer for {INSTALL_STALL_S:g} s while installing (last step: {stage})")
        return recv.result()
    finally:
        recv.cancel()   # a no-op once it's done; otherwise the connection is closed right after anyway


async def run_installer(ip, remote: str, on_percent, on_waiting=lambda: None) -> None:
    """installation_proxy Install of the staged package, until "Complete". Each message (a stage and/or
    a percentage) must come within INSTALL_STALL_S."""
    await _step(ip.service.send_plist({"Command": "Install", "ClientOptions": {}, "PackagePath": remote}),
                UPLOAD_STALL_S, "starting the installer")
    stage = "waiting for the installer"
    while True:
        resp = await _next_message(ip, stage, on_waiting)
        if not resp:
            raise AppInstallError("the installer closed the connection")
        if resp.get("Error"):
            raise AppInstallError(f"{resp.get('Error')}: {resp.get('ErrorDescription')}")
        status = resp.get("Status")
        pct = resp.get("PercentComplete")
        if isinstance(status, str) and status != stage:
            stage = status
            dlog(f"app.install: {status}" + (f" {pct}%" if isinstance(pct, (int, float)) else ""))
        if isinstance(pct, (int, float)):
            on_percent(pct)
        if status == "Complete":
            return


async def close_running_app(d, s, rsd, bid: str) -> str | None:
    """Close the installed copy of this app if it runs (app.kill), so iOS can replace it. Best effort and
    bounded: a failure only means the installer may wait for the app to leave the screen. Returns its
    name as installed (for messages), or None when it isn't installed."""
    async def close():
        apps, _, _ = await INDEX.list(s.udid, "User", device_fetcher(d.pmd3, rsd))
        hit = next((e for e in sorted(apps, key=lambda e: e["bundleId"]) if bundle_matches(e["bundleId"], bid)), None)
        if hit is None:
            return None
        out = await d.registry.lookup("app.kill")(d, {"bundleId": hit["bundleId"], "udid": s.udid})
        if out.get("killed"):
            dlog(f"app.install: closed {hit['bundleId']} (pid {', '.join(map(str, out['killed']))}) first")
        return hit.get("name") or None
    try:
        return await asyncio.wait_for(close(), CLOSE_APP_S)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        dlog(f"app.install: couldn't close the running app first: {exc!r}")
        return None


async def _remove_staged(afc, remote: str) -> None:
    try:
        await asyncio.wait_for(afc.rm_single(remote, force=True), CLEANUP_S)
    except asyncio.CancelledError:
        raise
    except BaseException as exc:  # noqa: BLE001 - a leftover in /PublicStaging is harmless
        dlog(f"app.install: couldn't remove {remote}: {exc!r}")


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
            data = await asyncio.to_thread(package_bytes, path)
        except Exception as exc:  # noqa: BLE001
            raise err("bad-package", "That app file is damaged or isn't a signed iPhone app.", None, repr(exc))
        s, rsd = await d.rsd(a)
        sp = str(path)
        emit("installProgress", {"path": sp, "percent": 0, "status": "uploading"})
        last = [-1]
        name = [path.stem]

        def on_percent(pct):
            p = int(pct)
            if p != last[0]:
                last[0] = p
                emit("installProgress", {"path": sp, "percent": p, "status": "installing"})

        def on_waiting():
            emit("installProgress", {"path": sp, "percent": max(last[0], 0), "status": "installing",
                                     "message": f"Waiting for {name[0]} to close on your iPhone"})

        t0 = time.monotonic()
        remote = f"{STAGING_DIR}/{uuid.uuid4()}.ipa"
        try:
            async with s.open_service(d.pmd3.AfcService) as afc:
                try:
                    await upload(afc, remote, data)
                    on_percent(0)   # uploaded: the phone's installer takes over
                    name[0] = await close_running_app(d, s, rsd, bid) or name[0]
                    async with s.open_service(d.pmd3.InstallationProxyService) as ip:
                        await run_installer(ip, remote, on_percent, on_waiting)
                finally:
                    await _remove_staged(afc, remote)
        except InstallStalled as exc:
            dlog(f"app.install: {exc}")
            raise err("install-stalled", STALLED_MESSAGE.format(app=name[0]),
                      fix("Reconnect iPhone", "device", action="reconnect"),
                      f"{exc} ({time.monotonic() - t0:.0f} s after the upload started)") from None
        except Exception as exc:  # noqa: BLE001 - cancellation and GeneratorExit pass through untouched
            raise link.install_error(exc, classify) from exc
        INDEX.invalidate(s.udid)
        await forget_containers(d, s, bid)
        installed = None
        with contextlib.suppress(Exception):   # refills the app list cache with the new app in it
            apps, _, _ = await INDEX.list(s.udid, "User", device_fetcher(d.pmd3, rsd), sync=True)
            installed = next((k for k in sorted(e["bundleId"] for e in apps) if bundle_matches(k, bid)), None)
        if installed and installed != bid:
            await forget_containers(d, s, installed)
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
        async with d.pmd3.InstallationProxyService(lockdown=rsd) as ip:
            await ip.uninstall(bid)
        INDEX.invalidate(s.udid)
        await forget_containers(d, s, bid)
        with contextlib.suppress(OSError):
            icon_path(s.udid, bid).unlink()
        return {"bundleId": bid}
