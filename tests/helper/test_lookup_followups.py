"""syslog's bundle id filter goes through the cached AppIndex (never a full `get_apps`), and apps.py
reads process paths with apps/lookup.exe_path.

    python -m pytest tests/helper/test_lookup_followups.py
"""
from __future__ import annotations

import shutil
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from iosstudio.device.features.apps import index as AI
from iosstudio.device.features.apps import real as A
from iosstudio.device.features.syslog import real as S

UDID = "00008150-SYSLOG"
APP_DIR = "/private/var/containers/Bundle/Application/AAAA/Habits.app"
PHONE_APPS = {
    "XTL-TEAM1.com.example.habits": {"CFBundleDisplayName": "Habits", "CFBundleExecutable": "Habits",
                                     "ApplicationType": "User", "Path": APP_DIR},
    "com.example.notes": {"CFBundleDisplayName": "Notes", "CFBundleExecutable": "Notes", "ApplicationType": "User",
                          "Path": "/private/var/containers/Bundle/Application/BBBB/Notes.app"},
}
PROCS = [
    {"processIdentifier": 501, "executableURL": {"relative": f"file://{APP_DIR}/Habits"}},
    {"processIdentifier": 1, "executableURL": "file:///sbin/launchd"},
    {"processIdentifier": 7, "executableURL": None},
]


class FakePmd3:
    """installation_proxy and app service of a fake phone; counts full app lists."""

    def __init__(self):
        self.get_apps_calls = 0
        self.lookups: list[dict] = []
        pmd3 = self

        class _Svc:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        class IP(_Svc):
            async def get_apps(self, **kw):
                pmd3.get_apps_calls += 1
                return dict(PHONE_APPS)

            async def lookup(self, opts):
                pmd3.lookups.append(opts)
                ids, kind = opts.get("BundleIDs"), opts.get("ApplicationType", "Any")
                return {k: v for k, v in PHONE_APPS.items()
                        if (ids is None or k in ids) and (kind == "Any" or v["ApplicationType"] == kind)}

        class AppService(_Svc):
            async def list_processes(self):
                return list(PROCS)

        self.InstallationProxyService = IP
        self.AppServiceService = AppService

    def full_lists(self) -> int:
        return self.get_apps_calls + sum(1 for o in self.lookups if "ReturnAttributes" not in o)


class Case(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        for p in (mock.patch.object(AI.INDEX, "cache_dir", tmp), mock.patch.object(AI.INDEX, "_devs", {}),
                  mock.patch.object(AI.INDEX, "_tasks", {}), mock.patch.object(AI.INDEX, "_gen", {})):
            p.start()
            self.addCleanup(p.stop)
        self.pmd3 = FakePmd3()
        self.s = types.SimpleNamespace(udid=UDID, pmd3=self.pmd3, rsd=object())


class SyslogResolve(Case):
    async def test_exact_then_cached(self):
        self.assertEqual(await S.PhoneSyslog.resolve(self.s, "com.example.notes"), ("Notes", "com.example.notes"))
        await S.PhoneSyslog.resolve(self.s, "com.example.notes")
        self.assertEqual(len(self.pmd3.lookups), 1)
        self.assertEqual(self.pmd3.full_lists(), 0)

    async def test_free_team_prefix(self):
        self.assertEqual(await S.PhoneSyslog.resolve(self.s, "com.example.habits"),
                         ("Habits", "XTL-TEAM1.com.example.habits"))
        self.assertEqual(self.pmd3.full_lists(), 0)

    async def test_not_installed_and_no_tunnel(self):
        self.assertEqual(await S.PhoneSyslog.resolve(self.s, "com.example.gone"), (None, "com.example.gone"))
        self.assertEqual(self.pmd3.full_lists(), 0)
        self.s.rsd = None
        self.assertEqual(await S.PhoneSyslog.resolve(self.s, "com.example.notes"), (None, None))


class AppsProcessPaths(Case):
    async def test_running_pids(self):
        d = types.SimpleNamespace(pmd3=self.pmd3)
        self.assertEqual(await A.running_pids(d, None), {AI.bundle_dir(APP_DIR): 501})

    async def test_app_pids(self):
        d = types.SimpleNamespace(pmd3=self.pmd3)
        self.assertEqual(await A.app_pids(d, self.s, None, "XTL-TEAM1.com.example.habits"), [501])

    async def test_processes_list(self):
        cmds = {}
        reg = types.SimpleNamespace(command=lambda name: lambda f: cmds.setdefault(name, f),
                                    on_tunnel_drop=lambda f: f, on_session_end=lambda f: f, on_shutdown=lambda f: f)
        A.register(reg)

        async def rsd(_a):
            return self.s, None

        d = types.SimpleNamespace(pmd3=self.pmd3, rsd=rsd)
        out = await cmds["processes.list"](d, {})
        self.assertEqual([(p["pid"], p["name"], p["path"], p["isApp"]) for p in out], [
            (7, "", "", False), (501, "Habits", f"{APP_DIR}/Habits", True), (1, "launchd", "/sbin/launchd", False)])
        out = await cmds["processes.list"](d, {"appsOnly": True})
        self.assertEqual([p["pid"] for p in out], [501])


if __name__ == "__main__":
    unittest.main()
