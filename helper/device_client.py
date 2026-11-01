"""Python client for the shared iOS Studio device daemon (stdlib asyncio only).

The device daemon (`bin/iosstudio-deviced`) is a per-user broker that owns the one tunnel to the
iPhone. The GUI and any number of other clients (e.g. the MCP server) talk to it over a Unix socket
with the JSON-lines protocol in docs/deviced-protocol.md. This module is that client. It is stdlib
only, so it can be imported from the repo (`sys.path.insert(0, "<repo>/helper")`) or vendored.

API
---

    from device_client import DeviceClient, DeviceError, socket_path

    async with DeviceClient(name="mcp") as dev:           # connects; spawns the daemon if needed
        info = await dev.request("device.info", {}, timeout=40)   # -> reply data (dict / list)
        apps = await dev.request("apps.list", {"icons": False})

        # events: async iterator (each call gets its own queue) ...
        async with dev.events(["syslog", "syslogState"]) as stream:
            await dev.request("syslog.start", {"filters": {"processes": ["MyApp"], "minLevel": "info"}})
            async for name, data in stream:                # (event name, event data)
                ...
        await dev.request("syslog.stop")                   # stops only *this* client's syslog

        # ... or a callback (sync or async); returns a function that removes it
        off = dev.on_event(lambda name, data: print(name), ["connection", "devices"])
        off()

class DeviceClient(name="client", events=None, *, demo=None, spawn=True, reconnect=True,
                   connect_timeout=30.0, deviced=None, socket=None)
    name             shown in `daemon.clients` and the daemon log.
    events           event names this client wants pushed (None = all, [] = none). Sent with `hello`;
                     change later with `await dev.subscribe([...])`. Replies always arrive.
                     Per-client events (`syslog`, `syslogState` for this client's stream) are always
                     sent to the client that asked for them, whatever the subscription.
    demo             None = follow $IOSSTUDIO_DEMO; True/str = demo daemon (fake iPhone, no phone
                     needed; str = scenario "ready"|"expiring"|"fresh"); False = real phone.
    spawn            start `bin/iosstudio-deviced --serve` (detached, own session) when no daemon
                     is listening, then wait for its socket.
    reconnect        after the daemon goes away, reconnect in the background (spawning a new one)
                     and re-send `hello`. In-flight requests fail with DeviceError("disconnected").
                     Daemon-side state of this client (syslog subscription, mirror reference) is
                     lost on reconnect; listen for the synthetic "reconnected" event to redo it.
    deviced / socket override the launcher path / socket path (tests).

  await connect()          connect (idempotent). Raises DeviceError("daemon-unavailable") on failure.
  await close()            disconnect. The daemon keeps running for other clients; it exits by itself
                           after its idle grace period (IOSSTUDIO_DEVICED_IDLE, default 120 s).
  await request(cmd, args=None, timeout=60.0) -> data
                           one request. Raises DeviceError(code, message, fix, detail) for an error
                           reply, code "timeout" after `timeout` s, "disconnected" if the daemon went
                           away mid-request. Connects (and spawns) on demand.
  await subscribe(events)  replace this client's event subscription (None = all).
  events(names=None, maxsize=1000) -> async context manager + async iterator of (name, data);
                           oldest items are dropped when the consumer is slower than `maxsize`.
  on_event(callback, names=None) -> unsubscribe()
                           callback(name, data); may be a coroutine function.
  connected: bool          hello: dict | None (the daemon's `hello` reply: version, pid, clientId, demo, ...)
  ready: dict | None       the daemon's last `ready` event data (pymobiledevice3 version, error if the
                           iPhone tools are missing).

Synthetic events (generated locally, delivered to iterators/callbacks that want them):
  "disconnected" {reason}         the connection to the daemon dropped
  "reconnected"  {hello}          connected again (daemon restarted); redo syslog.start etc.

class DeviceError(Exception): .code .message .fix (dict | None) .detail (str | None);
  .to_json() -> {"code", "message", "fix"?, "detail"?}. Codes: see docs/deviced-protocol.md, plus
  "timeout", "disconnected", "daemon-unavailable".

socket_path(demo=None) -> pathlib.Path    <base>/ios-studio[-demo]/deviced.sock, where <base> is
                                           $IOSSTUDIO_RUNTIME_DIR if set, else $XDG_RUNTIME_DIR
deviced_path() -> pathlib.Path             <repo>/bin/iosstudio-deviced (next to this file)

Global state (shared by every client): the active device (`device.select`), the simulated location
(`location.set/clear`), pairing (`pair.*`) and the mirror frame rate. See docs/deviced-protocol.md.
"""
from __future__ import annotations

import asyncio
import contextlib
import inspect
import itertools
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Iterable

__all__ = ["DeviceClient", "DeviceError", "socket_path", "deviced_path", "runtime_dir"]

PROTOCOL_VERSION = "1"
_MAX_LINE = 16 * 1024 * 1024


class DeviceError(Exception):
    def __init__(self, code: str, message: str, fix: dict | None = None, detail: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.fix = fix
        self.detail = detail

    @classmethod
    def from_json(cls, e: Any) -> "DeviceError":
        if not isinstance(e, dict):
            return cls("failed", str(e))
        return cls(str(e.get("code") or "failed"), str(e.get("message") or "Something went wrong."),
                   e.get("fix"), e.get("detail"))

    def to_json(self) -> dict:
        out: dict = {"code": self.code, "message": self.message}
        if self.fix:
            out["fix"] = self.fix
        if self.detail:
            out["detail"] = self.detail
        return out

    def __repr__(self) -> str:
        return f"DeviceError({self.code!r}, {self.message!r})"


def _demo_flag(demo) -> str | None:
    """None/False -> real phone; otherwise the demo scenario string."""
    if demo is None:
        v = os.environ.get("IOSSTUDIO_DEMO", "")
        return v or None
    if demo is False:
        return None
    return demo if isinstance(demo, str) and demo else "ready"


def runtime_base() -> Path:
    """$IOSSTUDIO_RUNTIME_DIR if set (a private daemon per worktree or test run), else
    $XDG_RUNTIME_DIR."""
    base = os.environ.get("IOSSTUDIO_RUNTIME_DIR") or os.environ.get("XDG_RUNTIME_DIR") \
        or f"/tmp/ios-studio-{os.getuid()}"
    return Path(base)


def runtime_dir(demo=None) -> Path:
    return runtime_base() / ("ios-studio-demo" if _demo_flag(demo) else "ios-studio")


def socket_path(demo=None) -> Path:
    return runtime_dir(demo) / "deviced.sock"


def deviced_path() -> Path:
    return Path(__file__).resolve().parent.parent / "bin" / "iosstudio-deviced"


class _Sub:
    def __init__(self, names: Iterable[str] | None, cb: Callable | None = None, maxsize: int = 1000):
        self.names = set(names) if names is not None else None
        self.cb = cb
        self.q: asyncio.Queue | None = None if cb else asyncio.Queue(maxsize)

    def wants(self, name: str) -> bool:
        return self.names is None or name in self.names

    def put(self, item):
        if self.q is None:
            return
        while True:
            try:
                self.q.put_nowait(item)
                return
            except asyncio.QueueFull:
                with contextlib.suppress(asyncio.QueueEmpty):
                    self.q.get_nowait()


class _EventStream:
    def __init__(self, client: "DeviceClient", sub: _Sub):
        self._c, self._sub = client, sub

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.close()

    def close(self):
        with contextlib.suppress(ValueError):
            self._c._subs.remove(self._sub)

    def __aiter__(self) -> AsyncIterator[tuple[str, Any]]:
        return self

    async def __anext__(self) -> tuple[str, Any]:
        if self._sub not in self._c._subs:
            raise StopAsyncIteration
        return await self._sub.q.get()


class DeviceClient:
    def __init__(self, name: str = "client", events: Iterable[str] | None = None, *, demo=None,
                 spawn: bool = True, reconnect: bool = True, connect_timeout: float = 30.0,
                 deviced: str | os.PathLike | None = None, socket: str | os.PathLike | None = None):
        self.name = name
        self._events = list(events) if events is not None else None
        self._demo = _demo_flag(demo)
        self.spawn = spawn
        self.reconnect = reconnect
        self.connect_timeout = connect_timeout
        self.deviced = Path(deviced) if deviced else deviced_path()
        self.socket = Path(socket) if socket else socket_path(self._demo or False)
        self.hello: dict | None = None
        self.ready: dict | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._read_task: asyncio.Task | None = None
        self._reconnect_task: asyncio.Task | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._ids = itertools.count(1)
        self._subs: list[_Sub] = []
        self._lock = asyncio.Lock()
        self._closed = False
        self._was_connected = False
        self._spawned: list[subprocess.Popen] = []   # reaped when the daemon goes away

    # ------------------------------------------------------------ lifecycle
    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def __aenter__(self) -> "DeviceClient":
        await self.connect()
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def connect(self) -> None:
        self._closed = False
        async with self._lock:
            if self.connected:
                return
            await self._open()

    async def close(self) -> None:
        self._closed = True
        if self._reconnect_task:
            self._reconnect_task.cancel()
        w = self._writer
        self._writer = None
        if w is not None:
            with contextlib.suppress(Exception):
                w.close()
                await asyncio.wait_for(w.wait_closed(), 2)
        if self._read_task:
            self._read_task.cancel()
            with contextlib.suppress(BaseException):
                await self._read_task
        self._fail_pending(DeviceError("disconnected", "The client was closed."))

    async def _try_connect(self) -> bool:
        try:
            r, w = await asyncio.open_unix_connection(str(self.socket), limit=_MAX_LINE)
        except (FileNotFoundError, ConnectionRefusedError, OSError):
            return False
        self._reader, self._writer = r, w
        return True

    def _spawn(self) -> subprocess.Popen:
        env = dict(os.environ)
        if self._demo:
            env["IOSSTUDIO_DEMO"] = self._demo
        else:
            env.pop("IOSSTUDIO_DEMO", None)
        return subprocess.Popen([str(self.deviced), "--serve"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True, env=env)

    @staticmethod
    def _spawn_failure(proc: subprocess.Popen) -> DeviceError | None:
        """The launcher exited before a socket appeared: report its `ready` error (e.g. needs-setup)."""
        out = b""
        with contextlib.suppress(Exception):
            out = proc.stdout.read() or b""
        for line in out.decode(errors="replace").splitlines():
            with contextlib.suppress(ValueError, AttributeError):
                e = (json.loads(line).get("data") or {}).get("error")
                if e:
                    return DeviceError.from_json(e)
        if proc.returncode:
            return DeviceError("daemon-unavailable", "The iPhone service couldn't be started.", None,
                               f"exit code {proc.returncode}")
        return None   # exit 0: another daemon won the race; keep waiting for its socket

    async def _open(self) -> None:
        ok = await self._try_connect()
        if not ok and self.spawn:
            try:
                proc = self._spawn()
                self._spawned.append(proc)
            except OSError as exc:
                raise DeviceError("daemon-unavailable", "The iPhone service couldn't be started.",
                                  None, repr(exc)) from None
            deadline = time.monotonic() + self.connect_timeout
            delay = 0.05
            try:
                while not ok and time.monotonic() < deadline:
                    await asyncio.sleep(delay)
                    delay = min(0.5, delay * 1.5)
                    ok = await self._try_connect()
                    if not ok and proc.poll() is not None:
                        failure = self._spawn_failure(proc)
                        if failure is not None:
                            raise failure
            finally:
                with contextlib.suppress(Exception):
                    proc.stdout.close()
        if not ok:
            raise DeviceError("daemon-unavailable", "The iPhone service isn't running.", None, str(self.socket))
        self._read_task = asyncio.create_task(self._read_loop())
        try:
            self.hello = await self._request("hello", {"name": self.name, "events": self._events,
                                                       "version": PROTOCOL_VERSION}, 15)
        except DeviceError:
            w, self._writer = self._writer, None
            if w:
                w.close()
            raise
        if self._was_connected:
            self._dispatch("reconnected", {"hello": self.hello})
        self._was_connected = True

    # ------------------------------------------------------------ requests
    async def request(self, cmd: str, args: dict | None = None, timeout: float = 60.0) -> Any:
        if not self.connected:
            if self._closed:
                raise DeviceError("disconnected", "The client was closed.")
            await self.connect()
        return await self._request(cmd, args or {}, timeout)

    async def _request(self, cmd: str, args: dict, timeout: float) -> Any:
        rid = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        line = json.dumps({"id": rid, "cmd": cmd, "args": args}, separators=(",", ":")) + "\n"
        try:
            w = self._writer
            if w is None:
                raise DeviceError("disconnected", "Not connected to the iPhone service.")
            w.write(line.encode())
            await w.drain()
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            raise DeviceError("timeout", f"The iPhone didn't answer {cmd} within {timeout:g} s. "
                              "Make sure it's awake and try again.") from None
        except (ConnectionError, OSError) as exc:
            raise DeviceError("disconnected", "Lost the connection to the iPhone service.", None, repr(exc)) from None
        finally:
            self._pending.pop(rid, None)

    async def subscribe(self, events: Iterable[str] | None) -> dict:
        self._events = list(events) if events is not None else None
        return await self.request("subscribe", {"events": self._events})

    # ------------------------------------------------------------ events
    def events(self, names: Iterable[str] | None = None, maxsize: int = 1000) -> _EventStream:
        sub = _Sub(names, None, maxsize)
        self._subs.append(sub)
        return _EventStream(self, sub)

    def on_event(self, callback: Callable[[str, Any], Any], names: Iterable[str] | None = None) -> Callable[[], None]:
        sub = _Sub(names, callback)
        self._subs.append(sub)

        def off():
            with contextlib.suppress(ValueError):
                self._subs.remove(sub)
        return off

    def _dispatch(self, name: str, data: Any) -> None:
        if name == "ready":
            self.ready = data
        for sub in list(self._subs):
            if not sub.wants(name):
                continue
            if sub.cb is None:
                sub.put((name, data))
                continue
            try:
                r = sub.cb(name, data)
                if inspect.isawaitable(r):
                    asyncio.ensure_future(r)
            except Exception:  # noqa: BLE001 - a bad callback must not kill the reader
                pass

    # ------------------------------------------------------------ reader
    async def _read_loop(self) -> None:
        reader = self._reader
        reason = "the iPhone service closed the connection"
        try:
            while True:
                raw = await reader.readline()
                if not raw:
                    break
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(msg, dict):
                    continue
                if "event" in msg:
                    self._dispatch(str(msg["event"]), msg.get("data"))
                    continue
                fut = self._pending.get(msg.get("id"))
                if fut is None or fut.done():
                    continue
                if msg.get("ok"):
                    fut.set_result(msg.get("data"))
                else:
                    fut.set_exception(DeviceError.from_json(msg.get("error")))
        except asyncio.CancelledError:
            raise
        except (ConnectionError, OSError, ValueError) as exc:
            reason = repr(exc)
        self._on_lost(reason)

    def _fail_pending(self, e: DeviceError) -> None:
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(e)

    def _on_lost(self, reason: str) -> None:
        self._spawned = [p for p in self._spawned if p.poll() is None]
        w, self._writer = self._writer, None
        if w is not None:
            with contextlib.suppress(Exception):
                w.close()
        self._fail_pending(DeviceError("disconnected", "The iPhone service restarted. Try again.", None, reason))
        if self._closed:
            return
        self._dispatch("disconnected", {"reason": reason})
        if self.reconnect and (self._reconnect_task is None or self._reconnect_task.done()):
            self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    async def _reconnect_loop(self) -> None:
        delay = 0.5
        while not self._closed and not self.connected:
            await asyncio.sleep(delay)
            try:
                await self.connect()
            except DeviceError:
                delay = min(10.0, delay * 2)


async def _main(argv: list[str]) -> int:
    """`python3 device_client.py <cmd> ['<json args>']`: one request, for debugging."""
    if not argv:
        print(__doc__)
        return 2
    async with DeviceClient(name="cli", events=[]) as dev:
        try:
            data = await dev.request(argv[0], json.loads(argv[1]) if len(argv) > 1 else {})
            print(json.dumps({"ok": True, "data": data}, indent=2, default=str))
            return 0
        except DeviceError as e:
            print(json.dumps({"ok": False, "error": e.to_json()}, indent=2))
            return 1


if __name__ == "__main__":
    import sys
    raise SystemExit(asyncio.run(_main(sys.argv[1:])))
