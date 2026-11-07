"""Stale container connections and running state after app.launch / app.kill (pass #2 on the iPhone).

files/real.py: container.list (also mkdir, delete) reuses a cached house_arrest connection. One kept from before the
app crashed or was relaunched answers "not found" even for the root; the command must retry once on a fresh
connection, while a path that really is missing is still `not-found`. app.launch / app.kill / app.uninstall drop
the app's cached connections (files provides "containers").

apps/real.py: app.launch / app.kill send `apps` with the cached user list and the new running state, so clients
that didn't start the app (the GUI, when MCP or the CLI did) see Running change. The demo does the same.

    .venv/bin/pytest -q tests/helper/test_dev_files_apps_stale.py
    cd tests && PYTHONPATH=../helper:. ~/pymobile3-venv/bin/python -m unittest helper.test_dev_files_apps_stale
"""
from __future__ import annotations

import posixpath
import shutil
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from support import devtest
from support.pmd3_stub import stub_pmd3

devtest.isolate()

from iosstudio.core.output import HelperError  # noqa: E402

UDID = "00008150-FILESSTALE"
BID = "XTL-TEAM1.dev.iosstudio.crashlab"
APP_DIR = "/private/var/containers/Bundle/Application/AAAA/CrashLab.app"
OTHER = "com.example.notes"


class Afc:
    """A house_arrest AFC connection. `stale` ones answer every request with AfcFileNotFoundError, as a
    connection kept from before the app was relaunched did on the phone."""

    def __init__(self, phone, n):
        self.phone, self.n, self.stale, self.closed = phone, n, False, False

    def _check(self, p):
        if self.stale or p not in self.phone.tree:
            raise self.phone.NotFound(f"no {p}", 8)

    async def listdir(self, p):
        self._check(p)
        return sorted(posixpath.basename(x) for x in self.phone.tree if posixpath.dirname(x) == p and x != p)

    async def stat(self, p):
        self._check(p)
        return {"st_ifmt": "S_IFDIR" if self.phone.tree[p] is None else "S_IFREG", "st_size": 3}

    async def exists(self, p):
        if self.stale:
            raise self.phone.NotFound(f"no {p}", 8)
        return p in self.phone.tree

    async def makedirs(self, p):
        self.phone.tree[p] = None

    async def rm(self, p, force=False):
        self._check(p)
        for k in [k for k in self.phone.tree if k == p or k.startswith(p + "/")]:
            del self.phone.tree[k]
        return []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed = True
        return False


class Phone:
    def __init__(self, not_found):
        self.NotFound = not_found
        self.tree = {"/": None, "/Documents": None, "/Library": None, "/Documents/a.txt": b"abc"}
        self.opened: list[Afc] = []
        phone = self

        class HouseArrestService:
            @staticmethod
            async def create(rsd, bid, documents_only=False):
                afc = Afc(phone, len(phone.opened))
                phone.opened.append(afc)
                return afc
        self.HouseArrestService = HouseArrestService


class Harness(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        stub = stub_pmd3()
        stub.__enter__()
        self.addCleanup(stub.__exit__, None, None, None)
        from iosstudio.device.features.apps import index as AI
        from iosstudio.device.real import core, daemon
        self.AI = AI
        for p in (mock.patch.object(AI.INDEX, "cache_dir", tmp), mock.patch.object(AI.INDEX, "_devs", {}),
                  mock.patch.object(AI.INDEX, "_tasks", {}), mock.patch.object(AI.INDEX, "_gen", {})):
            p.start()
            self.addCleanup(p.stop)
        self.events: list = []
        p = mock.patch.object(core, "BROKER", types.SimpleNamespace(emit=lambda n, d: self.events.append((n, d)),
                                                                    syslog=None))
        p.start()
        self.addCleanup(p.stop)
        self.signals: list = []
        test = self

        class CoreDeviceError(Exception):
            pass

        class AppServiceService(devtest.Svc):
            async def launch_application(self, bid, args, *rest, **kw):
                return {"processToken": {"processIdentifier": 777}}

            async def list_processes(self):
                return [{"processIdentifier": 777, "executableURL": {"relative": f"file://{APP_DIR}/CrashLab"}}]

            async def send_signal_to_process(self, pid, sig):
                test.signals.append(pid)

        class InstallationProxyService(devtest.Svc):
            async def uninstall(self, bid):
                pass

        self.d = daemon.Daemon(types.SimpleNamespace(
            version="fake", iter_remote_pair_records_by_identifier=lambda: iter(()),
            AppServiceService=AppServiceService, InstallationProxyService=InstallationProxyService,
            exceptions=types.SimpleNamespace(CoreDeviceError=CoreDeviceError)))
        self.addAsyncCleanup(self.d.shutdown)
        self.g = self.d.registry.lookup("container.list").__globals__
        self.phone = Phone(self.g["AfcFileNotFoundError"])
        p = mock.patch.dict(self.g, {"HouseArrestService": self.phone.HouseArrestService})
        p.start()
        self.addCleanup(p.stop)

        s = daemon.Session(self.d, UDID)
        s.rsd = types.SimpleNamespace(udid=UDID, all_values={})
        s.where = {"transport": "wifi"}
        s.connected.set()

        async def no_ddi():
            pass
        s.ensure_ddi = no_ddi
        # what app_info would read from installation_proxy: a development build
        s.tunnel_data["files.apps"] = {BID: {"bundleId": BID, "name": "CrashLab", "developer": True,
                                             "fileSharing": False, "executable": "CrashLab", "root": "/"}}
        self.d.session = self.s = s

    async def fill_index(self):
        async def fetch(kind, ids=None):
            apps = {BID: {"CFBundleDisplayName": "CrashLab", "CFBundleExecutable": "CrashLab", "Path": APP_DIR,
                          "ApplicationType": "User"},
                    OTHER: {"CFBundleDisplayName": "Notes", "CFBundleExecutable": "Notes", "ApplicationType": "User",
                            "Path": "/private/var/containers/Bundle/Application/BBBB/Notes.app"}}
            return {k: v for k, v in apps.items() if ids is None or k in ids}
        await self.AI.INDEX.list(UDID, "User", fetch, sync=True)

    def apps_events(self):
        return [d for n, d in self.events if n == "apps"]


class StaleContainer(Harness):
    async def test_stale_cached_connection_is_retried_once(self):
        out = await self.d.dispatch("container.list", {"bundleId": BID})
        self.assertEqual([e["name"] for e in out["entries"]], ["Documents", "Library"])
        self.assertEqual(len(self.phone.opened), 1)
        self.phone.opened[0].stale = True            # the app crashed / was relaunched meanwhile
        out = await self.d.dispatch("container.list", {"bundleId": BID})
        self.assertEqual([e["name"] for e in out["entries"]], ["Documents", "Library"])
        self.assertEqual(len(self.phone.opened), 2)
        self.assertTrue(self.phone.opened[0].closed)
        out = await self.d.dispatch("container.list", {"bundleId": BID, "path": "/Documents"})
        self.assertEqual([e["name"] for e in out["entries"]], ["a.txt"])
        self.assertEqual(len(self.phone.opened), 2)   # the fresh one is kept

    async def test_missing_path_is_still_not_found(self):
        with self.assertRaises(HelperError) as cm:      # fresh connection: no retry
            await self.d.dispatch("container.list", {"bundleId": BID, "path": "/Gone"})
        self.assertEqual(cm.exception.code, "not-found")
        self.assertEqual(len(self.phone.opened), 1)
        with self.assertRaises(HelperError) as cm:      # kept connection: one retry, then not-found
            await self.d.dispatch("container.list", {"bundleId": BID, "path": "/Gone"})
        self.assertEqual(cm.exception.code, "not-found")
        self.assertEqual(len(self.phone.opened), 2)
        out = await self.d.dispatch("container.list", {"bundleId": BID})
        self.assertEqual(len(out["entries"]), 2)

    async def test_mkdir_and_delete_retry_a_stale_connection(self):
        await self.d.dispatch("container.list", {"bundleId": BID})
        self.phone.opened[0].stale = True
        self.assertEqual(await self.d.dispatch("container.mkdir", {"bundleId": BID, "path": "/Documents/new"}),
                         {"path": "/Documents/new"})
        self.phone.opened[1].stale = True
        out = await self.d.dispatch("container.delete", {"bundleId": BID, "paths": ["/Documents/a.txt"]})
        self.assertEqual(out, {"deleted": ["/Documents/a.txt"], "failed": []})
        self.assertEqual(len(self.phone.opened), 3)

    async def test_exists_error_is_not_retried(self):
        await self.d.dispatch("container.list", {"bundleId": BID})
        with self.assertRaises(HelperError) as cm:
            await self.d.dispatch("container.mkdir", {"bundleId": BID, "path": "/Documents"})
        self.assertEqual(cm.exception.code, "exists")
        self.assertEqual(len(self.phone.opened), 1)

    async def test_launch_kill_uninstall_drop_the_cached_connection(self):
        await self.fill_index()
        for cmd in ("app.launch", "app.kill", "app.uninstall"):
            self.s.tunnel_data.setdefault("files.apps", {})[BID] = {
                "bundleId": BID, "name": "CrashLab", "developer": True, "fileSharing": False, "executable": "", "root": "/"}
            await self.d.dispatch("container.list", {"bundleId": BID})
            n = len(self.phone.opened)
            await self.d.dispatch(cmd, {"bundleId": BID})
            self.assertTrue(self.phone.opened[n - 1].closed, cmd)
            self.assertNotIn(BID, self.s.tunnel_data["files.apps"], cmd)


class RunningEvents(Harness):
    async def test_launch_and_kill_send_apps(self):
        await self.fill_index()
        self.assertEqual(await self.d.dispatch("app.launch", {"bundleId": BID}), {"bundleId": BID, "pid": 777})
        ev = self.apps_events()
        self.assertEqual(len(ev), 1)
        self.assertEqual((ev[0]["udid"], ev[0]["system"]), (UDID, False))
        by = {a["bundleId"]: a for a in ev[0]["apps"]}
        self.assertEqual((by[BID]["running"], by[BID]["pid"]), (True, 777))
        self.assertFalse(by[OTHER]["running"])
        self.assertEqual(await self.d.dispatch("app.kill", {"bundleId": BID}), {"bundleId": BID, "killed": [777]})
        ev = self.apps_events()
        self.assertEqual(len(ev), 2)
        by = {a["bundleId"]: a for a in ev[1]["apps"]}
        self.assertEqual((by[BID]["running"], by[BID]["pid"]), (False, None))

    async def test_no_event_without_a_cached_list(self):
        await self.d.dispatch("app.launch", {"bundleId": BID})
        self.assertEqual(self.apps_events(), [])


class DemoRunningEvents(unittest.IsolatedAsyncioTestCase):
    async def test_demo_launch_and_kill_send_apps(self):
        from iosstudio.device.demo import core as dcore
        from iosstudio.device.demo.daemon import DemoBackend
        from iosstudio.device.features.apps import demo as AD
        events: list = []
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        for p in (mock.patch.object(AD, "event", lambda n, d: events.append((n, d))),
                  mock.patch.object(AD.INDEX, "cache_dir", tmp), mock.patch.object(AD.INDEX, "_devs", {}),
                  mock.patch.object(AD.INDEX, "_tasks", {}), mock.patch.object(AD.INDEX, "_gen", {}),
                  mock.patch.object(AD, "running", set(AD.running)), mock.patch.object(AD, "pids", dict(AD.pids))):
            p.start()
            self.addCleanup(p.stop)
        reg = DemoBackend(None).registry
        await AD.apps_list({"icons": False})
        bid = next(b for _n, b, *_ in dcore.APPS if b in dcore.installed and b not in AD.running)
        await reg.lookup("app.launch")(None, {"bundleId": bid})
        ev = [d for n, d in events if n == "apps"]
        self.assertTrue(next(a for a in ev[-1]["apps"] if a["bundleId"] == bid)["running"])
        reg.lookup("app.kill")(None, {"bundleId": bid})
        ev = [d for n, d in events if n == "apps"]
        self.assertFalse(next(a for a in ev[-1]["apps"] if a["bundleId"] == bid)["running"])


if __name__ == "__main__":
    unittest.main()
