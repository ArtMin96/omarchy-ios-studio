"""The device daemon client (helper/iosstudio/device/client.py) against a tiny in-process daemon on a Unix
socket in a private runtime dir: hello, replies and errors, timeouts, disconnects, the reconnect backoff,
event iterators and callbacks, and starting the daemon (a fake launcher script).

    python -m pytest tests/helper/test_dev_client.py
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

from iosstudio.device import client as C  # noqa: E402
from iosstudio.device import protocol  # noqa: E402
from iosstudio.device.client import DeviceClient, DeviceError  # noqa: E402

WAIT = 3.0


class FakeDaemon:
    """Speaks protocol.py framing: answers hello, echo, fail, hang (never), and pushes events on demand."""

    def __init__(self, path: Path):
        self.path = path
        self.requests: list[dict] = []
        self.writers: list[asyncio.StreamWriter] = []
        self.server: asyncio.AbstractServer | None = None
        self.hellos = 0
        self.got = asyncio.Event()
        self.welcome: list[tuple[str, object]] = [("ready", {"version": "1"})]

    async def start(self) -> None:
        self.server = await asyncio.start_unix_server(self._serve, path=str(self.path))

    async def stop(self) -> None:
        self.drop()
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()

    def drop(self) -> None:
        for w in self.writers:
            w.close()
        self.writers.clear()

    def push(self, name: str, data) -> None:
        for w in self.writers:
            w.write(protocol.encode(protocol.event(name, data)))

    def send_raw(self, b: bytes) -> None:
        for w in self.writers:
            w.write(b)

    async def _serve(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        self.writers.append(w)
        for name, data in self.welcome:
            w.write(protocol.encode(protocol.event(name, data)))
        try:
            async for m in protocol.read_lines_async(r):
                self.requests.append(m)
                self.got.set()
                reply = self.answer(m)
                if reply is not None:
                    w.write(protocol.encode(reply))
        except (ConnectionError, OSError):
            pass
        finally:
            w.close()

    def answer(self, m: dict) -> dict | None:
        rid, cmd, args = m["id"], m["cmd"], m["args"]
        if cmd == "hello":
            self.hellos += 1
            return protocol.reply_ok(rid, {"version": "1", "clientId": self.hellos, "name": args.get("name")})
        if cmd in ("echo", "subscribe"):
            return protocol.reply_ok(rid, args)
        if cmd == "fail":
            return protocol.reply_error(rid, {"code": "no-device", "message": "No iPhone.",
                                              "fix": {"action": "connect"}, "detail": "usb"})
        if cmd == "weird":
            return {"id": rid, "ok": False, "error": "just a string"}
        if cmd == "none":
            return protocol.reply_ok(rid, None)
        return None   # hang


class SleepRecorder:
    """Stands in for client.py's `asyncio` module: records asyncio.sleep() delays (the reconnect backoff)
    without waiting them out; everything else is the real asyncio."""

    def __init__(self, on_sleep=None):
        self.delays: list[float] = []
        self.on_sleep = on_sleep

    def __getattr__(self, name):
        return getattr(asyncio, name)

    async def sleep(self, delay, *a):
        self.delays.append(delay)
        if self.on_sleep is not None:
            await self.on_sleep(self)
        await asyncio.sleep(0)


class ClientCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        base = Path(os.environ["IOSSTUDIO_RUNTIME_DIR"])
        base.mkdir(parents=True, exist_ok=True)
        self.dir = Path(tempfile.mkdtemp(dir=base, prefix="c"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.sock = self.dir / "deviced.sock"
        self.daemon = FakeDaemon(self.sock)
        await self.daemon.start()
        self.clients: list[DeviceClient] = []

    async def asyncTearDown(self):
        for c in self.clients:
            await c.close()
        await self.daemon.stop()

    def client(self, **kw) -> DeviceClient:
        kw.setdefault("spawn", False)
        c = DeviceClient(socket=self.sock, **kw)
        self.clients.append(c)
        return c

    async def until(self, cond):
        async with asyncio.timeout(WAIT):
            while not cond():
                await asyncio.sleep(0.001)


# ---------------------------------------------------------------- connect and requests

class Requests(ClientCase):
    async def test_connect_sends_hello_and_keeps_its_reply(self):
        dev = self.client(name="mcp", events=["devices"])
        await dev.connect()
        self.assertTrue(dev.connected)
        hello = self.daemon.requests[0]
        self.assertEqual(hello["cmd"], "hello")
        self.assertEqual(hello["args"], {"name": "mcp", "events": ["devices"], "version": C.PROTOCOL_VERSION})
        self.assertEqual(dev.hello, {"version": "1", "clientId": 1, "name": "mcp"})
        await dev.connect()                                   # idempotent: no second hello
        self.assertEqual(self.daemon.hellos, 1)
        await self.until(lambda: dev.ready is not None)
        self.assertEqual(dev.ready, {"version": "1"})         # the welcome's `ready` is kept

    async def test_request_connects_on_demand_and_returns_data(self):
        dev = self.client()
        self.assertFalse(dev.connected)
        self.assertEqual(await dev.request("echo", {"a": [1, 2]}), {"a": [1, 2]})
        self.assertEqual(await dev.request("echo"), {})                 # args default to {}
        self.assertEqual(await dev.request("none"), {})                 # null data -> {}
        self.assertEqual(dev._pending, {})

    async def test_concurrent_replies_are_matched_by_id(self):
        dev = self.client()
        await dev.connect()
        results = await asyncio.gather(*(dev.request("echo", {"n": i}) for i in range(20)))
        self.assertEqual([r["n"] for r in results], list(range(20)))
        ids = [m["id"] for m in self.daemon.requests]
        self.assertEqual(len(ids), len(set(ids)))

    async def test_error_reply_maps_to_device_error(self):
        dev = self.client()
        with self.assertRaises(DeviceError) as cm:
            await dev.request("fail")
        e = cm.exception
        self.assertEqual((e.code, e.message, e.fix, e.detail), ("no-device", "No iPhone.", {"action": "connect"}, "usb"))
        self.assertEqual(e.to_json(), {"code": "no-device", "message": "No iPhone.", "fix": {"action": "connect"},
                                       "detail": "usb"})
        with self.assertRaises(DeviceError) as cm:
            await dev.request("weird")
        self.assertEqual((cm.exception.code, cm.exception.message), ("failed", "just a string"))
        self.assertTrue(dev.connected)                        # an error reply doesn't drop the connection

    async def test_timeout(self):
        dev = self.client()
        await dev.connect()
        with self.assertRaises(DeviceError) as cm:
            await dev.request("hang", timeout=0.01)
        self.assertEqual(cm.exception.code, "timeout")
        self.assertIn("hang", cm.exception.message)
        self.assertIn("0.01 s", cm.exception.message)
        self.assertEqual(dev._pending, {})                   # nothing left waiting
        self.assertEqual(await dev.request("echo", {"x": 1}), {"x": 1})   # still usable

    async def test_late_and_unknown_replies_and_bad_lines_are_ignored(self):
        dev = self.client()
        await dev.connect()
        self.daemon.send_raw(b"garbage\n[1]\n\n" + protocol.encode(protocol.reply_ok(999, {"stray": 1})))
        self.assertEqual(await dev.request("echo", {"ok": 1}), {"ok": 1})
        self.assertTrue(dev.connected)

    async def test_subscribe_sends_the_new_list(self):
        dev = self.client(events=[])
        self.assertEqual(await dev.subscribe(["syslog", "devices"]), {"events": ["syslog", "devices"]})
        self.assertEqual(await dev.subscribe(None), {"events": None})
        self.assertIsNone(dev._events)

    async def test_hello_error_leaves_it_disconnected(self):
        self.daemon.answer = lambda m: protocol.reply_error(m["id"], {"code": "bad-version", "message": "Too old."})
        dev = self.client(reconnect=False)
        with self.assertRaises(DeviceError) as cm:
            await dev.connect()
        self.assertEqual(cm.exception.code, "bad-version")
        self.assertFalse(dev.connected)


# ---------------------------------------------------------------- failures

class Failures(ClientCase):
    async def test_no_daemon_without_spawn(self):
        await self.daemon.stop()
        dev = self.client(reconnect=False)
        with self.assertRaises(DeviceError) as cm:
            await dev.connect()
        self.assertEqual(cm.exception.code, "daemon-unavailable")
        self.assertEqual(cm.exception.detail, str(self.sock))

    async def test_socket_path_too_long(self):
        dev = DeviceClient(socket=self.dir / ("s" * 120), spawn=True)
        with self.assertRaises(DeviceError) as cm:
            await dev.connect()
        self.assertEqual(cm.exception.code, "socket-path-too-long")
        self.assertIn("IOSSTUDIO_RUNTIME_DIR", cm.exception.message)

    async def test_daemon_going_away_fails_in_flight_requests(self):
        dev = self.client(reconnect=False)
        await dev.connect()
        lost: list = []
        dev.on_event(lambda n, d: lost.append((n, d)), ["disconnected"])
        req = asyncio.create_task(dev.request("hang", timeout=WAIT))
        await self.until(lambda: any(m["cmd"] == "hang" for m in self.daemon.requests))
        self.daemon.drop()
        with self.assertRaises(DeviceError) as cm:
            await req
        self.assertEqual(cm.exception.code, "disconnected")
        self.assertFalse(dev.connected)
        self.assertEqual([n for n, _ in lost], ["disconnected"])
        self.assertIn("closed the connection", lost[0][1]["reason"])
        self.assertIsNone(dev._reconnect_task)                # reconnect=False

    async def test_line_too_long_drops_the_connection(self):
        with mock.patch.object(C.proto, "MAX_LINE", 1024):
            dev = self.client(reconnect=False)
            await dev.connect()
        lost: list = []
        dev.on_event(lambda n, d: lost.append(d), ["disconnected"])
        self.daemon.push("big", "x" * 4096)
        await self.until(lambda: lost)
        self.assertIn("LineTooLong", lost[0]["reason"])
        self.assertFalse(dev.connected)

    async def test_close_fails_pending_and_never_reconnects(self):
        dev = self.client()
        await dev.connect()
        seen: list = []
        dev.on_event(lambda n, d: seen.append(n))
        req = asyncio.create_task(dev.request("hang", timeout=WAIT))
        await self.until(lambda: any(m["cmd"] == "hang" for m in self.daemon.requests))
        await dev.close()
        with self.assertRaises(DeviceError) as cm:
            await req
        self.assertEqual(cm.exception.code, "disconnected")
        self.assertNotIn("disconnected", seen)               # a deliberate close isn't a lost connection
        self.assertTrue(dev._reconnect_task is None or dev._reconnect_task.done())
        with self.assertRaises(DeviceError) as cm:
            await dev.request("echo")                        # closed: no silent reconnect
        self.assertEqual((cm.exception.code, cm.exception.message), ("disconnected", "The client was closed."))
        self.assertEqual(self.daemon.hellos, 1)

    async def test_connect_after_close_reopens(self):
        dev = self.client()
        await dev.connect()
        await dev.close()
        await dev.connect()
        self.assertTrue(dev.connected)
        self.assertEqual(await dev.request("echo", {"again": True}), {"again": True})


# ---------------------------------------------------------------- reconnect

class Reconnect(ClientCase):
    async def test_backoff_doubles_to_ten_seconds_then_reconnects(self):
        events: list = []

        async def on_sleep(rec: SleepRecorder):
            if len(rec.delays) == 8:                       # the daemon is back after 8 tries
                await self.daemon.start()

        rec = SleepRecorder(on_sleep)
        dev = self.client()
        await dev.connect()
        dev.on_event(lambda n, d: events.append((n, d)), ["disconnected", "reconnected"])
        with mock.patch.object(C, "asyncio", rec):
            await self.daemon.stop()                        # the daemon goes away for a while
            await self.until(lambda: dev.connected and len(events) == 2)
        self.assertEqual(rec.delays, [0.5, 1.0, 2.0, 4.0, 8.0, 10.0, 10.0, 10.0])
        self.assertEqual([n for n, _ in events], ["disconnected", "reconnected"])
        self.assertEqual(events[1][1], {"hello": dev.hello})
        self.assertEqual(self.daemon.hellos, 2)           # hello re-sent to the new daemon
        self.assertEqual(self.daemon.requests[-1]["args"]["name"], "client")
        self.assertEqual(await dev.request("echo", {"back": 1}), {"back": 1})
        self.assertTrue(dev._reconnect_task.done())

    async def test_close_stops_a_running_reconnect_loop(self):
        rec = SleepRecorder()
        dev = self.client()
        await dev.connect()
        with mock.patch.object(C, "asyncio", rec):
            await self.daemon.stop()
            await self.until(lambda: len(rec.delays) >= 3)
            await dev.close()
            task = dev._reconnect_task
            await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(task.done())
        self.assertFalse(dev.connected)

    async def test_requests_during_an_outage_fail_fast(self):
        dev = self.client()
        await dev.connect()
        with mock.patch.object(C, "asyncio", SleepRecorder()):
            await self.daemon.stop()
            await self.until(lambda: not dev.connected)
            with self.assertRaises(DeviceError) as cm:
                await dev.request("echo")
        self.assertEqual(cm.exception.code, "daemon-unavailable")


# ---------------------------------------------------------------- events

class Events(ClientCase):
    async def test_iterator_filters_by_name(self):
        dev = self.client()
        await dev.connect()
        async with dev.events(["syslog"]) as stream:
            self.daemon.push("devices", [])
            self.daemon.push("syslog", [{"message": "a"}])
            self.daemon.push("syslog", [{"message": "b"}])
            got = [await asyncio.wait_for(stream.__anext__(), WAIT) for _ in range(2)]
        self.assertEqual(got, [("syslog", [{"message": "a"}]), ("syslog", [{"message": "b"}])])
        self.assertEqual(dev._subs, [])                     # the context manager removed it
        with self.assertRaises(StopAsyncIteration):
            await stream.__anext__()

    async def test_each_iterator_has_its_own_queue(self):
        dev = self.client()
        await dev.connect()
        a, b = dev.events(), dev.events(["x"])
        self.daemon.push("x", 1)
        self.assertEqual(await asyncio.wait_for(a.__anext__(), WAIT), ("x", 1))
        self.assertEqual(await asyncio.wait_for(b.__anext__(), WAIT), ("x", 1))
        a.close()
        b.close()

    async def test_slow_consumer_keeps_only_the_newest(self):
        dev = self.client()
        await dev.connect()
        stream = dev.events(["n"], maxsize=3)
        for i in range(10):
            self.daemon.push("n", i)
        self.assertEqual(await dev.request("echo"), {})      # a round trip: all 10 events were read
        self.assertEqual(stream._sub.q.qsize(), 3)
        self.assertEqual([(await stream.__anext__())[1] for _ in range(3)], [7, 8, 9])
        stream.close()

    async def test_callbacks_sync_async_failing_and_off(self):
        dev = self.client()
        await dev.connect()
        sync_got, async_got = [], []

        async def acb(n, d):
            async_got.append(d)

        def bad(n, d):
            raise RuntimeError("callback bug")
        off = dev.on_event(lambda n, d: sync_got.append(d), ["e"])
        dev.on_event(acb, ["e"])
        dev.on_event(bad)
        self.daemon.push("e", 1)
        self.daemon.push("other", 0)
        await self.until(lambda: len(async_got) == 1)
        off()
        self.daemon.push("e", 2)
        await self.until(lambda: len(async_got) == 2)
        self.assertEqual(sync_got, [1])
        self.assertEqual(async_got, [1, 2])
        self.assertTrue(dev.connected)                      # the failing callback didn't kill the reader

    # An iterator blocked in `async for` ends when its stream (or the client) is closed.
    async def test_closing_a_stream_ends_a_waiting_iterator(self):
        dev = self.client()
        await dev.connect()
        stream = dev.events(["never"])

        async def consume():
            return [item async for item in stream]
        task = asyncio.create_task(consume())
        await asyncio.sleep(0)
        stream.close()
        try:
            self.assertEqual(await asyncio.wait_for(task, 0.2), [])
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


    async def test_closing_the_client_ends_waiting_iterators(self):
        dev = self.client()
        await dev.connect()
        streams = [dev.events(["never"]), dev.events(["nor-this"])]

        async def consume(stream):
            return [item async for item in stream]
        tasks = [asyncio.create_task(consume(s)) for s in streams]
        await asyncio.sleep(0)
        await dev.close()
        try:
            self.assertEqual(await asyncio.wait_for(asyncio.gather(*tasks), 0.5), [[], []])
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

# ---------------------------------------------------------------- starting the daemon

class Spawn(ClientCase):
    def launcher(self, body: str) -> Path:
        p = self.dir / "fake-deviced"
        p.write_text("#!/bin/sh\n" + body + "\n")
        p.chmod(0o755)
        return p

    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.daemon.stop()                            # nothing listening: the client must start one

    async def test_launcher_ready_error_is_reported(self):
        sh = self.launcher("""printf '{"event":"ready","data":{"error":{"code":"needs-setup","message":"Install it (%s)."}}}\\n' "$IOSSTUDIO_DEMO"
exit 3""")
        dev = self.client(spawn=True, deviced=sh, demo="fresh", reconnect=False)
        with self.assertRaises(DeviceError) as cm:
            await dev.connect()
        self.assertEqual(cm.exception.code, "needs-setup")
        self.assertEqual(cm.exception.message, "Install it (fresh).")   # demo scenario passed in the env

    async def test_launcher_exit_code_without_output(self):
        dev = self.client(spawn=True, deviced=self.launcher("exit 4"), reconnect=False)
        with self.assertRaises(DeviceError) as cm:
            await dev.connect()
        self.assertEqual((cm.exception.code, cm.exception.detail), ("daemon-unavailable", "exit code 4"))

    async def test_missing_launcher(self):
        dev = self.client(spawn=True, deviced=self.dir / "nope", reconnect=False)
        with self.assertRaises(DeviceError) as cm:
            await dev.connect()
        self.assertEqual(cm.exception.code, "daemon-unavailable")
        self.assertIn("FileNotFoundError", cm.exception.detail)

    async def test_launcher_that_starts_the_daemon(self):
        # The launcher's exit 0 means "another daemon is (or will be) serving": keep waiting for the socket.
        sh = self.launcher(f"touch {self.dir}/spawned; exit 0")
        dev = self.client(spawn=True, deviced=sh, demo=False, reconnect=False, connect_timeout=WAIT)

        async def bring_up():
            await self.until(lambda: (self.dir / "spawned").exists())
            await self.daemon.start()
        up = asyncio.create_task(bring_up())
        await dev.connect()
        await up
        self.assertTrue(dev.connected)
        self.assertEqual(self.daemon.hellos, 1)

    async def test_gives_up_after_connect_timeout(self):
        sh = self.launcher("exec sleep 5")
        dev = self.client(spawn=True, deviced=sh, reconnect=False, connect_timeout=0.1)
        with self.assertRaises(DeviceError) as cm:
            await dev.connect()
        self.assertEqual(cm.exception.code, "daemon-unavailable")
        for p in dev._spawned:
            p.kill()
            p.wait(2)


if __name__ == "__main__":
    unittest.main()
