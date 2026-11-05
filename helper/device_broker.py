"""Shared device-daemon broker: one daemon per user, many clients (docs/deviced-protocol.md#broker).

Stdlib only. Used two ways:

* As a library by the daemons (deviced.py for the real phone, deviced_demo.py for demo mode):
  `Broker(backend).run()` listens on $XDG_RUNTIME_DIR/ios-studio[-demo]/deviced.sock, speaks the
  JSON-lines protocol per connection, broadcasts device-wide events to subscribed clients, keeps
  one syslog stream to the phone with per-client filters, owns the per-client session resources
  (deviced_kit.Resources: mirror, port forwards, ...) and exits after an idle grace period with
  no clients.
* As the launcher behind bin/iosstudio-deviced (`device_broker.py launch ...`): connects to the live
  daemon (spawning a detached one when none is running) and bridges its own stdin/stdout to the
  socket, so a client that speaks JSON lines over stdio (the GUI's Stream) needs no changes.
  `--once` sends one request to the live daemon, or falls back to the standalone one-shot daemon.

Backend interface (duck-typed):
    async welcome() -> [(event, data), ...]     sent to every new client (ready, devices, ...)
    async dispatch(cmd, args) -> data           raise on error; current_client() tells who asks
    error_json(exc, cmd) -> dict                exception -> protocol error object
    syslog_target(args) -> udid                 raise if no device for a syslog stream
    syslog_open(pid: int)                       start the one phone stream (-1 = no phone filter)
    async syslog_close()
    async syslog_resolve(bundle_id) -> (executable | None, full bundle id)
    async client_gone(client)                   optional: a client disconnected (after its resources)
    async shutdown()                            close the tunnel and everything else
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
import socket
import stat
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from device_client import runtime_dir, socket_path_error  # noqa: E402
import deviced_kit  # noqa: E402

VERSION = "1"
IDLE_DEFAULT_S = 120.0
MAX_LINE = 16 * 1024 * 1024
CLIENT_BUFFER_MAX = 32 * 1024 * 1024   # a client that stops reading is disconnected
LOG_MAX = 4 * 1024 * 1024               # deviced.log (tmpfs, so RAM) moves to deviced.log.1 past this
LOG_CHECK_S = 30.0

SYSLOG_FLUSH_S = 0.1
SYSLOG_BATCH = 200
SYSLOG_BUFFER = 1000
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
    def __init__(self, client: "Client", filters: dict):
        self.client = client
        self.filters = filters
        self.min = LEVELS.get(filters.get("minLevel", "debug"), 0)
        self.exe: str | None = None
        self.bid: str | None = None
        self.buf: deque = deque()
        self.dropped = 0
        self.last_drop_emit = 0.0

    def match(self, e: dict) -> bool:
        f = self.filters
        if LEVELS.get(e["level"], 2) < self.min:
            return False
        if "pids" in f and e["pid"] not in f["pids"]:
            return False
        proc = e["process"]
        if "processes" in f and proc not in f["processes"]:
            return False
        msg = e["message"]
        if "bundleId" in f:
            exe, bid = self.exe, self.bid or f["bundleId"]
            if not ((exe and proc == exe) or (exe and exe in msg) or bid in msg):
                return False
        sub = e["subsystem"]
        if "subsystem" in f and not sub.startswith(f["subsystem"]):
            return False
        if "text" in f:
            t = f["text"].lower()
            if t not in msg.lower() and t not in proc.lower() and t not in sub.lower():
                return False
        return True


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
        sub.client.send({"event": "syslogState", "data": self.state(sub, running)})

    def min_level(self) -> int:
        return self._min

    def _recompute(self) -> int:
        self._min = min((s.min for s in self.subs.values()), default=0)
        pids = [tuple(s.filters.get("pids") or ()) for s in self.subs.values()]
        if pids and all(len(p) == 1 for p in pids) and len(set(pids)) == 1:
            return pids[0][0]
        return -1

    async def _resolve(self, sub: SyslogSub):
        bid = sub.filters.get("bundleId")
        sub.exe, sub.bid = (None, None)
        if bid:
            try:
                sub.exe, sub.bid = await asyncio.wait_for(self.b.backend.syslog_resolve(bid), 15)
            except Exception as exc:  # noqa: BLE001
                log(f"syslog: can't resolve {bid}: {exc!r}")
                sub.exe, sub.bid = None, bid

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
            self.flusher = asyncio.create_task(self._flush_loop())

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
        sub.filters, sub.min = f, LEVELS.get(f.get("minLevel", "debug"), 0)
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
        for sub in self.subs.values():
            if sub.match(entry):
                if len(sub.buf) >= SYSLOG_BUFFER:
                    sub.buf.popleft()
                    sub.dropped += 1
                    now = time.monotonic()
                    if now - sub.last_drop_emit > 1:
                        sub.last_drop_emit = now
                        self._send_state(sub)
                sub.buf.append(entry)

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
            sub.client.send({"event": "syslog", "data": [sub.buf.popleft() for _ in range(n)]})
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
            asyncio.create_task(self._sync_stream())


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
            line = json.dumps(obj, separators=(",", ":"), default=str) + "\n"
            self.w.write(line.encode("utf-8"))
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
        self.backend = None
        self.clients: set[Client] = set()
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
        obj = {"event": name, "data": data}
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
        if not self.clients:
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
            log("bye")
        return 0

    # -- connections
    async def _serve_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        c = Client(self, writer)
        await self._ready.wait()
        self.clients.add(c)
        self._check_idle()
        log(f"{c.name} connected ({len(self.clients)} client(s))")
        tasks: set[asyncio.Task] = set()
        try:
            for name, data in await self.backend.welcome():
                c.send({"event": name, "data": data})
            while not c.closed:
                try:
                    raw = await reader.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    c.send({"event": "log", "data": {"level": "warn", "text": "request line too long"}})
                    break
                if not raw:
                    break
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    m = json.loads(raw)
                    if not isinstance(m, dict):
                        raise ValueError
                except ValueError:
                    c.send({"event": "log", "data": {"level": "warn", "text": "ignored a malformed request line"}})
                    continue
                token = _current.set(c)
                try:
                    t = asyncio.create_task(self._request(c, m))
                finally:
                    _current.reset(token)
                tasks.add(t)
                t.add_done_callback(tasks.discard)
        except (ConnectionError, OSError):
            pass
        finally:
            c.close()
            self.clients.discard(c)
            log(f"{c.name} disconnected ({len(self.clients)} client(s) left)")
            self.syslog.client_gone(c)
            asyncio.create_task(self._client_gone(c))
            self._check_idle()

    async def _client_gone(self, c: Client):
        """Release what the client held (mirror, port forwards, ...), then tell the backend."""
        await self.resources.client_gone(c)
        hook = getattr(self.backend, "client_gone", None)
        if hook is not None:
            with contextlib.suppress(Exception):
                await hook(c)

    async def _request(self, c: Client, m: dict):
        rid = m.get("id")
        cmd = str(m.get("cmd") or "")
        args = m.get("args") if isinstance(m.get("args"), dict) else {}
        try:
            data = await self._command(c, cmd, args)
            c.send({"id": rid, "ok": True, "data": data if data is not None else {}})
        except asyncio.CancelledError:
            c.send({"id": rid, "ok": False, "error": {"code": "cancelled", "message": "Cancelled."}})
        except BaseException as exc:  # noqa: BLE001
            c.send({"id": rid, "ok": False, "error": self.backend.error_json(exc, cmd)})
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
                    "clients": len(self.clients), "demo": bool(self.demo), "socket": str(self.p["sock"]),
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
        sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
        sys.stdout.flush()
        return 0 if obj.get("ok") else 1
    try:
        args = json.loads(raw_args or "{}")
        if not isinstance(args, dict):
            raise ValueError("args must be a JSON object")
    except ValueError as exc:
        return reply({"ok": False, "error": {"code": "bad-args", "message": "The arguments aren't valid JSON.", "detail": str(exc)}})
    f = s.makefile("rwb", buffering=0)
    hello = {"id": "h", "cmd": "hello", "args": {"name": "once", "events": ["connection", "installProgress", "pair"]}}
    f.write((json.dumps(hello) + "\n" + json.dumps({"id": "r", "cmd": cmd, "args": args}) + "\n").encode())
    for raw in f:
        try:
            m = json.loads(raw)
        except ValueError:
            continue
        if "event" in m:
            if m["event"] in ("connection", "installProgress", "pair"):
                print(f"[{time.strftime('%H:%M:%S')}] deviced: {m['event']}: {json.dumps(m.get('data'))[:300]}",
                      file=sys.stderr, flush=True)
            continue
        if m.get("id") == "r":
            m.pop("id", None)
            return reply(m)
    return reply({"ok": False, "error": {"code": "unreachable", "message": "The iPhone service stopped before answering."}})


def pycache_args() -> list[str]:
    """Pass our -X pycache_prefix (set by bin/iosstudio-deviced) on to the python we start."""
    return ["-X", f"pycache_prefix={sys.pycache_prefix}"] if sys.pycache_prefix else []


def launch(argv: list[str]) -> int:
    """device_broker.py launch <deviced> <python> <server.py> [--once <cmd> [json]]"""
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
        os.execv(py, [py, "-I", *pycache_args(), server, *rest])
    bad = socket_path_error(p["sock"])
    if bad is not None:
        # the same `ready` error bin/iosstudio-deviced sends when the iPhone tools are missing
        print(json.dumps({"event": "ready", "data": {"version": VERSION, "pymobiledevice3": None,
                                                     "error": bad.to_json()}}), flush=True)
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
