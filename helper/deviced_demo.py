"""Fake device daemon for demo mode (IOSSTUDIO_DEMO set): same protocol as deviced.py
(docs/deviced-protocol.md) with a made-up iPhone. Stdlib only; never talks to a real device.

`--serve` runs it as the shared broker (helper/device_broker.py) on the demo socket
($XDG_RUNTIME_DIR/ios-studio-demo/deviced.sock, or under $IOSSTUDIO_RUNTIME_DIR), so the GUI and
MCP evaluations can share one fake iPhone. `--once <cmd> '<json>'` answers one request and exits.

Commands live in helper/deviced_demo_features/ (one module per feature, like the real daemon's
helper/deviced_features/); the fake iPhone's data is in deviced_demo_core.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import deviced_demo_core as core  # noqa: E402
import device_broker  # noqa: E402
import deviced_kit as kit  # noqa: E402
from deviced_demo_core import DEVICE, FRESH, INFO, SCENARIO, STARTED, UDID  # noqa: E402


def log(text: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] deviced-demo: {text}", file=sys.stderr, flush=True)


class DemoBackend:
    """device_broker backend for the fake iPhone; also the `ctx` demo feature handlers get."""

    def __init__(self, broker: device_broker.Broker | None):
        self.b = broker
        self.resources = broker.resources if broker is not None else kit.Resources(log=log)
        self.registry = kit.FeatureRegistry(log=log)
        self._register_core(self.registry.module("core"))
        kit.load_features(self.registry, "deviced_demo_features")

    def client(self):
        return device_broker.current_client() if self.b is not None else None

    def _register_core(self, reg):
        reg.command("daemon.ping", lambda ctx, a: {"pong": True, "ts": time.time()})
        reg.command("devices.list", lambda ctx, a: [] if FRESH else [DEVICE])
        reg.command("device.select", lambda ctx, a: DEVICE)
        reg.command("device.reconnect", lambda ctx, a: {})
        reg.command("device.info", lambda ctx, a: INFO)

        @reg.command("daemon.features")
        def features(ctx, a):
            return {**ctx.registry.describe(), "resources": ctx.resources.describe()}
        reg.commit()

    async def welcome(self):
        out = [("ready", {"version": "1", "pymobiledevice3": "demo", "python": sys.version.split()[0], "pid": os.getpid(),
                          "demo": SCENARIO})]
        if FRESH:
            out += [("devices", []), ("connection", {"udid": None, "state": "idle", "transport": None,
                                                     "message": "No iPhone selected", "detail": "", "retryIn": 0,
                                                     "since": time.time()})]
        else:
            out += [("devices", [DEVICE]), ("connection", {"udid": UDID, "state": "connected", "transport": "wifi",
                                                           "message": "Connected over Wi-Fi", "detail": "", "retryIn": 0,
                                                           "since": STARTED - 900}), ("deviceInfo", INFO)]
        out.extend(await self.registry.welcome(self))
        return out

    async def dispatch(self, cmd: str, a: dict):
        fn = self.registry.lookup(cmd)
        if fn is None:
            raise LookupError(f"Unknown command {cmd!r}.")
        if inspect.iscoroutinefunction(fn):
            return await fn(self, a)
        return await asyncio.to_thread(fn, self, a)   # sync handlers may sleep (app.install)

    def error_json(self, exc: BaseException, cmd: str) -> dict:
        if isinstance(exc, LookupError):
            return {"code": "bad-args", "message": str(exc).strip("'\"")}
        return {"code": "failed", "message": "Demo mode can't do that.", "detail": str(exc)}

    # syslog: one fake stream (deviced_demo_features/syslog.py), filtered per client by the broker
    def _syslog(self):
        fake = self.registry.provided.get("syslog")
        if fake is None:
            raise RuntimeError("the demo syslog module didn't load")
        return fake

    def syslog_target(self, args):
        return self._syslog().target(args)

    def syslog_open(self, pid: int):
        self._syslog().open(self.b.syslog, pid)

    async def syslog_close(self):
        fake = self.registry.provided.get("syslog")
        if fake is not None:
            await fake.close()

    async def syslog_resolve(self, bid: str):
        return await self._syslog().resolve(bid)

    async def client_gone(self, client):
        await self.registry.run_hooks("client_gone", self, client)

    async def shutdown(self):
        await self.resources.release_all("shutdown")
        await self.registry.run_hooks("shutdown", self)


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
    backend = DemoBackend(None)
    try:
        data = asyncio.run(backend.dispatch(cmd, json.loads(raw or "{}")))
        print(json.dumps({"ok": True, "data": data}))
        return 0
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": backend.error_json(exc, cmd)}))
        return 1


def main() -> int:
    if len(sys.argv) >= 3 and sys.argv[1] == "--once":
        return once(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "{}")
    if len(sys.argv) >= 2 and sys.argv[1] == "--serve":
        code = asyncio.run(serve())
        os._exit(code)
    print("usage: deviced_demo.py --serve | --once <cmd> '<json>'  (normally started by bin/iosstudio-deviced)",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
