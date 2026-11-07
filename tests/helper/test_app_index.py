"""App list cache (helper/iosstudio/device/features/app_index.py) and the demo's apps.list contract.

    python -m pytest tests/helper/test_app_index.py
"""
from __future__ import annotations

import asyncio
import contextlib
import shutil
import tempfile
import unittest
from pathlib import Path

from iosstudio.device.features import app_index as AI

UDID = "00008150-TEST"


def raw(*names: str, dev: bool = False) -> dict:
    return {f"com.example.{n.lower()}": {"CFBundleDisplayName": n, "CFBundleShortVersionString": "1.0",
                                          "CFBundleVersion": "1", "ApplicationType": "User",
                                          "Path": f"/private/var/containers/Bundle/Application/X/{n}.app",
                                          "CFBundleExecutable": n, "Entitlements": {"get-task-allow": dev},
                                          "SignerIdentity": "Apple Development: A" if dev else ""}
            for n in names}


class FakeFetch:
    """A Fetch that records each call and returns whatever `data` is at the time."""

    def __init__(self, data: dict, delay: float = 0):
        self.data = data
        self.delay = delay
        self.calls: list[tuple[str, list | None]] = []

    async def __call__(self, kind, bundle_ids=None):
        self.calls.append((kind, bundle_ids))
        if self.delay:
            await asyncio.sleep(self.delay)
        return {k: v for k, v in self.data.items() if bundle_ids is None or k in bundle_ids}


class Clock:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.clock = Clock()
        self.index = self.make()

    def make(self) -> AI.AppIndex:
        return AI.AppIndex(self.tmp, clock=self.clock)

    async def asyncTearDown(self):
        self.index.cancel()
        await asyncio.sleep(0)


class Listing(Base):
    async def test_miss_fetches_now_then_hit_is_served_from_memory(self):
        fetch = FakeFetch(raw("Bravo", "Alpha"))
        apps, stale, at = await self.index.list(UDID, "User", fetch)
        self.assertEqual([a["name"] for a in apps], ["Alpha", "Bravo"])
        self.assertFalse(stale)
        self.assertEqual(at, self.clock.t)
        self.assertEqual(len(fetch.calls), 1)
        apps2, stale2, _ = await self.index.list(UDID, "User", fetch)
        self.assertEqual(apps2, apps)
        self.assertFalse(stale2)
        self.assertEqual(len(fetch.calls), 1)

    async def test_entry_fields(self):
        apps, *_ = await self.index.list(UDID, "User", FakeFetch(raw("Alpha", dev=True)))
        e = apps[0]
        self.assertEqual(set(e), {"bundleId", "name", "version", "build", "developer", "signer", "removable", "hidden",
                                  "system", "executable", "path", "fileSharing", "debuggable"})
        self.assertTrue(e["developer"] and e["debuggable"] and e["removable"])
        self.assertFalse(e["system"] or e["hidden"] or e["fileSharing"])

    async def test_device_fetcher_limits_attributes(self):
        seen = {}

        class IP:
            def __init__(self, lockdown):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def lookup(self, opts):
                seen.update(opts)
                return raw("Alpha")

        class Pmd3:
            InstallationProxyService = IP

        out = await AI.device_fetcher(Pmd3, None)("User", ["com.example.alpha"])
        self.assertEqual(list(out), ["com.example.alpha"])
        self.assertEqual(seen["ReturnAttributes"], AI.ATTRS)
        self.assertEqual(seen["BundleIDs"], ["com.example.alpha"])
        self.assertNotIn("StaticDiskUsage", seen["ReturnAttributes"])

    async def test_expired_is_served_stale_and_refreshed_in_background(self):
        fetch = FakeFetch(raw("Alpha"))
        await self.index.list(UDID, "User", fetch)
        self.clock.t += AI.TTL + 1
        fetch.data = raw("Alpha", "Bravo")
        changes = []

        async def on_change(udid, kind, apps):
            changes.append((udid, kind, [a["name"] for a in apps]))

        apps, stale, _ = await self.index.list(UDID, "User", fetch, on_change=on_change)
        self.assertTrue(stale)
        self.assertEqual([a["name"] for a in apps], ["Alpha"])
        await asyncio.sleep(0.01)
        self.assertEqual(changes, [(UDID, "User", ["Alpha", "Bravo"])])
        apps, stale, at = await self.index.list(UDID, "User", fetch)
        self.assertFalse(stale)
        self.assertEqual(len(apps), 2)
        self.assertEqual(at, self.clock.t)

    async def test_force_refresh_without_change_emits_nothing(self):
        fetch = FakeFetch(raw("Alpha"))
        await self.index.list(UDID, "User", fetch)
        changes = []

        async def on_change(*a):
            changes.append(a)

        _, stale, _ = await self.index.list(UDID, "User", fetch, force=True, on_change=on_change)
        self.assertTrue(stale)
        await asyncio.sleep(0.01)
        self.assertEqual(len(fetch.calls), 2)
        self.assertEqual(changes, [])

    async def test_one_refresh_at_a_time(self):
        fetch = FakeFetch(raw("Alpha"))
        await self.index.list(UDID, "User", fetch)
        fetch.delay = 0.05
        for _ in range(5):
            await self.index.list(UDID, "User", fetch, force=True)
        await asyncio.sleep(0.1)
        self.assertEqual(len(fetch.calls), 2)

    async def test_concurrent_misses_share_one_fetch(self):
        fetch = FakeFetch(raw("Alpha"), delay=0.02)
        r = await asyncio.gather(*(self.index.list(UDID, "User", fetch) for _ in range(4)))
        self.assertEqual(len(fetch.calls), 1)
        self.assertTrue(all(x[0] == r[0][0] for x in r))

    async def test_kinds_are_cached_separately(self):
        fetch = FakeFetch(raw("Alpha"))
        await self.index.list(UDID, "User", fetch)
        await self.index.list(UDID, "Any", fetch)
        self.assertEqual([c[0] for c in fetch.calls], ["User", "Any"])

    async def test_sync_always_fetches(self):
        fetch = FakeFetch(raw("Alpha"))
        await self.index.list(UDID, "User", fetch)
        _, stale, _ = await self.index.list(UDID, "User", fetch, sync=True)
        self.assertFalse(stale)
        self.assertEqual(len(fetch.calls), 2)


class Disk(Base):
    async def test_disk_copy_makes_a_new_index_instant(self):
        await self.index.list(UDID, "User", FakeFetch(raw("Alpha")))
        self.assertTrue(self.index.path(UDID).exists())
        other = self.make()
        fetch = FakeFetch(raw("Alpha"))
        apps, stale, _ = await other.list(UDID, "User", fetch)
        self.assertEqual([a["name"] for a in apps], ["Alpha"])
        self.assertFalse(stale)
        self.assertEqual(fetch.calls, [])

    async def test_corrupt_disk_copy_is_a_miss(self):
        self.index.path(UDID).write_text("{nope")
        fetch = FakeFetch(raw("Alpha"))
        apps, stale, _ = await self.index.list(UDID, "User", fetch)
        self.assertEqual(len(apps), 1)
        self.assertEqual(len(fetch.calls), 1)

    async def test_another_process_invalidating_is_seen(self):
        fetch = FakeFetch(raw("Alpha"))
        await self.index.list(UDID, "User", fetch)
        self.make().invalidate(UDID)          # e.g. a --once daemon that installed an app
        fetch.data = raw("Alpha", "Bravo")
        apps, stale, _ = await self.index.list(UDID, "User", fetch)
        self.assertEqual(len(apps), 2)
        self.assertFalse(stale)


class Invalidation(Base):
    async def test_hard_invalidate_fetches_now(self):
        fetch = FakeFetch(raw("Alpha"))
        await self.index.list(UDID, "User", fetch)
        self.index.invalidate(UDID)
        self.assertFalse(self.index.path(UDID).exists())
        fetch.data = raw("Alpha", "Bravo")
        apps, stale, _ = await self.index.list(UDID, "User", fetch)
        self.assertEqual(len(apps), 2)
        self.assertFalse(stale)

    async def test_soft_invalidate_serves_stale_once(self):
        fetch = FakeFetch(raw("Alpha"))
        await self.index.list(UDID, "User", fetch)
        self.index.invalidate(UDID, hard=False)
        _, stale, _ = await self.index.list(UDID, "User", fetch)
        self.assertTrue(stale)
        await asyncio.sleep(0.01)
        _, stale, _ = await self.index.list(UDID, "User", fetch)
        self.assertFalse(stale)

    async def test_refresh_started_before_hard_invalidate_is_not_cached(self):
        fetch = FakeFetch(raw("Alpha"))
        await self.index.list(UDID, "User", fetch)
        fetch.delay = 0.03
        await self.index.list(UDID, "User", fetch, force=True)
        self.index.invalidate(UDID)          # app installed while the old list was being fetched
        await asyncio.sleep(0.05)
        fetch.delay = 0
        fetch.data = raw("Alpha", "New")
        apps, *_ = await self.index.list(UDID, "User", fetch)
        self.assertEqual(len(apps), 2)

    async def test_register_hooks(self):
        from iosstudio.device.deviced_kit import FeatureRegistry
        reg = FeatureRegistry(log=lambda *_: None)
        m = reg.module("app_index")
        AI.register(m)
        m.commit()
        self.assertEqual(len(reg.hooks["tunnel_drop"]), 1)
        self.assertEqual(len(reg.hooks["session_end"]), 1)
        self.assertEqual(len(reg.hooks["shutdown"]), 1)


class Lookup(Base):
    async def test_cached_exact_then_xtl_match(self):
        data = raw("Alpha")
        data["XTL-TEAM.com.example.mine"] = raw("Mine")["com.example.mine"]
        fetch = FakeFetch(data)
        await self.index.list(UDID, "User", fetch)
        self.assertEqual((await self.index.lookup(UDID, "com.example.alpha", fetch))["name"], "Alpha")
        self.assertEqual((await self.index.lookup(UDID, "XTL-TEAM.com.example.mine"))["name"], "Mine")
        self.assertEqual(len(fetch.calls), 1)
        hit = await self.index.lookup(UDID, "com.example.mine", fetch)   # exact miss -> one cheap lookup, then XTL
        self.assertEqual(hit["bundleId"], "XTL-TEAM.com.example.mine")
        self.assertEqual(fetch.calls[-1], ("Any", ["com.example.mine"]))

    async def test_miss_fetches_single_bundle_and_remembers_it(self):
        fetch = FakeFetch(raw("Alpha", "Bravo"))
        hit = await self.index.lookup(UDID, "com.example.bravo", fetch)
        self.assertEqual(hit["name"], "Bravo")
        self.assertEqual(fetch.calls, [("Any", ["com.example.bravo"])])
        await self.index.lookup(UDID, "com.example.bravo", fetch)
        self.assertEqual(len(fetch.calls), 1)
        self.assertIsNone(await self.index.lookup(UDID, "com.example.nope", fetch))

    def test_bundle_matches(self):
        self.assertTrue(AI.bundle_matches("XTL-ABC.com.example.app", "com.example.app"))
        self.assertTrue(AI.bundle_matches("com.example.app", "com.example.app"))
        self.assertFalse(AI.bundle_matches("com.example.application", "com.example.app"))
        self.assertFalse(AI.bundle_matches("xcom.example.app", "com.example.app"))
        self.assertFalse(AI.bundle_matches("com.example.app", ""))


class Sizes(Base):
    async def test_ttl(self):
        self.assertIsNone(self.index.sizes(UDID, "User"))
        self.index.put_sizes(UDID, "User", {"a": 1})
        self.assertEqual(self.index.sizes(UDID, "User"), {"a": 1})
        self.assertIsNone(self.index.sizes(UDID, "Any"))
        self.clock.t += AI.SIZES_TTL + 1
        self.assertIsNone(self.index.sizes(UDID, "User"))

    async def test_any_covers_user(self):
        self.index.put_sizes(UDID, "Any", {"a": 1})
        self.assertEqual(self.index.sizes(UDID, "User"), {"a": 1})


class Compose(unittest.TestCase):
    def test_running_by_bundle_dir_and_icon_and_size(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / "com.example.alpha.png").write_bytes(b"png")
        apps = AI.entries(raw("Alpha", "Bravo"))
        running = {AI.bundle_dir("file:///var/containers/Bundle/Application/X/Alpha.app/Alpha"): 42}
        out = {a["bundleId"]: a for a in AI.compose(apps, running, {"com.example.bravo": 7},
                                                    lambda b: tmp / f"{b}.png")}
        a, b = out["com.example.alpha"], out["com.example.bravo"]
        self.assertEqual((a["running"], a["pid"], a["size"]), (True, 42, None))
        self.assertEqual(a["icon"], str(tmp / "com.example.alpha.png"))
        self.assertEqual((b["running"], b["pid"], b["size"], b["icon"]), (False, None, 7, None))

    def test_bundle_dir(self):
        self.assertEqual(AI.bundle_dir("/private/var/a/X.app/X"), "/var/a/X.app")
        self.assertEqual(AI.bundle_dir("file:///var/a/X.app/"), "/var/a/X.app")
        self.assertEqual(AI.bundle_dir("/private/var/a/X.app"), "/var/a/X.app")


class FakeIcons:
    def __init__(self, fail: set[str] = frozenset(), fatal: set[str] = frozenset(), delay: float = 0.005):
        self.fail, self.fatal, self.delay = fail, fatal, delay
        self.opened = 0
        self.active = 0
        self.max_active = 0

    @contextlib.asynccontextmanager
    async def open(self):
        self.opened += 1
        yield self

    async def fetch(self, bid):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(self.delay)
            if bid in self.fatal:
                raise ConnectionError("gone")
            if bid in self.fail:
                raise ValueError("no icon")
            return b"\x89PNG" + bid.encode()
        finally:
            self.active -= 1


class Icons(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.events: list[tuple[str, dict]] = []

    def path_for(self, bid):
        return self.tmp / f"{bid}.png"

    @staticmethod
    def write(path, data):
        path.write_bytes(data)

    async def run_icons(self, bids, svc, **kw):
        return await AI.fetch_icons(UDID, bids, svc.open, self.path_for, self.write,
                                    lambda n, d: self.events.append((n, d)), **kw)

    async def test_batches_coalesce_and_cover_every_icon(self):
        bids = [f"b{i}" for i in range(25)]
        svc = FakeIcons(delay=0.001)
        n = await self.run_icons(bids, svc, interval=10)    
        self.assertEqual(n, 25)
        self.assertTrue(all(name == "appIcons" for name, _ in self.events))
        # the first icon goes out at once, then only the size limit (10) flushes until the end
        self.assertEqual([len(d["icons"]) for _, d in self.events], [1, 10, 10, 4])
        got = {b: p for _, d in self.events for b, p in d["icons"].items()}
        self.assertEqual(set(got), set(bids))
        self.assertEqual(got["b3"], str(self.path_for("b3")))
        self.assertEqual(svc.opened, AI.ICON_CONCURRENCY)
        self.assertLessEqual(svc.max_active, AI.ICON_CONCURRENCY)
        self.assertGreater(svc.max_active, 1)

    async def test_time_limited_batches(self):
        svc = FakeIcons(delay=0.02)
        await self.run_icons([f"b{i}" for i in range(6)], svc, concurrency=1, interval=0.05, size=100)
        sizes = [len(d["icons"]) for _, d in self.events]
        self.assertEqual(sum(sizes), 6)
        self.assertGreater(len(sizes), 1)
        self.assertLess(len(sizes), 6)

    async def test_cached_icons_are_skipped(self):
        self.path_for("b0").write_bytes(b"old")
        svc = FakeIcons()
        n = await self.run_icons(["b0"], svc)
        self.assertEqual((n, svc.opened, self.events), (0, 0, []))

    async def test_failed_icon_skipped_fatal_stops(self):
        svc = FakeIcons(fail={"b1"})
        n = await self.run_icons(["b0", "b1", "b2"], svc)
        self.assertEqual(n, 2)
        svc = FakeIcons(fatal={"c0"})
        self.events.clear()
        n = await self.run_icons([f"c{i}" for i in range(30)], svc, concurrency=1,
                                 fatal=lambda e: isinstance(e, ConnectionError))
        self.assertEqual(n, 0)
        self.assertEqual(self.events, [])


class DemoContract(unittest.IsolatedAsyncioTestCase):
    """The demo daemon's apps.list follows the same contract as the real one."""

    async def asyncSetUp(self):
        from iosstudio.device import deviced_demo_core as core
        from iosstudio.device.demo_features import apps as demo
        self.core, self.demo = core, demo
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.saved_index, self.saved_event = demo.INDEX, demo.event
        demo.INDEX = AI.AppIndex(self.tmp)
        self.events: list[tuple[str, dict]] = []
        demo.event = lambda n, d: self.events.append((n, d))
        icons = self.tmp / "icons"
        self.saved_icon_path = demo._icon_path
        demo._icon_path = lambda bid: icons / (bid + ".png")

    async def asyncTearDown(self):
        self.demo.INDEX.cancel()
        await asyncio.sleep(0)
        self.demo.INDEX, self.demo.event, self.demo._icon_path = self.saved_index, self.saved_event, self.saved_icon_path

    async def test_shape_icons_and_sizes(self):
        r = await self.demo.apps_list({"icons": True})
        self.assertEqual(set(r), {"apps", "stale", "fetchedAt"})
        self.assertFalse(r["stale"])
        self.assertEqual(len(r["apps"]), len(self.core.installed))
        a = r["apps"][0]
        for k in ("bundleId", "name", "version", "build", "developer", "removable", "hidden", "system", "executable",
                  "path", "size", "icon", "running", "pid", "fileSharing", "debuggable"):
            self.assertIn(k, a)
        self.assertIsNone(a["icon"])
        self.assertIsNone(a["size"])
        running = [x for x in r["apps"] if x["running"]]
        self.assertTrue(running and all(x["pid"] for x in running))
        await self.demo.INDEX.idle()
        icon_events = [d for n, d in self.events if n == "appIcons"]
        self.assertTrue(icon_events)
        self.assertLess(len(icon_events), len(r["apps"]))
        self.assertNotIn("appIcon", [n for n, _ in self.events])
        r2 = await self.demo.apps_list({"icons": True, "sizes": True})
        self.assertTrue(all(x["icon"] for x in r2["apps"]))
        await self.demo.INDEX.idle()
        sizes = [d for n, d in self.events if n == "appSizes"]
        self.assertEqual(len(sizes), 1)
        r3 = await self.demo.apps_list({"icons": False})
        self.assertTrue(all(x["size"] for x in r3["apps"]))

    async def test_force_is_stale_and_install_invalidates(self):
        await self.demo.apps_list({"icons": False})
        r = await self.demo.apps_list({"icons": False, "force": True})
        self.assertTrue(r["stale"])
        await asyncio.sleep(0.05)
        self.assertEqual([n for n, _ in self.events], [])    # refreshed, nothing changed: nothing to send
        bid = r["apps"][0]["bundleId"]
        self.core.installed.discard(bid)
        try:
            self.demo.INDEX.invalidate(self.core.UDID)
            r = await self.demo.apps_list({"icons": False})
            self.assertFalse(r["stale"])
            self.assertNotIn(bid, [x["bundleId"] for x in r["apps"]])
        finally:
            self.core.installed.add(bid)
        r = await self.demo.apps_list({"icons": False, "force": True})
        await asyncio.sleep(0.05)
        changed = [d for n, d in self.events if n == "apps"]
        self.assertEqual(len(changed), 1)
        self.assertIn(bid, [x["bundleId"] for x in changed[0]["apps"]])


if __name__ == "__main__":
    unittest.main()


class FakePmd3:
    """Just enough pymobiledevice3 for apps.list: installation_proxy Lookup, the process list, icons."""

    def __init__(self, apps: dict, running: dict[str, int]):
        self.apps, self.running = apps, running
        self.lookups: list[dict] = []
        self.icon_fetches = 0
        pm = self

        class Svc:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        class IP(Svc):
            async def lookup(self, opts):
                pm.lookups.append(opts)
                if "StaticDiskUsage" in opts["ReturnAttributes"]:
                    return {b: {"StaticDiskUsage": 1000, "DynamicDiskUsage": 1} for b in pm.apps}
                return {b: {k: v for k, v in i.items() if k in opts["ReturnAttributes"]} for b, i in pm.apps.items()}

        class AppSvc(Svc):
            async def list_processes(self):
                return [{"processIdentifier": pid, "executableURL": {"relative": f"file://{path}/X"}}
                        for path, pid in pm.running.items()] + [{"processIdentifier": 1, "executableURL": "/sbin/launchd"}]

        class Icon:
            png_data = b"\x89PNG"

        class IconSvc(Svc):
            async def fetch_icon(self, **kw):
                pm.icon_fetches += 1
                return Icon()

        self.InstallationProxyService, self.AppServiceService, self.IconService = IP, AppSvc, IconSvc


class RealHandler(unittest.IsolatedAsyncioTestCase):
    """features/apps.py's apps.list against a fake phone: calls made and events sent."""

    async def asyncSetUp(self):
        from unittest import mock

        from iosstudio.device import deviced_core
        from iosstudio.device.deviced_kit import FeatureRegistry
        from iosstudio.device.features import apps as A
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.index = AI.AppIndex(self.tmp)
        self.events: list[tuple[str, dict]] = []
        for target, attr, val in [(A, "INDEX", self.index), (A, "emit", lambda n, d: self.events.append((n, d))),
                                  (deviced_core, "ICONS_DIR", self.tmp / "icons"), (deviced_core, "ONCE", False)]:
            p = mock.patch.object(target, attr, val)
            p.start()
            self.addCleanup(p.stop)
        reg = FeatureRegistry(log=lambda *_: None)
        m = reg.module("apps")
        A.register(m)
        m.commit()
        self.handler = reg.lookup("apps.list")
        apps = {f"com.example.app{i}": {"CFBundleDisplayName": f"App {i:02}", "ApplicationType": "User",
                                        "Path": f"/private/var/containers/Bundle/Application/{i}/App{i}.app",
                                        "Junk": "x" * 100} for i in range(20)}
        self.pmd3 = FakePmd3(apps, {"/var/containers/Bundle/Application/3/App3.app": 333})
        session = type("S", (), {"udid": UDID})()

        async def get_rsd():
            return "rsd"
        session.get_rsd = get_rsd

        async def rsd(a):
            return session, "rsd"
        self.d = type("D", (), {"pmd3": self.pmd3, "rsd": staticmethod(rsd)})()

    async def asyncTearDown(self):
        self.index.cancel()
        await asyncio.sleep(0)

    async def test_first_open_then_cached(self):
        r = await self.handler(self.d, {"icons": True})
        self.assertEqual(set(r), {"apps", "stale", "fetchedAt"})
        self.assertFalse(r["stale"])
        self.assertEqual(len(self.pmd3.lookups), 1)                    # one Lookup, limited attributes, no sizes
        self.assertEqual(self.pmd3.lookups[0]["ReturnAttributes"], AI.ATTRS)
        self.assertNotIn("Junk", json_keys(r["apps"]))
        app3 = next(a for a in r["apps"] if a["bundleId"] == "com.example.app3")
        self.assertEqual((app3["running"], app3["pid"]), (True, 333))
        self.assertEqual(sum(a["running"] for a in r["apps"]), 1)
        await self.index.idle()
        names = [n for n, _ in self.events]
        self.assertEqual(set(names), {"appIcons"})
        self.assertLessEqual(len(names), 4)                            # 20 icons, not 20 events
        self.assertEqual(sum(len(d["icons"]) for _, d in self.events), 20)
        self.assertEqual(self.pmd3.icon_fetches, 20)

        r2 = await self.handler(self.d, {"icons": True})               # second open: no Lookup, no icon fetch
        self.assertEqual(len(self.pmd3.lookups), 1)
        self.assertTrue(all(a["icon"] for a in r2["apps"]))
        await self.index.idle()
        self.assertEqual(self.pmd3.icon_fetches, 20)

    async def test_sizes_only_when_asked_and_cached(self):
        await self.handler(self.d, {"icons": False})
        await self.index.idle()
        self.assertEqual(self.events, [])
        await self.handler(self.d, {"icons": False, "sizes": True})
        await self.index.idle()
        self.assertEqual([n for n, _ in self.events], ["appSizes"])
        self.assertEqual(self.events[0][1]["sizes"]["com.example.app1"], 1001)
        self.assertEqual(len(self.pmd3.lookups), 2)
        self.assertEqual(self.pmd3.lookups[1]["ReturnAttributes"], AI.SIZE_ATTRS)
        r = await self.handler(self.d, {"icons": False, "sizes": True})
        self.assertTrue(all(a["size"] == 1001 for a in r["apps"]))
        self.assertEqual(len(self.pmd3.lookups), 2)


def json_keys(apps):
    return {k for a in apps for k in a}
