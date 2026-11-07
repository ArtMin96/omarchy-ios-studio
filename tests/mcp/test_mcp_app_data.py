"""MCP side of app data (mcp/iosstudio_mcp/tools/app_data.py): ios_app_defaults read under the byte budget with
full_output_path, dry runs before set/delete, the app-running next step, ios_app_store summary and paging,
ios_app_reset dry run and its job. dcall is routed to the demo daemon's feature modules (real made-up data in a
temp folder), so the whole path from tool to helper/iosstudio/features/app_data.py runs.

Needs the MCP server's dependencies (the dev venv; skipped otherwise):
    python -m pytest tests/mcp/test_mcp_app_data.py
"""
from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mcp.server.mcpserver.exceptions import ToolError

from iosstudio.core.output import HelperError
from iosstudio.features import app_data as AD
from iosstudio_mcp.backends import device as D
from iosstudio_mcp.core import budget as B
from iosstudio_mcp.core.errors import device_error
from iosstudio_mcp.state import AppState
from iosstudio_mcp.tools import app_data as T
from support.mcp_fakes import FakeMCP, size_of

HABITS = "XTL-A1B2C3D4E5.com.example.habits"
WEATHER = "XTL-A1B2C3D4E5.com.example.weatherglance"


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from iosstudio.device.demo import core
        from iosstudio.device.features.appdata import demo
        from iosstudio.device.features.apps import demo as demo_apps
        from iosstudio.device.features.files import demo as demo_files
        from iosstudio.device.kit import FeatureRegistry
        self.core, self.demo = core, demo
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        for target, attr, val in [(demo_files, "ROOT", self.tmp / "containers"), (AD, "CACHE", self.tmp / "cache"),
                                  (B, "CACHE", self.tmp / "mcp-cache"), (demo, "INSTALL_STEP", 0)]:
            p = mock.patch.object(target, attr, val)
            p.start()
            self.addCleanup(p.stop)
        saved = (set(core.running), set(core.installed), set(demo._seeded))
        demo._seeded.clear()

        def restore():
            core.running.clear()
            core.running.update(saved[0])
            core.installed.clear()
            core.installed.update(saved[1])
            demo._seeded.clear()
            demo._seeded.update(saved[2])
        self.addCleanup(restore)
        core.running.discard(HABITS)
        core.running.add(WEATHER)
        self.reg = FeatureRegistry(log=lambda *_: None)
        for name, mod in (("appdata", demo), ("apps", demo_apps)):
            m = self.reg.module(name)
            mod.register(m)
            m.commit()
        mcp = FakeMCP()
        with mock.patch.object(T, "tool", lambda m, *a, **k: m.tool(**k)):
            T.register(mcp)
        self.tools = mcp.tools
        self.state = AppState()
        self.ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=self.state))
        self.calls: list = []
        self.delay = 0.0

    async def dcall(self, state, cmd, args=None, *, timeout=60.0, udid=None, hints=None):
        self.calls.append((cmd, dict(args or {})))
        slow = cmd == "appdata.reset" and (args or {}).get("confirm") or \
            cmd.startswith("appdata.defaults.") and not (args or {}).get("dryRun")
        if slow and self.delay:
            await asyncio.sleep(self.delay)
        try:
            r = self.reg.lookup(cmd)(None, dict(args or {}))
            return await r if asyncio.iscoroutine(r) else r
        except HelperError as exc:      # what the real dcall makes of a DeviceError
            raise device_error(SimpleNamespace(code=exc.code, message=exc.message, fix=exc.fix,
                                               detail=exc.detail), hints) from None

    def patches(self):
        return mock.patch.object(T, "dcall", self.dcall), mock.patch.object(D, "dcall", self.dcall)

    async def call(self, name, **kw):
        a, b = self.patches()
        with a, b:
            return await self.tools[name](self.ctx, **kw)

    async def call_wrapped(self, name, **kw):
        """Through the real @tool wrapper (to_result + budget), as the server runs it."""
        from iosstudio_mcp.core.registry import tool as real_tool
        rec = FakeMCP()
        a, b = self.patches()
        with mock.patch.object(T, "tool", lambda m, *a, **k: real_tool(rec, *a, **k)), a, b:
            T.register(rec)
            return await rec.tools[name](self.ctx, **kw)


class Defaults(Base):
    async def test_read_whole(self):
        r = await self.call("ios_app_defaults", bundle_id="com.example.habits")
        self.assertEqual(r.bundle_id, HABITS)
        self.assertTrue(r.exists)
        self.assertEqual(r.values["launchCount"], 42)
        self.assertEqual(r.types["lastOpened"], "date")
        self.assertEqual(r.values["notifications"]["quietHours"], {"from": 22, "to": 7})
        self.assertEqual(r.keys, len(r.values) + len(r.left_out))

    async def test_read_is_budgeted_with_full_output(self):
        res = await self.call_wrapped("ios_app_defaults", bundle_id=HABITS)
        self.assertLessEqual(size_of(res), B.DEFAULT_BYTES)
        sc = res.structured_content
        self.assertTrue(sc["truncated"])                     # the 2 KB blob and the long note don't fit inline
        full = json.loads(Path(sc["full_output_path"]).read_text())
        self.assertEqual(len(full["values"]), sc["keys"])
        self.assertEqual(full["values"]["avatarPNG"]["size"], 2048)
        self.assertIn("key=", sc["note"])
        one = await self.call("ios_app_defaults", bundle_id=HABITS, key="dailyGoal")
        self.assertEqual((one.values, one.shown), ({"dailyGoal": 3}, 1))
        with self.assertRaises(ToolError):
            await self.call("ios_app_defaults", bundle_id=HABITS, key="nope")

    async def test_set_is_a_dry_run_until_confirmed(self):
        dry = await self.call("ios_app_defaults_set", bundle_id=HABITS, action="set", key="dailyGoal", value="5", type="int")
        self.assertFalse(dry.done)
        self.assertEqual((dry.previous, dry.value, dry.type), (3, 5, "int"))
        self.assertIn("Nothing was changed", dry.message)
        self.assertEqual((await self.call("ios_app_defaults", bundle_id=HABITS, key="dailyGoal")).values["dailyGoal"], 3)
        self.assertIn("installed over it", dry.message)
        done = await self.call("ios_app_defaults_set", bundle_id=HABITS, action="set", key="dailyGoal", value="5",
                               type="int", confirm=True)
        self.assertEqual((done.state, done.done, done.method, done.verified), ("succeeded", True, "reinstall", True))
        self.assertTrue(done.artifact.endswith("HabitTracker.ipa"))
        self.assertTrue(done.backup)
        self.assertEqual((await self.call("ios_app_defaults", bundle_id=HABITS, key="dailyGoal")).values["dailyGoal"], 5)
        gone = await self.call("ios_app_defaults_set", bundle_id=HABITS, action="delete", key="dailyGoal", confirm=True)
        self.assertTrue(gone.existed)
        again = await self.call("ios_app_defaults_set", bundle_id=HABITS, action="delete", key="dailyGoal")
        self.assertIn("nothing to delete", again.message)

    async def test_change_runs_as_a_job(self):
        self.delay = 0.3
        r = await self.call("ios_app_defaults_set", bundle_id=HABITS, action="set", key="userName", value="Sam",
                            confirm=True, wait_seconds=0)
        self.assertEqual((r.state, r.done), ("running", False))
        done = await self.call("ios_app_defaults_set", job_id=r.job_id, wait_seconds=5)
        self.assertEqual((done.state, done.key, done.value), ("succeeded", "userName", "Sam"))
        with self.assertRaises(ToolError):
            await self.call("ios_app_defaults_set", job_id="d0-0")

    async def test_batch_without_refresh(self):
        r = await self.call("ios_app_defaults_set", bundle_id=HABITS, action="set", key="userName", value="Kim",
                            confirm=True, refresh=False)
        self.assertEqual((r.method, r.launch_args), ("file", ["-userName", "Kim"]))
        self.assertIn("cached copy", r.warning)
        self.assertTrue(any("ios_app_launch" in n for n in r.next_steps))
        f = await self.call("ios_app_defaults_set", bundle_id=HABITS, action="refresh")
        self.assertEqual((f.state, f.method), ("succeeded", "reinstall"))

    async def test_running_app(self):
        dry = await self.call("ios_app_defaults_set", bundle_id=WEATHER, action="set", key="unit", value="fahrenheit")
        self.assertTrue(dry.running)
        self.assertTrue(any("stop_app=true" in s for s in dry.next_steps))
        with self.assertRaises(ToolError) as c:
            await self.call("ios_app_defaults_set", bundle_id=WEATHER, action="set", key="unit", value="f", confirm=True)
        self.assertIn("[app-running]", str(c.exception))
        self.assertIn("stop_app=true", str(c.exception))
        ok = await self.call("ios_app_defaults_set", bundle_id=WEATHER, action="set", key="unit", value="f",
                             confirm=True, stop_app=True)
        self.assertTrue(ok.stopped_app)

    async def test_errors_have_next_steps(self):
        with self.assertRaises(ToolError) as c:
            await self.call("ios_app_defaults", bundle_id="com.example.podcasts")
        self.assertIn("not-developer", str(c.exception))
        with self.assertRaises(ToolError) as c:
            await self.call("ios_app_defaults_set", bundle_id=HABITS, action="set", key="x", value="abc", type="int",
                            confirm=True)
        self.assertIn("Next:", str(c.exception))
        with self.assertRaises(ToolError):
            await self.call("ios_app_defaults_set", bundle_id=HABITS, action="set", key="x")
        with self.assertRaises(ToolError):
            await self.call("ios_app_defaults", bundle_id="com.example.nothere")


class Store(Base):
    async def test_summary(self):
        r = await self.call("ios_app_store", bundle_id=HABITS)
        self.assertEqual([s.kind for s in r.stores], ["swiftdata", "sqlite"])
        ents = {t.entity: t.rows for t in r.stores[0].tables if not t.meta}
        self.assertEqual(ents["Completion"], 640)
        self.assertIn("ZNAME VARCHAR", next(t for t in r.stores[0].tables if t.entity == "Habit").columns)
        res = await self.call_wrapped("ios_app_store", bundle_id=HABITS, max_bytes=3000)
        self.assertLessEqual(size_of(res), 3000 + 200)
        self.assertTrue(res.structured_content["truncated"])

    async def test_pages_by_entity_name(self):
        res = await self.call_wrapped("ios_app_store", bundle_id=HABITS, table="Completion", limit=200)
        sc = res.structured_content
        self.assertLessEqual(size_of(res), B.DEFAULT_BYTES)
        self.assertEqual((sc["table"], sc["total"]), ("ZCOMPLETION", 640))
        self.assertTrue(sc["has_more"])
        self.assertEqual(sc["next_offset"], sc["count"])
        self.assertLess(sc["count"], 200)                    # cut to the budget, and next_offset follows
        nxt = await self.call("ios_app_store", bundle_id=HABITS, table="ZCOMPLETION", offset=630, limit=20,
                              store="/Library/Application Support/default.store")
        self.assertEqual((nxt.count, nxt.has_more, nxt.next_offset), (10, False, None))
        self.assertEqual(nxt.rows[-1][0], 640)

    async def test_unknown_table(self):
        with self.assertRaises(ToolError) as c:
            await self.call("ios_app_store", bundle_id=HABITS, table="Nope")
        self.assertIn("Completion", str(c.exception))


class Reset(Base):
    async def test_dry_run_lists_items(self):
        r = await self.call("ios_app_reset", bundle_id=HABITS)
        self.assertEqual(r.state, "dry_run")
        self.assertGreater(r.count, 3)
        self.assertTrue(any("default.store" in x for x in r.items))
        self.assertIn("Nothing was changed", r.message)
        self.assertFalse(any(c[0] == "appdata.reset" and c[1].get("confirm") for c in self.calls))
        w = await self.call("ios_app_reset", bundle_id=WEATHER)
        self.assertTrue(any("stop_app=true" in s for s in w.next_steps))

    async def test_wipe(self):
        r = await self.call("ios_app_reset", bundle_id=HABITS, confirm=True)
        self.assertEqual((r.state, r.refreshed), ("succeeded", True))
        self.assertGreater(r.removed, 3)
        d = await self.call("ios_app_defaults", bundle_id=HABITS)
        self.assertFalse(d.exists)
        with self.assertRaises(ToolError) as c:
            await self.call("ios_app_reset", bundle_id=WEATHER, confirm=True)
        self.assertIn("app-running", str(c.exception))

    async def test_reinstall_job(self):
        dry = await self.call("ios_app_reset", bundle_id=HABITS, mode="reinstall")
        self.assertEqual(dry.project, "HabitTracker")
        self.assertTrue(dry.artifact.endswith("HabitTracker.ipa"))
        self.delay = 0.3
        r = await self.call("ios_app_reset", bundle_id=HABITS, mode="reinstall", confirm=True, wait_seconds=0)
        self.assertEqual(r.state, "running")
        self.assertTrue(r.job_id)
        done = await self.call("ios_app_reset", job_id=r.job_id, wait_seconds=5)
        self.assertEqual(done.state, "succeeded")
        self.assertTrue(done.reinstalled)
        with self.assertRaises(ToolError):
            await self.call("ios_app_reset", job_id="r0-0")

    async def test_listed_in_server(self):
        from iosstudio_mcp import server
        self.assertIn("ios_app_reset", server.resources.GUIDE.read_text())
        from iosstudio_mcp.tools import MODULES
        self.assertIn("app_data", MODULES)


if __name__ == "__main__":
    unittest.main()
