"""Fake device daemon for demo mode (IOSSTUDIO_DEMO set): same protocol as deviced.py
(docs/deviced-protocol.md) with a made-up iPhone. Stdlib only; never talks to a real device.

`--serve` runs it as the shared broker (helper/iosstudio/device/broker.py) on the demo socket
($XDG_RUNTIME_DIR/ios-studio-demo/deviced.sock, or under $IOSSTUDIO_RUNTIME_DIR), so the GUI and
MCP evaluations can share one fake iPhone. `--once <cmd> '<json>'` answers one request and exits.

Commands live in helper/iosstudio/device/features/<name>/demo.py (one module per feature, next to the real
daemon's real.py); the fake iPhone's data is in demo/core.py.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

from iosstudio.device import broker as device_broker
from iosstudio.device import protocol
from iosstudio.device.backend import Backend
from iosstudio.device.demo import core
from iosstudio.device.demo.core import DEVICE, FRESH, INFO, SCENARIO, STARTED, UDID


def log(text: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] deviced-demo: {text}", file=sys.stderr, flush=True)


class DemoBackend(Backend):
    """device_broker backend for the fake iPhone; also the `ctx` demo feature handlers get."""

    features = "demo"

    def __init__(self, broker: device_broker.Broker | None):
        self.b = broker
        super().__init__(broker.resources if broker is not None else None, log)

    def register_core(self, reg):
        reg.command("daemon.ping", lambda ctx, a: {"pong": True, "ts": time.time()})
        reg.command("devices.list", lambda ctx, a: [] if FRESH else [DEVICE])
        reg.command("device.select", lambda ctx, a: DEVICE)
        reg.command("device.reconnect", lambda ctx, a: {})
        reg.command("device.info", lambda ctx, a: {**INFO, **ctx.registry.info(ctx, None)})

        @reg.command("daemon.shutdown")
        async def shutdown(ctx, a):   # --once only; the broker answers it for its clients
            return {}

        @reg.command("daemon.features")
        def features(ctx, a):
            return {**ctx.registry.describe(), "resources": ctx.resources.describe()}

    async def welcome(self):
        out = [("ready", {"version": "1", "pymobiledevice3": "demo", "python": sys.version.split()[0], "pid": os.getpid(),
                          "demo": SCENARIO})]
        if FRESH:
            out += [("devices", []), ("connection", {"udid": None, "state": "idle", "transport": None,
                                                     "message": "No iPhone selected", "detail": "", "retryIn": 0,
                                                     "since": time.time()})]
        else:
            usb = DEVICE.get("transport") == "usb"   # demo.transport can plug the made-up cable in
            out += [("devices", [DEVICE]), ("connection", {"udid": UDID, "state": "connected",
                                                           "transport": "usb" if usb else "wifi",
                                                           "message": "Connected over USB" if usb else "Connected over Wi-Fi",
                                                           "detail": "", "retryIn": 0,
                                                           "since": STARTED - 900}), ("deviceInfo", INFO)]
        out.extend(await super().welcome())
        return out

    def error_json(self, exc: BaseException, cmd: str) -> dict:
        if isinstance(exc, LookupError):   # a demo handler's missing key / unknown item
            return {"code": "bad-args", "message": str(exc).strip("'\"")}
        return protocol.error_json(exc, "Demo mode can't do that.")

    # syslog: one fake stream (features/syslog/demo.py), filtered per client by the broker
    def syslog_target(self, args):
        return self._syslog().target(args)

    def syslog_open(self, pid: int):
        self._syslog().open(self.b.syslog, pid)

    async def syslog_resolve(self, bid: str):
        return await self._syslog().resolve(bid)


async def serve() -> int:
    broker = device_broker.Broker(demo=SCENARIO)
    core.BROKER = broker
    if not broker.acquire():
        return 0   # another demo daemon is live
    broker.redirect_stderr()

    async def make(b):
        backend = DemoBackend(b)
        log(f"features: {', '.join(backend.registry.loaded)}"
            + (f"; failed: {', '.join(backend.registry.failed)}" if backend.registry.failed else ""))
        return backend
    return await broker.run(make)


def once(cmd: str, raw: str) -> int:
    """`--once <cmd> '<json>'`: one reply on stdout, same contract as the real daemon's --once."""
    async def run() -> dict:
        backend = DemoBackend(None)
        try:
            return await backend.handle(protocol.request(None, cmd, protocol.parse_args(raw)))
        finally:
            await backend.shutdown()
    try:
        out = asyncio.run(run())
    except protocol.DeviceError as exc:   # bad args
        out = protocol.reply_error(None, exc)
    out.pop("id", None)
    protocol.write_line(sys.stdout.buffer, out)
    return 0 if out.get("ok") else 1


def main() -> int:
    if len(sys.argv) >= 3 and sys.argv[1] == "--once":
        return once(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "{}")
    if len(sys.argv) >= 2 and sys.argv[1] == "--serve":
        code = asyncio.run(serve())
        os._exit(code)
    print("usage: demo/daemon.py --serve | --once <cmd> '<json>'  (normally started by bin/iosstudio-deviced)",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
