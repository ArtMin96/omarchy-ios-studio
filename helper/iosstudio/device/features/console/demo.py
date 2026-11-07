"""Demo: console.* (same commands as features/console.py) with a made-up app printing.

The fake app writes stdout and stderr on separate streams (so the UI's stderr styling shows),
including the awkward cases: a partial line finished later, invalid UTF-8, an over-long line and a
short burst. It keeps printing a heartbeat until app.kill (or a relaunch) ends it.
"""
from __future__ import annotations

import asyncio
import contextlib
import random

from iosstudio.core.output import HelperError
from iosstudio.device.demo.core import APPS, event, pids, running
from iosstudio.device.features.console import core as app_console

SCRIPT = [
    (0.3, "stdout", b"%(name)s 1.4.0 (12) starting\n"),
    (0.2, "stdout", b"Loaded 3 saved places from UserDefaults\n"),
    (0.4, "stdout", b"Fetching forecast"),                       # print(..., terminator: "")
    (0.5, "stdout", b"... done in 412 ms\n"),
    (0.3, "stderr", b"warning: ForecastCache miss for 'Lisbon', refetching\n"),
    (0.3, "stdout", b"Temperature: 21\xc2\xb0C, feels like 19\xc2\xb0C \xe2\x98\x80\xef\xb8\x8f\n"),
    (0.3, "stdout", b"Raw header bytes: \xff\xfe\x00 (not UTF-8)\n"),
    (0.4, "stdout", b"Decoded payload: " + b'{"hourly":[' + b",".join(b'{"t":%d,"c":21.5}' % i for i in range(900))
     + b"]}\n"),
    (0.3, "stderr", b"Fatal error: Unexpectedly found nil while unwrapping an Optional value (simulated, recovered)\n"),
    (0.5, "stdout", b"ForecastView appeared\n"),
]
BURST = 12   # lines printed at once after the script, like a debug dump in a loop


def _err(code, message, fix=None, detail=None):
    return HelperError(code, message, fix, detail)


def _name(bid: str) -> tuple[str, str]:
    for name, b, *_ in APPS:
        if app_console.match_bundle(b, bid) or name.lower() == str(bid).lower():
            return name, b
    return bid.rsplit(".", 1)[-1], bid


class FakeApp:
    """Writes the script, then a heartbeat, into one queue per stream; b"" ends both."""

    def __init__(self, bid: str, name: str, gen: int, gens: dict):
        self.bid, self.name, self.gen, self.gens = bid, name.encode(), gen, gens
        self.q = {"stdout": asyncio.Queue(), "stderr": asyncio.Queue()}
        self.task = asyncio.create_task(self._run())

    def alive(self) -> bool:
        return self.bid in running and self.gens.get(self.bid) == self.gen

    async def _run(self):
        try:
            for delay, stream, data in SCRIPT:
                await asyncio.sleep(delay)
                if not self.alive():
                    return
                self.q[stream].put_nowait(data.replace(b"%(name)s", self.name))
            for i in range(BURST):
                self.q["stdout"].put_nowait(b"cell %d: height=44.0 reuse=ForecastRow\n" % i)
            beat = 0
            while True:
                await asyncio.sleep(1.0 + random.random())
                if not self.alive():
                    return
                beat += 1
                if beat % 7 == 0:
                    self.q["stderr"].put_nowait(b"warning: location update took %d ms\n" % random.randint(900, 2400))
                else:
                    self.q["stdout"].put_nowait(b"tick %d: %d places up to date\n" % (beat, 3))
        finally:
            for q in self.q.values():
                q.put_nowait(b"")

    async def read(self, stream: str) -> bytes:
        return await self.q[stream].get()

    async def close(self):
        self.task.cancel()
        with contextlib.suppress(BaseException):
            await self.task


class DemoBackend:
    emit = staticmethod(event)
    err = staticmethod(_err)

    def __init__(self):
        self.gens: dict[str, int] = {}

    async def plain_launch(self, ctx, a):
        return await ctx.registry.lookup("app.launch")(ctx, {**a, "console": False})

    async def launch(self, ctx, sess, a):
        name, bid = _name(str(a["bundleId"]))
        sess.name = name
        res = await self.plain_launch(ctx, {**a, "bundleId": bid})
        pids[bid] = res.get("pid") or 812
        self.gens[bid] = self.gens.get(bid, 0) + 1    # a relaunch ends the previous fake app
        app = FakeApp(bid, name, self.gens[bid], self.gens)
        readers = [app_console.Reader("stdout", lambda: app.read("stdout"), app.close),
                   app_console.Reader("stderr", lambda: app.read("stderr"))]
        return {**res, "bundleId": bid}, readers


def register(reg):
    app_console.register_commands(reg, DemoBackend())
