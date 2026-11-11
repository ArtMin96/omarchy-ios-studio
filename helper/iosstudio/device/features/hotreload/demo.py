"""Demo: hotreload.push / clean / runtime on the made-up iPhone. A push takes about 300 ms and the made-up
runtime always answers (3 functions swapped); every development app has the runtime. Nothing is stored."""
from __future__ import annotations

import asyncio

from iosstudio.core.output import HelperError, fix
from iosstudio.core.subproc import demo_delay
from iosstudio.device.demo.core import APPS, STARTED, installed, pids
from iosstudio.device.features.hotreload import core as hr

RUN_FIX = fix("Run the app", "projects", action="build")


def _app(a: dict) -> str:
    bid = hr.bundle_id(a)
    for _name, b, _hue, _shape, dev, *_ in APPS:
        if (b == bid or b.endswith("." + bid)) and b in installed:
            if not dev:
                raise HelperError("not-dev-app", hr.NOT_DEV)
            return b
    raise HelperError("not-installed", hr.NOT_INSTALLED, RUN_FIX)


def register(reg):
    generation: dict[str, int] = {}

    @reg.command("hotreload.push")
    async def push(ctx, a):
        bid, seq, src, _wait = hr.push_args(a)
        bid = _app({"bundleId": bid})
        size = src.stat().st_size
        await asyncio.sleep(demo_delay(0.25))
        await asyncio.sleep(demo_delay(0.05))
        generation[bid] = generation.get(bid, 0) + 1
        ack = {"seq": seq, "ok": True, "ms": 3.1, "swapped": 3, "kept": 9, "vtable": 0, "reverted": 0, "error": None,
               "generation": generation[bid]}
        return {"seq": seq, "bytes": size, "pushMs": 250, "acked": True, "ack": ack, "ackMs": 50}

    @reg.command("hotreload.clean")
    def clean(ctx, a):
        _app(a)
        return {"removed": 0}

    @reg.command("hotreload.runtime")
    def runtime(ctx, a):
        bid = _app(a)
        return {"present": True, "runtime": {"version": 1, "pid": pids.get(bid), "started": float(int(STARTED))}}
