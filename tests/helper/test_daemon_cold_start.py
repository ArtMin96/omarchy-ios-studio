"""The real device daemon right after it starts (helper/iosstudio/device/real/daemon.py, features/apps/real.py):
requests sent before the first discovery pass or while the tunnel is still coming up.

Found on a real iPhone (Wi-Fi only): `device.info` sent right after a cold start answered `no-device`
at once, and the first `apps.list` waited 3-5 s for the tunnel although the app list was cached on disk.
Here discovery and the tunnel are slowed down on purpose; pymobiledevice3 is faked at the service boundary.

    python -m pytest tests/helper/test_daemon_cold_start.py
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from iosstudio.device import link
from iosstudio.device.features.apps.index import AppIndex
from iosstudio.device.real import core
from support.pmd3_stub import stub_pmd3

UDID = "00008150-COLDSTART"
DISCOVERY_S = 0.4     # reading the pairing records (off the loop)
TUNNEL_S = 1.5        # bringing the Wi-Fi tunnel up
APP_DIR = "/private/var/containers/Bundle/Application/X/Habits.app"


class Events:
    """Stands in for the broker: collects the events the daemon emits."""

    def __init__(self):
        self.got: list[tuple[str, object]] = []

    def emit(self, name, data):
        self.got.append((name, data))

    def named(self, name):
        return [d for n, d in self.got if n == name]


class Svc:
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def fake_pmd3(tmp: Path):
    class AppServiceService(Svc):
        async def list_processes(self):
            return [{"processIdentifier": 321, "executableURL": {"relative": f"file://{APP_DIR}/Habits"}}]

    class InstallationProxyService(Svc):
        async def lookup(self, opts):
            return {"com.example.habits": {"CFBundleIdentifier": "com.example.habits", "CFBundleName": "Habits",
                                           "Path": APP_DIR, "ApplicationType": "User"}}

    class Usbmux:
        @staticmethod
        async def list_devices():
            return []
    return types.SimpleNamespace(
        version="fake", usbmux=Usbmux, remote_dir=str(tmp),
        exceptions=types.SimpleNamespace(ConnectionFailedToUsbmuxdError=OSError),
        iter_remote_pair_records_by_identifier=lambda: iter(()),
        AppServiceService=AppServiceService, InstallationProxyService=InstallationProxyService)


@contextlib.asynccontextmanager
async def slow_tunnel(pmd3, transport, udid):
    await asyncio.sleep(TUNNEL_S)
    yield types.SimpleNamespace(udid=udid, all_values={"DeviceName": "Test iPhone", "ProductVersion": "27.3"}), \
        {"transport": transport, "address": "fe80::1"}


class ColdStart(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        stub = stub_pmd3()
        stub.__enter__()
        self.addCleanup(stub.__exit__, None, None, None)
        from iosstudio.device.real import daemon as deviced
        self.deviced = deviced
        self.events = Events()
        state = {"DEVICE_STATE": self.tmp / "device.json", "DEVICE_CACHE": self.tmp / "devices.json",
                 "LAST_DEVICE": self.tmp / "last-device"}       # the daemon's machine-wide files stay in tmp
        for p in (mock.patch.object(core, "BROKER", self.events), mock.patch.object(link, "rsd_for", slow_tunnel),
                  mock.patch.multiple(deviced, **state)):
            p.start()
            self.addCleanup(p.stop)
        self.d = deviced.Daemon(fake_pmd3(self.tmp))
        self.addAsyncCleanup(self.stop)

    async def stop(self):
        await self.d.shutdown()
        await self.d.close_device()
        if self.d.disc_task is not None:
            await asyncio.gather(self.d.disc_task, return_exceptions=True)

    def start(self, wifi: dict[str, str]):
        def read():
            time.sleep(DISCOVERY_S)      # runs in a worker thread, like the real record read
            return dict(wifi), set()
        self.d.records.read = read
        self.d.disc_task = asyncio.create_task(self.d.discovery())

    async def test_device_info_right_after_start_waits_and_succeeds(self):
        self.start({UDID: "/remote/pair.plist"})
        t0 = time.monotonic()
        reply = await self.d.handle({"id": 1, "cmd": "device.info", "args": {}})
        took = time.monotonic() - t0
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["data"]["udid"], UDID)
        self.assertEqual(reply["data"]["transport"], "wifi")
        self.assertGreaterEqual(took, TUNNEL_S)     # it waited for discovery and the tunnel ...
        self.assertLess(took, TUNNEL_S + 5)         # ... and no longer than needed

    async def test_no_device_only_after_discovery_looked(self):
        self.start({})
        t0 = time.monotonic()
        reply = await self.d.handle({"id": 1, "cmd": "device.info", "args": {}})
        took = time.monotonic() - t0
        self.assertEqual(reply["error"]["code"], "no-device")
        self.assertGreaterEqual(took, DISCOVERY_S * 0.9)   # not answered before discovery ran
        self.assertLess(took, DISCOVERY_S + 2)
        t0 = time.monotonic()                               # later requests don't wait again
        await self.d.handle({"id": 2, "cmd": "device.info", "args": {}})
        self.assertLess(time.monotonic() - t0, 0.1)

    async def test_ping_never_waits(self):
        self.start({UDID: "/remote/pair.plist"})
        t0 = time.monotonic()
        self.assertTrue((await self.d.handle({"id": 1, "cmd": "daemon.ping"}))["ok"])
        self.assertLess(time.monotonic() - t0, 0.1)

    def cache_apps(self):
        apps_real = sys.modules["iosstudio.device.features.apps.real"]
        index = AppIndex(self.tmp / "cache")
        cached = [{"bundleId": "com.example.habits", "name": "Habits (cached)", "version": "", "build": "",
                   "developer": False, "signer": "", "removable": True, "hidden": False, "system": False,
                   "executable": "", "path": APP_DIR, "fileSharing": False, "debuggable": False}]
        index.path(UDID).parent.mkdir(parents=True)
        index.path(UDID).write_text(json.dumps(
            {"version": 1, "udid": UDID, "lists": {"User": {"fetchedAt": time.time(), "apps": cached}}}))
        p = mock.patch.object(apps_real, "INDEX", index)
        p.start()
        self.addCleanup(p.stop)

    async def test_apps_list_while_the_tunnel_connects_comes_from_the_disk_cache(self):
        self.cache_apps()
        self.start({UDID: "/remote/pair.plist"})
        await self.d.discovered.wait()               # a session exists, its tunnel is still coming up
        t0 = time.monotonic()
        reply = await self.d.handle({"id": 1, "cmd": "apps.list", "args": {"icons": False}})
        self.assertLess(time.monotonic() - t0, 0.5)
        self.assertTrue(reply["data"]["stale"])

    async def test_apps_list_right_after_start_comes_from_the_disk_cache(self):
        self.cache_apps()
        self.start({UDID: "/remote/pair.plist"})
        t0 = time.monotonic()
        reply = await self.d.handle({"id": 1, "cmd": "apps.list", "args": {"icons": False}})
        took = time.monotonic() - t0
        self.assertTrue(reply["ok"], reply)
        self.assertLess(took, DISCOVERY_S + 0.5)      # never waits for the tunnel
        data = reply["data"]
        self.assertTrue(data["stale"])
        self.assertEqual([a["name"] for a in data["apps"]], ["Habits (cached)"])
        self.assertFalse(data["apps"][0]["running"])  # not known yet

        # Once the tunnel is up: the refreshed list with live running state follows as `apps`.
        deadline = time.monotonic() + TUNNEL_S + 5
        while not self.events.named("apps") and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        ev = self.events.named("apps")
        self.assertEqual(len(ev), 1, self.events.got)
        app = ev[0]["apps"][0]
        self.assertEqual((app["name"], app["running"], app["pid"]), ("Habits", True, 321))

        # Connected now: the next list is live, with running state, and quick.
        t0 = time.monotonic()
        reply = await self.d.handle({"id": 2, "cmd": "apps.list", "args": {"icons": False}})
        self.assertLess(time.monotonic() - t0, 0.5)
        self.assertEqual(reply["data"]["apps"][0]["pid"], 321)


if __name__ == "__main__":
    unittest.main()
