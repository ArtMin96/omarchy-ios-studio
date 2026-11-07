"""Building blocks shared by both device daemons (deviced.py and deviced_demo.py). Stdlib only, no
side effects on import.

* FeatureRegistry + load_features(): each feature module in a package (features/ for the
  real phone, demo_features/ for demo mode) exposes `register(reg)`. The daemon imports
  every module of the package; a module that fails to import or register is logged and skipped,
  so one broken feature never takes the daemon down.
* Resources: things a feature holds open on behalf of clients (mirror, condition, perf tap, pcap,
  port forward), reference counted per client and released automatically on client disconnect,
  tunnel drop, device switch, daemon shutdown or a timeout.
* CursorRing: a bounded (count and bytes) ring of numbered entries for streaming features, read
  with a cursor like the MCP server's syslog sessions (entries numbered from 0, `missed` counts
  entries overwritten before the reader got to them).

How to write a feature: docs/development.md#device-daemon-features.
"""
from __future__ import annotations

import asyncio
import contextlib
import importlib
import inspect
import json
import threading
import time
import traceback
from collections import deque
from pathlib import Path
from typing import Any, Callable

HOOKS = ("client_gone", "tunnel_drop", "session_end", "shutdown")
SCOPES = ("tunnel", "session", "daemon")


def norm(cmd: str) -> str:
    """Command lookup key. The old getattr dispatch accepted `a.b`, `a_b` and `a-b` alike."""
    return str(cmd).replace(".", "_").replace("-", "_")


async def maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value


# ====================================================================== feature registry

class FeatureRegistry:
    """Commands, cleanup hooks and snapshot/info contributors, filled by feature modules.

    Handlers are called as `handler(ctx, args)`; ctx is the daemon (deviced.Daemon) or the demo
    backend. Hooks: client_gone(ctx, client), tunnel_drop(ctx, session), session_end(ctx, session),
    shutdown(ctx). info(ctx, session) -> dict merged into device.info and the welcome deviceInfo;
    welcome(ctx) -> [(event, data), ...] appended to every new client's snapshot.
    Any of these may be sync or async.
    """

    def __init__(self, log: Callable[[str], None] = print):
        self.log = log
        self.commands: dict[str, tuple[str, Callable, str]] = {}   # norm(name) -> (name, fn, module)
        self.hooks: dict[str, list[tuple[Callable, str]]] = {h: [] for h in HOOKS}
        self.infos: list[tuple[Callable, str]] = []
        self.welcomes: list[tuple[Callable, str]] = []
        self.provided: dict[str, Any] = {}
        self.loaded: list[str] = []
        self.failed: dict[str, str] = {}

    # -- lookups
    def lookup(self, cmd: str) -> Callable | None:
        hit = self.commands.get(norm(cmd))
        return hit[1] if hit else None

    def has(self, cmd: str) -> bool:
        return norm(cmd) in self.commands

    def describe(self) -> dict:
        by_mod: dict[str, list[str]] = {}
        for name, _fn, mod in self.commands.values():
            by_mod.setdefault(mod, []).append(name)
        return {"loaded": list(self.loaded), "failed": dict(self.failed),
                "commands": {m: sorted(c) for m, c in sorted(by_mod.items())}}

    # -- running contributors and hooks (each one isolated)
    async def run_hooks(self, kind: str, *args) -> None:
        for fn, mod in list(self.hooks[kind]):
            try:
                await maybe_await(fn(*args))
            except asyncio.CancelledError:
                raise
            except BaseException:  # noqa: BLE001
                self.log(f"feature {mod}: {kind} hook failed:\n{traceback.format_exc()}")

    def info(self, *args) -> dict:
        out: dict = {}
        for fn, mod in self.infos:
            try:
                out.update(fn(*args) or {})
            except Exception:  # noqa: BLE001
                self.log(f"feature {mod}: info contributor failed:\n{traceback.format_exc()}")
        return out

    async def welcome(self, *args) -> list:
        out: list = []
        for fn, mod in self.welcomes:
            try:
                out.extend(await maybe_await(fn(*args)) or [])
            except Exception:  # noqa: BLE001
                self.log(f"feature {mod}: welcome contributor failed:\n{traceback.format_exc()}")
        return out

    # -- registration
    def module(self, name: str) -> "ModuleRegistrar":
        return ModuleRegistrar(self, name)


class ModuleRegistrar:
    """What a feature's `register(reg)` gets. Everything is staged and only committed when
    register() returns, so a module that raises halfway registers nothing."""

    def __init__(self, registry: FeatureRegistry, name: str):
        self._r = registry
        self.name = name
        self._commands: dict[str, tuple[str, Callable, str]] = {}
        self._hooks: dict[str, list] = {h: [] for h in HOOKS}
        self._infos: list = []
        self._welcomes: list = []
        self._provided: dict[str, Any] = {}

    def log(self, text: str) -> None:
        self._r.log(f"{self.name}: {text}")

    def command(self, name: str, fn: Callable | None = None):
        """`@reg.command("perf.start")` or `reg.command("perf.start", handler)`."""
        def add(f: Callable) -> Callable:
            key = norm(name)
            if key in self._r.commands or key in self._commands:
                owner = (self._r.commands.get(key) or self._commands.get(key))[2]
                raise ValueError(f"command {name!r} is already registered by {owner}")
            self._commands[key] = (name, f, self.name)
            return f
        return add(fn) if fn is not None else add

    def _hook(self, kind: str, fn: Callable | None):
        def add(f: Callable) -> Callable:
            self._hooks[kind].append((f, self.name))
            return f
        return add(fn) if fn is not None else add

    def on_client_gone(self, fn=None):
        """fn(ctx, client): a client disconnected (per-client resources are released already)."""
        return self._hook("client_gone", fn)

    def on_tunnel_drop(self, fn=None):
        """fn(ctx, session): the tunnel to the phone dropped; the session reconnects by itself."""
        return self._hook("tunnel_drop", fn)

    def on_session_end(self, fn=None):
        """fn(ctx, session): the active device is about to change (device.select). Not called at
        shutdown: register on_shutdown too if you need both."""
        return self._hook("session_end", fn)

    def on_shutdown(self, fn=None):
        """fn(ctx): the daemon is shutting down."""
        return self._hook("shutdown", fn)

    def info(self, fn=None):
        """fn(ctx, session) -> dict merged into the device.info reply and the welcome deviceInfo."""
        def add(f):
            self._infos.append((f, self.name))
            return f
        return add(fn) if fn is not None else add

    def welcome(self, fn=None):
        """fn(ctx) -> [(event, data), ...] sent to each new client after the core snapshot."""
        def add(f):
            self._welcomes.append((f, self.name))
            return f
        return add(fn) if fn is not None else add

    def provide(self, name: str, obj: Any) -> None:
        """Hand the core an object it looks up by name (e.g. "syslog": the phone log stream)."""
        if name in self._r.provided or name in self._provided:
            raise ValueError(f"{name!r} is already provided")
        self._provided[name] = obj

    def commit(self) -> None:
        r = self._r
        clash = set(self._commands) & set(r.commands)
        if clash:
            raise ValueError(f"commands already registered: {sorted(clash)}")
        r.commands.update(self._commands)
        for k, v in self._hooks.items():
            r.hooks[k].extend(v)
        r.infos.extend(self._infos)
        r.welcomes.extend(self._welcomes)
        r.provided.update(self._provided)


def load_features(registry: FeatureRegistry, package: str, directory: Path | None = None) -> FeatureRegistry:
    """Import every `<package>/<name>.py` (sorted, `_*` skipped) and call its register(reg).

    `package` is a subpackage of iosstudio.device ("features" or "demo_features"). A module that
    fails is recorded in registry.failed (see `daemon.features`) and logged with its traceback.
    """
    if directory is None:
        directory = Path(__file__).resolve().parent / package
    for path in sorted(directory.glob("*.py")):
        name = path.stem
        if name.startswith("_"):
            continue
        try:
            mod = importlib.import_module(f".{package}.{name}", __package__)
            register = getattr(mod, "register", None)
            if register is None:
                raise AttributeError("no register(reg) function")
            reg = registry.module(name)
            register(reg)
            reg.commit()
            registry.loaded.append(name)
        except Exception as exc:  # noqa: BLE001 - one broken feature must not stop the daemon
            registry.failed[name] = f"{type(exc).__name__}: {exc}"
            registry.log(f"feature {name} failed to load:\n{traceback.format_exc()}")
    return registry


# ====================================================================== session resources

class _Res:
    __slots__ = ("key", "handle", "close", "scope", "holders", "timeout", "timer", "opening", "since")

    def __init__(self, key, close, scope, timeout):
        self.key = key
        self.handle = None
        self.close = close
        self.scope = scope
        self.holders: dict[Any, int] = {}
        self.timeout = timeout
        self.timer: asyncio.TimerHandle | None = None
        self.opening: asyncio.Future | None = None
        self.since = time.time()


class Resources:
    """Reference-counted things held open for clients, released automatically.

        handle = await res.acquire("perf", client, open_fn, close_fn, scope="tunnel", timeout=300)
        left = await res.release("perf", client)        # close_fn runs when the last holder goes

    open_fn() -> handle (sync or async) runs once, for the first holder; concurrent acquires wait
    for it, and if it raises nobody holds the resource. close_fn(handle, reason) runs exactly once
    with reason "released" (last holder let go), "client" (last holder disconnected), "timeout",
    "tunnel" (tunnel dropped), "session" (device switched), "shutdown" or "error" (forget()).
    A client holds a resource at most once (acquire is idempotent per client).

    scope: "tunnel" = also released when the tunnel drops (anything bound to the current RSD/DVT
    connection); "session" = survives reconnects, released when the active device changes;
    "daemon" = only on shutdown, timeout or the last holder leaving.
    timeout: seconds after which the resource is closed whoever holds it (touch() restarts it).
    client None = background / --once work.
    """

    def __init__(self, log: Callable[[str], None] = print):
        self.log = log
        self._r: dict[str, _Res] = {}

    # -- queries
    def held(self, key: str) -> bool:
        r = self._r.get(key)
        return r is not None and r.opening is None

    def holders(self, key: str) -> int:
        r = self._r.get(key)
        return len(r.holders) if r else 0

    def holds(self, key: str, client) -> bool:
        r = self._r.get(key)
        return r is not None and client in r.holders

    def handle(self, key: str):
        r = self._r.get(key)
        return r.handle if r is not None and r.opening is None else None

    def keys_for(self, client) -> list[str]:
        return sorted(k for k, r in self._r.items() if client in r.holders)

    def describe(self) -> list[dict]:
        return [{"key": k, "scope": r.scope, "holders": len(r.holders), "since": r.since, "timeout": r.timeout}
                for k, r in sorted(self._r.items())]

    # -- acquire / release
    async def acquire(self, key: str, client, open_fn: Callable[[], Any],
                      close_fn: Callable[[Any, str], Any] | None = None, *, scope: str = "tunnel",
                      timeout: float | None = None):
        if scope not in SCOPES:
            raise ValueError(f"scope must be one of {SCOPES}")
        r = self._r.get(key)
        if r is not None and r.opening is not None:
            await asyncio.shield(r.opening)          # raises if that open failed
            r = self._r.get(key)
        if r is not None:
            r.holders[client] = r.holders.get(client, 0) + 1
            return r.handle
        r = _Res(key, close_fn, scope, timeout)
        r.opening = asyncio.get_running_loop().create_future()
        self._r[key] = r
        try:
            r.handle = await maybe_await(open_fn())
        except BaseException as exc:
            self._r.pop(key, None)
            if isinstance(exc, asyncio.CancelledError):
                r.opening.cancel()
            else:
                r.opening.set_exception(exc)
                r.opening.exception()   # retrieved: no "never retrieved" warning
            raise
        r.opening.set_result(None)
        r.opening = None
        r.holders[client] = 1
        self._arm(r)
        return r.handle

    async def release(self, key: str, client) -> int:
        """Drop `client`'s hold. Returns how many holders are left (0 = it was closed)."""
        r = self._r.get(key)
        if r is None or r.opening is not None:
            return 0
        r.holders.pop(client, None)
        if r.holders:
            return len(r.holders)
        await self._close(r, "released")
        return 0

    def touch(self, key: str) -> None:
        r = self._r.get(key)
        if r is not None:
            self._arm(r)

    async def close(self, key: str, reason: str = "released") -> bool:
        """Close a resource now, whoever holds it."""
        r = self._r.get(key)
        if r is None or r.opening is not None:
            return False
        await self._close(r, reason)
        return True

    def forget(self, key: str) -> None:
        """The resource ended by itself (stream died): drop it without calling close_fn."""
        r = self._r.pop(key, None)
        if r is not None and r.timer:
            r.timer.cancel()

    async def client_gone(self, client) -> None:
        for r in [r for r in self._r.values() if client in r.holders and r.opening is None]:
            r.holders.pop(client, None)
            if not r.holders:
                await self._close(r, "client")

    async def release_all(self, reason: str, scopes=SCOPES) -> None:
        """reason "tunnel" -> scopes ("tunnel",); "session" -> ("tunnel", "session"); "shutdown" -> all."""
        for r in [r for r in self._r.values() if r.scope in scopes and r.opening is None]:
            await self._close(r, reason)

    # -- internals
    def _arm(self, r: _Res) -> None:
        if r.timer:
            r.timer.cancel()
            r.timer = None
        if r.timeout:
            loop = asyncio.get_running_loop()
            r.timer = loop.call_later(r.timeout, lambda: asyncio.ensure_future(self._expire(r)))

    async def _expire(self, r: _Res) -> None:
        if self._r.get(r.key) is r and r.opening is None:
            self.log(f"resource {r.key}: timed out after {r.timeout:g} s")
            await self._close(r, "timeout")

    async def _close(self, r: _Res, reason: str) -> None:
        if self._r.get(r.key) is not r:
            return
        self._r.pop(r.key, None)
        if r.timer:
            r.timer.cancel()
        if r.close is None:
            return
        try:
            await asyncio.wait_for(maybe_await(r.close(r.handle, reason)), 10)
        except asyncio.CancelledError:
            raise
        except BaseException:  # noqa: BLE001
            self.log(f"resource {r.key}: close ({reason}) failed:\n{traceback.format_exc()}")


# ====================================================================== cursor ring

def _json_size(item) -> int:
    try:
        return len(json.dumps(item, separators=(",", ":"), default=str))
    except (TypeError, ValueError):
        return 256


class CursorRing:
    """Numbered entries, oldest dropped first once `max_items` or `max_bytes` is exceeded.

        ring = CursorRing(max_items=5000, max_bytes=4 << 20)
        ring.append({"cpu": 12.5})                     # thread-safe; returns the entry's number
        page = ring.read(cursor, limit=500)
        # {"entries": [...], "cursor": c, "nextCursor": n, "hasMore": bool, "missed": m,
        #  "bufferStart": s, "total": t}
        page = await ring.wait_read(cursor, timeout=10)  # waits for at least one entry past cursor

    Entries are numbered from 0 for the ring's lifetime. cursor=None reads from the oldest kept
    entry. A cursor older than bufferStart reports how many entries were lost as `missed`. Same
    semantics as the MCP server's ios_logs_read (next_cursor / has_more / missed / buffer_start).
    """

    def __init__(self, max_items: int = 5000, max_bytes: int = 8 << 20, size: Callable[[Any], int] = _json_size):
        self.max_items = max(1, int(max_items))
        self.max_bytes = max(1, int(max_bytes))
        self.size = size
        self._q: deque = deque()   # (seq, item, size)
        self._bytes = 0
        self.seq = 0               # number the next entry gets
        self._lock = threading.Lock()
        self._waiters: list[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = []

    @property
    def buffer_start(self) -> int:
        with self._lock:
            return self._q[0][0] if self._q else self.seq

    def __len__(self) -> int:
        return len(self._q)

    @property
    def bytes(self) -> int:
        return self._bytes

    def append(self, item) -> int:
        n = self.size(item)
        with self._lock:
            seq = self.seq
            self._q.append((seq, item, n))
            self._bytes += n
            self.seq += 1
            while len(self._q) > self.max_items or (self._bytes > self.max_bytes and len(self._q) > 1):
                _, _, m = self._q.popleft()
                self._bytes -= m
            waiters, self._waiters = self._waiters, []
        for loop, ev in waiters:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(ev.set)
        return seq

    def extend(self, items) -> int:
        last = self.seq - 1
        for it in items:
            last = self.append(it)
        return last

    def clear(self) -> None:
        """Drop the kept entries; numbering continues (readers see them as missed)."""
        with self._lock:
            self._q.clear()
            self._bytes = 0

    def read(self, cursor: int | None = None, limit: int = 500) -> dict:
        limit = max(1, int(limit))
        with self._lock:
            start = self._q[0][0] if self._q else self.seq
            cur = start if cursor is None else max(0, int(cursor))
            cur = min(cur, self.seq)
            missed = max(0, start - cur)
            first = max(cur, start)
            entries = [it for s, it, _ in self._q if s >= first][:limit] if self._q else []
            nxt = first + len(entries)
            return {"entries": entries, "cursor": first, "nextCursor": nxt, "hasMore": nxt < self.seq,
                    "missed": missed, "bufferStart": start, "total": self.seq}

    async def wait_read(self, cursor: int | None = None, timeout: float = 0, limit: int = 500) -> dict:
        page = self.read(cursor, limit)
        if page["entries"] or timeout <= 0:
            return page
        if cursor is None:
            cursor = page["cursor"]
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            left = deadline - loop.time()
            if left <= 0:
                return self.read(cursor, limit)
            waiter = (loop, asyncio.Event())
            with self._lock:
                ready = self.seq > page["nextCursor"]
                if not ready:
                    self._waiters.append(waiter)
            if not ready:
                try:
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(waiter[1].wait(), left)
                finally:
                    # timed out or cancelled: don't leave the waiter behind on a ring nobody appends to
                    with self._lock, contextlib.suppress(ValueError):
                        self._waiters.remove(waiter)
            page = self.read(cursor, limit)
            if page["entries"] or loop.time() >= deadline:
                return page
