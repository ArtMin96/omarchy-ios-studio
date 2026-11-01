"""Demo: screenshot, mirror.start, mirror.stop (painted frames; the mirror is a shared resource)."""
from __future__ import annotations

import threading
import time

import demo_art
from deviced_demo_core import DIR, event

KEY = "mirror"


class Mirror:
    def __init__(self):
        self.on = threading.Event()
        self.fps = 1.0
        self.thread: threading.Thread | None = None

    def loop(self) -> None:
        seq = 0
        while True:
            if not self.on.is_set():
                time.sleep(0.2)
                continue
            seq += 1
            p = DIR / "frames" / f"frame-{seq % 3}.png"
            demo_art.phone_screen(p, seq)
            event("frame", {"path": str(p), "seq": seq, "ts": time.time(), "width": 390, "height": 844})
            time.sleep(1.0 / max(0.2, self.fps))

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self.loop, daemon=True)
            self.thread.start()
        self.on.set()
        return self

    def stop(self, *_):
        self.on.clear()
        event("mirror", {"running": False, "fps": self.fps})


def register(reg):
    m = Mirror()

    @reg.command("screenshot")
    def screenshot(ctx, a):
        p = demo_art.phone_screen(DIR / "screens" / f"screenshot-{int(time.time())}.png")
        return {"path": str(p), "width": 390, "height": 844, "bytes": p.stat().st_size, "ts": time.time()}

    @reg.command("mirror.start")
    async def mirror_start(ctx, a):
        m.fps = max(0.2, min(4.0, float(a.get("fps") or 1.0)))
        await ctx.resources.acquire(KEY, ctx.client(), m.start, m.stop, scope="session")
        event("mirror", {"running": True, "fps": m.fps})
        return {"running": True, "fps": m.fps}

    @reg.command("mirror.stop")
    async def mirror_stop(ctx, a):
        held = ctx.resources.held(KEY)
        left = await ctx.resources.release(KEY, ctx.client())
        if left:
            return {"running": False, "sharedWith": left}
        if not held:   # (when held, the release above already stopped it)
            m.stop()
        return {"running": False}

    @reg.welcome
    def snapshot(ctx):
        return [("mirror", {"running": True, "fps": m.fps})] if m.on.is_set() else []

    @reg.on_shutdown
    def stop(ctx):
        m.on.clear()
