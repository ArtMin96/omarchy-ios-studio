"""The shared device-daemon broker (helper/iosstudio/device/broker.py) over a real Unix socket in a private
runtime dir: client lifecycle and welcome, event fan-out and subscriptions, the syslog hub feeding several
clients through their own bounded buffers, slow clients, idle shutdown and the single-instance lock.

The backend is a small fake; SyslogSub/route/SyslogWorker are covered in test_daemon_loop_perf.py.

    python -m pytest tests/helper/test_dev_broker.py
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import devtest

devtest.isolate()

from iosstudio.device import broker as B  # noqa: E402
from iosstudio.device import protocol  # noqa: E402
from iosstudio.device.protocol import DeviceError  # noqa: E402

WAIT = 3.0   # upper bound for anything awaited; tests finish far sooner


class FakeBackend:
    """What the broker calls on a daemon (backend.py lists it), recording the calls."""

    def __init__(self):
        self.gone: list = []
        self.opened: list[int] = []
        self.closed = 0
        self.shut = False

    async def welcome(self):
        return [("ready", {"version": "1", "pymobiledevice3": "fake"}), ("devices", [{"udid": "U1"}])]

    async def dispatch(self, cmd, args):
        if cmd == "echo":
            return {"args": args, "client": B.current_client().id}
        if cmd == "boom":
            raise DeviceError("no-device", "No iPhone.", {"action": "connect"}, "detail")
        if cmd == "crash":
            raise ValueError("bad thing")
        if cmd == "hang":
            await asyncio.Event().wait()
        raise DeviceError("bad-args", f"Unknown command {cmd!r}.")

    def error_json(self, exc, cmd):
        return protocol.error_json(exc)

    async def settle(self, cmd):
        pass

    def syslog_target(self, args):
        return "U1"

    def syslog_open(self, pid):
        self.opened.append(pid)

    async def syslog_close(self):
        self.closed += 1

    async def syslog_resolve(self, bid):
        return "MyApp", bid

    async def client_gone(self, client):
        self.gone.append(client.id)

    async def shutdown(self):
        self.shut = True


class Conn:
    """One test client on the broker socket. A background reader queues every message; next() takes the
    first one matching, leaving the others (events that arrive before a reply) for later lookups."""

    @classmethod
    async def open(cls, path: Path, read: bool = True, limit: int = protocol.MAX_LINE) -> "Conn":
        r, w = await asyncio.open_unix_connection(str(path), limit=limit)
        c = cls.__new__(cls)
        c.r, c.w, c.q, c.ids, c.backlog, c.eof = r, w, asyncio.Queue(), 0, [], False
        c.task = asyncio.create_task(c._read()) if read else None
        return c

    async def _read(self):
        async for m in protocol.read_lines_async(self.r):
            self.q.put_nowait(m)
        self.q.put_nowait(None)    # EOF

    def _pull(self) -> None:
        while not self.q.empty():
            m = self.q.get_nowait()
            if m is None:
                self.eof = True
            else:
                self.backlog.append(m)

    async def next(self, pred=lambda m: True) -> dict | None:
        """The first message matching pred, or None at EOF."""
        while True:
            self._pull()
            for i, m in enumerate(self.backlog):
                if pred(m):
                    return self.backlog.pop(i)
            if self.eof:
                return None
            m = await asyncio.wait_for(self.q.get(), WAIT)
            if m is None:
                self.eof = True
            else:
                self.backlog.append(m)

    async def event(self, name: str) -> dict:
        m = await self.next(lambda m: m.get("event") == name)
        assert m is not None, f"EOF before {name}"
        return m["data"]

    async def call(self, cmd: str, args: dict | None = None) -> dict:
        self.ids += 1
        rid = self.ids
        await protocol.write_line_async(self.w, protocol.request(rid, cmd, args))
        m = await self.next(lambda m: m.get("id") == rid)
        assert m is not None, f"EOF before the reply to {cmd}"
        return m

    def take_events(self, name: str | None = None) -> list:
        """Every queued event (named `name`, or all as (name, data)), removed from the backlog."""
        self._pull()
        keep, out = [], []
        for m in self.backlog:
            if "event" in m and (name is None or m["event"] == name):
                out.append(m["data"] if name else (m["event"], m["data"]))
            else:
                keep.append(m)
        self.backlog = keep
        return out

    async def close(self):
        self.w.close()
        with contextlib.suppress(Exception):
            await self.w.wait_closed()
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)


class BrokerCase(unittest.IsolatedAsyncioTestCase):
    """A Broker in its own runtime dir, served by run() on this test's loop."""

    idle = 60.0

    async def asyncSetUp(self):
        base = Path(os.environ["IOSSTUDIO_RUNTIME_DIR"])
        base.mkdir(parents=True, exist_ok=True)
        self.base = Path(tempfile.mkdtemp(dir=base, prefix="b"))
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        for p in (mock.patch.dict(os.environ, {"IOSSTUDIO_RUNTIME_DIR": str(self.base)}),
                  mock.patch.object(B, "log", lambda text: None)):
            p.start()
            self.addCleanup(p.stop)
        self.backend = FakeBackend()
        self.conns: list[Conn] = []
        self.b = B.Broker(demo=None, idle=self.idle)
        self.sock = self.b.p["sock"]
        self.ready = asyncio.Event()     # held by tests that want a slow backend start
        self.ready.set()
        self.run_task: asyncio.Task | None = None

    async def asyncTearDown(self):
        for c in self.conns:
            await c.close()
        if self.run_task is not None:
            if not self.run_task.done():
                self.b.stop()
            await asyncio.wait_for(asyncio.gather(self.run_task, return_exceptions=True), WAIT)

    async def make_backend(self, broker):
        await self.ready.wait()
        return self.backend

    async def start(self):
        B._prepare_dir(self.b.p["dir"])
        self.run_task = asyncio.create_task(self.b.run(self.make_backend))
        while self.b._server is None:            # bound before make_backend runs
            await asyncio.sleep(0)
            self.assertFalse(self.run_task.done(), "run() ended early")

    async def connect(self, read: bool = True, limit: int = protocol.MAX_LINE) -> Conn:
        c = await Conn.open(self.sock, read, limit)
        self.conns.append(c)
        return c

    async def until(self, cond):
        async with asyncio.timeout(WAIT):
            while not cond():
                await asyncio.sleep(0.001)


# ---------------------------------------------------------------- lifecycle

class Lifecycle(BrokerCase):
    async def test_welcome_then_hello(self):
        await self.start()
        c = await self.connect()
        ready = await c.event("ready")
        self.assertEqual(ready["pymobiledevice3"], "fake")
        self.assertEqual(ready["socket"], str(self.sock))                 # where() merged into `ready`
        self.assertEqual(ready["runtimeDir"], str(self.b.p["dir"]))
        self.assertEqual(ready["log"], str(self.b.p["log"]))
        self.assertEqual(await c.event("devices"), [{"udid": "U1"}])
        reply = await c.call("hello", {"name": "x" * 60, "events": ["devices"]})
        self.assertTrue(reply["ok"])
        d = reply["data"]
        cid = d["clientId"]
        self.assertEqual(d["name"], "x" * 40 + f"-{cid}")                  # cut to 40 + "-<id>"
        self.assertEqual((d["version"], d["pid"], d["clients"], d["demo"], d["idle"], d["events"]),
                         (B.VERSION, os.getpid(), 1, False, 60.0, ["devices"]))
        self.assertEqual(d["socket"], str(self.sock))
        self.assertEqual(os.stat(self.sock).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(self.b.p["dir"]).st_mode & 0o777, 0o700)

    async def test_requests_run_as_their_client_and_errors_map(self):
        await self.start()
        a, b = await self.connect(), await self.connect()
        ida = (await a.call("hello"))["data"]["clientId"]
        idb = (await b.call("hello"))["data"]["clientId"]
        self.assertNotEqual(ida, idb)
        self.assertEqual((await a.call("echo", {"x": 1}))["data"], {"args": {"x": 1}, "client": ida})
        self.assertEqual((await b.call("echo"))["data"]["client"], idb)
        err = await a.call("boom")
        self.assertEqual(err, {"id": err["id"], "ok": False, "error": {
            "code": "no-device", "message": "No iPhone.", "fix": {"action": "connect"}, "detail": "detail"}})
        err = (await a.call("crash"))["error"]
        self.assertEqual(err["code"], "failed")
        self.assertEqual(err["detail"], "ValueError: bad thing")
        # args that aren't an object reach the backend as {}
        await protocol.write_line_async(a.w, {"id": "s", "cmd": "echo", "args": [1, 2]})
        self.assertEqual((await a.next(lambda m: m.get("id") == "s"))["data"]["args"], {})

    async def test_malformed_line_is_reported_and_connection_survives(self):
        await self.start()
        c = await self.connect()
        c.w.write(b"not json\n[1,2]\n")
        warn = await c.event("log")
        self.assertEqual(warn, {"level": "warn", "text": "ignored a malformed request line"})
        self.assertTrue((await c.call("hello"))["ok"])

    async def test_disconnect_releases_resources_and_tells_backend(self):
        await self.start()
        a, b = await self.connect(), await self.connect()
        ida = (await a.call("hello"))["data"]["clientId"]
        await b.call("hello")
        client_a = next(x for x in self.b.clients if x.id == ida)
        closed: list = []
        await self.b.resources.acquire("mirror", client_a, lambda: "H", lambda h, why: closed.append((h, why)))
        listing = (await b.call("daemon.clients"))["data"]
        self.assertEqual([x["id"] for x in listing], sorted(x.id for x in self.b.clients))
        self.assertTrue(next(x for x in listing if x["id"] == ida)["mirror"])
        await a.close()
        await self.until(lambda: self.backend.gone == [ida])
        self.assertEqual(closed, [("H", "client")])
        self.assertEqual(len(self.b.clients), 1)
        self.assertEqual([x["id"] for x in (await b.call("daemon.clients"))["data"]], [x.id for x in self.b.clients])

    async def test_in_flight_request_is_dropped_with_its_client(self):
        await self.start()
        c = await self.connect()
        await c.call("hello")
        await protocol.write_line_async(c.w, protocol.request(9, "hang"))
        await self.until(lambda: any(t.get_coro().__qualname__ == "Broker._request" for t in asyncio.all_tasks()))
        await c.close()
        await self.until(lambda: not self.b.clients and self.backend.gone)

    async def test_shutdown_without_all_only_detaches_the_caller(self):
        await self.start()
        a, b = await self.connect(), await self.connect()
        self.assertEqual((await a.call("daemon.shutdown"))["data"], {})
        self.assertIsNone(await a.next(lambda m: False))       # the broker closes a's connection
        self.assertFalse(self.run_task.done())
        self.assertTrue((await b.call("hello"))["ok"])         # b is still served
        await b.call("daemon.shutdown", {"all": True})
        self.assertEqual(await asyncio.wait_for(self.run_task, WAIT), 0)
        self.assertTrue(self.backend.shut)
        self.assertFalse(self.sock.exists())


# ---------------------------------------------------------------- events

class Events(BrokerCase):
    async def test_events_are_delivered_in_order_to_matching_clients(self):
        await self.start()
        everything, narrow, none = await self.connect(), await self.connect(), await self.connect()
        await narrow.call("hello", {"events": ["devices"]})
        await none.call("subscribe", {"events": []})
        for c in (everything, narrow, none):
            await c.event("devices")
            c.take_events()                                    # the welcome
        self.b.emit("connection", {"state": "up"})
        self.b.emit("devices", [{"udid": "U2"}])
        await asyncio.to_thread(self.b.emit, "pair", {"from": "thread"})   # from another thread
        await everything.event("pair")
        for c in (everything, narrow, none):
            await c.call("hello")       # a round trip: anything emitted to c is queued before the reply
        self.assertEqual(everything.take_events(), [("connection", {"state": "up"}), ("devices", [{"udid": "U2"}])])
        self.assertEqual(narrow.take_events(), [("devices", [{"udid": "U2"}])])
        self.assertEqual(none.take_events(), [])

    async def test_wildcard_resets_to_everything(self):
        await self.start()
        c = await self.connect()
        self.assertEqual((await c.call("subscribe", {"events": ["a"]}))["data"]["events"], ["a"])
        self.assertIsNone((await c.call("subscribe", {"events": ["*", "a"]}))["data"]["events"])
        self.assertEqual((await c.call("subscribe", {"events": []}))["data"]["events"], [])
        self.assertIsNone((await c.call("subscribe", {"events": None}))["data"]["events"])


# ---------------------------------------------------------------- slow clients

class FakeTransport:
    def __init__(self):
        self.size = 0

    def get_write_buffer_size(self):
        return self.size


class FakeWriter:
    def __init__(self):
        self.transport = FakeTransport()
        self.data: list[bytes] = []
        self.closed = False

    def write(self, b):
        if self.closed:
            raise ConnectionResetError
        self.data.append(b)
        self.transport.size += len(b)

    def close(self):
        self.closed = True


class SlowClients(BrokerCase):
    async def test_client_over_buffer_is_disconnected(self):
        w = FakeWriter()
        c = B.Client(self.b, w)
        with mock.patch.object(B, "CLIENT_BUFFER_MAX", 100):
            c.send({"event": "x", "data": "a" * 10})
            self.assertFalse(c.closed)
            c.send({"event": "x", "data": "a" * 200})
            self.assertTrue(c.closed and w.closed)
            n = len(w.data)
            c.send({"event": "x"})                                    # closed: nothing more is written
            self.assertEqual(len(w.data), n)

    async def test_write_error_closes_the_client(self):
        w = FakeWriter()
        w.closed = True
        c = B.Client(self.b, w)
        c.send({"event": "x"})
        self.assertTrue(c.closed)

    async def stall(self) -> tuple[Conn, int]:
        """A fast client and one that never reads; emit 32 KB events until the broker gives up on the
        stalled one. Returns the fast client and how many events were sent."""
        await self.start()
        fast = await self.connect()
        await self.connect(read=False, limit=16 * 1024)        # never reads: its socket fills up
        await fast.call("hello")
        await self.until(lambda: len(self.b.clients) == 2)
        chunk = "x" * 32 * 1024
        for i in range(200):                                    # up to ~6 MB; stops at the drop
            self.b.emit("blob", {"i": i, "pad": chunk})
            got = await fast.event("blob")                      # the fast client keeps draining
            self.assertEqual(got["i"], i)
            if any(c.closed for c in self.b.clients):
                return fast, i + 1
        self.fail("the stalled client was never given up on")

    async def test_stalled_reader_is_cut_off_and_others_keep_up(self):
        with mock.patch.object(B, "CLIENT_BUFFER_MAX", 256 * 1024):
            fast, sent = await self.stall()
            self.assertLess(sent * 32 * 1024, 4 * 1024 * 1024)   # bounded: given up on after a few hundred KB
            stalled = next(c for c in self.b.clients if c.closed)
            before = stalled.w.transport.get_write_buffer_size()
            self.assertLessEqual(before, B.CLIENT_BUFFER_MAX + 64 * 1024)
            self.b.emit("blob", {"pad": "x" * 32 * 1024})          # nothing more is queued for it
            self.assertEqual(stalled.w.transport.get_write_buffer_size(), before)
            self.assertTrue((await fast.call("hello"))["ok"])

    # writer.close() waits for the write buffer to flush, which a client that stopped reading never
    # lets happen: the broker aborts it instead, so it leaves broker.clients, its resources are
    # released and the daemon can idle out.
    async def test_stalled_reader_is_disconnected_and_released(self):
        with mock.patch.object(B, "CLIENT_BUFFER_MAX", 256 * 1024):
            await self.stall()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.until(lambda: len(self.b.clients) == 1), 0.3)
            self.assertEqual(len(self.b.clients), 1, "the stalled client is still connected")
            self.assertEqual(len(self.backend.gone), 1)


# ---------------------------------------------------------------- syslog hub

def entry(i: int, proc="MyApp", level="default", pid=100) -> dict:
    return {"ts": float(i), "process": proc, "pid": pid, "level": level, "subsystem": "", "category": "",
            "message": f"line {i}"}


class SyslogFanOut(BrokerCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        p = mock.patch.object(B, "SYSLOG_FLUSH_S", 0.002)
        p.start()
        self.addCleanup(p.stop)

    async def lines(self, c: Conn, n: int) -> list[dict]:
        out: list[dict] = []
        while len(out) < n:
            out += await c.event("syslog")
        return out

    async def test_each_client_gets_its_own_matches(self):
        await self.start()
        app, errs, other = await self.connect(), await self.connect(), await self.connect()
        r = await app.call("syslog.start", {"filters": {"processes": ["MyApp"]}})
        self.assertEqual(r["data"], {"running": True, "filters": {"processes": ["MyApp"]}})
        st = await app.event("syslogState")
        self.assertEqual((st["running"], st["udid"], st["dropped"]), (True, "U1", 0))
        await other.call("syslog.filter", {"filters": {"pids": [7]}})   # pending until start
        await errs.call("syslog.start", {"filters": {"minLevel": "error"}})
        r = await other.call("syslog.start")
        self.assertEqual(r["data"]["filters"], {"pids": [7]})
        self.assertEqual(self.backend.opened, [-1])            # one phone stream, not one per client
        for i in range(10):
            self.b.syslog.feed(entry(i, proc="MyApp" if i % 2 else "Other", level="error" if i % 3 == 0 else "info",
                                     pid=7 if i == 4 else 100))
        self.assertEqual([e["message"] for e in await self.lines(app, 5)],
                         ["line 1", "line 3", "line 5", "line 7", "line 9"])
        self.assertEqual([e["message"] for e in await self.lines(errs, 4)], ["line 0", "line 3", "line 6", "line 9"])
        self.assertEqual([e["message"] for e in await self.lines(other, 1)], ["line 4"])
        # nobody got more than its share
        await app.call("hello")
        await errs.call("hello")
        self.assertEqual(app.take_events("syslog") + errs.take_events("syslog"), [])

    async def test_single_shared_pid_is_pushed_to_the_phone(self):
        await self.start()
        a, b = await self.connect(), await self.connect()
        await a.call("syslog.start", {"filters": {"pids": [42]}})
        self.assertEqual(self.backend.opened, [42])
        await b.call("syslog.start", {"filters": {"pids": [42], "minLevel": "error"}})
        self.assertEqual(self.backend.opened, [42])            # same pid: no restart
        await b.call("syslog.filter", {"filters": {"pids": [43]}})
        self.assertEqual(self.backend.opened, [42, -1])        # different pids: phone filter off
        self.assertEqual(self.backend.closed, 1)

    async def test_overflow_is_bounded_counted_and_reported(self):
        clock = devtest.FakeClock()
        await self.start()
        c = await self.connect()
        with mock.patch.object(B, "SYSLOG_BUFFER", 5), mock.patch.object(B, "time", clock.module()):
            await c.call("syslog.start")
            await c.event("syslogState")
            sub = self.b.syslog.subs[next(iter(self.b.clients))]
            for i in range(12):                                # synchronous: no flush in between
                self.b.syslog.feed(entry(i))
            self.assertEqual(len(sub.buf), 5)
            self.assertEqual(sub.dropped, 7)
            st = await c.event("syslogState")
            self.assertEqual(st["dropped"], 1)                 # the first notice is sent at the first drop
            self.assertEqual([e["message"] for e in await self.lines(c, 5)],
                             ["line 7", "line 8", "line 9", "line 10", "line 11"])
            for i in range(12, 20):                            # within 1 s: no second notice
                self.b.syslog.feed(entry(i))
            self.assertEqual(sub.dropped, 10)
            await self.lines(c, 5)
            clock.advance(1.5)
            for i in range(20, 26):
                self.b.syslog.feed(entry(i))
            st = await c.event("syslogState")
            self.assertEqual(st["dropped"], 11)
            await c.call("hello")
            self.assertEqual(c.take_events("syslogState"), [])

    async def test_last_subscriber_leaving_closes_the_phone_stream(self):
        await self.start()
        a, b = await self.connect(), await self.connect()
        await a.call("syslog.start")
        await b.call("syslog.start")
        r = await a.call("syslog.stop")
        self.assertEqual(r["data"], {"running": False})
        self.assertEqual([st["running"] for st in a.take_events("syslogState")], [True, False])
        self.assertEqual(self.backend.closed, 0)
        await b.close()                                        # disconnect, not syslog.stop
        await self.until(lambda: self.backend.closed == 1)
        self.assertFalse(self.b.syslog.open)
        self.assertEqual(self.b.syslog.subs, {})

    async def test_stop_flushes_what_was_buffered(self):
        await self.start()
        c = await self.connect()
        await c.call("syslog.start")
        for i in range(3):
            self.b.syslog.feed(entry(i))
        await protocol.write_line_async(c.w, protocol.request("s", "syslog.stop"))
        got: list = []
        while True:
            m = await c.next()
            if m.get("event") == "syslog":
                got += m["data"]
            if m.get("id") == "s":
                break
        self.assertEqual([e["message"] for e in got], ["line 0", "line 1", "line 2"])

    async def test_stream_failed_ends_every_subscription(self):
        await self.start()
        a, b = await self.connect(), await self.connect()
        await a.call("syslog.start")
        await b.call("syslog.start")
        await a.event("syslogState")
        await b.event("syslogState")
        self.b.syslog.stream_failed({"code": "not-paired", "message": "Pair first."})
        for c in (a, b):
            st = await c.event("syslogState")
            self.assertFalse(st["running"])
            self.assertEqual(st["error"]["code"], "not-paired")
        self.assertEqual(self.b.syslog.subs, {})
        self.assertIsNone(self.b.syslog.error)


# ---------------------------------------------------------------- idle shutdown

class Idle(BrokerCase):
    idle = 0.01

    async def test_exits_when_nobody_connects(self):
        await self.start()
        self.assertEqual(await asyncio.wait_for(self.run_task, WAIT), 0)
        self.assertTrue(self.backend.shut)
        self.assertFalse(self.sock.exists())

    async def test_stays_up_while_a_client_is_connected_then_exits(self):
        await self.start()
        c = await self.connect()
        await c.call("hello")
        self.assertIsNone(self.b._idle_handle)                  # no idle timer while someone is here
        self.b._idle_exit()                                     # a stale timer firing anyway
        self.assertFalse(self.b._stopping.is_set())
        await c.close()
        self.assertEqual(await asyncio.wait_for(self.run_task, WAIT), 0)
        self.assertEqual(len(self.backend.gone), 1)

    async def test_a_client_waiting_for_a_slow_start_is_not_dropped(self):
        self.ready.clear()                                      # the backend is still coming up
        await self.start()
        c = await self.connect()
        await self.until(lambda: self.b._connecting == 1)
        self.b._idle_exit()                                     # the idle timer fires meanwhile
        self.assertFalse(self.b._stopping.is_set())
        self.ready.set()
        self.assertEqual((await c.event("ready"))["socket"], str(self.sock))
        self.assertTrue((await c.call("hello"))["ok"])

    async def test_idle_from_environment(self):
        with mock.patch.dict(os.environ, {"IOSSTUDIO_DEVICED_IDLE": "7.5"}):
            self.assertEqual(B.Broker(demo=None).idle, 7.5)
        with mock.patch.dict(os.environ, {"IOSSTUDIO_DEVICED_IDLE": "soon"}):
            self.assertEqual(B.Broker(demo=None).idle, B.IDLE_DEFAULT_S)
        self.assertEqual(B.Broker(demo=None, idle=-3).idle, 0.0)


# ---------------------------------------------------------------- single instance

class SingleInstance(BrokerCase):
    def release(self, b: B.Broker):
        if b._lock is not None:
            b._lock.close()
            b._lock = None

    async def test_second_instance_is_refused_while_the_first_holds_the_lock(self):
        self.assertTrue(self.b.acquire(wait=0))
        self.addCleanup(self.release, self.b)
        self.assertEqual(self.b.p["lock"].read_text(), f"{os.getpid()}\n")
        second = B.Broker(demo=None, idle=60)
        self.assertFalse(second.acquire(wait=0))
        self.release(self.b)
        self.assertTrue(second.acquire(wait=0))                # free again: the next one gets it
        self.release(second)

    async def test_live_socket_refuses_at_once(self):
        self.assertTrue(self.b.acquire(wait=0))
        self.addCleanup(self.release, self.b)
        await self.start()
        second = B.Broker(demo=None, idle=60)
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        got = await asyncio.to_thread(second.acquire, 10.0)    # a live daemon answers: no 10 s wait
        self.assertFalse(got)
        self.assertLess(loop.time() - t0, 1.0)

    async def test_stale_socket_file_is_replaced(self):
        B._prepare_dir(self.b.p["dir"])
        self.sock.write_text("stale")
        await self.start()
        self.assertTrue(B.socket_alive(self.sock))

    async def test_demo_and_real_daemons_do_not_collide(self):
        demo = B.Broker(demo="ready", idle=60)
        self.assertNotEqual(demo.p["sock"], self.b.p["sock"])
        self.assertTrue(self.b.acquire(wait=0))
        self.addCleanup(self.release, self.b)
        self.assertTrue(demo.acquire(wait=0))
        self.release(demo)

    async def test_socket_path_too_long_is_refused(self):
        deep = self.base / ("d" * 120)
        with mock.patch.dict(os.environ, {"IOSSTUDIO_RUNTIME_DIR": str(deep)}):
            b = B.Broker(demo=None, idle=60)
        self.assertFalse(b.acquire(wait=0))
        self.assertFalse(deep.exists())                          # refused before creating anything

    async def test_runtime_dir_permissions_are_tightened(self):
        d = self.b.p["dir"]
        d.mkdir(parents=True)
        os.chmod(d, 0o755)
        self.assertTrue(self.b.acquire(wait=0))
        self.addCleanup(self.release, self.b)
        self.assertEqual(os.stat(d).st_mode & 0o777, 0o700)


if __name__ == "__main__":
    unittest.main()
