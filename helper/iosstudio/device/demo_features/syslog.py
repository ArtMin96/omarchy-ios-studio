"""Demo: the fake phone log stream behind syslog.start/filter/stop (filtered per client by the
broker's SyslogHub). Provided to the backend as "syslog"."""
from __future__ import annotations

import asyncio
import contextlib
import random
import time

from iosstudio.device.deviced_demo_core import APPS, FRESH, UDID

LOG_LINES = [
    ("WeatherGlance", "default", "com.example.weatherglance", "network", "Fetching forecast for 3 saved places"),
    ("WeatherGlance", "info", "com.example.weatherglance", "location", "Location authorization: authorizedWhenInUse"),
    ("WeatherGlance", "default", "com.example.weatherglance", "network", "Forecast updated in 412 ms (24 hourly, 10 daily)"),
    ("WeatherGlance", "debug", "com.example.weatherglance", "ui", "ForecastView appeared"),
    ("WeatherGlance", "error", "com.example.weatherglance", "network", "Request timed out for place 'Lisbon', retrying in 2 s"),
    ("WeatherGlance", "default", "com.apple.UIKit", "scene", "Scene did become active"),
    ("SpringBoard", "default", "com.apple.springboard", "app", "Application launched: com.example.weatherglance"),
    ("WeatherGlance", "fault", "com.example.weatherglance", "store", "Cache file is corrupt, rebuilding"),
    ("WeatherGlance", "info", "com.example.weatherglance", "widgets", "Reloaded 2 widget timelines"),
]


class FakeSyslog:
    def __init__(self):
        self.task: asyncio.Task | None = None
        self.hub = None

    def target(self, args):
        if FRESH:
            raise RuntimeError("no device")
        return UDID

    def open(self, hub, pid: int):
        self.hub = hub
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._run())

    async def close(self):
        t, self.task = self.task, None
        if t:
            t.cancel()
            with contextlib.suppress(BaseException):
                await t

    async def resolve(self, bid: str):
        for name, b, *_ in APPS:
            if b == bid or b.endswith("." + bid):
                return name.replace(" ", ""), b
        return None, bid

    async def _run(self):
        i = 0
        while True:
            await asyncio.sleep(0.7)
            for _ in range(random.randint(1, 3)):
                proc, lvl, sub, cat, msg = LOG_LINES[i % len(LOG_LINES)]
                i += 1
                self.hub.feed({"ts": time.time(), "process": proc, "pid": 812 if proc != "SpringBoard" else 54, "level": lvl,
                               "subsystem": sub, "category": cat, "message": msg, "image": proc})


def register(reg):
    fake = FakeSyslog()
    reg.provide("syslog", fake)
    reg.on_shutdown(lambda ctx: fake.close())
