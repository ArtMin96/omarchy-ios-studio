"""tools/account.py against a fake iosstudio-helper (real helper.call / spawn_stream, real @tool wrapper):
ios_account_status, ios_signing_status, ios_project_renew (a job), ios_project_autorenew and ios_account_logout
(destructive: dry runs until confirm=true).

    python -m pytest tests/mcp/test_mcp_cov_account.py
"""
from __future__ import annotations

from iosstudio_mcp.core import budget as B
from iosstudio_mcp.tools import account as M
from iosstudio_mcp.tools import jobs as JOBS
from support.mcp_harness import FakeHelper, ToolCase, size_of

SIGNED_IN = {"signedIn": True, "appleId": "dev@example.com", "teamId": "TEAM123456", "tokenDaysLeft": 20,
             "hasLocalSigningKey": True}


def details(n_profiles=2, n_certs=1, **extra):
    return {"summary": {"team": {"name": "Dev Team"}, "teamType": "free", "free": True,
                        "limits": {"apps": 3}, "counts": {"apps": 1},
                        "health": {"title": "Healthy", "detail": "all good"}},
            "sections": {"profiles": {"items": [{"bundleId": f"XTL-TEAM.com.ex.app{i}", "displayBundleId": f"com.ex.app{i}",
                                                 "expires": 1_900_000_000, "daysLeft": 6 - (i % 6), "devicesCount": 1}
                                                for i in range(n_profiles)]},
                         "certificates": {"items": [{"name": f"cert{i}", "kind": "development", "daysLeft": 300}
                                                    for i in range(n_certs)]}},
            "fetchedAt": 1_800_000_000, **extra}


def project(tmp, name="App", **extra):
    return {"name": name, "path": str(tmp / name), "bundleId": f"com.ex.{name}", **extra}


class Annotations(ToolCase):
    DAEMON = False

    def test_kinds(self):
        ann = self.rec(M).annotations
        for name in ("ios_account_status", "ios_signing_status"):
            self.assertTrue(ann[name].read_only_hint, name)
            self.assertFalse(ann[name].destructive_hint, name)
        self.assertTrue(ann["ios_account_status"].open_world_hint)        # asks Apple with refresh=true
        self.assertFalse(ann["ios_project_renew"].read_only_hint)
        self.assertTrue(ann["ios_project_renew"].open_world_hint)
        for name in ("ios_project_autorenew", "ios_account_logout"):
            self.assertTrue(ann[name].destructive_hint, name)
            self.assertIn("confirm", self.fn(M, name).__signature__.parameters, name)


class AccountStatus(ToolCase):
    DAEMON = False

    async def test_signed_out_points_to_the_human_step(self):
        self.helper.reply("account", "status", {"signedIn": False})
        d = await self.data(M, "ios_account_status")
        self.assertFalse(d["signed_in"])
        self.assertIn("ios_open_app(page='account')", d["next_steps"][0])
        self.assertFalse(self.helper.calls("account", "details"))     # no Apple call when signed out

    async def test_signed_in_reads_cached_details(self):
        self.helper.reply("account", "status", SIGNED_IN)
        self.helper.reply("account", "details", details())
        res = await self.call(M, "ios_account_status")
        d = res.structured_content
        self.assertEqual((d["team_name"], d["team_type"], d["profiles_total"]), ("Dev Team", "free", 2))
        self.assertEqual(d["profiles"][0]["days_left"], 5)     # soonest expiry first
        self.assertEqual(d["health"], "Healthy: all good")
        self.assertEqual(FakeHelper.flag(self.helper.calls("account", "details")[0], "cached"), "")
        self.assertLessEqual(size_of(res), B.DEFAULT_BYTES)

    async def test_refresh_asks_apple(self):
        self.helper.reply("account", "status", SIGNED_IN)
        self.helper.reply("account", "details", details())
        await self.data(M, "ios_account_status", refresh=True)
        self.assertIsNone(FakeHelper.flag(self.helper.calls("account", "details")[0], "cached"))

    async def test_long_lists_are_capped_and_say_so(self):
        self.helper.reply("account", "status", SIGNED_IN)
        self.helper.reply("account", "details", details(n_profiles=200, n_certs=60))
        res = await self.call(M, "ios_account_status")
        d = res.structured_content
        self.assertEqual((len(d["profiles"]), d["profiles_total"]), (B.LIST_CAP, 200))
        self.assertEqual(d["certificates_total"], 60)
        self.assertTrue(any("at most" in s for s in d["next_steps"]))
        self.assertLessEqual(size_of(res), B.DEFAULT_BYTES)

    async def test_details_failure_is_reported_not_raised(self):
        self.helper.reply("account", "status", SIGNED_IN)
        self.helper.fail("account", "details", "network", "Apple didn't answer.")
        d = await self.data(M, "ios_account_status")
        self.assertTrue(d["signed_in"])
        self.assertIn("[network]", d["errors"][0])

    async def test_helper_failure_is_one_line_with_next_step(self):
        self.helper.fail("account", "status", "missing-xtool", "xtool isn't installed.")
        text = await self.error(M, "ios_account_status")
        self.assertIn("[missing-xtool]", text)
        self.assertIn("Next: Run ios_toolchain_check", text)


class SigningStatus(ToolCase):
    DAEMON = False

    async def test_expiring_projects_first_with_renew_steps(self):
        self.helper.reply("projects", "list", {"projects": [
            project(self.tmp, "Fine", signing={"daysLeft": 6, "level": "ok"}),
            project(self.tmp, "Soon", signing={"daysLeft": 1, "level": "warn"}),
            project(self.tmp, "NeverSigned")]})
        self.helper.reply("account", "details", details())
        self.helper.reply("projects", "autorenew", {"installed": False, "enabled": False,
                                                   "projects": [{"path": "/x", "daysLeft": 1, "due": True}]})
        res = await self.call(M, "ios_signing_status")
        d = res.structured_content
        self.assertEqual([p["name"] for p in d["projects"]], ["Soon", "Fine", "NeverSigned"])
        self.assertIn("ios_project_renew(project_path=", d["next_steps"][0])
        self.assertTrue(any("ios_project_autorenew(action='enable')" in s for s in d["next_steps"]))
        self.assertEqual(d["renewal_due"][0]["due"], True)
        self.assertLessEqual(size_of(res), B.DEFAULT_BYTES)

    async def test_missing_account_and_timer_still_answer(self):
        self.helper.reply("projects", "list", {"projects": []})
        self.helper.fail("account", "details", "not-signed-in", "Sign in first.")
        self.helper.fail("projects", "autorenew", "failed", "no systemd")
        d = await self.data(M, "ios_signing_status")
        self.assertEqual(d["projects_total"], 0)

    async def test_projects_failure_is_one_line(self):
        self.helper.fail("projects", "list", "failed", "Couldn't scan.\nTraceback ...")
        text = await self.error(M, "ios_signing_status")
        self.assertIn("Couldn't scan.", text)


class Renew(ToolCase):
    DAEMON = False

    async def asyncSetUp(self):
        await super().asyncSetUp()
        (self.tmp / "App").mkdir()

    def projects(self, **signing):
        self.helper.reply("projects", "list", {"projects": [project(self.tmp, signing=signing or None)]})

    async def test_a_renewal_is_a_job_pollable_with_ios_job_status(self):
        self.projects(udid="UDID-1", daysLeft=0.5)
        self.helper.stream("projects", "renew", [
            {"event": "build", "project": "App"},
            {"event": "step", "id": "sign", "title": "Signing", "state": "done"},
            {"event": "done", "ok": True, "data": {"installed": True, "udid": "UDID-1", "bundleId": "com.ex.App"}},
        ], delay=0.6)
        first = await self.data(M, "ios_project_renew", project_path="App")
        self.assertEqual(first["state"], "running")
        self.assertTrue(first["job_id"].startswith("r-"))
        done = await self.data(JOBS, "ios_job_status", job_id=first["job_id"], wait_seconds=3)
        self.assertEqual(done["state"], "succeeded")
        argv = self.helper.calls("projects", "renew")[0]
        self.assertEqual(FakeHelper.flag(argv, "udid"), "UDID-1")         # the project's own iPhone

    async def test_a_second_renewal_of_the_same_project_is_refused(self):
        self.projects(udid="UDID-1")
        self.helper.stream("projects", "renew", [{"event": "done", "ok": True, "data": {}}], delay=2)
        await self.data(M, "ios_project_renew", project_path="App")
        text = await self.error(M, "ios_project_renew", project_path="App")
        self.assertIn("already has a running job r-", text)
        self.assertIn("ios_job_status", text)

    async def test_no_known_iphone_is_an_error_with_a_next_step(self):
        self.projects()
        text = await self.error(M, "ios_project_renew", project_path="App")
        self.assertIn("ios_open_app(page='device')", text)
        self.assertFalse(self.helper.calls("projects", "renew"))

    async def test_unknown_project_names_the_known_ones(self):
        self.projects()
        text = await self.error(M, "ios_project_renew", project_path="Nope")
        self.assertIn("Known projects: App", text)


class Autorenew(ToolCase):
    DAEMON = False

    async def test_status_reads_the_timer(self):
        self.helper.reply("projects", "autorenew", {"installed": True, "enabled": True})
        d = await self.data(M, "ios_project_autorenew")
        self.assertEqual((d["done"], d["message"]), (True, "Automatic renewal is on."))

    async def test_enable_is_a_dry_run_without_confirm(self):
        (self.tmp / "App").mkdir()
        self.helper.reply("projects", "list", {"projects": [project(self.tmp)]})
        self.helper.reply("projects", "autorenew", {"dryRun": True, "actions": ["write ios-studio-renew.timer"]})
        d = await self.data(M, "ios_project_autorenew", action="enable", project_path="App")
        self.assertFalse(d["done"])
        self.assertIn("write ios-studio-renew.timer", d["would"])
        self.assertTrue(any("autoRenew=True" in w for w in d["would"]))
        self.assertIn("confirm=true", d["next_steps"][0])
        self.assertEqual(FakeHelper.flag(self.helper.calls("projects", "autorenew")[0], "dry_run"), "")
        self.assertFalse(self.helper.calls("projects", "set"))            # nothing changed

    async def test_confirm_sets_the_project_and_the_timer(self):
        (self.tmp / "App").mkdir()
        self.helper.reply("projects", "list", {"projects": [project(self.tmp)]})
        self.helper.reply("projects", "set", project(self.tmp, autoRenew=False))
        self.helper.reply("projects", "autorenew", {"installed": False, "enabled": False})
        d = await self.data(M, "ios_project_autorenew", action="disable", project_path="App", confirm=True)
        self.assertEqual((d["done"], d["message"]), (True, "Automatic renewal disabled."))
        self.assertEqual(FakeHelper.flag(self.helper.calls("projects", "set")[0], "auto_renew"), "0")
        self.assertIsNone(FakeHelper.flag(self.helper.calls("projects", "autorenew")[0], "dry_run"))

    async def test_timer_failure_is_one_line(self):
        self.helper.fail("projects", "autorenew", "failed", "systemctl failed")
        text = await self.error(M, "ios_project_autorenew")
        self.assertIn("systemctl failed [failed]", text)


class Logout(ToolCase):
    DAEMON = False

    async def test_not_signed_in_does_nothing(self):
        self.helper.reply("account", "status", {"signedIn": False})
        d = await self.data(M, "ios_account_logout", confirm=True)
        self.assertFalse(d["done"])
        self.assertFalse(self.helper.calls("account", "logout"))

    async def test_dry_run_lists_what_it_would_do(self):
        self.helper.reply("account", "status", SIGNED_IN)
        d = await self.data(M, "ios_account_logout", reset_2fa=True)
        self.assertFalse(d["done"])
        self.assertIn("dev@example.com", d["would"][0])
        self.assertIn("Forget the trusted 2FA session", d["would"])
        self.assertFalse(self.helper.calls("account", "logout"))

    async def test_confirm_signs_out(self):
        self.helper.reply("account", "status", SIGNED_IN)
        self.helper.reply("account", "logout", {"signedIn": False})
        d = await self.data(M, "ios_account_logout", confirm=True)
        self.assertTrue(d["done"])
        self.assertIn("ios_open_app(page='account')", d["next_steps"][0])
        self.assertIsNone(FakeHelper.flag(self.helper.calls("account", "logout")[0], "reset_2fa"))

    async def test_logout_failure_has_a_next_step(self):
        self.helper.reply("account", "status", SIGNED_IN)
        self.helper.fail("account", "logout", "missing-xtool", "xtool is gone.")
        text = await self.error(M, "ios_account_logout", confirm=True)
        self.assertIn("Next: Run ios_toolchain_check", text)
