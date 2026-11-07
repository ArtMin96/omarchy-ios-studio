"""Features that need one installed app (pcap, metrics, ui) go through the cached AppIndex, never a
full `get_apps` (helper/deviced_features/_app_lookup.py), plus the metrics crash-check task.

    python -m pytest tests/helper/test_app_lookups.py

pymobiledevice3 isn't in the test environment: the feature modules are imported with stub
pymobiledevice3 modules, and the phone is a fake pmd3 that counts its calls.
"""
from __future__ import annotations

import asyncio
import contextlib
import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from deviced_features import _app_lookup as L
from deviced_features import app_index as AI

UDID = "00008150-LOOKUP"
APP_DIR = "/private/var/containers/Bundle/Application/AAAA/Habits.app"
PHONE_APPS = {
    "XTL-TEAM1.com.example.habits": {"CFBundleDisplayName": "Habits", "CFBundleExecutable": "Habits",
                                     "ApplicationType": "User", "Path": APP_DIR},
    "com.example.notes": {"CFBundleDisplayName": "Notes", "CFBundleExecutable": "Notes", "ApplicationType": "User",
                          "Path": "/private/var/containers/Bundle/Application/BBBB/Notes.app"},
    "com.apple.mobilesafari": {"CFBundleDisplayName": "Safari", "CFBundleExecutable": "MobileSafari",
                               "ApplicationType": "System", "Path": "/Applications/MobileSafari.app"},
}


# ---------------------------------------------------------------- stub pymobiledevice3

class _StubModule(types.ModuleType):
    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        cls = type(name, (Exception,), {})
        setattr(self, name, cls)
        return cls


class _StubFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(self, name, path=None, target=None):
        if name == "pymobiledevice3" or name.startswith("pymobiledevice3."):
            return importlib.machinery.ModuleSpec(name, self, is_package=True)
        return None

    def create_module(self, spec):
        m = _StubModule(spec.name)
        m.__path__ = []
        return m

    def exec_module(self, module):
        pass


FEATURES = ("deviced_features.pcap", "deviced_features.metrics", "deviced_features.ui")


@contextlib.contextmanager
def feature_modules():
    """Import the real feature modules against stub pymobiledevice3, undone afterwards."""
    saved = {k: v for k, v in sys.modules.items() if k.startswith("pymobiledevice3") or k in FEATURES}
    finder = _StubFinder()
    have_real = importlib.util.find_spec("pymobiledevice3") is not None
    if not have_real:
        sys.meta_path.insert(0, finder)
    try:
        yield [importlib.import_module(m) for m in FEATURES]
    finally:
        if finder in sys.meta_path:
            sys.meta_path.remove(finder)
        for k in [k for k in sys.modules if k.startswith("pymobiledevice3") or k in FEATURES]:
            if k not in saved:
                del sys.modules[k]
        sys.modules.update(saved)


# ---------------------------------------------------------------- fake phone

class FakePmd3:
    """installation_proxy and app service of a fake phone; counts what each feature asks for."""

    def __init__(self, apps: dict, procs: list[dict] | None = None):
        self.apps = apps
        self.procs = procs or []
        self.get_apps_calls = 0
        self.lookups: list[dict] = []
        self.list_processes_calls = 0
        pmd3 = self

        class IP:
            def __init__(self, lockdown=None):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get_apps(self, **kw):
                pmd3.get_apps_calls += 1
                return dict(pmd3.apps)

            async def lookup(self, opts):
                pmd3.lookups.append(opts)
                ids = opts.get("BundleIDs")
                kind = opts.get("ApplicationType", "Any")
                return {k: v for k, v in pmd3.apps.items()
                        if (ids is None or k in ids) and (kind == "Any" or v.get("ApplicationType") == kind)}

        class AppService:
            def __init__(self, rsd=None):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def list_processes(self):
                pmd3.list_processes_calls += 1
                return list(pmd3.procs)

        self.InstallationProxyService = IP
        self.AppServiceService = AppService

    def full_lists(self) -> int:
        """Full app lists with every attribute: get_apps, or a Lookup without ReturnAttributes."""
        return self.get_apps_calls + sum(1 for o in self.lookups if "ReturnAttributes" not in o)


class FakeSession:
    def __init__(self, pmd3):
        self.udid = UDID
        self.pmd3 = pmd3
        self.rsd = object()

    async def get_rsd(self):
        return self.rsd


class IndexCase(unittest.IsolatedAsyncioTestCase):
    """A fresh, private AppIndex.INDEX (the module-level one the features use)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        for p in (mock.patch.object(AI.INDEX, "cache_dir", self.tmp), mock.patch.object(AI.INDEX, "_devs", {}),
                  mock.patch.object(AI.INDEX, "_tasks", {}), mock.patch.object(AI.INDEX, "_gen", {})):
            p.start()
            self.addCleanup(p.stop)


# ---------------------------------------------------------------- shared helpers

class ExePath(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(L.exe_path({"executableURL": {"relative": "file:///var/X.app/X"}}), "/var/X.app/X")
        self.assertEqual(L.exe_path({"executableURL": "file:///usr/libexec/foo"}), "/usr/libexec/foo")
        self.assertEqual(L.exe_path({"executableURL": "/var/X.app/X"}), "/var/X.app/X")
        self.assertEqual(L.exe_path({}), "")
        self.assertEqual(L.exe_path({"executableURL": None}), "")


class FindApp(IndexCase):
    async def test_exact_is_one_small_lookup_then_cached(self):
        pmd3 = FakePmd3(PHONE_APPS)
        fetch = AI.device_fetcher(pmd3, None)
        e = await L.find_app(UDID, "com.example.notes", fetch)
        self.assertEqual(e["executable"], "Notes")
        self.assertEqual(pmd3.lookups[0]["BundleIDs"], ["com.example.notes"])
        await L.find_app(UDID, "com.example.notes", fetch)
        self.assertEqual(len(pmd3.lookups), 1)          # second time from the cache
        self.assertEqual(pmd3.full_lists(), 0)

    async def test_free_team_prefix(self):
        pmd3 = FakePmd3(PHONE_APPS)
        fetch = AI.device_fetcher(pmd3, None)
        e = await L.find_app(UDID, "com.example.habits", fetch)
        self.assertEqual(e["bundleId"], "XTL-TEAM1.com.example.habits")
        # one Lookup of the plain id (miss), then the user list with limited attributes
        self.assertEqual([o.get("ApplicationType") for o in pmd3.lookups], ["Any", "User"])
        e = await L.find_app(UDID, "XTL-OTHER.com.example.habits", fetch)   # another team's prefix
        self.assertEqual(e["bundleId"], "XTL-TEAM1.com.example.habits")
        self.assertEqual(pmd3.full_lists(), 0)

    async def test_not_installed(self):
        pmd3 = FakePmd3(PHONE_APPS)
        self.assertIsNone(await L.find_app(UDID, "com.example.gone", AI.device_fetcher(pmd3, None)))
        self.assertIsNone(await L.find_app(UDID, "", AI.device_fetcher(pmd3, None)))
        self.assertEqual(pmd3.full_lists(), 0)


# ---------------------------------------------------------------- features

class Features(IndexCase):
    def setUp(self):
        super().setUp()
        cm = feature_modules()
        self.pcap, self.metrics, self.ui = cm.__enter__()
        self.addCleanup(cm.__exit__, None, None, None)
        self.pmd3 = FakePmd3(PHONE_APPS, procs=[
            {"processIdentifier": 501, "executableURL": {"relative": f"file://{APP_DIR}/Habits"}},
            {"processIdentifier": 90, "executableURL": {"relative": "file:///Applications/MobileSafari.app/MobileSafari"}},
            {"processIdentifier": 1, "executableURL": {"relative": "file:///sbin/launchd"}},
        ])
        self.s = FakeSession(self.pmd3)

    async def test_pcap_resolve_process(self):
        exe, bid = await self.pcap.resolve_process(self.s, "com.example.habits")
        self.assertEqual((exe, bid), ("Habits", "XTL-TEAM1.com.example.habits"))
        await self.pcap.resolve_process(self.s, "com.example.habits")
        self.assertEqual(self.pmd3.full_lists(), 0)
        with self.assertRaises(Exception) as cm:
            await self.pcap.resolve_process(self.s, "com.example.gone")
        self.assertEqual(getattr(cm.exception, "code", None), "not-found")

    async def test_metrics_exe(self):
        b = self.metrics.Backend()
        self.assertEqual(await b._exe(self.s, "com.example.notes"), ("Notes", "com.example.notes", "Notes"))
        self.assertEqual(self.pmd3.full_lists(), 0)

    async def test_ui_running_apps_uses_cached_list(self):
        d = types.SimpleNamespace(pmd3=self.pmd3)
        apps = await self.ui._running_apps(d, self.s, None)
        self.assertEqual(apps, [(501, "XTL-TEAM1.com.example.habits"), (90, "com.apple.mobilesafari")])
        await self.ui._running_apps(d, self.s, None)
        self.assertEqual(len(self.pmd3.lookups), 1)      # the second press reuses the list
        self.assertEqual(self.pmd3.full_lists(), 0)

    async def test_ui_running_apps_refreshes_for_an_unknown_user_app(self):
        d = types.SimpleNamespace(pmd3=self.pmd3)
        await self.ui._running_apps(d, self.s, None)
        new_dir = "/private/var/containers/Bundle/Application/CCCC/Fresh.app"
        self.pmd3.apps = {**PHONE_APPS, "com.example.fresh": {"CFBundleExecutable": "Fresh", "ApplicationType": "User",
                                                              "Path": new_dir}}
        self.pmd3.procs.append({"processIdentifier": 700, "executableURL": f"file://{new_dir}/Fresh"})
        apps = await self.ui._running_apps(d, self.s, None)
        self.assertEqual(apps[0], (700, "com.example.fresh"))
        self.assertEqual(len(self.pmd3.lookups), 2)

    async def test_ui_front_app_with_bundle_id(self):
        d = types.SimpleNamespace(pmd3=self.pmd3)

        async def target(ax, pid, wait=0):
            return {"pid": pid}

        with mock.patch.object(self.ui, "target", target):
            app = await self.ui.front_app(d, self.s, None, None, "com.example.habits")
            again = await self.ui.front_app(d, self.s, None, None, "com.example.habits")
        self.assertEqual((app["bundleId"], app["pid"]), ("XTL-TEAM1.com.example.habits", 501))
        self.assertEqual(again["pid"], 501)
        self.assertEqual(self.pmd3.full_lists(), 0)


class FakeMetricsSession:
    def __init__(self):
        self.id = "m1"
        self.name = "Habits"
        self.bundle_id = "com.example.habits"
        self.pid = 501
        self.follow = True
        self.running = True
        self.sets = ["events"]
        self.data = {"wake": asyncio.Event()}
        self.status = "running"
        self.events: list = []

    def add_event(self, kind, text, **kw):
        self.events.append(kind)

    def set_status(self, status, *_):
        self.status = status


class MetricsCrashCheck(IndexCase):
    def setUp(self):
        super().setUp()
        cm = feature_modules()
        _, self.metrics, _ = cm.__enter__()
        self.addCleanup(cm.__exit__, None, None, None)

    async def test_crash_check_is_tracked_and_cancelled_on_detach(self):
        b = self.metrics.Backend()
        sess = FakeMetricsSession()
        started = asyncio.Event()

        async def pid_for(_s, _bid):
            return 0          # the app quit

        async def crash_check(*_a):
            started.set()
            await asyncio.sleep(3600)

        with mock.patch.object(b, "_pid_for", pid_for), mock.patch.object(b, "_crash_check", crash_check):
            sess.data["follower"] = asyncio.create_task(b._follow(None, None, sess))
            sess.data["wake"].set()
            await asyncio.wait_for(started.wait(), 2)
            checks = set(sess.data["crashChecks"])
            self.assertEqual(len(checks), 1)
            sess.running = False
            await b.detach(None, sess)
        task = checks.pop()
        self.assertTrue(task.cancelled())
        self.assertNotIn("crashChecks", sess.data)
        self.assertEqual(sess.status, "waiting-app")

    async def test_finished_crash_check_forgets_itself(self):
        b = self.metrics.Backend()
        sess = FakeMetricsSession()
        done = asyncio.Event()

        async def pid_for(_s, _bid):
            return 0

        async def crash_check(*_a):
            done.set()

        with mock.patch.object(b, "_pid_for", pid_for), mock.patch.object(b, "_crash_check", crash_check):
            sess.data["follower"] = asyncio.create_task(b._follow(None, None, sess))
            sess.data["wake"].set()
            await asyncio.wait_for(done.wait(), 2)
            await asyncio.sleep(0)
            self.assertEqual(sess.data["crashChecks"], set())
            sess.running = False
            await b.detach(None, sess)


if __name__ == "__main__":
    unittest.main()
