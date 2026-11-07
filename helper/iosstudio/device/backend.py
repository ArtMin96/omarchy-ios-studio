"""The device daemon backend contract: what the broker (device_broker.py) calls on a daemon. Stdlib only.

Three backends subclass Backend: deviced.Daemon (the real iPhone), deviced.BrokenBackend
(pymobiledevice3 unusable: every request answers with one error) and deviced_demo.DemoBackend (the
made-up iPhone). The backend is also the `ctx` / `d` every feature handler gets as its first
argument: ctx.registry, ctx.resources, ctx.bg, ctx.client().

What the broker calls:
    async welcome() -> [(event, data), ...]     sent to every new client (ready, devices, ...)
    async dispatch(cmd, args) -> data           raise on error; current_client() tells who asks
    error_json(exc, cmd) -> dict                exception -> protocol error object
    syslog_target(args) -> udid                 raise if no device for a syslog stream
    syslog_open(pid: int)                       start the one phone stream (-1 = no phone filter)
    async syslog_close()
    async syslog_resolve(bundle_id) -> (executable | None, full bundle id)
    async client_gone(client)                   a client disconnected (after its resources)
    async shutdown()                            close the tunnel and everything else
"""
from __future__ import annotations

import asyncio
import inspect
from typing import Callable

from iosstudio.device import deviced_kit as kit
from iosstudio.device import protocol
from iosstudio.device.protocol import DeviceError


class TaskSet:
    """Background tasks a backend starts and doesn't await at once (closing a service after a tunnel
    drop, a deviceInfo refresh, ...). Each is kept until it ends, so none is garbage-collected
    mid-flight or outlives the daemon: close() waits a moment for them, then cancels the rest."""

    def __init__(self, log: Callable[[str], None] | None = None):
        self.tasks: set[asyncio.Task] = set()
        self.log = log

    def spawn(self, coro, name: str | None = None) -> asyncio.Task:
        t = asyncio.get_running_loop().create_task(coro, name=name)
        self.tasks.add(t)
        t.add_done_callback(self._done)
        return t

    def _done(self, t: asyncio.Task) -> None:
        self.tasks.discard(t)
        if not t.cancelled() and t.exception() is not None and self.log is not None:
            self.log(f"background task {t.get_name()} failed: {t.exception()!r}")

    def __len__(self) -> int:
        return len(self.tasks)

    def __iter__(self):
        return iter(list(self.tasks))

    async def close(self, grace: float = 0.0) -> None:
        """Wait up to `grace` seconds for running tasks, then cancel what's left and wait for it."""
        pending = [t for t in self.tasks if not t.done()]
        if pending and grace > 0:
            _, pending = await asyncio.wait(pending, timeout=grace)
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


def unknown_command(cmd: str) -> DeviceError:
    return DeviceError("bad-args", f"Unknown command {cmd!r}.")


class Backend:
    """Shared dispatch, errors, hooks and background tasks; subclasses add welcome() and syslog_*.

    `features` names the feature package loaded into the registry ("features", "demo_features");
    None loads none. Subclasses register their own core commands in register_core(reg)."""

    features: str | None = None

    def __init__(self, resources: kit.Resources | None = None, log: Callable[[str], None] = print):
        self.log = log
        self.resources = resources or kit.Resources(log=log)
        self.bg = TaskSet(log)                  # fire-and-forget work, cancelled at shutdown
        self.registry = kit.FeatureRegistry(log=log)
        core = self.registry.module("core")
        self.register_core(core)
        core.commit()
        if self.features:
            kit.load_features(self.registry, self.features)
            if self.registry.failed:
                log(f"features that failed to load: {', '.join(sorted(self.registry.failed))}")

    def register_core(self, reg) -> None:
        """Register the backend's own commands (daemon.ping, device.info, ...) on `reg`."""

    def client(self):
        """The broker client whose request is running (key for ctx.resources); None outside the broker."""
        from iosstudio.device import device_broker
        return device_broker.current_client()

    # -- requests
    async def dispatch(self, cmd: str, args: dict):
        """Run a command's handler: `async def` handlers are awaited, plain ones run in a worker thread
        (they may block, e.g. the demo's sleeps), so neither ever stalls the event loop."""
        fn = self.registry.lookup(cmd)
        if fn is None:
            raise unknown_command(cmd)
        args = args if isinstance(args, dict) else {}
        if inspect.iscoroutinefunction(fn):
            return await fn(self, args)
        return await kit.maybe_await(await asyncio.to_thread(fn, self, args))

    def error_json(self, exc: BaseException, cmd: str) -> dict:
        return protocol.error_json(exc)

    async def handle(self, msg: dict) -> dict:
        """One request message -> its reply (--once mode; the broker builds replies itself)."""
        rid = msg.get("id")
        cmd = str(msg.get("cmd") or "")
        try:
            return protocol.reply_ok(rid, await self.dispatch(cmd, msg.get("args") or {}))
        except asyncio.CancelledError:
            return protocol.reply_error(rid, protocol.CANCELLED)
        except BaseException as exc:  # noqa: BLE001
            return protocol.reply_error(rid, self.error_json(exc, cmd))

    # -- broker hooks
    async def welcome(self) -> list:
        return await self.registry.welcome(self)

    def _syslog(self):
        """The log stream a feature module provided ("syslog"), or an error saying it's missing."""
        stream = self.registry.provided.get("syslog")
        if stream is None:
            raise DeviceError("failed", "The live log isn't available (its module failed to load; see deviced.log).")
        return stream

    def syslog_target(self, args: dict) -> str:
        raise NotImplementedError

    def syslog_open(self, pid: int) -> None:
        raise NotImplementedError

    async def syslog_close(self) -> None:
        stream = self.registry.provided.get("syslog")
        if stream is not None:
            await stream.close()

    async def syslog_resolve(self, bid: str):
        return None, bid

    async def client_gone(self, client) -> None:
        await self.registry.run_hooks("client_gone", self, client)

    async def shutdown(self) -> None:
        await self.resources.release_all("shutdown")
        await self.registry.run_hooks("shutdown", self)
        await self.bg.close(grace=3)   # let bounded closes finish, cancel the rest
