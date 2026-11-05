"""Demo: screenshot (a painted frame). The live view and recordings are in mirror.py."""
from __future__ import annotations

import time

import common
import demo_art
from deviced_demo_core import DIR


def register(reg):
    @reg.command("screenshot")
    def screenshot(ctx, a):
        p = demo_art.phone_screen(DIR / "screens" / f"screenshot-{int(time.time())}.png")
        common.prune((DIR / "screens").glob("screenshot-*.png"), 30, 300 << 20)   # as the real daemon
        return {"path": str(p), "width": 390, "height": 844, "bytes": p.stat().st_size, "ts": time.time()}
