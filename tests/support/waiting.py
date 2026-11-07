"""Wait for something that happens on the event loop (a request reaching a fake, an event reaching a job) instead of
sleeping a fixed time and hoping it happened: the wait ends as soon as it did, and a slow machine only fails at the
timeout."""
from __future__ import annotations

import asyncio


async def until(cond, /, timeout: float = 5.0, what: str = ""):
    """Return cond()'s first truthy value (cond may be a plain or an async function); AssertionError after timeout."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        got = cond()
        if asyncio.iscoroutine(got):
            got = await got
        if got:
            return got
        if loop.time() > deadline:
            raise AssertionError(f"timed out waiting for {what or cond}")
        await asyncio.sleep(0.01)
