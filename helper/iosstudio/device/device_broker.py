"""Shared device-daemon broker: one daemon per user, many clients (docs/deviced-protocol.md#broker).

Stdlib only. Used two ways:

* As a library by the daemons (deviced.py for the real phone, deviced_demo.py for demo mode):
  `Broker(backend).run()` listens on $XDG_RUNTIME_DIR/ios-studio[-demo]/deviced.sock, speaks the
  JSON-lines protocol per connection, broadcasts device-wide events to subscribed clients, keeps
  one syslog stream to the phone with per-client filters, owns the per-client session resources
  (deviced_kit.Resources: mirror, port forwards, ...) and exits after an idle grace period with
  no clients.
* As the launcher behind bin/iosstudio-deviced (`run.py iosstudio.device.device_broker launch ...`): connects to the live
  daemon (spawning a detached one when none is running) and bridges its own stdin/stdout to the
  socket, so a client that speaks JSON lines over stdio (the GUI's Stream) needs no changes.
  `--once` sends one request to the live daemon, or falls back to the standalone one-shot daemon.

The backend is a backend.Backend (deviced.Daemon, deviced.BrokenBackend, deviced_demo.DemoBackend);
backend.py lists what the broker calls on it. Framing and message shapes come from protocol.py.
The backend calls broker.emit(name, data) for device-wide events and feeds syslog entries to
broker.syslog.feed(entry) / .stream_state(...) / .stream_failed(error). broker.resources is the
shared deviced_kit.Resources; the broker releases a client's holds when it disconnects.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import errno
import fcntl
import json
import os
import queue
import socket
import stat
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

from iosstudio.device import deviced_kit, protocol
from iosstudio.device.backend import Backend, TaskSet
from iosstudio.device.device_client import runtime_dir, socket_path_error

VERSION = "1"
IDLE_DEFAULT_S = 120.0
MAX_LINE = protocol.MAX_LINE
CLIENT_BUFFER_MAX = 32 * 1024 * 1024   # a client that stops reading is disconnected
LOG_MAX = 4 * 1024 * 1024               # deviced.log (tmpfs, so RAM) moves to deviced.log.1 past this
LOG_CHECK_S = 30.0

SYSLOG_FLUSH_S = 0.1
SYSLOG_BATCH = 200
SYSLOG_BUFFER = 1000                    # entries waiting per client; older ones are dropped (counted)
SYSLOG_YIELD_EVERY = 16                 # records the worker filters between handing the GIL to the loop
SYSLOG_QUEUE = 64                       # raw batches waiting for the worker; a full queue drops the batch
LEVELS = {"debug": 0, "info": 1, "default": 2, "error": 3, "fault": 4}

# Events every client gets by default; `subscribe` narrows it. Per-client events are sent directly.
PER_CLIENT_EVENTS = {"syslog", "syslogState"}

_current: contextvars.ContextVar = contextvars.ContextVar("deviced_client", default=None)


def current_client() -> "Client | None":
    """The client whose request is running (None for background work)."""
    return _current.get()


def log(text: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] broker: {text}", file=sys.stderr, flush=True)


def paths(demo) -> dict:
    d = runtime_dir(demo)
    return {"dir": d, "sock": d / "deviced.sock", "lock": d / "deviced.lock", "log": d / "deviced.log"}


def _prepare_dir(d: Path) -> None:
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = d.stat()
    if st.st_uid != os.getuid():
        raise PermissionError(f"{d} isn't owned by you")
    if stat.S_IMODE(st.st_mode) != 0o700:
        os.chmod(d, 0o700)


def socket_alive(path: Path, timeout: float = 1.0) -> bool:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(str(path))
        return True
    except OSError:
        return False
    finally:
        s.close()


# ====================================================================== syslog filters

def norm_filters(f) -> dict:
    f = dict(f or {}) if isinstance(f, dict) else {}
    out: dict = {}
    if f.get("processes"):
        out["processes"] = [str(x) for x in f["processes"] if str(x)]
    if f.get("pids"):
        out["pids"] = [int(x) for x in f["pids"]]
    if f.get("minLevel") in LEVELS:
        out["minLevel"] = f["minLevel"]
    if f.get("text"):
        out["text"] = str(f["text"])
    if f.get("subsystem"):
        out["subsystem"] = str(f["subsystem"])
    if f.get("bundleId"):
        out["bundleId"] = str(f["bundleId"])
    return out


class SyslogSub:
    """One client's syslog subscription. match() runs on the syslog worker thread: it reads only
    `_m`, a tuple rebuilt (and swapped in one assignment) whenever the filters or the resolved
    bundle id change on the loop."""

    def __init__(self, client: "Client", filters: dict):
        self.client = client
        self.exe: str | None = None
        self.bid: str | None = None
        self.buf: deque = deque(maxlen=SYSLOG_BUFFER)
        self.dropped = 0
        self.last_drop_emit = 0.0
        self.filters = filters

    @property
    def filters(self) -> dict:
        return self._filters

    @filters.setter
    def filters(self, f: dict) -> None:
        self._filters = f
        self.compile()

    @property
    def min(self) -> int:
        return self._m[0]

    def compile(self) -> None:
        """Precompute what match() needs (sets, lowercased text) once instead of per entry."""
        f = self._filters
        self._m = (LEVELS.get(f.get("minLevel", "debug"), 0),
                   frozenset(f["pids"]) if "pids" in f else None,
                   frozenset(f["processes"]) if "processes" in f else None,
                   (self.exe, self.bid or f["bundleId"]) if "bundleId" in f else None,
                   f.get("subsystem"),
                   f["text"].lower() if "text" in f else None)

    def set_resolved(self, exe: str | None, bid: str | None) -> None:
        self.exe, self.bid = exe, bid
        self.compile()

    def match(self, e: dict) -> bool:
        lvl, pids, procs, bundle, subsys, text = self._m
        if LEVELS.get(e["level"], 2) < lvl:
            return False
        if pids is not None and e["pid"] not in pids:
            return False
        proc = e["process"]
        if procs is not None and proc not in procs:
            return False
        msg = e["message"]
        if bundle is not None:
            exe, bid = bundle
            if not ((exe and proc == exe) or (exe and exe in msg) or bid in msg):
                return False
        sub = e["subsystem"]
        if subsys is not None and not sub.startswith(subsys):
            return False
        if text is not None and text not in msg.lower() and text not in proc.lower() and text not in sub.lower():
            return False
        return True


def route(batch, subs, parse=None, min_level: int = 0, pause=None) -> list[tuple["SyslogSub", list[dict]]]:
    """Pure: parse each raw record (parse(raw, min_level) -> entry | None; None = already entries)
    and sort the entries into the subscriptions that match them. Runs on the syslog worker, which
    passes pause=time.sleep(0) every SYSLOG_YIELD_EVERY records: hands the GIL to the event loop
    so it never waits a whole switch interval (5 ms) per wakeup while a big batch is filtered."""
    out: dict[SyslogSub, list[dict]] = {sub: [] for sub in subs}
    for i, raw in enumerate(batch):
        if pause is not None and i and i % SYSLOG_YIELD_EVERY == 0:
            pause()
        e = parse(raw, min_level) if parse is not None else raw
        if e is None:
            continue
        for sub, lst in out.items():
            if sub.match(e):
                lst.append(e)
    return [(sub, lst) for sub, lst in out.items() if lst]


def _yield_gil() -> None:
    time.sleep(0)


class SyslogWorker:
    """A thread that parses and filters syslog batches off the event loop (it also hosts the tunnel,
    so a busy log must never delay other requests) and hands the per-client results back to the loop.
    The queue is bounded: when the worker falls behind, whole batches are dropped and counted."""

    def __init__(self, hub: "SyslogHub", loop: asyncio.AbstractEventLoop):
        self.hub = hub
        self.loop = loop
        self.q: queue.Queue = queue.Queue(maxsize=SYSLOG_QUEUE)
        self.lost = 0
        self.thread = threading.Thread(target=self._run, name="syslog-worker", daemon=True)
        self.thread.start()

    def submit(self, batch: list, parse=None) -> bool:
        try:
            self.q.put_nowait((batch, parse))
            return True
        except queue.Full:
            self.lost += len(batch)
            return False

    def _run(self) -> None:
        while True:
            item = self.q.get()
            if item is None:
                return
            batch, parse = item
            try:
                results = route(batch, self.hub.snapshot, parse, self.hub.min_level(), _yield_gil)
            except Exception as exc:  # noqa: BLE001 - one bad batch mustn't end the log
                log(f"syslog: worker failed on a batch: {exc!r}")
                continue
            if results:
                try:
                    self.loop.call_soon_threadsafe(self.hub.deliver, results)
                except RuntimeError:   # the loop is closed: the daemon is going away
                    return

    def close(self, timeout: float = 2.0) -> None:
        """Drop what's queued, stop the thread and wait for it (blocking: call via to_thread)."""
        with contextlib.suppress(queue.Empty):
            while True:
                self.q.get_nowait()
        with contextlib.suppress(queue.Full):
            self.q.put_nowait(None)
        self.thread.join(timeout)


class SyslogHub:
    """One phone-side log stream, many client subscriptions with their own filters.

    Each entry is filtered per client in the daemon. The phone-side filter is a single pid when
    every subscriber filters on exactly that one pid (cheapest), otherwise none. `min_level()` lets
    the backend drop entries no subscriber wants before building them.
    """

    def __init__(self, broker: "Broker"):
        self.b = broker
        self.subs: dict[Client, SyslogSub] = {}
        self.pending_filters: dict[Client, dict] = {}   # syslog.filter before syslog.start
        self.open = False
        self.phone_pid = -1
        self.udid = None
        self.reconnecting = False
        self.error = None
        self._min = 0
        self.snapshot: tuple[SyslogSub, ...] = ()      # what the worker routes to (replaced, never mutated)
        self.worker: SyslogWorker | None = None
        self.flusher: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    # -- per-client state
    def state(self, sub: SyslogSub | None, running: bool | None = None) -> dict:
        s = {"running": (sub is not None and self.open) if running is None else running, "udid": self.udid,
             "filters": sub.filters if sub else {}, "dropped": sub.dropped if sub else 0,
             "reconnecting": self.reconnecting}
        if self.error:
            s["error"] = self.error
        return s

    def _send_state(self, sub: SyslogSub, running: bool | None = None):
        sub.client.send(protocol.event("syslogState", self.state(sub, running)))

    def min_level(self) -> int:
        return self._min

    def _recompute(self) -> int:
        self.snapshot = tuple(self.subs.values())
        self._min = min((s.min for s in self.subs.values()), default=0)
        pids = [tuple(s.filters.get("pids") or ()) for s in self.subs.values()]
        if pids and all(len(p) == 1 for p in pids) and len(set(pids)) == 1:
            return pids[0][0]
        return -1

    async def _resolve(self, sub: SyslogSub):
        bid = sub.filters.get("bundleId")
        sub.set_resolved(None, None)
        if bid:
            try:
                sub.set_resolved(*await asyncio.wait_for(self.b.backend.syslog_resolve(bid), 15))
            except Exception as exc:  # noqa: BLE001
                log(f"syslog: can't resolve {bid}: {exc!r}")
                sub.set_resolved(None, bid)

    async def resolve_missing(self):
        """After (re)connecting: resolve bundleId filters that couldn't be resolved without a tunnel."""
        for sub in list(self.subs.values()):
            if sub.filters.get("bundleId") and sub.exe is None:
                await self._resolve(sub)

    async def _sync_stream(self):
        async with self._lock:
            await self._sync_locked()

    async def _sync_locked(self):
        want_pid = self._recompute()
        if not self.subs:
            if self.open:
                self.open = False
                await self.b.backend.syslog_close()
                log("syslog: phone stream stopped (no subscribers)")
            await self._stop_worker()
            return
        if self.open and want_pid == self.phone_pid:
            return
        was = self.open
        if was:
            await self.b.backend.syslog_close()
        self.open, self.phone_pid, self.reconnecting, self.error = True, want_pid, False, None
        log(f"syslog: phone stream {'restarted' if was else 'started'} (pid filter {want_pid}, "
            f"{len(self.subs)} subscriber(s))")
        self.b.backend.syslog_open(want_pid)
        if self.flusher is None or self.flusher.done():
            self.flusher = self.b.spawn(self._flush_loop(), name="syslog-flush")

    # -- commands
    async def start(self, client: "Client", args: dict) -> dict:
        self.udid = self.b.backend.syslog_target(args)
        old = self.subs.pop(client, None)
        if old:
            self._flush_sub(old, all_=True)
        filters = norm_filters(args.get("filters") if "filters" in args else self.pending_filters.get(client))
        self.pending_filters.pop(client, None)
        sub = SyslogSub(client, filters)
        await self._resolve(sub)
        self.subs[client] = sub
        await self._sync_stream()
        self._send_state(sub)
        return {"running": True, "filters": sub.filters}

    async def set_filters(self, client: "Client", filters) -> dict:
        f = norm_filters(filters)
        sub = self.subs.get(client)
        if sub is None:
            self.pending_filters[client] = f
            return {"filters": f}
        sub.filters = f
        await self._resolve(sub)
        await self._sync_stream()
        self._send_state(sub)
        return {"filters": f}

    async def stop(self, client: "Client", emit_state: bool = True) -> dict:
        self.pending_filters.pop(client, None)
        sub = self.subs.pop(client, None)
        if sub:
            self._flush_sub(sub, all_=True)
            if emit_state:
                self._send_state(sub, running=False)
        await self._sync_stream()
        return {"running": False}

    async def stop_all(self):
        """The active device changed or the stream can't run: end every subscription."""
        subs = list(self.subs.values())
        self.subs.clear()
        for sub in subs:
            self._flush_sub(sub, all_=True)
            self._send_state(sub, running=False)
        await self._sync_stream()

    # -- from the backend
    def feed(self, entry: dict):
        """One entry, filtered right here on the loop (the demo's few lines a second)."""
        for sub in self.subs.values():
            if sub.match(entry):
                self._append(sub, [entry])

    def feed_batch(self, batch: list, parse=None) -> bool:
        """Raw records (or entries, parse=None) to parse and filter on the worker thread; the matches
        come back through deliver(). False when the worker is behind and the batch was dropped."""
        if not self.subs:
            return True
        if self.worker is None:
            self.worker = SyslogWorker(self, asyncio.get_running_loop())
        if self.worker.submit(batch, parse):
            return True
        for sub in self.subs.values():      # every subscriber may have lost lines: say so
            sub.dropped += len(batch)
            self._drop_notice(sub)
        return False

    def deliver(self, results: list[tuple[SyslogSub, list[dict]]]) -> None:
        """On the loop: the worker's matches into each (still subscribed) client's buffer."""
        for sub, entries in results:
            if self.subs.get(sub.client) is sub:
                self._append(sub, entries)

    def _append(self, sub: SyslogSub, entries: list[dict]) -> None:
        over = len(sub.buf) + len(entries) - SYSLOG_BUFFER
        sub.buf.extend(entries)              # bounded deque: the oldest fall out
        if over > 0:
            sub.dropped += over
            self._drop_notice(sub)

    def _drop_notice(self, sub: SyslogSub) -> None:
        now = time.monotonic()
        if now - sub.last_drop_emit > 1:
            sub.last_drop_emit = now
            self._send_state(sub)

    async def _stop_worker(self) -> None:
        w, self.worker = self.worker, None
        if w is not None:
            await asyncio.to_thread(w.close)

    async def close(self) -> None:
        """Broker shutdown: end the worker thread."""
        await self._stop_worker()

    def stream_state(self, reconnecting: bool, error: dict | None = None):
        self.reconnecting, self.error = reconnecting, error
        for sub in self.subs.values():
            self._send_state(sub)

    def stream_failed(self, error: dict):
        """The phone stream gave up (e.g. not paired): every subscriber gets running:false + error."""
        self.error = error
        self.open = False
        subs = list(self.subs.values())
        self.subs.clear()
        for sub in subs:
            self._send_state(sub, running=False)
        self.error = None
        self._recompute()

    # -- flushing
    def _flush_sub(self, sub: SyslogSub, all_: bool = False):
        while sub.buf:
            n = min(len(sub.buf), SYSLOG_BATCH)
            sub.client.send(protocol.event("syslog", [sub.buf.popleft() for _ in range(n)]))
            if not all_:
                break

    async def _flush_loop(self):
        while self.open or any(s.buf for s in self.subs.values()):
            await asyncio.sleep(SYSLOG_FLUSH_S)
            for sub in list(self.subs.values()):
                if sub.buf:
                    self._flush_sub(sub)

    def client_gone(self, client: "Client"):
        self.pending_filters.pop(client, None)
        if self.subs.pop(client, None) is not None:
            self._recompute()
            self.b.spawn(self._sync_stream(), name="syslog-sync")


# ====================================================================== clients

class Client:
    _ids = 0

    def __init__(self, broker: "Broker", writer: asyncio.StreamWriter):
        Client._ids += 1
        self.id = Client._ids
        self.b = broker
        self.w = writer
        self.name = f"client-{self.id}"
        self.events: set[str] | None = None     # None = everything
        self.since = time.time()
        self.closed = False

    def wants(self, name: str) -> bool:
        return self.events is None or name in self.events

    def send(self, obj: dict) -> None:
        if self.closed:
            return
        try:
            self.w.write(protocol.encode(obj))
            if self.w.transport.get_write_buffer_size() > CLIENT_BUFFER_MAX:
                log(f"{self.name} isn't reading; disconnecting it")
                self.close()
        except (ConnectionError, RuntimeError, OSError):
            self.close()

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            with contextlib.suppress(Exception):
                self.w.close()

    def describe(self) -> dict:
        b = self.b
        return {"id": self.id, "name": self.name, "since": self.since,
                "events": sorted(self.events) if self.events is not None else None,
                "syslog": b.syslog.subs[self].filters if self in b.syslog.subs else None,
                "mirror": b.resources.holds("mirror", self)}


# ====================================================================== broker

class Broker:
    def __init__(self, demo, idle: float | None = None):
        self.demo = demo
        self.p = paths(demo)
        if idle is None:
            try:
                idle = float(os.environ.get("IOSSTUDIO_DEVICED_IDLE", IDLE_DEFAULT_S))
            except ValueError:
                idle = IDLE_DEFAULT_S
        self.idle = max(0.0, idle)
        self.backend: Backend | None = None
        self.clients: set[Client] = set()
        self._connecting = 0                  # clients waiting for the backend to come up
        self.syslog = SyslogHub(self)
        self.resources = deviced_kit.Resources(log=log)
        self.loop: asyncio.AbstractEventLoop | None = None
        self._thread = threading.get_ident()
        self._ready = asyncio.Event()
        self._stopping: asyncio.Event | None = None
        self._idle_handle: asyncio.TimerHandle | None = None
        self._server: asyncio.AbstractServer | None = None
        self._lock_fd: int | None = None
        self._log_redirected = False
        self._bg = TaskSet(log)               # fire-and-forget work, cancelled at shutdown

    def spawn(self, coro, name: str | None = None) -> asyncio.Task:
        """A background task the broker keeps a reference to until it ends (or shutdown cancels it)."""
        return self._bg.spawn(coro, name=name)

    def where(self) -> dict:
        """The runtime paths clients show (the GUI's "open log"); sent in `hello` and `ready` so no
        client works out the runtime-dir rules itself (device_client.runtime_base is the one rule)."""
        return {"socket": str(self.p["sock"]), "runtimeDir": str(self.p["dir"]), "log": str(self.p["log"])}

    # -- single instance
    def acquire(self, wait: float = 10.0) -> bool:
        """Take the lock file. False when another live daemon owns it (caller should exit)."""
        bad = socket_path_error(self.p["sock"])
        if bad is not None:   # clients check this too and report it; never serve a socket that can't exist
            log(f"{bad.message} ({bad.detail})")
            return False
        _prepare_dir(self.p["dir"])
        fd = os.open(self.p["lock"], os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EACCES):
                    raise
            # Held: either a live daemon (socket answers) or one starting / shutting down.
            if socket_alive(self.p["sock"]) or time.monotonic() > deadline:
                os.close(fd)
                return False
            time.sleep(0.2)
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self._lock_fd = fd
        return True

    def redirect_stderr(self) -> None:
        """Server mode has no terminal: stdout/stderr go to <runtime dir>/deviced.log."""
        fd = os.open(self.p["log"], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.dup2(fd, 1)
        os.dup2(fd, 2)
        os.close(fd)
        os.chdir("/")
        self._log_redirected = True

    def _rotate_log(self) -> None:
        """Size-based rotation: past LOG_MAX the log becomes deviced.log.1 (replacing the older one)
        and a new deviced.log starts."""
        try:
            if os.fstat(2).st_size < LOG_MAX:
                return
            with contextlib.suppress(Exception):
                sys.stderr.flush()
            log_path = self.p["log"]
            os.replace(log_path, log_path.with_name(log_path.name + ".1"))
            fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            os.dup2(fd, 1)
            os.dup2(fd, 2)
            os.close(fd)
            log("log rotated (the previous part is deviced.log.1)")
        except OSError:
            pass

    async def _log_loop(self) -> None:
        while True:
            await asyncio.sleep(LOG_CHECK_S)
            self._rotate_log()

    # -- events
    def emit(self, name: str, data) -> None:
        """Broadcast a device-wide event to every client subscribed to it. Thread-safe."""
        if threading.get_ident() != self._thread and self.loop is not None:
            self.loop.call_soon_threadsafe(self.emit, name, data)
            return
        obj = protocol.event(name, data)
        for c in list(self.clients):
            if c.wants(name):
                c.send(obj)

    # -- lifetime
    def _check_idle(self):
        if self._idle_handle:
            self._idle_handle.cancel()
            self._idle_handle = None
        if not self.clients and self._stopping is not None:
            self._idle_handle = self.loop.call_later(self.idle, self._idle_exit)

    def _idle_exit(self):
        if not self.clients and not self._connecting:   # a slow first start mustn't drop its first client
            log(f"no clients for {self.idle:g} s, exiting")
            self.stop()

    def stop(self):
        if self._stopping is not None:
            self._stopping.set()

    async def run(self, make_backend) -> int:
        """Listen, build the backend (`await make_backend(self)`), serve until idle/stop, shut down."""
        self.loop = asyncio.get_running_loop()
        self._thread = threading.get_ident()
        self._stopping = asyncio.Event()
        sock = self.p["sock"]
        with contextlib.suppress(FileNotFoundError):
            sock.unlink()   # stale: we hold the lock, so nobody else serves it
        old = os.umask(0o177)
        try:
            self._server = await asyncio.start_unix_server(self._serve_client, path=str(sock), limit=MAX_LINE)
        finally:
            os.umask(old)
        os.chmod(sock, 0o600)
        log(f"listening on {sock} (pid {os.getpid()}, idle exit after {self.idle:g} s)")
        for sig in ("SIGTERM", "SIGINT", "SIGHUP"):
            with contextlib.suppress(NotImplementedError, AttributeError):
                self.loop.add_signal_handler(getattr(__import__("signal"), sig), self.stop)
        self._check_idle()
        log_task = asyncio.create_task(self._log_loop()) if self._log_redirected else None
        try:
            self.backend = await make_backend(self)
            self._ready.set()
            await self._stopping.wait()
        finally:
            if log_task is not None:
                log_task.cancel()
            log("shutting down")
            self._server.close()
            with contextlib.suppress(FileNotFoundError):
                if sock.exists():
                    sock.unlink()
            for c in list(self.clients):
                c.close()
            if self.backend is not None:
                with contextlib.suppress(BaseException):
                    await asyncio.wait_for(self.backend.shutdown(), 8)
            await self._bg.close()
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(self.syslog.close(), 3)
            log("bye")
        return 0

    # -- connections
    async def _serve_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        c = Client(self, writer)
        self._connecting += 1
        try:
            await self._ready.wait()
        finally:
            self._connecting -= 1
        self.clients.add(c)
        self._check_idle()
        log(f"{c.name} connected ({len(self.clients)} client(s))")
        tasks: set[asyncio.Task] = set()
        try:
            for name, data in await self.backend.welcome():
                if name == "ready" and isinstance(data, dict):
                    data = {**data, **self.where()}
                c.send(protocol.event(name, data))

            def malformed():
                c.send(protocol.event("log", {"level": "warn", "text": "ignored a malformed request line"}))
            try:
                async for m in protocol.read_lines_async(reader, malformed):
                    if c.closed:
                        break
                    token = _current.set(c)
                    try:
                        t = asyncio.create_task(self._request(c, m))
                    finally:
                        _current.reset(token)
                    tasks.add(t)
                    t.add_done_callback(tasks.discard)
            except protocol.LineTooLong:
                c.send(protocol.event("log", {"level": "warn", "text": "request line too long"}))
        except (ConnectionError, OSError):
            pass
        finally:
            c.close()
            self.clients.discard(c)
            log(f"{c.name} disconnected ({len(self.clients)} client(s) left)")
            self.syslog.client_gone(c)
            self.spawn(self._client_gone(c), name=f"{c.name}-gone")
            self._check_idle()

    async def _client_gone(self, c: Client):
        """Release what the client held (mirror, port forwards, ...), then tell the backend."""
        await self.resources.client_gone(c)
        if self.backend is not None:
            with contextlib.suppress(Exception):
                await self.backend.client_gone(c)

    async def _request(self, c: Client, m: dict):
        rid = m.get("id")
        cmd = str(m.get("cmd") or "")
        args = m.get("args") if isinstance(m.get("args"), dict) else {}
        try:
            c.send(protocol.reply_ok(rid, await self._command(c, cmd, args)))
        except asyncio.CancelledError:
            c.send(protocol.reply_error(rid, protocol.CANCELLED))
        except BaseException as exc:  # noqa: BLE001
            c.send(protocol.reply_error(rid, self.backend.error_json(exc, cmd)))
        if cmd == "daemon.shutdown" and not args.get("all"):
            await asyncio.sleep(0.05)
            c.close()

    async def _command(self, c: Client, cmd: str, a: dict):
        if cmd in ("hello", "subscribe"):
            if cmd == "hello" and a.get("name"):
                c.name = f"{str(a['name'])[:40]}-{c.id}"
            if "events" in a:
                ev = a.get("events")
                c.events = None if ev is None or ev == "*" or (isinstance(ev, list) and "*" in ev) \
                    else {str(x) for x in ev}
            return {"version": VERSION, "pid": os.getpid(), "clientId": c.id, "name": c.name,
                    "clients": len(self.clients), "demo": bool(self.demo), **self.where(),
                    "idle": self.idle, "events": sorted(c.events) if c.events is not None else None}
        if cmd == "daemon.clients":
            return [x.describe() for x in sorted(self.clients, key=lambda x: x.id)]
        if cmd == "daemon.shutdown":
            # Old single-client protocol: "I'm done". It only detaches this client; the daemon keeps
            # serving the others. {"all": true} stops the daemon for everyone.
            if a.get("all"):
                self.loop.call_later(0.1, self.stop)
            return {}
        if cmd == "syslog.start":
            return await self.syslog.start(c, a)
        if cmd == "syslog.filter":
            return await self.syslog.set_filters(c, a.get("filters") or {})
        if cmd == "syslog.stop":
            return await self.syslog.stop(c)
        return await self.backend.dispatch(cmd, a)


# ====================================================================== launcher / bridge

def _connect(path: Path) -> socket.socket | None:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(str(path))
        return s
    except OSError:
        s.close()
        return None


def spawn_server(deviced: str) -> None:
    subprocess.Popen([deviced, "--serve"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)


def connect_or_spawn(path: Path, deviced: str, timeout: float = 30.0) -> socket.socket | None:
    s = _connect(path)
    if s is not None:
        return s
    spawn_server(deviced)
    deadline = time.monotonic() + timeout
    delay = 0.03
    respawned = False
    while time.monotonic() < deadline:
        time.sleep(delay)
        delay = min(0.3, delay * 1.4)
        s = _connect(path)
        if s is not None:
            return s
        # The spawned server may have bowed out to a daemon that was shutting down; try once more.
        if not respawned and time.monotonic() > deadline - timeout + 12:
            respawned = True
            spawn_server(deviced)
    return None


def bridge(s: socket.socket) -> int:
    """stdin -> socket, socket -> stdout until either side ends."""
    out = sys.stdout.fileno()
    done = threading.Event()
    stdin_eof = threading.Event()

    def down():
        try:
            while True:
                data = s.recv(65536)
                if not data:
                    break
                view = memoryview(data)
                while view:
                    n = os.write(out, view)
                    view = view[n:]
        except OSError:
            pass
        done.set()

    threading.Thread(target=down, daemon=True).start()
    inp = sys.stdin.fileno()
    try:
        while not done.is_set():
            data = os.read(inp, 65536)
            if not data:
                break
            s.sendall(data)
    except OSError:
        pass
    else:
        stdin_eof.set()
    if not done.is_set():
        with contextlib.suppress(OSError):
            s.shutdown(socket.SHUT_WR)
        done.wait(3)   # let the daemon's last replies through
    s.close()
    return 0 if stdin_eof.is_set() else 1


def once_via_socket(s: socket.socket, cmd: str, raw_args: str) -> int:
    """`--once` against the live daemon: same output contract as the standalone one-shot mode."""
    def reply(obj: dict) -> int:
        obj.pop("id", None)
        protocol.write_line(sys.stdout.buffer, obj)
        return 0 if obj.get("ok") else 1
    try:
        args = protocol.parse_args(raw_args)
    except protocol.DeviceError as exc:
        return reply(protocol.reply_error(None, exc))
    shown = ["connection", "installProgress", "pair"]
    f = s.makefile("rwb")
    protocol.write_line(f, protocol.request("h", "hello", {"name": "once", "events": shown}))
    protocol.write_line(f, protocol.request("r", cmd, args))
    for m in protocol.read_lines(f):
        if protocol.is_event(m):
            if m["event"] in shown:
                print(f"[{time.strftime('%H:%M:%S')}] deviced: {m['event']}: {json.dumps(m.get('data'))[:300]}",
                      file=sys.stderr, flush=True)
            continue
        if m.get("id") == "r":
            return reply(m)
    return reply(protocol.reply_error(None, protocol.DeviceError("unreachable", "The iPhone service stopped before answering.")))


def pycache_args() -> list[str]:
    """Pass our -X pycache_prefix (set by bin/iosstudio-deviced) on to the python we start."""
    return ["-X", f"pycache_prefix={sys.pycache_prefix}"] if sys.pycache_prefix else []


RUN_PY = Path(__file__).resolve().parents[2] / "run.py"   # helper/run.py: starts a module of the iosstudio package


def launch(argv: list[str]) -> int:
    """device_broker launch <deviced> <python> <server module> [--once <cmd> [json]]
    (the server module is iosstudio.device.deviced or iosstudio.device.deviced_demo, started through helper/run.py)"""
    deviced, py, server = argv[0], argv[1], argv[2]
    rest = argv[3:]
    demo = os.environ.get("IOSSTUDIO_DEMO") or None
    p = paths(demo)
    try:
        _prepare_dir(p["dir"])
    except OSError as exc:
        print(f"deviced: can't use {p['dir']}: {exc}", file=sys.stderr)
        return 1
    if rest and rest[0] == "--once":
        if len(rest) < 2:
            print("usage: iosstudio-deviced --once <cmd> '<json args>'", file=sys.stderr)
            return 2
        s = _connect(p["sock"])
        if s is not None:
            with s:
                return once_via_socket(s, rest[1], rest[2] if len(rest) > 2 else "{}")
        os.execv(py, [py, "-I", *pycache_args(), str(RUN_PY), server, *rest])
    bad = socket_path_error(p["sock"])
    if bad is not None:
        # the same `ready` error bin/iosstudio-deviced sends when the iPhone tools are missing
        protocol.write_line(sys.stdout.buffer, protocol.event("ready", {"version": VERSION, "pymobiledevice3": None,
                                                                         "error": bad.to_json()}))
        return 3
    s = connect_or_spawn(p["sock"], deviced)
    if s is None:
        print(f"deviced: the daemon didn't come up; see {p['log']}", file=sys.stderr, flush=True)
        return 1
    return bridge(s)


if __name__ == "__main__":
    if len(sys.argv) >= 5 and sys.argv[1] == "launch":
        sys.exit(launch(sys.argv[2:]))
    print(__doc__)
    sys.exit(2)
