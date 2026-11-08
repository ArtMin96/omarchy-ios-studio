"""MCP side of screenshot compare (mcp/iosstudio_mcp/tools/screenshots.py): compare/save results, capped regions,
the small inline image, baselines list/delete (dry run), and the matrix job (shots, failures, paging, cancel,
putting the app back), with result sizes checked against the output budget. dcall and helper.call are mocked;
the helper parts run for real where a test says so.

Needs the MCP server's dependencies (the dev venv; skipped otherwise):
    python -m pytest tests/mcp/test_mcp_screenshots.py
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mcp.server.mcpserver.exceptions import ToolError

from iosstudio_mcp.core import budget as B
from iosstudio_mcp.state import AppState
from iosstudio_mcp.tools import screenshots as T
from support import ROOT
from support.mcp_fakes import FakeMCP, size_of
from support.waiting import until

FIX = ROOT / "tests" / "fixtures" / "screenshots"
HOME = str(Path.home())
PROJECT = {"name": "MyApp", "path": HOME + "/Developer/MyApp", "bundleId": "com.example.myapp"}


def compare_data(n_regions: int = 3, verdict: str = "changed") -> dict:
    regs = [{"x": i * 10, "y": i * 20, "w": 300 - i, "h": 40, "pixels": 5000 - i} for i in range(n_regions)]
    p = HOME + "/.local/share/ios-studio/screenshots/MyApp-1234abcd/"
    return {"name": "Home", "verdict": verdict, "diff_percent": 1.25, "threshold": 0.1, "changed_pixels": 4000,
            "width": 1206, "height": 2622, "masked_top": 162, "regions": regs, "regions_total": n_regions,
            "baseline_path": p + "baselines/Home.png", "current_path": p + "compare/x-Home/current.png",
            "diff_path": str(FIX / "label.png"), "baseline_created": 1793000000.0}


class Ctx(SimpleNamespace):
    async def report_progress(self, *a, **k):
        pass


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        mcp = FakeMCP()
        with mock.patch.object(T, "tool", lambda m, *a, **k: m.tool(**k)):   # the bare functions: models, not results
            T.register(mcp)
        self.tools = mcp.tools
        self.state = AppState()
        self.ctx = Ctx(request_context=SimpleNamespace(lifespan_context=self.state))
        self.hcalls: list = []
        self.dcalls: list = []
        self.helper_replies: dict = {}
        self.patches = [
            mock.patch.object(T, "resolve_project", self.resolve),
            mock.patch.object(T.helper, "call", self.hcall),
            mock.patch.object(T, "dcall", self.dcall),
        ]
        for p in self.patches:
            p.start()

    async def asyncTearDown(self):
        for p in reversed(self.patches):
            p.stop()

    async def resolve(self, state, project):
        return dict(PROJECT)

    async def hcall(self, group, action, flags=None, *, timeout=25, stdin=None):
        self.hcalls.append((action, dict(flags or {})))
        r = self.helper_replies[action]
        r = r(flags) if callable(r) else r
        if isinstance(r, Exception):
            raise r
        return r

    async def dcall(self, state, cmd, args=None, *, timeout=60.0, udid=None, hints=None):
        self.dcalls.append((cmd, dict(args or {})))
        if cmd == "screenshot":
            return {"path": str(FIX / "base.png"), "width": 120, "height": 260}
        return {}


class Compare(Base):
    async def test_changed_without_image(self):
        self.helper_replies["compare"] = compare_data()
        res = await self.tools["ios_screenshot_compare"](self.ctx, project_path="MyApp", name="Home")
        self.assertFalse(any(c.type == "image" for c in res.content))
        sc = res.structured_content
        self.assertEqual((sc["verdict"], sc["regions_total"], len(sc["regions"])), ("changed", 3, 3))
        self.assertEqual(sc["current_path"], "~/.local/share/ios-studio/screenshots/MyApp-1234abcd/compare/x-Home/current.png")
        flags = self.hcalls[-1][1]
        self.assertEqual((flags["shot"], flags["threshold"], flags["mask_top"]), (str(FIX / "base.png"), 0.1, "auto"))
        self.assertEqual(self.dcalls[0][0], "screenshot")

    async def test_many_regions_capped_and_small(self):
        self.helper_replies["compare"] = compare_data(50)
        res = await self.tools["ios_screenshot_compare"](self.ctx, project_path="MyApp", name="Home")
        sc = res.structured_content
        self.assertEqual(len(sc["regions"]), T.REGIONS_CAP)
        self.assertEqual((sc["regions_total"], sc["truncated"], sc["omitted"]), (50, True, 40))
        self.assertIn("not listed", sc["note"])
        self.assertLess(size_of(res), 6000)

    async def test_include_image_is_small(self):
        big = Path(tempfile.mkdtemp()) / "diff.png"
        try:
            from PIL import Image
            await asyncio.to_thread(lambda: Image.new("RGB", (1206, 2622), (40, 40, 40)).save(big))
            self.helper_replies["compare"] = {**compare_data(), "diff_path": str(big)}
            res = await self.tools["ios_screenshot_compare"](self.ctx, project_path="MyApp", name="Home",
                                                             include_image=True)
            imgs = [c for c in res.content if c.type == "image"]
            self.assertEqual(len(imgs), 1)
            import base64
            data = base64.b64decode(imgs[0].data)
            self.assertLessEqual(len(data), T.IMAGE_BYTES)
            self.assertLessEqual(max(B.image_size(data)), T.IMAGE_DIM)
            self.assertTrue(res.structured_content["inline_image"])
        finally:
            shutil.rmtree(big.parent)

    async def test_save_baseline_and_no_baseline(self):
        self.helper_replies["save"] = {"name": "Home", "baseline_path": "/x/Home.png", "width": 120, "height": 260,
                                       "replaced": True, "created": 1793000000.0}
        res = await self.tools["ios_screenshot_compare"](self.ctx, project_path="MyApp", name="Home",
                                                         save_baseline=True, udid="U1")
        sc = res.structured_content
        self.assertEqual((sc["verdict"], sc["replaced"]), ("saved", True))
        self.assertEqual(self.hcalls[-1][1]["device"], "U1")
        self.assertEqual(self.hcalls[-1][1]["bundle"], "com.example.myapp")
        self.helper_replies["compare"] = {**compare_data(0, "no-baseline"), "diff_path": None, "diff_percent": None}
        res = await self.tools["ios_screenshot_compare"](self.ctx, project_path="MyApp", name="Home")
        self.assertIn("save_baseline=true", res.structured_content["next_steps"][0])

    async def test_shot_path(self):
        self.helper_replies["compare"] = compare_data()
        with mock.patch.object(T.helper, "inside_home", return_value=True):
            await self.tools["ios_screenshot_compare"](self.ctx, project_path="MyApp", name="Home",
                                                       shot_path=str(FIX / "label.png"))
        self.assertEqual(self.hcalls[-1][1]["shot"], str(FIX / "label.png"))
        self.assertEqual(self.dcalls, [])
        with self.assertRaises(ToolError):
            await self.tools["ios_screenshot_compare"](self.ctx, project_path="MyApp", name="Home",
                                                       shot_path="/nope/x.png")

    async def test_real_helper_end_to_end(self):
        """The real helper CLI (pure-Python codec) behind the tool, on the fixtures."""
        t = tempfile.mkdtemp()
        proj = Path(t) / "MyApp"
        proj.mkdir()
        env = {"XDG_DATA_HOME": t + "/data", "IOSSTUDIO_SCREENSHOTS_PURE": "1", "IOSSTUDIO_DEMO": ""}
        self.patches[1].stop()
        try:
            with mock.patch.dict(os.environ, env), \
                    mock.patch.object(T, "resolve_project", lambda s, p: asyncio.sleep(0, {**PROJECT, "path": str(proj)})):
                r = await self.tools["ios_screenshot_compare"](self.ctx, project_path="MyApp", name="Home",
                                                               save_baseline=True)
                self.assertEqual(r.structured_content["verdict"], "saved")
                with mock.patch.object(T.helper, "inside_home", return_value=True):
                    r = await self.tools["ios_screenshot_compare"](self.ctx, project_path="MyApp", name="Home",
                                                                   shot_path=str(FIX / "label.png"))
                sc = r.structured_content
                self.assertEqual((sc["verdict"], sc["regions"][0]["w"]), ("changed", 30))
                lst = await self.tools["ios_screenshot_baselines"](self.ctx, project_path="MyApp")
                self.assertEqual([b.name for b in lst.baselines], ["Home"])
        finally:
            self.patches[1].start()
            shutil.rmtree(t)


class Baselines(Base):
    async def test_list(self):
        self.helper_replies["list"] = {"baselines": [{"name": f"Screen{i}", "path": f"/x/{i}.png", "created": 1.0e9,
                                                      "width": 1206, "height": 2622} for i in range(300)]}
        res = await self.tools["ios_screenshot_baselines"](self.ctx, project_path="MyApp")
        r = T.to_result(res)
        self.assertLessEqual(size_of(r), B.DEFAULT_BYTES)
        self.assertTrue(res.truncated)
        self.assertEqual(res.total, 300)

    async def test_delete_is_dry_run(self):
        self.helper_replies["delete"] = lambda f: {"name": "Home", "baseline_path": "/x/Home.png",
                                                   "deleted": bool(f.get("confirm")), "dryRun": not f.get("confirm"),
                                                   "compare_runs": 3, "compare_bytes": 120_000}
        res = await self.tools["ios_screenshot_baseline_delete"](self.ctx, project_path="MyApp", name="Home")
        self.assertFalse(res.done)
        self.assertEqual(len(res.would), 2)
        self.assertIn("3 kept compare run(s)", res.would[1])
        self.assertIsNone(self.hcalls[-1][1]["confirm"])
        res = await self.tools["ios_screenshot_baseline_delete"](self.ctx, project_path="MyApp", name="Home",
                                                                 confirm=True)
        self.assertTrue(res.done)
        self.assertIn("3 compare run(s)", res.message)
        self.assertTrue(self.hcalls[-1][1]["confirm"])
        with self.assertRaises(TypeError):        # name is required (MCP validation rejects the call)
            await self.tools["ios_screenshot_baseline_delete"](self.ctx, project_path="MyApp")


class Matrix(Base):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.running = False
        self.fail_launch: set = set()
        self.slow = 0.0
        self.helper_replies["matrix-plan"] = self.plan
        self.helper_replies["matrix-finish"] = lambda f: {"state": f["state"]}
        self.helper_replies["matrix-save"] = lambda f: {"config": f["config"], "name": f["name"],
                                                        "path": None if f.get("error") else
                                                        HOME + f"/.local/share/ios-studio/screenshots/MyApp-1/matrix/"
                                                        f"S1/{f['config']}/{f['name']}.png",
                                                        "width": 1206, "height": 2622, "error": f.get("error")}
        self.patches += [mock.patch.object(T, "apps", self.apps), mock.patch.object(T, "launch", self.launch)]
        for p in self.patches[-2:]:
            p.start()

    def plan(self, flags):
        cfgs = json.loads(flags["configs"]) if flags.get("configs") else [{"name": "default"}]
        names = json.loads(flags["names"]) if flags.get("names") else ["launch"]
        return {"set_id": "S1", "set_dir": HOME + "/.local/share/ios-studio/screenshots/MyApp-1/matrix/S1",
                "manifest_path": HOME + "/.local/share/ios-studio/screenshots/MyApp-1/matrix/S1/manifest.json",
                "project": PROJECT["path"],
                "configs": [{"key": c.get("preset") or c.get("name") or c.get("language"), "args": ["-A"], "env": {}}
                            for c in cfgs],
                "names": [{"name": n, "press": None, "wait": 0} if isinstance(n, str) else
                          {"name": n["name"], "press": n.get("press"), "wait": 0} for n in names]}

    async def apps(self, state, udid=None, system=False):
        return [{"bundleId": "XTL-T.com.example.myapp", "name": "MyApp", "running": self.running}]

    async def launch(self, state, udid, bundle_id, args=None, env=None, console=False):
        self.dcalls.append(("launch", {"bundleId": bundle_id, "args": args}))
        if args and any(a in self.fail_launch for a in args):
            raise ToolError("Launch failed: locked.")
        await asyncio.sleep(self.slow)
        return {"pid": 1}

    def finish(self) -> dict:
        calls = [f for a, f in self.hcalls if a == "matrix-finish"]
        self.assertEqual(len(calls), 1)
        return calls[0]

    async def start(self, **kw):
        kw.setdefault("project_path", "MyApp")
        kw.setdefault("settle_seconds", 0)
        return await self.tools["ios_screenshot_matrix"](self.ctx, **kw)

    async def test_big_matrix_stays_small(self):
        cfgs = [{"name": f"Config{i:02d}", "language": "de"} for i in range(12)]
        names = [f"Screen{i:02d}" for i in range(12)] [:11] + [{"name": "Settings", "press": "Settings"}]
        res = await self.start(configs=cfgs, names=names, wait_seconds=10)
        self.assertEqual((res.state, res.total, res.captured), ("succeeded", 144, 144))
        r = T.to_result(res)
        self.assertLessEqual(size_of(r), B.DEFAULT_BYTES)
        self.assertLessEqual(len(res.shots), T.SHOTS_PAGE)
        self.assertEqual(res.next_offset, len(res.shots))
        self.assertTrue(res.full_output_path.endswith("manifest.json"))
        self.assertIn(("ui.press", {"label": "Settings", "match": "exact", "bundleId": "XTL-T.com.example.myapp"}),
                      self.dcalls)
        page = await self.tools["ios_screenshot_matrix"](self.ctx, job_id=res.job_id, offset=140)
        self.assertEqual((len(page.shots), page.next_offset), (4, None))
        self.assertEqual(self.dcalls[-1], ("app.kill", {"bundleId": "XTL-T.com.example.myapp"}))   # wasn't running
        self.assertEqual(self.finish(), {"project": PROJECT["path"], "set": "S1", "state": "done", "error": None,
                                         "restored": res.app_restored})

    async def test_running_poll_is_tiny(self):
        self.slow = 0.5
        res = await self.start(presets=["German", "French"], names=["Home"], wait_seconds=0.1)
        self.assertEqual(res.state, "running")
        self.assertEqual(res.shots, [])
        self.assertLess(size_of(T.to_result(res)), 1500)
        done = await self.tools["ios_screenshot_matrix"](self.ctx, job_id=res.job_id, wait_seconds=10)
        self.assertEqual((done.state, done.captured), ("succeeded", 2))
        self.assertEqual([c for c in done.configs], ["German", "French"])

    async def test_launch_failure_is_recorded_and_app_relaunched(self):
        self.running = True
        self.fail_launch = {"-A"}
        res = await self.start(configs=[{"name": "A"}], names=["Home", "Settings"], wait_seconds=10)
        self.assertEqual((res.state, res.captured, res.failed), ("succeeded", 0, 2))
        self.assertTrue(all("Launch failed" in s.error for s in res.shots))
        self.assertEqual(self.dcalls[-1], ("app.launch", {"bundleId": "XTL-T.com.example.myapp"}))
        self.assertIn("relaunched", res.app_restored)

    async def test_cancel_keeps_partial_results(self):
        self.slow = 0.3
        res = await self.start(configs=[{"name": f"C{i}"} for i in range(6)], names=["Home"], wait_seconds=0.5)
        self.assertEqual(res.state, "running")
        job = await self.state.jobs.cancel(self.state.jobs.get(res.job_id))     # ios_job_cancel
        self.assertEqual(job.state, "cancelled")
        res = await self.tools["ios_screenshot_matrix"](self.ctx, job_id=res.job_id)
        self.assertEqual(res.state, "cancelled")
        self.assertLess(res.captured, 6)
        self.assertEqual(len(res.shots), res.captured + res.failed)
        self.assertIn("stopped", res.app_restored)
        self.assertEqual(self.dcalls[-1][0], "app.kill")
        f = self.finish()
        self.assertEqual((f["state"], f["restored"]), ("cancelled", res.app_restored))

    async def test_app_not_installed(self):
        self.patches[-2].stop()
        with mock.patch.object(T, "apps", lambda *a, **k: asyncio.sleep(0, [])):
            res = await self.start(wait_seconds=5)
        self.patches[-2].start()
        self.assertEqual(res.state, "failed")
        self.assertIn("ios_run", res.error)
        f = self.finish()
        self.assertEqual((f["state"], f["error"]), ("failed", res.error))

    async def test_errors(self):
        with self.assertRaises(ToolError):
            await self.tools["ios_screenshot_matrix"](self.ctx, job_id="matrix-99")
        with self.assertRaises(ToolError):
            await self.tools["ios_screenshot_matrix"](self.ctx)

    async def test_shutdown_cancels(self):
        self.slow = 1.0
        res = await self.start(configs=[{"name": f"C{i}"} for i in range(5)], wait_seconds=0)
        await self.state.jobs.shutdown()
        job = self.state.jobs.get(res.job_id)
        self.assertEqual(job.state, "cancelled")
        # cancelled before its first step: the manifest is finished too
        await until(lambda: any(a == "matrix-finish" for a, _ in self.hcalls), what="matrix-finish")
        self.assertEqual(self.finish()["state"], "cancelled")


if __name__ == "__main__":
    unittest.main()
