"""The device daemon's wire format and backend contract: helper/iosstudio/device/protocol.py, backend.py, and how
deviced.py (real), deviced_demo.py (demo), device_broker.py and device_client.py use them.

    python -m pytest tests/helper/test_device_protocol.py

The real daemon runs in-process against a fake pymobiledevice3 (service objects at the boundary), and as a
subprocess (`--serve` / `--once`) under ~/pymobile3-venv's python when it exists, with only load_pmd3 faked: no
phone, no tunnel, and its own runtime dir (never the user's daemon socket).
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from iosstudio.core.output import HelperError
from iosstudio.device import backend, protocol
from iosstudio.device.device_client import DeviceClient, DeviceError
from support import ROOT
from support.pmd3_stub import stub_pmd3

HELPER = ROOT / "helper"
DEVICED = ROOT / "bin" / "iosstudio-deviced"
PMD3_PY = Path.home() / "pymobile3-venv" / "bin" / "python"
DAEMON_PY = str(PMD3_PY) if PMD3_PY.exists() else sys.executable
UDID = "00008150-PROTOCOL"


# ====================================================================== protocol.py

class Framing(unittest.IsolatedAsyncioTestCase):
    def test_round_trip_and_bad_lines(self):
        line = protocol.encode(protocol.request(3, "apps.list", {"icons": False}))
        self.assertTrue(line.endswith(b"\n"))
        self.assertEqual(line.count(b"\n"), 1)
        self.assertEqual(protocol.decode(line), {"id": 3, "cmd": "apps.list", "args": {"icons": False}})
        for bad in (b"", b"\n", b"not json\n", b"[1,2]\n", b'"x"\n'):
            self.assertIsNone(protocol.decode(bad))
        bad: list[int] = []
        msgs = list(protocol.read_lines([b'{"event":"a","data":1}\n', b"junk\n", b"\n", b'{"id":1,"ok":true}\n'],
                                        lambda: bad.append(1)))
        self.assertEqual(msgs, [{"event": "a", "data": 1}, {"id": 1, "ok": True}])
        self.assertEqual(bad, [1])   # blank lines aren't malformed

    def test_messages(self):
        self.assertEqual(protocol.reply_ok(1, None), {"id": 1, "ok": True, "data": {}})
        self.assertEqual(protocol.event("devices", []), {"event": "devices", "data": []})
        r = protocol.reply_error(2, DeviceError("locked", "Unlock it."))
        self.assertEqual(r, {"id": 2, "ok": False, "error": {"code": "locked", "message": "Unlock it."}})
        self.assertTrue(protocol.is_event({"event": "x"}))

    def test_errors_are_one_type(self):
        e = DeviceError("no-device", "No iPhone.", {"label": "Pair", "page": "device"}, "x" * 5000)
        self.assertIsInstance(e, HelperError)          # inside the package, `except HelperError` catches it
        j = e.to_json()
        self.assertEqual(len(j["detail"]), protocol.DETAIL_MAX)
        self.assertEqual(DeviceError.from_json(j).to_json(), j)
        self.assertEqual(protocol.error_json(HelperError("x", "Y")), {"code": "x", "message": "Y"})
        j = protocol.error_json(ValueError("boom"), "Demo mode can't do that.")
        self.assertEqual((j["code"], j["message"]), ("failed", "Demo mode can't do that."))
        self.assertIn("ValueError: boom", j["detail"])
        self.assertEqual(protocol.error_json(asyncio.CancelledError()), protocol.CANCELLED)

    def test_parse_args(self):
        self.assertEqual(protocol.parse_args(""), {})
        self.assertEqual(protocol.parse_args('{"udid":"x"}'), {"udid": "x"})
        for bad in ("{", "[1]", "3"):
            with self.assertRaises(DeviceError) as cm:
                protocol.parse_args(bad)
            self.assertEqual(cm.exception.code, "bad-args")

    async def test_async_reader(self):
        r = asyncio.StreamReader(limit=64)
        r.feed_data(b'{"a":1}\nnope\n{"b":2}\n')
        r.feed_eof()
        bad: list[int] = []
        self.assertEqual([m async for m in protocol.read_lines_async(r, lambda: bad.append(1))], [{"a": 1}, {"b": 2}])
        self.assertEqual(bad, [1])
        r = asyncio.StreamReader(limit=64)
        r.feed_data(b'{"a":"' + b"x" * 200 + b'"}\n')
        r.feed_eof()
        with self.assertRaises(protocol.LineTooLong):
            [m async for m in protocol.read_lines_async(r)]


class StandaloneClient(unittest.TestCase):
    def test_device_client_loads_by_path(self):
        """As the MCP server loads it: by file path, outside the package (protocol.py comes along by path)."""
        code = (
            "import importlib.util, sys\n"
            f"spec = importlib.util.spec_from_file_location('iosstudio_device_client', {str(HELPER / 'iosstudio/device/device_client.py')!r})\n"
            "m = importlib.util.module_from_spec(spec); sys.modules[spec.name] = m; spec.loader.exec_module(m)\n"
            "e = m.DeviceError('timeout', 'Slow.', None, 'd')\n"
            "assert 'iosstudio' not in sys.modules, sorted(k for k in sys.modules if 'iosstudio' in k)\n"
            "print(type(e).__module__, e.to_json(), m.socket_path(False).name)\n")
        r = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("iosstudio_device_protocol {'code': 'timeout', 'message': 'Slow.', 'detail': 'd'} deviced.sock",
                      r.stdout)


# ====================================================================== backend.py

class Dispatch(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        class B(backend.Backend):
            def register_core(self, reg):
                reg.command("sync.cmd", lambda ctx, a: {"thread": __import__("threading").current_thread().name,
                                                        "args": a})

                @reg.command("async.cmd")
                async def a_cmd(ctx, a):
                    return None

                @reg.command("fail.cmd")
                async def fail(ctx, a):
                    raise DeviceError("locked", "Unlock it.")

                @reg.command("crash.cmd")
                def crash(ctx, a):
                    raise RuntimeError("bug")
        self.b = B(log=lambda *_: None)

    async def test_sync_handlers_run_off_the_loop(self):
        out = await self.b.dispatch("sync.cmd", None)
        self.assertNotEqual(out["thread"], "MainThread")
        self.assertEqual(out["args"], {})

    async def test_handle_replies(self):
        self.assertEqual(await self.b.handle({"id": 1, "cmd": "async.cmd"}), {"id": 1, "ok": True, "data": {}})
        self.assertEqual((await self.b.handle({"id": 2, "cmd": "fail.cmd"}))["error"]["code"], "locked")
        self.assertEqual((await self.b.handle({"id": 3, "cmd": "crash.cmd"}))["error"]["code"], "failed")
        self.assertEqual((await self.b.handle({"id": 4, "cmd": "nope"}))["error"],
                         {"code": "bad-args", "message": "Unknown command 'nope'."})

    async def test_shutdown_cancels_background_tasks(self):
        t = self.b.bg.spawn(asyncio.sleep(60))
        with mock.patch.object(backend.TaskSet, "close", backend.TaskSet.close):
            await self.b.shutdown()
        self.assertTrue(t.cancelled())

    async def test_syslog_missing_is_an_error(self):
        with self.assertRaises(DeviceError):
            self.b._syslog()
        await self.b.syslog_close()   # nothing provided: a no-op


# ====================================================================== the real daemon, in-process

class Svc:
    """A pymobiledevice3 service object: an async context manager made from the RSD."""
    opened: list = []

    def __init__(self, rsd, *a, **k):
        self.rsd = rsd
        Svc.opened.append(type(self).__name__)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def fake_pmd3(tmp: Path):
    class MobileImageMounterService(Svc):
        async def is_image_mounted(self, kind):
            return kind == "Developer"

    class MisagentService(Svc):
        async def copy_all(self):
            return []

    class CrashReportsManager(Svc):
        async def flush(self):
            pass

        async def ls(self, path, depth=1):
            return ["/Retired", "/Habits-2026-11-01-101010.ips"]

    class Usbmux:
        @staticmethod
        async def list_devices():
            return []
    return types.SimpleNamespace(
        version="fake", usbmux=Usbmux, remote_dir=str(tmp),
        exceptions=types.SimpleNamespace(ConnectionFailedToUsbmuxdError=OSError),
        iter_remote_pair_records_by_identifier=lambda: iter(()),
        image_type_for_device=lambda rsd: "Developer",
        MobileImageMounterService=MobileImageMounterService, MisagentService=MisagentService,
        CrashReportsManager=CrashReportsManager)


class RealDaemon(unittest.IsolatedAsyncioTestCase):
    """deviced.Daemon's dispatch with services faked at the pymobiledevice3 boundary."""

    async def asyncSetUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        stub = stub_pmd3()
        stub.__enter__()
        self.addCleanup(stub.__exit__, None, None, None)
        from iosstudio.device import deviced
        self.deviced = deviced
        self.d = deviced.Daemon(fake_pmd3(self.tmp))
        self.addAsyncCleanup(self.d.shutdown)

    def connect(self):
        s = self.deviced.Session(self.d, UDID)
        s.rsd = types.SimpleNamespace(udid=UDID, all_values={})
        s.connected.set()
        self.d.session = s
        return s

    async def test_no_device(self):
        reply = await self.d.handle({"id": 1, "cmd": "device.info", "args": {}})
        self.assertEqual(reply["error"]["code"], "no-device")
        self.assertEqual((await self.d.handle({"id": 2, "cmd": "nope"}))["error"]["code"], "bad-args")
        self.assertTrue((await self.d.handle({"id": 3, "cmd": "daemon.ping"}))["ok"])

    async def test_feature_commands_through_open_service(self):
        self.connect()
        Svc.opened = []
        self.assertEqual(await self.d.dispatch("ddi.status", {}), {"mounted": True})
        self.assertEqual(await self.d.dispatch("profiles.list", {}), [])
        crashes = await self.d.dispatch("crash.list", {"flush": True})
        self.assertEqual([c["process"] for c in crashes], ["Habits"])
        self.assertEqual(Svc.opened, ["MobileImageMounterService", "MisagentService", "CrashReportsManager"])

    async def test_open_service_timeout_drops_the_tunnel(self):
        s = self.connect()

        class Hangs(Svc):
            async def __aenter__(self):
                await asyncio.sleep(60)
        with self.assertRaises(asyncio.TimeoutError) as cm:
            async with s.open_service(Hangs, open_timeout=0.05):
                pass
        self.assertEqual(self.d.error_json(cm.exception, "x")["code"], "unreachable")
        self.assertTrue(s.dropped.is_set())   # dispatch's error path dropped the dead tunnel

    async def test_open_service_fail_and_bounded_close(self):
        s = self.connect()
        fail = self.deviced.err("ax-unavailable", "Couldn't reach the accessibility service.")

        class Weird(Svc):
            async def __aenter__(self):
                raise RuntimeError("odd reply")

        class Dropped(Svc):
            async def __aenter__(self):
                raise ConnectionResetError("gone")

        class SlowClose(Svc):
            async def __aexit__(self, *exc):
                await asyncio.sleep(60)
        with self.assertRaises(DeviceError) as cm:
            async with s.open_service(Weird, fail=fail):
                pass
        self.assertEqual(cm.exception.code, "ax-unavailable")
        self.assertIn("odd reply", cm.exception.detail)
        with self.assertRaises(ConnectionResetError):   # connection problems keep their own meaning
            async with s.open_service(Dropped, fail=fail):
                pass
        with mock.patch.object(self.deviced, "SERVICE_CLOSE_S", 0.05):
            t0 = time.monotonic()
            with self.assertRaises(KeyError):           # the body's error wins; the close is bounded
                async with s.open_service(SlowClose):
                    raise KeyError("body")
            self.assertLess(time.monotonic() - t0, 2)

    async def test_broken_backend(self):
        from iosstudio.device import device_link as link
        b = self.deviced.BrokenBackend(link.ApiError("pymobiledevice3 isn't installed in this Python"))
        ready = dict(await b.welcome())["ready"]
        self.assertEqual(ready["error"]["code"], "pmd3-api")
        self.assertTrue((await b.handle({"id": 1, "cmd": "daemon.ping"}))["ok"])
        self.assertEqual((await b.handle({"id": 2, "cmd": "apps.list"}))["error"]["code"], "pmd3-api")
        with self.assertRaises(HelperError):
            b.syslog_target({})


class Parity(unittest.TestCase):
    def test_real_and_demo_register_the_same_commands(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        with stub_pmd3():
            from iosstudio.device import deviced
            real = deviced.Daemon(fake_pmd3(tmp)).registry
        from iosstudio.device.deviced_demo import DemoBackend
        demo = DemoBackend(None).registry
        self.assertEqual(real.failed, {})
        self.assertEqual(demo.failed, {})
        names = lambda r: {n for n, *_ in r.commands.values()}  # noqa: E731
        self.assertEqual(names(real), names(demo) - {"demo.transport"})
        self.assertGreater(len(names(real)), 60)


# ====================================================================== daemons as processes

SERVE = """
import sys, types
sys.path.insert(0, {helper!r})
from iosstudio.device import device_link as link, deviced

class Usbmux:
    @staticmethod
    async def list_devices():
        return []

def fake():
    return types.SimpleNamespace(version="fake", usbmux=Usbmux, remote_dir={tmp!r},
                                 exceptions=types.SimpleNamespace(ConnectionFailedToUsbmuxdError=OSError),
                                 iter_remote_pair_records_by_identifier=lambda: iter(()))

def broken():
    raise link.ApiError("pymobiledevice3 isn't installed in this Python")

link.load_pmd3 = fake if sys.argv[1] == "fake" else broken
sys.argv = ["deviced", *sys.argv[2:]]
deviced.main()
"""


class Processes(unittest.IsolatedAsyncioTestCase):
    """Real framing end to end: device_client <-> broker <-> backend, each in its own process."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dp-"))   # short: the socket path must fit AF_UNIX
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.script = self.tmp / "serve.py"
        self.script.write_text(SERVE.format(helper=str(HELPER), tmp=str(self.tmp)))
        self.env = {**os.environ, "IOSSTUDIO_RUNTIME_DIR": str(self.tmp / "rt"), "XDG_RUNTIME_DIR": str(self.tmp),
                    "IOSSTUDIO_DEVICED_IDLE": "30"}
        self.env.pop("IOSSTUDIO_DEMO", None)
        self.sock = self.tmp / "rt" / "ios-studio" / "deviced.sock"

    def run_daemon(self, mode: str, *argv: str, **kw) -> subprocess.Popen | subprocess.CompletedProcess:
        cmd = [DAEMON_PY, "-I", str(self.script), mode, *argv]
        if kw.pop("once", False):
            return subprocess.run(cmd, capture_output=True, text=True, env=self.env, timeout=60)
        p = subprocess.Popen(cmd, env=self.env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        self.addCleanup(self._stop, p)
        return p

    @staticmethod
    def _stop(p: subprocess.Popen):
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(5)

    async def client(self) -> DeviceClient:
        deadline = time.monotonic() + 60
        while not self.sock.exists():
            self.assertLess(time.monotonic(), deadline, "the daemon didn't come up")
            await asyncio.sleep(0.1)
        dev = DeviceClient(name="test", demo=False, spawn=False, reconnect=False, socket=self.sock)
        await dev.connect()
        self.addAsyncCleanup(dev.close)
        return dev

    async def test_real_daemon_serves_device_client(self):
        self.run_daemon("fake", "--serve")
        dev = await self.client()
        self.assertEqual(dev.hello["socket"], str(self.sock))
        self.assertEqual(dev.hello["log"], str(self.sock.with_name("deviced.log")))
        self.assertEqual(dev.ready["log"], dev.hello["log"])        # the GUI reads the log path from `ready`
        self.assertEqual(dev.ready["pymobiledevice3"], "fake")
        self.assertTrue((await dev.request("daemon.ping"))["pong"])
        self.assertIsInstance(await dev.request("devices.list"), list)
        feats = await dev.request("daemon.features")
        if PMD3_PY.exists():
            self.assertEqual(feats["failed"], {})
        self.assertIn("apps", feats["loaded"])
        for cmd, code in (("device.info", "no-device"), ("nope.nope", "bad-args"), ("syslog.start", "no-device")):
            with self.assertRaises(DeviceError) as cm:
                await dev.request(cmd, {}, timeout=20)
            self.assertEqual(cm.exception.code, code, cmd)
        await asyncio.to_thread(self.raw_framing)

    def raw_framing(self):
        """A malformed line gets a warning event; the next request is still answered."""
        with socket.socket(socket.AF_UNIX) as s:
            s.settimeout(15)
            s.connect(str(self.sock))
            f = s.makefile("rwb")
            f.write(b"not json\n" + protocol.encode(protocol.request("r1", "daemon.ping")))
            f.flush()
            warned = False
            for m in protocol.read_lines(f):
                if m.get("event") == "log":
                    warned = True
                if m.get("id") == "r1":
                    self.assertTrue(m["ok"])
                    break
            self.assertTrue(warned)

    async def test_broken_daemon_answers_with_its_error(self):
        self.run_daemon("broken", "--serve")
        dev = await self.client()
        self.assertEqual(dev.ready["error"]["code"], "pmd3-api")
        self.assertTrue((await dev.request("daemon.ping"))["pong"])
        with self.assertRaises(DeviceError) as cm:
            await dev.request("apps.list")
        self.assertEqual(cm.exception.code, "pmd3-api")

    def test_real_once(self):
        """Standalone --once: one reply line, no id (with no iPhone around every command is no-device)."""
        for args, code in ((["device.info", "{}"], "no-device"), (["daemon.ping", "{}"], "no-device"), (["device.info", "{"], "bad-args"),
                           (["nope", "{}"], "bad-args")):
            r = self.run_daemon("fake", "--once", *args, once=True)
            self.assertEqual(r.returncode, 1)
            self.assertEqual(len(r.stdout.splitlines()), 1, r.stdout)
            out = json.loads(r.stdout)
            self.assertEqual(out["error"]["code"], code, args)
            self.assertNotIn("id", out)
        r = self.run_daemon("broken", "--once", "daemon.ping", "{}", once=True)
        self.assertEqual(json.loads(r.stdout)["error"]["code"], "pmd3-api")

    async def test_needs_setup_without_the_iphone_tools(self):
        """bin/iosstudio-deviced with no pymobiledevice3 venv: a `ready` error the client reports."""
        env = {**self.env, "HOME": str(self.tmp), "XDG_DATA_HOME": str(self.tmp / "data")}
        r = subprocess.run([str(DEVICED)], capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(r.returncode, 3)
        self.assertEqual(json.loads(r.stdout)["data"]["error"]["code"], "needs-setup")
        with mock.patch.dict(os.environ, env):
            dev = DeviceClient(name="t", demo=False, reconnect=False, deviced=DEVICED, socket=self.sock,
                               connect_timeout=20)
            with self.assertRaises(DeviceError) as cm:
                await dev.connect()
            await dev.close()
        self.assertEqual(cm.exception.code, "needs-setup")

    async def test_demo_daemon_via_bin(self):
        """The demo daemon through bin/iosstudio-deviced: standalone --once, then --serve + --once over the socket."""
        env = {**self.env, "IOSSTUDIO_DEMO": "ready"}
        demo_sock = self.tmp / "rt" / "ios-studio-demo" / "deviced.sock"

        def once(*args):
            r = subprocess.run([str(DEVICED), "--once", *args], capture_output=True, text=True, env=env, timeout=60)
            return json.loads(r.stdout.strip().splitlines()[-1])
        self.assertTrue(once("apps.list", "{}")["ok"])                      # no daemon: standalone
        self.assertEqual(once("nope", "{}")["error"]["code"], "bad-args")
        with mock.patch.dict(os.environ, env):
            dev = DeviceClient(name="t", demo="ready", reconnect=False, deviced=DEVICED, socket=demo_sock)
            await dev.connect()
            try:
                self.assertEqual(dev.ready["log"], str(demo_sock.with_name("deviced.log")))
                info = await dev.request("device.info")
                self.assertTrue(info["udid"])
                async with dev.events(["syslog"]) as stream:
                    await dev.request("syslog.start", {})
                    name, data = await asyncio.wait_for(stream.__anext__(), 10)
                    self.assertEqual(name, "syslog")
                    self.assertIsInstance(data, list)
                self.assertEqual(await dev.request("syslog.stop"), {"running": False})
                st = await dev.request("mirror.start", {})
                self.assertIsInstance(st, dict)
                await dev.request("mirror.stop", {})
                r = await asyncio.to_thread(once, "device.info", "{}")      # through the live daemon's socket
                self.assertEqual(r["data"]["udid"], info["udid"])
                await dev.request("daemon.shutdown", {"all": True})
            finally:
                await dev.close()


if __name__ == "__main__":
    unittest.main()
