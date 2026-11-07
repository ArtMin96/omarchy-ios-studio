"""tools/toolchain.py against a fake iosstudio-helper (real helper.call / spawn_stream, real @tool wrapper):
ios_toolchain_check, ios_toolchain_info (a job), ios_toolchain_install (a job that asks first) and
ios_toolchain_clean (destructive: a dry run until confirm=true).

    python -m pytest tests/mcp/test_mcp_cov_toolchain.py
"""
from __future__ import annotations

from iosstudio_mcp.core import budget as B
from iosstudio_mcp.tools import jobs as JOBS
from iosstudio_mcp.tools import toolchain as M
from support.mcp_harness import FakeHelper, ToolCase, size_of

MB = 1024 * 1024

CHECK = {"summary": {"ready": False, "title": "2 steps left", "done": 3, "total": 5,
                     "essentialMissing": ["xtool", "sdk"], "autoInstallable": ["xtool"]},
         "steps": [{"id": "swift", "title": "Swift", "group": "Toolchain", "state": "done", "essential": True},
                   {"id": "xtool", "title": "xtool", "group": "Toolchain", "state": "missing", "essential": True,
                    "canAutoInstall": True, "size": "40 MB"},
                   {"id": "sdk", "title": "iOS SDK", "group": "Toolchain", "state": "missing", "essential": True,
                    "interactive": True, "action": {"kind": "page", "page": "setup", "label": "Get Xcode"}}]}

DISK = {"items": [{"id": "xip", "name": "Xcode download", "size": 3000 * MB, "cleanable": True, "safe": True,
                   "paths": [{"path": "/home/u/.cache/xcode.xip", "size": 3000 * MB}], "note": "re-download later"},
                  {"id": "sdk", "name": "iOS SDK", "size": 5000 * MB, "cleanable": False, "note": "in use"},
                  {"id": "risky", "name": "Old builds", "size": 10 * MB, "cleanable": True, "safe": False,
                   "paths": [{"path": "/home/u/x", "size": 10 * MB}]}],
        "total": 8010 * MB, "reclaimable": 3000 * MB, "free": 20000 * MB}


class Annotations(ToolCase):
    DAEMON = False

    def test_kinds(self):
        ann = self.rec(M).annotations
        for name in ("ios_toolchain_check", "ios_toolchain_info"):
            self.assertTrue(ann[name].read_only_hint, name)
        self.assertFalse(ann["ios_toolchain_install"].read_only_hint)
        self.assertFalse(ann["ios_toolchain_install"].destructive_hint)
        self.assertTrue(ann["ios_toolchain_install"].open_world_hint)        # downloads
        self.assertTrue(ann["ios_toolchain_clean"].destructive_hint)


class Check(ToolCase):
    DAEMON = False

    async def test_missing_steps_with_auto_and_human_steps(self):
        self.helper.reply("setup", "check", CHECK)
        res = await self.call(M, "ios_toolchain_check")
        d = res.structured_content
        self.assertFalse(d["ready"])
        self.assertEqual([s["id"] for s in d["steps"]], ["xtool", "sdk"])      # done steps hidden
        self.assertTrue(d["steps"][0]["can_auto_install"])
        self.assertIn("ios_open_app(page='setup')", d["steps"][1]["human_step"])
        self.assertIn("ios_toolchain_install(all=true)", d["next_steps"][0])
        self.assertLessEqual(size_of(res), B.DEFAULT_BYTES)

    async def test_include_done(self):
        self.helper.reply("setup", "check", CHECK)
        d = await self.data(M, "ios_toolchain_check", include_done=True)
        self.assertEqual(len(d["steps"]), 3)

    async def test_failure_is_one_line_with_next_step(self):
        self.helper.fail("setup", "check", "needs-step", "Setup state is unreadable.")
        text = await self.error(M, "ios_toolchain_check")
        self.assertIn("[needs-step] Next: Run ios_toolchain_check", text)


class Info(ToolCase):
    DAEMON = False

    async def test_info_is_a_job_pollable_with_ios_job_status(self):
        self.helper.reply("toolchain", "info", {"items": [{"id": "xtool", "name": "xtool", "version": "1.2"}],
                                                "sdks": {"iPhoneOS": "26.0"}}, delay=0.5)
        self.helper.reply("toolchain", "disk", DISK)
        first = await self.data(M, "ios_toolchain_info", wait_seconds=0)
        self.assertEqual(first["state"], "running")
        st = await self.data(JOBS, "ios_job_status", job_id=first["job_id"], wait_seconds=3)
        self.assertEqual(st["state"], "succeeded")
        res = await self.call(M, "ios_toolchain_info", job_id=first["job_id"])
        d = res.structured_content
        self.assertEqual(d["tools"][0]["version"], "1.2")
        self.assertEqual(d["reclaimable_mb"], 3000.0)
        self.assertIn("ios_toolchain_clean", d["next_steps"][0])
        self.assertLessEqual(size_of(res), B.DEFAULT_BYTES)
        self.assertEqual(FakeHelper.flag(self.helper.calls("toolchain", "disk")[0], "cached"), "")

    async def test_failed_versions_fail_the_call_in_one_line(self):
        self.helper.fail("toolchain", "info", "missing-tool", "swift is missing.")
        text = await self.error(M, "ios_toolchain_info")
        self.assertIn("swift is missing.", text)

    async def test_a_job_of_another_kind_is_refused(self):
        job = self.state.jobs.run(lambda j: _noop(), "install", title="x")
        text = await self.error(M, "ios_toolchain_info", job_id=job.id)
        self.assertIn("toolchain-info", text)


async def _noop():
    return {}


class Install(ToolCase):
    DAEMON = False

    async def test_step_or_all_is_required(self):
        text = await self.error(M, "ios_toolchain_install")
        self.assertIn("step='<id>'", text)
        text = await self.error(M, "ios_toolchain_install", step="xtool", all=True)
        self.assertIn("all=true", text)

    async def test_install_asks_first_then_runs_after_the_answer(self):
        self.helper.stream("setup", "install", [
            {"event": "prompt", "id": "confirm", "title": "Install xtool?", "message": "40 MB",
             "options": [{"value": "yes", "label": "Install"}, {"value": "no", "label": "Cancel"}]},
        ])
        first = await self.data(M, "ios_toolchain_install", step="xtool", wait_seconds=2)
        self.assertEqual(first["state"], "needs_input")
        self.assertTrue(first["job_id"].startswith("t-"))
        self.assertEqual(first["pending_prompt"]["id"], "confirm")
        argv = self.helper.calls("setup", "install")[0]
        self.assertEqual(FakeHelper.flag(argv, "step"), "xtool")
        self.assertIsNone(FakeHelper.flag(argv, "all"))
        # a second install while one runs is refused, pointing at the running job
        text = await self.error(M, "ios_toolchain_install", all=True)
        self.assertIn(first["job_id"], text)
        st = await self.data(JOBS, "ios_job_status", job_id=first["job_id"], wait_seconds=0)
        self.assertEqual(st["state"], "needs_input")
        await self.data(JOBS, "ios_job_cancel", job_id=first["job_id"])

    async def test_include_optional_only_with_all(self):
        self.helper.stream("setup", "install", [{"event": "done", "ok": True, "data": {}}])
        d = await self.data(M, "ios_toolchain_install", step="xtool", include_optional=True, wait_seconds=3)
        self.assertEqual(d["state"], "succeeded")
        self.assertIsNone(FakeHelper.flag(self.helper.calls("setup", "install")[0], "include_optional"))

    async def test_a_failed_install_reports_its_error(self):
        self.helper.stream("setup", "install", [
            {"event": "done", "ok": False, "error": {"code": "pacman-locked", "message": "pacman is busy"}}])
        d = await self.data(M, "ios_toolchain_install", all=True, wait_seconds=3)
        self.assertEqual(d["state"], "failed")
        self.assertIn("pacman is busy", d["error"]["message"])


class Clean(ToolCase):
    DAEMON = False

    async def test_dry_run_without_confirm(self):
        self.helper.reply("toolchain", "disk", DISK)
        d = await self.data(M, "ios_toolchain_clean", item="xip")
        self.assertFalse(d["done"])
        self.assertIn("3000 MB", d["message"])
        self.assertIn("confirm=true", d["next_steps"][0])
        self.assertFalse(self.helper.calls("toolchain", "clean"))

    async def test_an_unsafe_item_warns_in_its_dry_run(self):
        self.helper.reply("toolchain", "disk", DISK)
        d = await self.data(M, "ios_toolchain_clean", item="risky")
        self.assertIn("NOT marked safe", d["message"])

    async def test_confirm_deletes_through_a_job(self):
        self.helper.reply("toolchain", "disk", DISK)
        self.helper.stream("toolchain", "clean", [{"event": "done", "ok": True, "data": {"freed": 3000 * MB}}])
        d = await self.data(M, "ios_toolchain_clean", item="xip", confirm=True, wait_seconds=3)
        self.assertTrue(d["done"])
        self.assertIn("freed 3000 MB", d["message"])
        self.assertEqual(FakeHelper.flag(self.helper.calls("toolchain", "clean")[0], "item"), "xip")

    async def test_a_slow_clean_returns_its_job_id(self):
        self.helper.reply("toolchain", "disk", DISK)
        self.helper.stream("toolchain", "clean", [{"event": "done", "ok": True, "data": {}}], delay=1)
        d = await self.data(M, "ios_toolchain_clean", item="xip", confirm=True, wait_seconds=0)
        self.assertTrue(d["data"]["job_id"].startswith("t-"))
        self.assertIn("ios_job_status", d["message"])

    async def test_unknown_and_in_use_items_are_refused(self):
        self.helper.reply("toolchain", "disk", DISK)
        text = await self.error(M, "ios_toolchain_clean", item="nope")
        self.assertIn("Items: xip, sdk, risky (see ios_toolchain_info)", text)
        text = await self.error(M, "ios_toolchain_clean", item="sdk", confirm=True)
        self.assertIn("can't be cleaned (in use)", text)
        self.assertFalse(self.helper.calls("toolchain", "clean"))

    async def test_a_failed_clean_is_one_line(self):
        self.helper.reply("toolchain", "disk", DISK)
        self.helper.stream("toolchain", "clean", [
            {"event": "done", "ok": False, "error": {"code": "failed", "message": "permission denied"}}])
        text = await self.error(M, "ios_toolchain_clean", item="xip", confirm=True, wait_seconds=3)
        self.assertIn("Cleaning failed: permission denied", text)
