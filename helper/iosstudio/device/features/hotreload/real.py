"""Hot reload: hotreload.push (send a signed patch and wait for the app's ack), hotreload.clean (delete old
patches and acks) and hotreload.runtime (is the hot reload runtime in this build: its runtime.json).

Your own development builds only: everything goes through the app's whole container (house_arrest
VendContainer) under Library/Caches/HotReload, on the files feature's cached connection per app (F.on_container:
a kept connection that went stale is retried once on a fresh one). A push writes patch-<seq>.dylib.part, renames
it to patch-<seq>.dylib (the runtime only looks at whole names), then reads ack-<seq>.json every 50 ms until it
is there or waitMs (≤ 15 s) has passed. Uses the files feature's helpers, so it loads only when that one does.
"""
from __future__ import annotations

import asyncio
import contextlib
import posixpath
import time

from pymobiledevice3.exceptions import AfcException, AfcFileNotFoundError, AppNotInstalledError

from iosstudio.core.output import HelperError, fix
from iosstudio.device.features.files import core as df
from iosstudio.device.features.files import real as F
from iosstudio.device.features.hotreload import core as hr
from iosstudio.device.real.core import dlog, err

RUN_FIX = fix("Run the app", "projects", action="build")


async def _app(d, s, bid: str) -> dict:
    """The app's info (files feature cache); not-installed / not-dev-app."""
    try:
        info = await F.app_info(d, s, bid)
    except HelperError as exc:
        if exc.code == "not-found":
            raise err("not-installed", hr.NOT_INSTALLED, RUN_FIX) from None
        raise
    if not info["developer"]:
        raise err("not-dev-app", hr.NOT_DEV)
    return info


def _error(exc: BaseException) -> BaseException:
    if isinstance(exc, AppNotInstalledError):
        return err("not-installed", hr.NOT_INSTALLED, RUN_FIX, str(exc))
    return F.afc_error(exc, "the app's hot reload folder")


async def _call(s, bid: str, fn):
    try:
        return await F.on_container(s, bid, False, fn)
    except (asyncio.CancelledError, HelperError):
        raise
    except BaseException as exc:  # noqa: BLE001
        raise _error(exc) from None


async def _read_small(afc, path: str) -> bytes | None:
    """At most ACK_MAX + 1 bytes of a phone file; None when it isn't there."""
    try:
        h = await afc.fopen(path, "r")
    except AfcFileNotFoundError:
        return None
    try:
        out = bytearray()
        while len(out) <= hr.ACK_MAX:
            chunk = await afc.fread(h, hr.ACK_MAX + 1 - len(out))
            if not chunk:
                break
            out += chunk
        return bytes(out)
    finally:
        with contextlib.suppress(BaseException):
            await asyncio.shield(afc.fclose(h))


async def _write(afc, data: bytes, final: str) -> None:
    """data -> final through final.part and a rename (so the runtime never loads half a patch)."""
    if not await afc.exists(hr.DIR):
        await afc.makedirs(hr.DIR)
    part = final + ".part"
    h = await afc.fopen(part, "w")
    try:
        for i in range(0, len(data), df.CHUNK):
            await afc.fwrite(h, data[i:i + df.CHUNK])
    finally:
        with contextlib.suppress(BaseException):
            await asyncio.shield(afc.fclose(h))
    try:
        await afc.rename(part, final)
    except AfcFileNotFoundError:
        raise
    except AfcException:          # some iOS versions refuse to rename over a file: replace it instead
        with contextlib.suppress(AfcFileNotFoundError):
            await afc.rm(final, force=True)
        await afc.rename(part, final)


def register(reg):
    locks: dict[str, asyncio.Lock] = {}

    @reg.command("hotreload.push")
    async def c_push(d, a):
        s = d.need_session(a)
        bid, seq, src, wait = hr.push_args(a)
        await _app(d, s, bid)
        data = await asyncio.to_thread(src.read_bytes)
        final = hr.patch_path(seq)
        async with locks.setdefault(bid, asyncio.Lock()):
            t0 = time.monotonic()
            await _call(s, bid, lambda afc: _write(afc, data, final))
            push_ms = int((time.monotonic() - t0) * 1000)
            t1 = time.monotonic()
            deadline = t1 + wait / 1000
            ack_file = hr.ack_path(seq)
            raw = None
            while True:
                raw = await _call(s, bid, lambda afc: _read_small(afc, ack_file))
                if raw is not None or time.monotonic() >= deadline:
                    break
                await asyncio.sleep(hr.POLL_S)
        out = {"seq": seq, "bytes": len(data), "pushMs": push_ms, "acked": raw is not None}
        if raw is not None:
            out["ack"] = hr.parse_ack(raw, seq)
            out["ackMs"] = int((time.monotonic() - t1) * 1000)
        dlog(f"hotreload: patch {seq} for {bid}: {len(data)} bytes in {push_ms} ms, "
             f"{'acked' if raw is not None else 'no ack'}")
        return out

    @reg.command("hotreload.clean")
    async def c_clean(d, a):
        s = d.need_session(a)
        bid = hr.bundle_id(a)
        await _app(d, s, bid)

        async def clean(afc):
            await afc.stat("/")    # a stale connection answers "not found" for everything: retried by on_container
            try:
                names = await afc.listdir(hr.DIR)
            except AfcFileNotFoundError:
                return 0
            removed = 0
            for n in names:
                if not hr.is_ours(n):
                    continue
                with contextlib.suppress(AfcFileNotFoundError):
                    await afc.rm(posixpath.join(hr.DIR, n), force=True)
                    removed += 1
            return removed
        async with locks.setdefault(bid, asyncio.Lock()):
            return {"removed": await _call(s, bid, clean)}

    @reg.command("hotreload.runtime")
    async def c_runtime(d, a):
        s = d.need_session(a)
        bid = hr.bundle_id(a)
        await _app(d, s, bid)
        async def read(afc):
            await afc.stat("/")    # a stale connection answers "not found" for everything: retried by on_container
            return await _read_small(afc, hr.RUNTIME)
        raw = await _call(s, bid, read)
        rt = hr.parse_runtime(raw) if raw is not None else None
        return {"present": rt is not None, **({"runtime": rt} if rt else {})}
