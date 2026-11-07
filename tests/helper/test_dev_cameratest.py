"""Camera test loop (helper/iosstudio/device/features/cameratest/{real,demo}.py): cameratest.run.

The real side runs in a real Daemon with a connected Session: the app's container is an in-memory AFC
(Session.house_arrest), installation_proxy is faked on the `pmd3` namespace, and the app.launch / app.kill /
crash.list commands it calls through the registry are replaced by fakes (the "app" writes done.json when launched).

    .venv/bin/pytest -q tests/helper/test_dev_cameratest.py
    cd tests && PYTHONPATH=../helper:. ~/pymobile3-venv/bin/python -m unittest helper.test_dev_cameratest
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import posixpath
import shutil
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from support import devtest
from support.pmd3_stub import stub_pmd3

devtest.isolate()

from iosstudio.core.output import HelperError  # noqa: E402
from iosstudio.device.kit import norm  # noqa: E402

UDID = "00008150-CAMERATEST"
BID = "com.example.habits"
DONE = {"results": [{"file": "a.jpg", "label": "cat", "confidence": 0.9}]}
_SLEEP = asyncio.sleep
DEMO_BID = "XTL-A1B2C3D4E5.com.example.weatherglance"   # demo/core.py's developer app
STEP_IDS = ["check", "clear", "push", "launch", "wait", "pull", "crashes"]


async def fast_sleep(delay, result=None):
    return await _SLEEP(0, result)


class MemAfc:
    """An app container over AFC, in memory."""

    def __init__(self, missing_exc):
        self.files: dict[str, bytearray] = {}
        self.dirs: set[str] = {"/", "/Documents"}
        self.handles: dict[int, tuple[str, int]] = {}
        self.missing = missing_exc
        self.exists_calls = 0
        self.on_exists = None

    def put(self, path: str, data: bytes) -> None:
        self.makedirs_sync(posixpath.dirname(path))
        self.files[path] = bytearray(data)

    def makedirs_sync(self, p: str) -> None:
        while p not in ("", "/"):
            self.dirs.add(p)
            p = posixpath.dirname(p)

    async def rm(self, p, force=False):
        if p not in self.dirs and p not in self.files:
            raise self.missing(f"no {p}", 8)
        for f in [f for f in self.files if f == p or f.startswith(p + "/")]:
            del self.files[f]
        self.dirs -= {d for d in self.dirs if d == p or d.startswith(p + "/")}

    async def makedirs(self, p):
        self.makedirs_sync(p)

    async def exists(self, p):
        self.exists_calls += 1
        if self.on_exists:
            self.on_exists()
        return p in self.files or p in self.dirs

    async def stat(self, p):
        if p in self.dirs:
            return {"st_ifmt": "S_IFDIR"}
        if p in self.files:
            return {"st_ifmt": "S_IFREG", "st_size": len(self.files[p])}
        raise self.missing(f"no {p}", 8)

    async def listdir(self, p):
        kids = {x for x in list(self.files) + list(self.dirs) if posixpath.dirname(x) == p and x != p}
        return sorted(posixpath.basename(k) for k in kids)

    async def fopen(self, path, mode):
        h = len(self.handles) + 1
        if mode == "w":
            self.files[path] = bytearray()
        elif path not in self.files:
            raise self.missing(f"no {path}", 8)
        self.handles[h] = (path, 0)
        return h

    async def fwrite(self, h, chunk):
        self.files[self.handles[h][0]] += chunk

    async def fread(self, h, n):
        path, pos = self.handles[h]
        self.handles[h] = (path, pos + n)
        return bytes(self.files[path][pos:pos + n])

    async def fclose(self, h):
        self.handles.pop(h, None)


class RealHarness:
    async def setup_real(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        stub = stub_pmd3()
        stub.__enter__()
        self.addCleanup(stub.__exit__, None, None, None)
        from iosstudio.device.real import core, daemon
        self.events: list = []
        p = mock.patch.object(core, "BROKER", types.SimpleNamespace(emit=lambda n, d: self.events.append((n, d)),
                                                                    syslog=None))
        p.start()
        self.addCleanup(p.stop)
        self.apps = {BID: {"CFBundleDisplayName": "Habits", "CFBundleExecutable": "HabitTracker",
                           "Entitlements": {"get-task-allow": True}},
                     "com.example.store": {"CFBundleName": "Store", "SignerIdentity": "Apple iPhone OS Application Signing"}}
        apps = self.apps

        class InstallationProxyService(devtest.Svc):
            async def get_apps(self, application_type=None, bundle_identifiers=()):
                return {b: apps[b] for b in bundle_identifiers if b in apps}

        p = mock.patch("asyncio.sleep", fast_sleep)
        p.start()
        self.addCleanup(p.stop)
        self.d = daemon.Daemon(types.SimpleNamespace(version="fake", iter_remote_pair_records_by_identifier=lambda: iter(()),
                                                     InstallationProxyService=InstallationProxyService))
        self.addAsyncCleanup(self.d.shutdown)
        self.g = self.d.registry.lookup("cameratest.run").__globals__
        self.afc = MemAfc(self.g["AfcException"])
        self.calls: list = []
        self.crashes_after: list = []
        self.write_done = True
        self.pids = [812]

        async def launch(d, a):
            self.calls.append(("app.launch", a))
            if self.write_done:
                self.afc.put("/Documents/CameraTestOutput/done.json", json.dumps(DONE).encode())
                self.afc.put("/Documents/CameraTestOutput/a-annotated.png", b"\x89PNG")
            return {"pid": 812}

        async def kill(d, a):
            self.calls.append(("app.kill", a))
            raise HelperError("not-running", "Not running.")

        lists = []

        async def crash_list(d, a):
            self.calls.append(("crash.list", a))
            lists.append(1)
            return [] if len(lists) == 1 else list(self.crashes_after)

        async def app_pids(d, rsd, bid):
            return list(self.pids)
        cmds = self.d.registry.commands
        p = mock.patch.dict(cmds, {norm("app.launch"): ("app.launch", launch, "test"),
                                   norm("app.kill"): ("app.kill", kill, "test"),
                                   norm("crash.list"): ("crash.list", crash_list, "test")})
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.dict(self.g, {"OUT_DIR": self.tmp / "cameratest", "F_app_pids": app_pids})
        p.start()
        self.addCleanup(p.stop)
        self.inputs = [self.tmp / "a.jpg", self.tmp / "clip.mov"]
        self.inputs[0].write_bytes(b"jpeg" * 100)
        self.inputs[1].write_bytes(b"mov" * 100)

    def connect(self):
        from iosstudio.device.real import daemon
        s = daemon.Session(self.d, UDID)
        s.rsd = types.SimpleNamespace(udid=UDID, all_values={})
        s.where = {"transport": "wifi"}
        s.connected.set()
        self.opened: list = []

        @contextlib.asynccontextmanager
        async def house_arrest(bid, documents=False):
            self.opened.append((bid, documents))
            yield self.afc
        s.house_arrest = house_arrest
        self.d.session = s
        return s

    def args(self, **kw):
        return {"bundleId": BID, "inputs": [str(p) for p in self.inputs], "testId": "ct-test", **kw}


class RealCameraTest(RealHarness, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await self.setup_real()

    async def test_bad_args(self):
        with self.assertRaises(HelperError) as cm:
            await self.d.dispatch("cameratest.run", self.args())
        self.assertEqual(cm.exception.code, "no-device")
        self.connect()
        for args in ({"inputs": [str(self.inputs[0])]}, {"bundleId": BID}, {"bundleId": BID, "inputs": []}):
            with self.assertRaises(HelperError) as cm:
                await self.d.dispatch("cameratest.run", args)
            self.assertEqual(cm.exception.code, "bad-args", args)
        with self.assertRaises(HelperError) as cm:
            await self.d.dispatch("cameratest.run", self.args(inputs=[str(self.tmp / "gone.jpg")]))
        self.assertEqual(cm.exception.code, "not-found")
        self.assertIn("gone.jpg", cm.exception.detail)
        self.assertEqual(self.calls, [])

    async def test_full_run(self):
        self.connect()
        self.afc.put("/Documents/CameraTestOutput/stale.json", b"{}")       # from an earlier run
        self.afc.put("/Documents/CameraTestInput/old.jpg", b"x")
        out = await self.d.dispatch("cameratest.run", self.args(args="-cameraTestMode 1 -fast", env="A=1, B=two"))
        self.assertEqual(self.opened, [(BID, False)])
        # inputs pushed into a fresh CameraTestInput
        self.assertEqual(sorted(p for p in self.afc.files if "CameraTestInput" in p),
                         ["/Documents/CameraTestInput/a.jpg", "/Documents/CameraTestInput/clip.mov"])
        self.assertEqual(bytes(self.afc.files["/Documents/CameraTestInput/a.jpg"]), self.inputs[0].read_bytes())
        launch = next(a for c, a in self.calls if c == "app.launch")
        self.assertEqual(launch, {"bundleId": BID, "args": ["-cameraTestMode", "1", "-fast"], "env": {"A": "1", "B": "two"}})
        self.assertEqual([a for c, a in self.calls if c == "crash.list"],
                         [{"process": "HabitTracker", "flush": True}] * 2)
        self.assertTrue(out["ok"])
        self.assertEqual(out["done"], DONE)
        self.assertEqual((out["pid"], out["timedOut"], out["exited"], out["newCrashes"]), (812, False, False, []))
        self.assertEqual(sorted(Path(p).name for p in out["outputs"]), ["a-annotated.png", "done.json"])  # no stale.json
        self.assertEqual(out["outputDir"], str(self.tmp / "cameratest" / "ct-test" / "pulled" / "CameraTestOutput"))
        self.assertEqual([s["id"] for s in out["steps"]], STEP_IDS)
        self.assertEqual({s["state"] for s in out["steps"]}, {"done"})
        self.assertEqual(out["logLines"], [])
        self.assertIsNone(out["logFile"])
        evs = [d for n, d in self.events if n == "cameratest"]
        self.assertEqual(evs[0], {"testId": "ct-test", "step": "check", "state": "running", "title": "Check the app",
                                  "detail": ""})
        self.assertTrue(any(n == "transfer" and d["kind"] == "cameratest.push" for n, d in self.events))
        self.assertEqual(self.d.registry.provided["transfers"].running, {})   # untracked again

    async def test_new_crash_fails_the_run(self):
        self.connect()
        self.crashes_after = [{"name": "/HabitTracker-new.ips", "ts": time.time()},
                              {"name": "/HabitTracker-old.ips", "ts": time.time() - 3600}]
        out = await self.d.dispatch("cameratest.run", self.args())
        self.assertEqual([c["name"] for c in out["newCrashes"]], ["/HabitTracker-new.ips"])
        self.assertFalse(out["ok"])
        self.assertEqual(out["steps"][-1]["state"], "failed")
        self.assertEqual(out["args"], ["-cameraTestMode", "1"])          # the default launch arguments

    async def test_timeout_and_exit(self):
        self.connect()
        self.write_done = False
        clock = devtest.FakeClock()
        self.afc.on_exists = lambda: clock.advance(2)
        with mock.patch.dict(self.g, {"time": clock.module()}):
            out = await self.d.dispatch("cameratest.run", self.args(timeout=1))     # raised to 5 s
            self.assertTrue(out["timedOut"])
            self.assertFalse(out["ok"])
            self.assertEqual(out["timeout"], 5.0)
            wait = next(s for s in out["steps"] if s["id"] == "wait")
            self.assertEqual(wait["state"], "failed")
            self.assertIn("no done.json", wait["detail"])

            self.pids = []                                                  # the app quits early
            out = await self.d.dispatch("cameratest.run", self.args(timeout=60, testId="ct-2"))
            self.assertTrue(out["exited"])
            self.assertFalse(out["timedOut"])
            self.assertIn("closed", next(s for s in out["steps"] if s["id"] == "wait")["detail"])

    async def test_app_store_app_is_refused(self):
        self.connect()
        with self.assertRaises(HelperError) as cm:
            await self.d.dispatch("cameratest.run", self.args(bundleId="com.example.store"))
        self.assertEqual(cm.exception.code, "not-dev-app")
        last = [d for n, d in self.events if n == "cameratest"][-1]
        self.assertEqual((last["step"], last["state"]), ("check", "failed"))
        with self.assertRaises(HelperError) as cm:
            await self.d.dispatch("cameratest.run", self.args(bundleId="com.example.missing"))
        self.assertEqual(cm.exception.code, "not-found")


class DemoHarness:
    async def setup_demo(self):
        from iosstudio.device.demo import core as dcore
        from iosstudio.device.demo.daemon import DemoBackend
        self.demo_events: list = []
        p = mock.patch.object(dcore, "BROKER", types.SimpleNamespace(emit=lambda n, d: self.demo_events.append((n, d))))
        p.start()
        self.addCleanup(p.stop)
        self.b = DemoBackend(None)
        self.addAsyncCleanup(self.b.shutdown)
        self.demo_tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.demo_tmp, ignore_errors=True)
        self.demo_inputs = [self.demo_tmp / "a.jpg"]
        self.demo_inputs[0].write_bytes(b"jpeg" * 100)

    def demo_args(self, **kw):
        return {"bundleId": DEMO_BID, "inputs": [str(p) for p in self.demo_inputs], **kw}


class DemoCameraTest(DemoHarness, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await self.setup_demo()

    async def test_run_and_errors(self):
        out = await self.b.dispatch("cameratest.run", self.demo_args())
        self.assertTrue(out["ok"])
        self.assertEqual([r["file"] for r in out["done"]["results"]], ["a.jpg"])
        self.assertEqual({s["state"] for s in out["steps"]}, {"done"})
        for args in ({"inputs": [str(self.demo_inputs[0])]}, {"bundleId": DEMO_BID}):
            with self.assertRaises(HelperError) as cm:
                await self.b.dispatch("cameratest.run", args)
            self.assertEqual(cm.exception.code, "bad-args")
        with self.assertRaises(HelperError) as cm:
            await self.b.dispatch("cameratest.run", self.demo_args(inputs=[str(self.demo_tmp / "gone.jpg")]))
        self.assertEqual(cm.exception.code, "not-found")
        with self.assertRaises(HelperError) as cm:      # an App Store app that shares files
            await self.b.dispatch("cameratest.run", self.demo_args(bundleId="com.example.podcasts"))
        self.assertEqual(cm.exception.code, "not-dev-app")

    async def test_app_store_app_without_sharing(self):
        with self.assertRaises(HelperError) as cm:
            await self.b.dispatch("cameratest.run", self.demo_args(bundleId="com.example.citytransit"))
        self.assertEqual(cm.exception.code, "not-dev-app")


class Parity(RealHarness, DemoHarness, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await self.setup_real()
        await self.setup_demo()
        self.connect()

    async def test_reply_and_event_shapes(self):
        real = await self.d.dispatch("cameratest.run", self.args(inputs=[str(self.inputs[0])]))
        demo = await self.b.dispatch("cameratest.run", self.demo_args())
        self.assertEqual(set(real), set(demo))
        for k in real:
            if real[k] is not None and demo[k] is not None:
                self.assertIs(type(real[k]), type(demo[k]), k)
        self.assertEqual([set(s) for s in real["steps"]], [set(s) for s in demo["steps"]])
        self.assertEqual([s["title"] for s in real["steps"]], [s["title"] for s in demo["steps"]])
        rev = [(d["step"], d["state"], set(d)) for n, d in self.events if n == "cameratest"]
        dev = [(d["step"], d["state"], set(d)) for n, d in self.demo_events if n == "cameratest"]
        self.assertEqual(rev, dev)


if __name__ == "__main__":
    unittest.main()
