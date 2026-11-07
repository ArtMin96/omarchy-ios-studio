"""pair.status, pair.start, pair.code, pair.cancel (pair events). One pairing flow at a time."""
from __future__ import annotations

import asyncio
import contextlib
import platform
import re
from pathlib import Path

from iosstudio.core import fileio
from iosstudio.device import device_link as link
from iosstudio.device.device_models import marketing_name
from iosstudio.device.deviced_core import DEVICE_CACHE, classify, emit, err

PAIR_HELP_WIFI = ("On your iPhone, open Settings › Developer › Paired Macs and tap this computer "
                  "under “Other Devices”.")


def _event(phase, method, message, **kw):
    emit("pair", {"phase": phase, "method": method, "message": message, "code": kw.pop("code", None),
                  "udid": kw.pop("udid", None), **kw})


async def pair_usb(d, udid: str):
    p = d.pmd3
    m = "usb"
    try:
        _event("starting", m, "Talking to your iPhone over the cable…", udid=udid)
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
            _event("trust", m, "Unlock your iPhone and tap “Trust”, then enter your iPhone passcode.", udid=udid)
            lockdown = await p.create_using_usbmux(serial=udid, autopair=True, pair_timeout=120)
        _event("waiting", m, "Setting up Wi-Fi access…", udid=udid)
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
        d.wifi = link.wifi_records(p)
        d.lockdown = link.lockdown_records()
        _event("done", m, "Paired. Your iPhone now works over USB and Wi-Fi.", udid=udid,
               name=d.cache.get(udid, {}).get("name", ""))
        await d.select(udid)
        d.session.reconnect_now()
    except asyncio.CancelledError:
        _event("cancelled", m, "Pairing cancelled.")
        raise
    except BaseException as exc:  # noqa: BLE001
        e = classify(exc)
        msg = e.message
        if type(exc).__name__ in ("PairingDialogResponsePendingError", "UserDeniedPairingError"):
            msg = "The iPhone didn't confirm “Trust”. Unlock it and try again."
        _event("failed", m, msg, detail=e.detail or repr(exc))


async def pair_wifi(d, timeout: float):
    ts = d.pmd3.tunnel_service
    m = "wifi"
    try:
        info = ts.PairableHostInfo(name=f"iOS Studio on {platform.node()}")
        _event("waiting", m, PAIR_HELP_WIFI + " Developer Mode must be on.")

        def on_pin(pin):
            _event("show-code", m, "Type this code on your iPhone", code=str(pin))

        def on_wait(elapsed):
            _event("waiting", m, PAIR_HELP_WIFI, elapsed=int(elapsed))

        result = await ts.serve_pairable_host(info, pin_callback=on_pin, timeout=timeout,
                                              waiting_callback=on_wait)
        peer = result.peer_device
        udid = peer.udid or ""
        with contextlib.suppress(Exception):
            link.pin_record(Path(result.record_path), info.identifier)
        d.wifi = link.wifi_records(d.pmd3)
        if udid:
            d.cache.setdefault(udid, {}).update(name=peer.name or "", productType=peer.model or "",
                                                model=marketing_name(peer.model or ""))
            with contextlib.suppress(OSError):
                fileio.save_json(DEVICE_CACHE, d.cache)
        _event("done", m, "Paired over Wi-Fi.", udid=udid, name=peer.name)
        if udid:
            await d.select(udid)
            d.session.reconnect_now()
    except asyncio.CancelledError:
        _event("cancelled", m, "Pairing cancelled.")
        raise
    except (asyncio.TimeoutError, TimeoutError):
        _event("failed", m, "No iPhone started pairing. Open Settings › Developer › Paired Macs on "
               "the iPhone while this window waits (both on the same Wi-Fi).")
    except BaseException as exc:  # noqa: BLE001
        e = classify(exc)
        _event("failed", m, e.message if e.code != "failed" else "Pairing didn't finish. Try again.",
               detail=e.detail or repr(exc))


def register(reg):
    st = {"task": None, "code": None}   # code: future for a 6-digit code (no flow asks for one yet)

    @reg.command("pair.status")
    async def c_pair_status(d, a):
        recs = []
        for u in sorted(set(d.wifi) | d.lockdown):
            recs.append({"udid": u, "wifi": u in d.wifi, "usb": u in d.lockdown, "path": d.wifi.get(u, "")})
        return {"records": recs, "usb": sorted(d.usb)}

    @reg.command("pair.start")
    async def c_pair_start(d, a):
        t = st["task"]
        if t and not t.done():
            raise err("busy", "Pairing is already in progress.")
        method = a.get("method") or ("usb" if d.usb else "wifi")
        if method not in ("usb", "wifi"):
            raise err("bad-args", "Pairing method must be usb or wifi.")
        if method == "usb":
            udid = a.get("udid") or (sorted(d.usb)[0] if d.usb else None)
            if not udid or udid not in d.usb:
                raise err("unreachable", "Plug your iPhone in with a USB cable and unlock it, then try again.")
            st["task"] = asyncio.create_task(pair_usb(d, udid))
        else:
            st["task"] = asyncio.create_task(pair_wifi(d, float(a.get("timeout", 300))))
        return {"started": True, "method": method}

    @reg.command("pair.code")
    async def c_pair_code(d, a):
        fut = st["code"]
        if not fut or fut.done():
            raise err("bad-args", "No pairing code was asked for.")
        code = re.sub(r"\D", "", str(a.get("code", "")))
        if len(code) != 6:
            raise err("bad-args", "The code has 6 digits.")
        fut.set_result(code)
        return {}

    @reg.command("pair.cancel")
    async def c_pair_cancel(d, a):
        t = st["task"]
        if t and not t.done():
            t.cancel()
            with contextlib.suppress(BaseException):
                await t
        return {}

    @reg.on_shutdown
    def cancel(d):
        if st["task"]:
            st["task"].cancel()
