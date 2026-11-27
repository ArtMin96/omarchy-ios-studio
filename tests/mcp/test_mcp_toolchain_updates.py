"""tools/toolchain.py updates against a fake iosstudio-helper (real helper.call / spawn_stream, real @tool
wrapper): ios_toolchain_updates (reads `updates status`; refresh=true runs `updates check --force` as a job)
and ios_toolchain_update (destructive: the helper's `updates apply --dry-run` until confirm=true, then
`updates apply` as a job that can ask first, be answered with ios_job_respond and cancelled with ios_job_cancel,
with a long kill grace so a cancel can roll back). Also: an interrupted update and how it's put back, and the
helper's error codes as plain messages and next steps.

    python -m pytest tests/mcp/test_mcp_toolchain_updates.py
"""
from __future__ import annotations

import copy
import time

from iosstudio_mcp.core import budget as B
from iosstudio_mcp.tools import jobs as JOBS
from iosstudio_mcp.tools import toolchain as M
from support.mcp_harness import FakeHelper, ToolCase, size_of


def _comp(cid, name, installed, tested, latest, target, state="current", **kw):
    return {"id": cid, "name": name, "installed": installed, "tested": tested, "latest": latest, "target": target,
            "state": state, "untested": kw.pop("untested", False), "notesUrl": f"https://example.org/{cid}/notes",
            "latestAt": 1764000000, "applicable": kw.pop("applicable", True),
            "blockedReason": kw.pop("blockedReason", None), "checksum": {"source": kw.pop("source", "git-commit")},
            "restarts": kw.pop("restarts", []), **kw}


STATUS = {
    "channel": "tested", "checkedAt": 1764000000, "nextCheckAt": 1764086400, "due": False, "checking": False,
    "error": None, "count": 1, "rate": {"remaining": 55, "resetAt": 0},
    "components": [
        _comp("apple-dev", "omarchy-apple-dev", "9f8352f", "9f8352f", "1a2b3c4", "9f8352f",
              applicable=False, blockedReason="Needs the iOS 26.2 SDK (installed: 26.0)."),
        _comp("xtool", "xtool", "1.15.0", "1.15.0", "1.15.0", "1.15.0"),
        _comp("pymobiledevice3", "pymobiledevice3", "4.20.0", "4.21.1", "4.22.0", "4.21.1", state="update",
              source="pypi", restarts=["iphone-connection"]),
        _comp("ipsw", "ipsw", "3.1.700", "3.1.700", "3.1.752", "3.1.700", source="checksums.txt"),
        _comp("rcodesign", "rcodesign", "0.29.0", "0.29.0", "0.29.0", "0.29.0", source="SHA256SUMS"),
    ],
    "notices": [{"id": "xtool-upstream", "level": "info", "message": "xtool 1.16.0 is out upstream (no checksums).",
                 "fix": None},
                {"id": "xcode", "level": "warn", "message": "Xcode 26.2 has a newer iOS SDK.",
                 "fix": {"label": "Get the SDK", "page": "setup", "step": "xcode"}}],
}


def status(**over):
    s = copy.deepcopy(STATUS)
    s.update(over)
    return s


def dry(*would):
    """`updates apply --dry-run`'s done data: what it would install, as {id, installed, target}."""
    return {"applied": [], "rolledBack": [], "status": status(), "dryRun": True,
            "would": [{"id": i, "installed": a, "target": b} for i, a, b in would]}


class Annotations(ToolCase):
    DAEMON = False

    def test_kinds(self):
        ann = self.rec(M).annotations
        self.assertTrue(ann["ios_toolchain_updates"].read_only_hint)
        self.assertTrue(ann["ios_toolchain_updates"].open_world_hint)      # refresh asks GitHub / PyPI
        self.assertTrue(ann["ios_toolchain_update"].destructive_hint)
        self.assertTrue(ann["ios_toolchain_update"].open_world_hint)


class Updates(ToolCase):
    DAEMON = False

    async def test_reads_the_cached_status_only(self):
        self.helper.reply("updates", "status", status())
        res = await self.call(M, "ios_toolchain_updates")
        d = res.structured_content
        self.assertEqual(d["channel"], "tested")
        self.assertEqual(d["count"], 1)
        self.assertEqual([c["id"] for c in d["components"]],
                         ["apple-dev", "xtool", "pymobiledevice3", "ipsw", "rcodesign"])
        pmd = d["components"][2]
        self.assertEqual((pmd["installed"], pmd["target"], pmd["state"]), ("4.20.0", "4.21.1", "update"))
        self.assertEqual(pmd["checksum"], "pypi")
        self.assertEqual(pmd["restarts"], ["iphone-connection"])
        self.assertEqual(pmd["notes_url"], "https://example.org/pymobiledevice3/notes")
        self.assertIn("ios_toolchain_update(component='<id>')", d["next_steps"][0])
        self.assertIn("pymobiledevice3 4.20.0 -> 4.21.1", d["next_steps"][0])
        self.assertTrue(any("channel='latest'" in s and "ipsw" in s for s in d["next_steps"]))
        self.assertEqual(d["notices"][1]["fix"], "Get the SDK: ios_open_app(page='setup')")
        self.assertFalse(self.helper.calls("updates", "check"))
        self.assertFalse({"notes", "body", "release_notes"} & set(pmd))      # a link, never the notes text
        self.assertLessEqual(size_of(res), B.DEFAULT_BYTES)

    async def test_a_due_check_and_a_rate_limit_say_what_next(self):
        self.helper.reply("updates", "status", status(count=0, due=True))
        d = await self.data(M, "ios_toolchain_updates")
        self.assertTrue(any("refresh=true" in s for s in d["next_steps"]))
        later = time.time() + 600
        self.helper.reply("updates", "status", status(
            count=0, due=True, rate={"remaining": 0, "resetAt": later},
            error={"code": "rate-limited", "message": "GitHub's rate limit was reached."}))
        d = await self.data(M, "ios_toolchain_updates")
        self.assertEqual(d["error"]["code"], "rate-limited")
        self.assertTrue(d["rate_limited_until"])
        self.assertTrue(any("rate limit" in s for s in d["next_steps"]))

    async def test_refresh_runs_the_check_as_a_job(self):
        self.helper.stream("updates", "check", [
            {"event": "progress", "fraction": 0.2, "detail": "GitHub: ipsw"},
            {"event": "component", "id": "ipsw", "latest": "3.1.752"},
            {"event": "done", "ok": True, "data": status(checkedAt=time.time())}], delay=0.5)
        first = await self.data(M, "ios_toolchain_updates", refresh=True, wait_seconds=0)
        self.assertEqual(first["state"], "running")
        self.assertTrue(first["job_id"].startswith("t-"))
        self.assertIn("ios_toolchain_updates(job_id=", first["next_steps"][0])
        # a second refresh while it runs joins the same check
        again = await self.data(M, "ios_toolchain_updates", refresh=True, wait_seconds=0)
        self.assertEqual(again["job_id"], first["job_id"])
        d = await self.poll(M, "ios_toolchain_updates", first["job_id"], wait_seconds=3)
        self.assertEqual(d["state"], "done")
        self.assertEqual(d["count"], 1)
        calls = self.helper.calls("updates", "check")
        self.assertEqual(len(calls), 1)
        self.assertEqual(FakeHelper.flag(calls[0], "force"), "")
        self.assertFalse(self.helper.calls("updates", "status"))

    async def test_a_failed_check_is_one_line(self):
        self.helper.stream("updates", "check", [
            {"event": "done", "ok": False, "error": {"code": "network", "message": "GitHub is unreachable."}}])
        first = await self.data(M, "ios_toolchain_updates", refresh=True, wait_seconds=0)
        text = await self.error(M, "ios_toolchain_updates", job_id=first["job_id"], wait_seconds=3)
        self.assertIn("GitHub is unreachable.", text)
        self.assertIn("internet connection", text)

    async def test_a_failed_check_still_shows_the_cached_status(self):
        err = {"code": "network", "message": "Couldn't check for updates: GitHub is unreachable."}
        self.helper.stream("updates", "check", [{"event": "done", "ok": False, "error": err,
                                                 "data": status(error=err)}])
        first = await self.data(M, "ios_toolchain_updates", refresh=True, wait_seconds=0)
        d = await self.poll(M, "ios_toolchain_updates", first["job_id"], wait_seconds=3)
        self.assertEqual((d["state"], d["error"]["code"]), ("done", "network"))
        self.assertEqual(len(d["components"]), 5)
        self.assertTrue(any("internet connection" in s for s in d["next_steps"]))

    async def test_a_partial_check_says_it_retries(self):
        self.helper.reply("updates", "status", status(error={"code": "partial", "message": "PyPI didn't answer."}))
        d = await self.data(M, "ios_toolchain_updates")
        self.assertEqual(d["error"]["code"], "partial")
        self.assertTrue(any("tries again later" in s for s in d["next_steps"]))

    async def test_the_result_stays_in_budget(self):
        many = [_comp(f"c{i}", "x" * 500, "1" * 200, "2" * 200, "3" * 200, "2" * 200, state="update",
                      blockedReason="r" * 2000, notesUrl="https://example.org/" + "n" * 900) for i in range(30)]
        notes = [{"id": f"n{i}", "level": "warn", "message": "m" * 5000} for i in range(30)]
        self.helper.reply("updates", "status", status(components=many, notices=notes, count=30))
        res = await self.call(M, "ios_toolchain_updates")
        d = res.structured_content
        self.assertLessEqual(size_of(res), B.DEFAULT_BYTES)
        self.assertTrue(d["truncated"])
        self.assertIn("note", d)
        self.assertLessEqual(len(d.get("components", [])), 8)
        self.assertLessEqual(len(d.get("notices", [])), 8)


class Update(ToolCase):
    DAEMON = False

    def interactive_apply(self, after: dict) -> None:
        """`updates apply` as a real stream: it asks, waits for the answer on stdin, then succeeds."""
        script = self.tmp / "apply.py"
        script.write_text(APPLY_SCRIPT % {"after": repr(after)})
        self.helper.stream("updates", "apply", [])
        exe = self.helper.exe.read_text()
        self.helper.exe.write_text(exe.replace("spec = json.load", f"exec(open({str(script)!r}).read()) if key == "
                                               "'updates apply' else None\nspec = json.load"))

    async def test_component_or_all_is_required(self):
        self.helper.reply("updates", "status", status())
        text = await self.error(M, "ios_toolchain_update")
        self.assertIn("component='<id>'", text)
        text = await self.error(M, "ios_toolchain_update", component="ipsw", all=True)
        self.assertIn("all=true", text)
        self.assertFalse(self.helper.calls("updates", "apply"))

    async def test_dry_run_lists_what_would_change_and_installs_nothing(self):
        self.helper.reply("updates", "status", status())
        self.helper.reply("updates", "apply", dry(("pymobiledevice3", "4.20.0", "4.21.1")))
        d = await self.data(M, "ios_toolchain_update", all=True)
        self.assertEqual(d["state"], "dry_run")
        self.assertTrue(d["dry_run"])
        self.assertEqual(len(d["would"]), 1)
        self.assertIn("pymobiledevice3: 4.20.0 -> 4.21.1, verified by pypi", d["would"][0])
        self.assertIn("restarts the iPhone connection", d["would"][0])
        self.assertIn("confirm=true", d["message"])
        self.assertIn("confirm=true", d["next_steps"][0])
        argv = self.helper.calls("updates", "apply")[0]                 # the helper plans, nothing installs
        self.assertEqual((FakeHelper.flag(argv, "dry-run"), FakeHelper.flag(argv, "all")), ("", ""))
        self.assertIsNone(FakeHelper.flag(argv, "channel"))

    async def test_dry_run_on_latest_marks_untested_and_blocked(self):
        self.helper.reply("updates", "status", status())
        self.helper.reply("updates", "apply", dry(("ipsw", "3.1.700", "3.1.752"),
                                                  ("pymobiledevice3", "4.20.0", "4.22.0")))
        d = await self.data(M, "ios_toolchain_update", all=True, channel="latest")
        self.assertEqual(d["channel"], "latest")
        would = "\n".join(d["would"])
        self.assertIn("ipsw: 3.1.700 -> 3.1.752 (untested), verified by checksums.txt", would)
        self.assertIn("pymobiledevice3: 4.20.0 -> 4.22.0 (untested)", would)
        self.assertIn("Save the update channel 'latest'", d["would"][-1])
        self.assertEqual(len(d["blocked"]), 1)
        self.assertIn("Needs the iOS 26.2 SDK", d["blocked"][0])
        argv = self.helper.calls("updates", "apply")[0]
        self.assertEqual((FakeHelper.flag(argv, "dry-run"), FakeHelper.flag(argv, "channel")), ("", "latest"))

    async def test_nothing_to_do_says_so(self):
        self.helper.reply("updates", "status", status())
        self.helper.reply("updates", "apply", dry())
        d = await self.data(M, "ios_toolchain_update", component="xtool")
        self.assertEqual(d.get("would", []), [])
        self.assertIn("Nothing to update", d["message"])
        self.assertNotIn("confirm=true", d["message"])

    async def test_an_unknown_component_is_refused(self):
        self.helper.reply("updates", "status", status(components=STATUS["components"][:2]))
        text = await self.error(M, "ios_toolchain_update", component="ipsw")
        self.assertIn("apple-dev, xtool", text)

    async def test_dry_run_would_is_capped(self):
        many = [_comp(f"c{i}", f"tool{i}", "1", "2", "3", "2", state="update") for i in range(20)]
        self.helper.reply("updates", "status", status(components=many, count=20))
        self.helper.reply("updates", "apply", dry(*[(f"c{i}", "1", "2") for i in range(20)]))
        res = await self.call(M, "ios_toolchain_update", all=True)
        d = res.structured_content
        self.assertEqual(len(d["would"]), 8)
        self.assertTrue(d["truncated"])
        self.assertIn("component='<id>'", d["note"])
        self.assertLessEqual(size_of(res), B.DEFAULT_BYTES)

    async def test_dry_run_refusals_are_plain_with_a_next_step(self):
        self.helper.reply("updates", "status", status())
        cases = {
            "blocked": ("Needs the iOS 26.2 SDK.", {"label": "Get Xcode", "page": "setup", "step": "xcode"},
                        "ios_open_app(page='setup')"),
            "iphone-busy": ("The iPhone connection is in use (debugging).", {"label": "Open iPhone", "page": "device"},
                            "updating pymobiledevice3 restarts the connection"),
            "busy": ("A toolchain update is running. Wait for it to finish, then try again.", None, "Wait for it"),
        }
        for code, (message, fix, step) in cases.items():
            with self.subTest(code):
                self.helper.fail("updates", "apply", code, message, **({"fix": fix} if fix else {}))
                d = await self.data(M, "ios_toolchain_update", component="pymobiledevice3")
                self.assertEqual((d["state"], d["error"]["code"]), ("dry_run", code))
                self.assertIn(message, d["message"])
                self.assertTrue(any(step in s for s in d["next_steps"]), d["next_steps"])
                self.assertEqual(d.get("would", []), [])
                self.assertEqual(d.get("blocked", []), [message] if code == "blocked" else [])

    async def test_an_unexpected_dry_run_error_is_one_line(self):
        self.helper.reply("updates", "status", status())
        self.helper.fail("updates", "apply", "usage", "There's no component 'x'.")
        text = await self.error(M, "ios_toolchain_update", component="ipsw")
        self.assertIn("There's no component 'x'.", text)

    async def test_an_interrupted_update_is_shown_and_put_back(self):
        st = status(interrupted={"component": "ipsw", "since": 1764000000})
        self.helper.reply("updates", "status", st)
        d = await self.data(M, "ios_toolchain_updates")
        self.assertEqual(d["interrupted"]["component"], "ipsw")
        self.assertEqual(d["interrupted"]["name"], "ipsw")
        self.assertTrue(d["interrupted"]["since"])
        self.assertIn("An update of ipsw was interrupted", d["next_steps"][0])
        self.assertIn("ios_toolchain_update()", d["next_steps"][0])
        # no component, no all: a dry run of putting it back (the helper isn't asked to plan anything)
        d = await self.data(M, "ios_toolchain_update")
        self.assertEqual(d["state"], "dry_run")
        self.assertIn("put back the previous ipsw", d["would"][0])
        self.assertIn("confirm=true", d["message"])
        self.assertFalse(self.helper.calls("updates", "apply"))
        # an update of another component puts it back first
        self.helper.reply("updates", "apply", dry(("pymobiledevice3", "4.20.0", "4.21.1")))
        d = await self.data(M, "ios_toolchain_update", component="pymobiledevice3")
        self.assertIn("First put back the previous ipsw", d["would"][0])
        self.assertIn("pymobiledevice3: 4.20.0 -> 4.21.1", d["would"][1])
        # confirm: `updates apply` with neither flag only puts it back
        self.helper.stream("updates", "apply", [
            {"event": "rollback", "id": "ipsw", "reason": "interrupted"},
            {"event": "done", "ok": True, "data": {"applied": [], "rolledBack": ["ipsw"], "status": status()}}])
        d = await self.data(M, "ios_toolchain_update", confirm=True, wait_seconds=3)
        self.assertEqual(d["state"], "succeeded")
        self.assertEqual(d["rolled_back"], ["ipsw"])
        self.assertEqual(d["message"], "Put back the previous ipsw; the interrupted update is undone.")
        argv = self.helper.calls("updates", "apply")[-1]
        for absent in ("component", "all", "dry-run", "channel"):
            self.assertIsNone(FakeHelper.flag(argv, absent), absent)
        self.assertEqual(self.state.jobs.get(d["job_id"]).title, "Put back ipsw")

    async def test_a_failed_verify_says_nothing_is_left_to_undo(self):
        self.helper.reply("updates", "status", status())
        self.helper.stream("updates", "apply", [
            {"event": "done", "ok": False,
             "error": {"code": "verify-failed", "message": "The new ipsw reports no version. The previous version "
                                                           "is back."},
             "data": {"applied": [], "rolledBack": ["ipsw"], "status": status()}}])
        d = await self.data(M, "ios_toolchain_update", component="ipsw", confirm=True, wait_seconds=3)
        self.assertEqual((d["state"], d["error"]["code"]), ("failed", "verify-failed"))
        self.assertIn("The previous version is back.", d["message"])
        self.assertTrue(any("Nothing to undo" in s for s in d["next_steps"]))

    async def test_the_update_job_gets_time_to_roll_back(self):
        self.helper.reply("updates", "status", status())
        self.helper.stream("updates", "apply", [
            {"event": "done", "ok": True, "data": {"applied": [], "rolledBack": [], "status": status()}}], delay=1)
        d = await self.data(M, "ios_toolchain_update", component="ipsw", confirm=True, wait_seconds=0)
        self.assertEqual(self.state.jobs.get(d["job_id"]).kill_grace, M.KILL_GRACE)
        self.assertGreaterEqual(M.KILL_GRACE, 60)
        first = await self.data(M, "ios_toolchain_updates", refresh=False)
        self.assertEqual(first["channel"], "tested")

    async def test_confirm_starts_a_job_that_asks_and_cancel_puts_it_back(self):
        self.helper.reply("updates", "status", status())
        self.interactive_apply(status())
        d = await self.data(M, "ios_toolchain_update", component="pymobiledevice3", confirm=True, wait_seconds=3)
        self.assertEqual(d["state"], "needs_input")
        self.assertTrue(d["job_id"].startswith("t-"))
        self.assertEqual(d["pending_prompt"]["id"], "restart-iphone")
        self.assertTrue(any("ios_job_respond" in s for s in d["next_steps"]))
        argv = self.helper.calls("updates", "apply")[0]
        self.assertEqual(FakeHelper.flag(argv, "component"), "pymobiledevice3")
        for absent in ("all", "dry_run", "yes", "channel"):
            self.assertIsNone(FakeHelper.flag(argv, absent), absent)
        # the shared job tools see it; a second update while it runs is refused
        st = await self.data(JOBS, "ios_job_status", job_id=d["job_id"], wait_seconds=0)
        self.assertEqual((st["state"], st["tool"]), ("needs_input", "ios_toolchain_update"))
        text = await self.error(M, "ios_toolchain_update", all=True, confirm=True)
        self.assertIn(d["job_id"], text)
        text = await self.error(M, "ios_toolchain_install", all=True)
        self.assertIn(d["job_id"], text)
        cancelled = await self.data(JOBS, "ios_job_cancel", job_id=d["job_id"])
        self.assertEqual(cancelled["state"], "cancelled")
        d = await self.data(M, "ios_toolchain_update", job_id=d["job_id"])
        self.assertEqual(d["state"], "cancelled")
        self.assertIn("put back", d["message"])

    async def test_answered_prompt_reaches_the_helper_and_the_job_succeeds(self):
        self.helper.reply("updates", "status", status())
        after = status(count=0)
        after["components"][2].update(installed="4.21.1", state="current")
        self.interactive_apply(after)
        d = await self.data(M, "ios_toolchain_update", component="pymobiledevice3", channel="tested",
                            confirm=True, wait_seconds=3)
        self.assertEqual(d["state"], "needs_input")
        await self.data(JOBS, "ios_job_respond", job_id=d["job_id"], prompt_id="restart-iphone", value="yes")
        d = await self.poll(M, "ios_toolchain_update", d["job_id"], wait_seconds=3)
        self.assertEqual(d["state"], "succeeded")
        self.assertEqual(d["applied"], ["pymobiledevice3"])
        self.assertEqual(d["channel"], "tested")
        self.assertIn("post-update check", d["message"])
        self.assertNotIn("components", d)          # everything is current afterwards
        self.assertEqual(FakeHelper.flag(self.helper.calls("updates", "apply")[0], "channel"), "tested")

    async def test_a_failed_update_reports_rollback_and_the_fix(self):
        self.helper.reply("updates", "status", status())
        self.helper.stream("updates", "apply", [
            {"event": "rollback", "id": "pymobiledevice3", "reason": "post-update check failed"},
            {"event": "done", "ok": False,
             "error": {"code": "session-active", "message": "A debug session is using the iPhone connection.",
                       "fix": {"label": "Open Devices", "page": "device"}},
             "data": {"applied": [], "rolledBack": ["pymobiledevice3"], "status": status()}}])
        d = await self.data(M, "ios_toolchain_update", component="pymobiledevice3", confirm=True, wait_seconds=3)
        self.assertEqual(d["state"], "failed")
        self.assertEqual(d["error"]["code"], "session-active")
        self.assertEqual(d["rolled_back"], ["pymobiledevice3"])
        self.assertIn("Put back: pymobiledevice3", d["message"])
        self.assertIn("ios_open_app(page='device')", d["next_steps"][0])
        self.assertEqual(d["count"], 1)

    async def test_a_slow_update_returns_running_with_its_job_id(self):
        self.helper.reply("updates", "status", status())
        self.helper.stream("updates", "apply", [
            {"event": "done", "ok": True, "data": {"applied": [], "rolledBack": [], "status": status(count=0)}}],
            delay=1)
        d = await self.data(M, "ios_toolchain_update", all=True, confirm=True, wait_seconds=0)
        self.assertEqual(d["state"], "running")
        self.assertIn("ios_job_cancel", d["next_steps"][0])
        d = await self.poll(M, "ios_toolchain_update", d["job_id"], wait_seconds=3)
        self.assertEqual(d["state"], "succeeded")
        self.assertIn("Nothing needed updating", d["message"])
        self.assertIsNone(FakeHelper.flag(self.helper.calls("updates", "apply")[0], "component"))
        self.assertEqual(FakeHelper.flag(self.helper.calls("updates", "apply")[0], "all"), "")

    async def test_a_job_of_another_kind_is_refused(self):
        self.helper.stream("updates", "check", [{"event": "done", "ok": True, "data": status()}])
        first = await self.data(M, "ios_toolchain_updates", refresh=True, wait_seconds=3)
        text = await self.error(M, "ios_toolchain_update", job_id=first["job_id"])
        self.assertIn("toolchain-update", text)


# Runs inside the fake helper for `updates apply`: asks, reads the answer from stdin, then reports success.
APPLY_SCRIPT = """
import json, sys
print(json.dumps({"event": "step", "id": "pymobiledevice3", "title": "pymobiledevice3", "state": "running"}),
      flush=True)
print(json.dumps({"event": "prompt", "id": "restart-iphone", "title": "Restart the iPhone connection?",
                  "message": "It restarts.", "options": [{"value": "yes"}, {"value": "no"}]}), flush=True)
answer = json.loads(sys.stdin.readline())
assert answer == {"prompt": "restart-iphone", "value": "yes"}, answer
print(json.dumps({"event": "verified", "id": "pymobiledevice3", "source": "pypi"}), flush=True)
print(json.dumps({"event": "done", "ok": True,
                  "data": {"applied": ["pymobiledevice3"], "rolledBack": [], "status": %(after)s}}), flush=True)
sys.exit(0)
"""
