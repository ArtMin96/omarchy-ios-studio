"""screenshot (one DVT screenshot). The live view and recordings are in mirror.py."""
from __future__ import annotations

import asyncio
import time

from iosstudio.core import fileio
from iosstudio.device.deviced_core import SCREENS_DIR, err, png_size, write_bytes

SHOTS_KEEP = 30                 # screenshots kept in ~/.cache/ios-studio/screens (newest first)
SHOTS_MAX_BYTES = 300 << 20


async def screenshot(s) -> bytes:
    """One PNG over the shared DVT connection (rebuilt once if it broke)."""
    async def take():
        shot = await s.dvt_instrument("screenshot", s.pmd3.Screenshot)
        return await asyncio.wait_for(shot.get_screenshot(), 15)
    return await s.dvt_retry(take, err("failed", "Couldn't take a screenshot."))


def register(reg):
    @reg.command("screenshot")
    async def c_screenshot(d, a):
        s, _ = await d.rsd(a)
        png = await screenshot(s)
        path = SCREENS_DIR / f"shot-{time.strftime('%Y%m%d-%H%M%S')}.png"
        await asyncio.to_thread(write_bytes, path, png)
        await asyncio.to_thread(fileio.prune, SCREENS_DIR.glob("shot-*.png"), SHOTS_KEEP, SHOTS_MAX_BYTES)
        w, h = png_size(png)
        return {"path": str(path), "width": w, "height": h, "bytes": len(png), "ts": time.time()}
