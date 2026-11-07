"""A request sent while the device daemon shuts down: the broker closes its listener and removes the socket
before anything else and answers `daemon-stopping` while it drains; DeviceClient reconnects (starting a fresh
daemon) and sends the request once more when that is safe, never resending a command that may have run.

    python -m pytest tests/helper/test_daemon_shutdown_retry.py
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import devtest

devtest.isolate()

from iosstudio.device import broker as B  # noqa: E402
from iosstudio.device import protocol  # noqa: E402
from iosstudio.device.client import STOPPING, DeviceClient, DeviceError  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
DEVICED = ROOT / "bin" / "iosstudio-deviced"
WAIT = 5.0   # upper bound for anything awaited; tests finish far sooner


async def until(cond, what: str):
    async with asyncio.timeout(WAIT):
        while not cond():
            await asyncio.sleep(0.001)


def private_dir(prefix: str) -> Path:
    base = Path(os.environ["IOSSTUDIO_RUNTIME_DIR"])
    base.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(dir=base, prefix=prefix))


# ---------------------------------------------------------------- broker side

class EchoBackend:
    async def welcome(self):
        return [("ready", {"version": "1"})]

    async def dispatch(self, cmd, args):
        return {"cmd": cmd}

    def error_json(self, exc, cmd):
        return protocol.error_json(exc)

    async def client_gone(self, client):
        pass

    async def shutdown(self):
        pass


class BrokerDraining(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.base = private_dir("b")
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        for p in (mock.patch.dict(os.environ, {"IOSSTUDIO_RUNTIME_DIR": str(self.base)}),
                  mock.patch.object(B, "log", lambda text: None)):
            p.start()
            self.addCleanup(p.stop)
        self.b = B.Broker(demo=None, idle=60)
        self.sock = self.b.p["sock"]
        B._prepare_dir(self.b.p["dir"])

        async def make(_b):
            return EchoBackend()
        self.task = asyncio.create_task(self.b.run(make))
        await until(lambda: self.b._ready.is_set(), "the broker")

    async def asyncTearDown(self):
        self.b.stop()
        await asyncio.wait_for(self.task, WAIT)

    async def call(self, r, w, rid, cmd, args=None) -> dict:
        await protocol.write_line_async(w, protocol.request(rid, cmd, args))
        async for m in protocol.read_lines_async(r):
            if m.get("id") == rid:
                return m
        raise AssertionError(f"no reply to {cmd}")

    async def test_listener_and_socket_go_first_then_requests_answer_stopping(self):
        r, w = await asyncio.open_unix_connection(str(self.sock))
        self.addCleanup(w.close)
        self.assertTrue((await self.call(r, w, 1, "echo"))["ok"])
        self.b.begin_shutdown()
        self.assertFalse(self.sock.exists())
        with self.assertRaises(OSError):
            await asyncio.open_unix_connection(str(self.sock))
        reply = await self.call(r, w, 2, "echo")
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"]["code"], STOPPING)

    async def test_shutdown_all_removes_the_socket_before_it_replies(self):
        r, w = await asyncio.open_unix_connection(str(self.sock))
        self.addCleanup(w.close)
        reply = await self.call(r, w, 1, "daemon.shutdown", {"all": True})
        self.assertTrue(reply["ok"])
        self.assertTrue(self.b.draining)
        self.assertFalse(self.sock.exists())   # by the time the caller hears back, a new client spawns a new daemon
        await asyncio.wait_for(self.task, WAIT)

    async def test_a_successors_socket_is_left_alone(self):
        self.sock.unlink()   # as if a newer daemon had replaced it
        other = socket.socket(socket.AF_UNIX)
        self.addCleanup(other.close)
        other.bind(str(self.sock))
        self.b.begin_shutdown()
        self.assertTrue(self.sock.exists())


# ---------------------------------------------------------------- client side

class ScriptedDaemon:
    """Answers hello and everything else, except that the first time it sees a command listed in `script`
    it either drops the connection without replying ("drop") or answers STOPPING and closes ("stopping")."""

    def __init__(self, path: Path, script: dict[str, str]):
        self.path, self.script = path, dict(script)
        self.seen: dict[str, int] = {}
        self.server: asyncio.AbstractServer | None = None

    async def start(self):
        self.server = await asyncio.start_unix_server(self._serve, path=str(self.path))

    async def stop(self):
        self.server.close()
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()

    async def _serve(self, r: asyncio.StreamReader, w: asyncio.StreamWriter):
        try:
            async for m in protocol.read_lines_async(r):
                cmd = m["cmd"]
                self.seen[cmd] = self.seen.get(cmd, 0) + 1
                how = self.script.pop(cmd, None)
                if how == "drop":
                    break
                if how == "stopping":
                    w.write(protocol.encode(protocol.reply_error(m["id"], B.STOPPING_ERROR)))
                    await w.drain()
                    break
                w.write(protocol.encode(protocol.reply_ok(m["id"], {"cmd": cmd, "n": self.seen[cmd]})))
        except (ConnectionError, OSError):
            pass
        finally:
            w.close()


class ClientRetry(unittest.IsolatedAsyncioTestCase):
    async def daemon(self, **script) -> tuple[ScriptedDaemon, DeviceClient]:
        d = private_dir("c")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        daemon = ScriptedDaemon(d / "deviced.sock", {k.replace("_", "."): v for k, v in script.items()})
        await daemon.start()
        self.addAsyncCleanup(daemon.stop)
        dev = DeviceClient(name="t", socket=daemon.path, spawn=False, reconnect=False)
        self.addAsyncCleanup(dev.close)
        await dev.connect()
        return daemon, dev

    async def test_a_read_is_sent_again_after_the_connection_drops(self):
        daemon, dev = await self.daemon(device_info="drop")
        self.assertEqual(await dev.request("device.info"), {"cmd": "device.info", "n": 2})
        self.assertEqual(daemon.seen["device.info"], 2)

    async def test_an_install_is_not_sent_again_once_it_may_have_arrived(self):
        daemon, dev = await self.daemon(app_install="drop")
        with self.assertRaises(DeviceError) as cm:
            await dev.request("app.install", {"path": "/x.ipa"})
        self.assertEqual(cm.exception.code, "disconnected")
        self.assertEqual(daemon.seen["app.install"], 1)
        for cmd in ("app.uninstall", "app.launch", "app.kill", "container.delete", "appdata.reset", "location.set"):
            self.assertFalse(dev._may_resend(cmd, cm.exception, True), cmd)
            self.assertTrue(dev._may_resend(cmd, cm.exception, False), cmd)   # never written: safe

    async def test_stopping_is_always_sent_again_because_nothing_ran(self):
        daemon, dev = await self.daemon(app_install="stopping")
        self.assertEqual(await dev.request("app.install", {"path": "/x.ipa"}), {"cmd": "app.install", "n": 2})
        self.assertEqual(daemon.seen["app.install"], 2)

    async def test_a_hello_refused_by_a_stopping_daemon_reconnects(self):
        daemon, dev = await self.daemon()
        await dev.close()
        daemon.script["hello"] = "stopping"
        await dev.connect()
        self.assertEqual(daemon.seen["hello"], 3)
        self.assertTrue(dev.connected)

    async def test_only_one_retry(self):
        daemon, dev = await self.daemon(device_info="drop")
        orig = daemon._serve

        async def drop_again(r, w):
            daemon.script.setdefault("device.info", "drop")
            await orig(r, w)
        daemon.server.close()
        with contextlib.suppress(FileNotFoundError):   # Python 3.13+ removes it on close()
            daemon.path.unlink()
        daemon.server = await asyncio.start_unix_server(drop_again, path=str(daemon.path))
        with self.assertRaises(DeviceError) as cm:
            await dev.request("device.info")
        self.assertEqual(cm.exception.code, "disconnected")
        self.assertEqual(daemon.seen["device.info"], 2)


# ---------------------------------------------------------------- end to end: bin/iosstudio-deviced, demo daemon

class ShutdownThenRequest(unittest.IsolatedAsyncioTestCase):
    """daemon.shutdown {all} and at once a request, from the same client and from a new one: both succeed every
    time, against a fresh daemon. No sleeps: each round waits only for the replies."""

    ROUNDS = 20

    async def asyncSetUp(self):
        d = private_dir("e")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        p = mock.patch.dict(os.environ, {"IOSSTUDIO_RUNTIME_DIR": str(d), "IOSSTUDIO_DEMO": "ready",
                                         "IOSSTUDIO_DEVICED_IDLE": "20"})
        p.start()
        self.addCleanup(p.stop)
        self.clients: list[DeviceClient] = []

    def client(self, name: str) -> DeviceClient:
        c = DeviceClient(name=name, demo="ready", deviced=DEVICED, reconnect=False, connect_timeout=30)
        self.clients.append(c)
        return c

    async def asyncTearDown(self):
        procs = [p for c in self.clients for p in c._spawned]
        last = DeviceClient(name="cleanup", demo="ready", spawn=False, reconnect=False)
        with contextlib.suppress(DeviceError):
            await last.request("daemon.shutdown", {"all": True})
        await last.close()
        for c in self.clients:
            await c.close()
        for p in procs:
            await asyncio.to_thread(p.wait, 30)

    async def test_requests_right_after_shutdown_succeed(self):
        dev = self.client("old")
        pids = set()
        ok = 0
        for _ in range(self.ROUNDS):
            info = await dev.request("device.info", timeout=30)
            self.assertTrue(info["udid"])
            pids.add(dev.hello["pid"])
            await dev.request("daemon.shutdown", {"all": True})
            fresh = self.client("new")
            same, new = await asyncio.gather(dev.request("apps.list", {"icons": False}, timeout=30),
                                             fresh.request("device.info", timeout=30))
            self.assertIn("apps", same)
            self.assertEqual(new["udid"], info["udid"])
            self.assertNotIn(fresh.hello["pid"], pids)   # a fresh daemon, never the dying one
            await fresh.close()
            ok += 1
        self.assertEqual(ok, self.ROUNDS)


if __name__ == "__main__":
    unittest.main()
