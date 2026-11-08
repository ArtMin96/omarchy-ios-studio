"""tools/status.py (ios_status) and tools/app.py (ios_open_app) on the real code paths: a fake iosstudio-helper
run by the real helper.call(), a fake device daemon reached by the real DeviceClient, and a fake bin/ios-studio
launcher (never the real window).

    python -m pytest tests/mcp/test_mcp_cov_status_app.py
"""
from __future__ import annotations

import json
import os
import time
from unittest import mock

from iosstudio_mcp import helper
from iosstudio_mcp import state as STATE
from iosstudio_mcp.backends import device as DEVICE
from iosstudio_mcp.core import budget as B
from iosstudio_mcp.tools import app as APP
from iosstudio_mcp.tools import status as M
from support import write_exe
from support.mcp_harness import ToolCase, size_of

READY_SETUP = {"summary": {"title": "Ready", "essentialMissing": [], "broken": []},
               "steps": [{"id": "swift", "title": "Swift", "group": "Toolchain"}]}
SIGNED_IN = {"signedIn": True, "appleId": "dev@example.com", "teamId": "TEAM123456"}
PHONE = {"udid": "UDID-1", "name": "Test iPhone", "model": "iPhone 17", "ios": "26.1", "state": "connected",
         "transport": "usb", "active": True}


class Status(ToolCase):
    def ready(self, **over):
        self.helper.reply("setup", "check", over.get("setup", READY_SETUP))
        self.helper.reply("account", "status", over.get("account", SIGNED_IN))
        self.helper.reply("projects", "list", over.get("projects", {"projects": [
            {"name": "App", "path": "/home/u/App", "signing": {"daysLeft": 5}}]}))
        self.helper.reply("build", "history", over.get("history", {"builds": []}))

    def test_kind(self):
        ann = self.rec(M).annotations["ios_status"]
        self.assertTrue(ann.read_only_hint)
        self.assertFalse(ann.open_world_hint)

    async def test_ready_with_a_live_iphone(self):
        self.ready()
        self.daemon.replies["devices.list"] = (0, [PHONE])
        await DEVICE.client(self.state)                          # the daemon is up: live device info
        res = await self.call(M, "ios_status")
        d = res.structured_content
        self.assertTrue(d["ready"])
        self.assertEqual((d["device"]["udid"], d["device"]["source"]), ("UDID-1", "device-client"))
        self.assertEqual(d["device"]["ios_version"], "26.1")
        self.assertIn("ios_run(project_path)", d["next_steps"][-1])
        self.assertFalse(d.get("problems"))
        self.assertLessEqual(size_of(res), B.DEFAULT_BYTES)

    async def test_problems_name_their_fix(self):
        setup = {"summary": {"title": "Not ready", "essentialMissing": ["xtool"], "autoInstallable": ["xtool"],
                             "broken": ["swift"]},
                 "steps": [{"id": "xtool", "title": "xtool", "group": "Toolchain"},
                           {"id": "swift", "title": "Swift", "group": "Toolchain"}]}
        self.ready(setup=setup, account={"signedIn": False}, projects={"projects": [
            {"name": "Old", "path": "/home/u/Old", "signing": {"daysLeft": 0.5}}]},
            history={"builds": [{"project": "Old", "path": "/home/u/Old", "result": "failed", "errors": 2,
                                 "time": time.time()}]})
        d = await self.data(M, "ios_status")
        self.assertFalse(d["ready"])
        fixes = {p["area"]: p["fix"] for p in d["problems"]}
        self.assertEqual(set(fixes), {"toolchain", "account", "device", "signing", "build"})
        self.assertIn("ios_open_app(page='account')", fixes["account"])
        self.assertIn("ios_open_app(page='device')", fixes["device"])
        self.assertIn("ios_project_renew", fixes["signing"])
        self.assertIn("ios_build_diagnostics(project_path='/home/u/Old')", fixes["build"])
        self.assertTrue(any("ios_toolchain_install(all=true)" == p["fix"] for p in d["problems"]))
        self.assertEqual(d["expiring"], ["Old (0.5 days left)"])

    async def test_a_failing_source_does_not_sink_the_status(self):
        self.ready()
        self.helper.fail("setup", "check", "failed", "setup state broken")
        self.helper.fail("projects", "list", "failed", "scan failed")
        d = await self.data(M, "ios_status")
        self.assertEqual(d["problems"][0]["fix"], "ios_toolchain_check")
        self.assertIn("ios_project_templates", d["next_steps"][-1])

    async def test_no_daemon_falls_back_to_the_last_known_iphone(self):
        self.ready()
        STATE.DEVICE_STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.DEVICE_STATE.write_text(json.dumps({"udid": "UDID-OLD", "name": "Old iPhone", "connection": "offline"}))
        self.addCleanup(STATE.DEVICE_STATE.unlink, True)
        d = await self.data(M, "ios_status")
        self.assertEqual((d["device"]["udid"], d["device"]["source"]), ("UDID-OLD", "last-known"))
        self.assertTrue(any("last seen offline" in p["message"] for p in d["problems"]))
        self.assertFalse(self.daemon.requests)                  # ios_status never starts the daemon

    async def test_running_jobs_are_listed(self):
        self.ready()
        self.helper.stream("toolchain", "clean", [], delay=5)
        job = await self.state.jobs.start("toolchain-clean", "toolchain", "clean", {}, title="Clean x", prefix="t")
        d = await self.data(M, "ios_status")
        self.assertEqual(d["running_jobs"], [f"{job.id} Clean x (running)"])


class OpenApp(ToolCase):
    DAEMON = False

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.args = self.tmp / "launcher.args"
        self.launcher = write_exe(self.tmp / "ios-studio", f"""#!/bin/sh
echo "$@" > {self.args}
[ -f {self.tmp}/refuse ] && {{ echo "ios-studio: the open window is in demo mode" >&2; exit 3; }}
exit 0
""")
        env = {k: v for k, v in os.environ.items() if k not in ("HYPRLAND_INSTANCE_SIGNATURE", "IOSSTUDIO_DEMO")}
        for p in (mock.patch.object(helper, "APP_LAUNCHER", self.launcher), mock.patch.dict(os.environ, env, clear=True)):
            p.start()
            self.addCleanup(p.stop)

    def test_kind(self):
        ann = self.rec(APP).annotations["ios_open_app"]
        self.assertFalse(ann.read_only_hint)
        self.assertFalse(ann.destructive_hint)
        self.assertTrue(ann.idempotent_hint)

    async def test_without_a_graphical_session_it_hands_the_step_to_the_user(self):
        d = await self.data(APP, "ios_open_app", page="account")
        self.assertFalse(d["done"])
        self.assertIn("'account' page", d["message"])
        self.assertIn("never through the agent", d["next_steps"][0])
        self.assertFalse(self.args.exists())

    async def test_opens_the_page(self):
        os.environ["HYPRLAND_INSTANCE_SIGNATURE"] = "test"
        res = await self.call(APP, "ios_open_app", page="device")
        d = res.structured_content
        self.assertTrue(d["done"])
        self.assertEqual(self.args.read_text().split(), ["device"])
        self.assertIn("ios_status", d["next_steps"][0])
        self.assertLessEqual(size_of(res), B.DEFAULT_BYTES)

    async def test_demo_mode_opens_the_demo_window(self):
        os.environ.update(HYPRLAND_INSTANCE_SIGNATURE="test", IOSSTUDIO_DEMO="1")
        await self.data(APP, "ios_open_app", page="setup")
        self.assertEqual(self.args.read_text().split(), ["--demo=ready", "setup"])

    async def test_a_refusing_launcher_says_why(self):
        os.environ["HYPRLAND_INSTANCE_SIGNATURE"] = "test"
        (self.tmp / "refuse").touch()
        d = await self.data(APP, "ios_open_app", page="home")
        self.assertFalse(d["done"])
        self.assertEqual(d["message"], "the open window is in demo mode")
        self.assertIn("close the open iOS Studio window", d["next_steps"][0])

    async def test_a_missing_launcher_is_an_error(self):
        self.launcher.unlink()
        text = await self.error(APP, "ios_open_app")
        self.assertIn("bin/ios-studio", text)
