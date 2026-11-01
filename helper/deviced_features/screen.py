"""screenshot, mirror.start, mirror.stop (DVT screenshots; the mirror is a shared session resource)."""
from __future__ import annotations

import asyncio
import contextlib
import time

from deviced_core import SCREENS_DIR, classify, dlog, emit, err, png_size, safe_name, write_bytes


async def screenshot(s) -> bytes:
    """One PNG over the shared DVT connection (rebuilt once if it broke)."""
    async def take():
        shot = await s.dvt_instrument("screenshot", s.pmd3.Screenshot)
        return await asyncio.wait_for(shot.get_screenshot(), 15)
    return await s.dvt_retry(take, err("failed", "Couldn't take a screenshot."))


class Mirror:
    KEY = "mirror"

    def __init__(self):
        self.task: asyncio.Task | None = None
        self.fps = 1.0

    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    async def stop(self, *_):
        t, self.task = self.task, None
        if t:
            t.cancel()
            with contextlib.suppress(BaseException):
                await t
            emit("mirror", {"running": False, "fps": self.fps})

    async def run(self, d, s):
        seq = 0
        fails = 0
        safe = safe_name(s.udid)
        while True:
            t0 = time.monotonic()
            try:
                png = await screenshot(s)
                fails = 0
                path = SCREENS_DIR / f"mirror-{safe}-{seq % 2}.png"
                await asyncio.to_thread(write_bytes, path, png)
                w, h = png_size(png)
                seq += 1
                emit("frame", {"path": str(path), "seq": seq, "ts": time.time(), "width": w, "height": h})
            except asyncio.CancelledError:
                raise
            except BaseException as exc:  # noqa: BLE001
                fails += 1
                e = classify(exc)
                dlog(f"mirror: {exc!r}")
                if e.code == "unreachable" and fails < 30:
                    await asyncio.sleep(2)
                    continue
                self.task = None
                d.resources.forget(self.KEY)   # ended by itself: every holder is released
                emit("mirror", {"running": False, "fps": self.fps, "error": e.to_json()})
                return
            delay = 1.0 / self.fps - (time.monotonic() - t0)
            if delay > 0:
                await asyncio.sleep(delay)


def register(reg):
    m = Mirror()

    @reg.command("screenshot")
    async def c_screenshot(d, a):
        s, _ = await d.rsd(a)
        png = await screenshot(s)
        path = SCREENS_DIR / f"shot-{time.strftime('%Y%m%d-%H%M%S')}.png"
        await asyncio.to_thread(write_bytes, path, png)
        w, h = png_size(png)
        return {"path": str(path), "width": w, "height": h, "bytes": len(png), "ts": time.time()}

    @reg.command("mirror.start")
    async def c_mirror_start(d, a):
        s = d.need_session(a)
        try:
            fps = float(a.get("fps", 1.0))
        except (TypeError, ValueError):
            fps = 1.0
        m.fps = max(0.2, min(4.0, fps))

        async def open_():
            if not m.running():
                await s.get_rsd()
                m.task = asyncio.create_task(m.run(d, s))
            return m

        # Reference counted per client: the frames stop when the last holder stops or disconnects.
        await d.resources.acquire(Mirror.KEY, d.client(), open_, m.stop, scope="session")
        emit("mirror", {"running": True, "fps": m.fps})
        return {"running": True, "fps": m.fps}

    @reg.command("mirror.stop")
    async def c_mirror_stop(d, a):
        left = await d.resources.release(Mirror.KEY, d.client())
        if left:
            return {"running": False, "sharedWith": left}
        await m.stop()
        return {"running": False}

    @reg.welcome
    def snapshot(d):
        return [("mirror", {"running": True, "fps": m.fps})] if m.running() else []
