"""tools/hot_reload.py: hot reload on the iPhone (ios_hot_reload_set / _start / _status / _control).

- Against fakes (support/mcp_harness.py): `hotreload enable|disable|status|stop|pause|resume` are FakeHelper replies;
  `hotreload run` is an interactive fake below shaped like helper/iosstudio/features/hotreload/session.py's stream
  (`hotreload` snapshots, then `done` after {"cancel": true} on stdin or SIGTERM).
- The real helper in demo mode (helper/iosstudio/demo/hot_reload.py's scripted session) in private folders:
  start -> watching, status with after_seq waits for the next reload, pause/resume, stop.

Needs the MCP server's dependencies (the dev venv; skipped otherwise):
    python -m pytest tests/mcp/test_mcp_hot_reload.py
"""
from __future__ import annotations

import json
import os
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from iosstudio_mcp.tools import hot_reload as HR
from support.mcp_harness import FAKE_HELPER, ToolCase, size_of

HOME = os.path.expanduser("~")
PROJ = f"{HOME}/Developer/Weather"
TPROJ = "~/Developer/Weather"
SRC = "Sources/Weather/ContentView.swift"

# The `hotreload run` stream: project …Slow waits for <exe>.go before watching, …Broken fails its build; stdin lines
# are logged to <exe>.stdin.
SESSION = textwrap.dedent("""\
    def session(argv):
        import os, select, signal
        flags, i = {}, 3
        while i < len(argv):
            if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                flags[argv[i]] = argv[i + 1]; i += 2
            else:
                flags[argv[i]] = True; i += 1
        proj = flags.get("--project") or ""
        base = {"project": os.path.basename(proj), "path": proj, "pid": os.getpid(), "patchSeq": 3, "reloads": 0,
                "bundleId": "XTL-TEAM.com.example.weather", "paused": False, "pending": [], "pendingCount": 0}
        def snap(state, seq, **kw):
            print(json.dumps({"event": "hotreload", "seq": seq, "state": state, "message": state, **base, **kw}),
                  flush=True)
        snap("starting", 1)
        snap("building", 2, progress={"fraction": 0.3, "detail": "Compiling Weather"}, logFile="/tmp/hr-build.log")
        if proj.endswith("Broken"):
            err = {"code": "build-failed", "message": "The build failed.", "logFile": HOME + "/.local/state/b.log",
                   "diagnostics": [{"severity": "error", "category": "compiler", "file": "Sources/Broken/A.swift",
                                    "line": 3, "col": 7, "message": "cannot find 'x' in scope"}]}
            final = {**base, "event": "hotreload", "seq": 3, "state": "stopped", "stopReason": "build-failed",
                     "message": "Stopped: the build failed", "lastError": err, "history": []}
            print(json.dumps({"event": "done", "ok": False, "error": err, "data": final}), flush=True)
            return
        while proj.endswith("Slow") and not os.path.exists(me + ".go"):
            time.sleep(0.02)
        snap("watching", 3, message="Watching for saves", mode="inotify")
        stop = []
        signal.signal(signal.SIGTERM, lambda *a: stop.append("term"))
        while not stop:
            try:
                r, _, _ = select.select([0], [], [], 0.05)
            except InterruptedError:
                continue
            if r:
                line = sys.stdin.readline()
                if not line:
                    continue
                with open(me + ".stdin", "a") as f:
                    f.write(line)
                if json.loads(line).get("cancel"):
                    break
        final = {**base, "event": "hotreload", "seq": 9, "state": "stopped", "stopReason": "stopped",
                 "message": "Stopped", "history": [{"seq": 4, "ok": True, "files": [SRC], "ms": 2100, "swapped": 3}]}
        print(json.dumps({"event": "done", "ok": True, "data": final}), flush=True)
""")


def snapshot(state="watching", **extra) -> dict:
    return {"event": "hotreload", "seq": 12, "state": state, "message": "Watching for saves", "project": "Weather",
            "path": PROJ, "pid": 4242, "bundleId": "XTL-TEAM.com.example.weather", "reloads": 2, "patchSeq": 5,
            "paused": False, "pending": [], "pendingCount": 0, "running": state != "stopped", "notice": None,
            "progress": None, "lastReload": None, "lastError": None, "stopReason": None, **extra}


RELOAD = {"seq": 5, "ok": True, "files": [SRC], "fileCount": 1, "ms": 2900, "time": 1_790_000_000,
          "steps": {"check": 40, "compile": 900, "link": 1300, "sign": 200, "push": 300, "apply": 3.1},
          "swapped": 4, "reverted": 1, "vtable": 0, "notSwapped": 0}


def failed_error(seq=5) -> dict:
    return {"code": "unsupported-change", "seq": seq, "message": "Adds the stored property count to ContentView. "
            + "x" * 900, "file": f"{PROJ}/{SRC}", "line": 12, "detail": "d" * 4000,
            "fix": {"label": "Run again", "page": "projects", "action": "hot-reload-restart"},
            "blockers": [{"kind": "stored-property", "type": "ContentView", "name": f"p{i}", "file": SRC, "line": i,
                          "message": f"Adds the stored property p{i} " + "y" * 500} for i in range(30)],
            "diagnostics": [{"severity": "error", "file": f"{PROJ}/{SRC}", "line": i, "col": 2,
                             "message": "boom " * 200} for i in range(12)]}


def history(n=20) -> list[dict]:
    return [{"seq": i, "ok": i % 3 != 0, "time": 1_790_000_000 + i, "files": [SRC] * 8, "ms": 2000 + i, "swapped": 2,
             **({} if i % 3 else {"code": "compile-failed", "message": "m" * 500})} for i in range(1, n + 1)]


class HotReloadCase(ToolCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.helper.reply("projects", "list", {"projects": [
            {"name": n, "path": f"{HOME}/Developer/{n}"} for n in ("Weather", "Slow", "Broken")]})
        self.helper.reply("hotreload", "status", {"project": PROJ, "enabled": True, "mode": "on", "session": None})
        script = FAKE_HELPER.replace(
            'key = " ".join(sys.argv[1:3])\n',
            'key = " ".join(sys.argv[1:3])\n'
            'if key == "hotreload run":\n    session(sys.argv)\n    sys.exit(0)\n')
        script = script.replace("me = sys.argv[0]\n", f"me = sys.argv[0]\nimport time\nHOME = {HOME!r}\nSRC = {SRC!r}\n"
                                + SESSION)
        self.helper.exe.write_text(script)

    def stdin_lines(self) -> list[str]:
        p = Path(f"{self.helper.exe}.stdin")
        return p.read_text().splitlines() if p.exists() else []


class Registration(HotReloadCase):
    async def test_kinds_registration_and_guide(self):
        ann = self.rec(HR).annotations
        self.assertTrue(ann["ios_hot_reload_status"].read_only_hint)
        for name in ("ios_hot_reload_set", "ios_hot_reload_start", "ios_hot_reload_control"):
            self.assertFalse(ann[name].read_only_hint, name)
            self.assertFalse(ann[name].destructive_hint, name)
        self.assertTrue(ann["ios_hot_reload_start"].open_world_hint)
        self.assertTrue(ann["ios_hot_reload_control"].idempotent_hint)
        from iosstudio_mcp import resources, tools
        self.assertIn("hot_reload", tools.MODULES)
        guide = resources.GUIDE.read_text()
        for name in self.rec(HR).tools:
            self.assertIn(name, guide)


class SetTool(HotReloadCase):
    async def test_on_returns_the_diff_and_next_steps(self):
        self.helper.reply("hotreload", "enable", {"enabled": True, "mode": "on", "appTarget": "Weather",
                                                  "changed": True, "diff": "--- a\n+++ b\n+// >>> iOS Studio\n",
                                                  "truncated": False, "packagePath": f"{PROJ}/Package.swift",
                                                  "dryRun": False, "runtimeUpdated": True})
        d = await self.data(HR, "ios_hot_reload_set", project_path="Weather", enabled=True)
        argv = self.helper.calls("hotreload", "enable")[0]
        self.assertEqual(self.helper.flag(argv, "project"), PROJ)
        self.assertIsNone(self.helper.flag(argv, "dry_run"))
        self.assertGreater(int(self.helper.flag(argv, "max_bytes")), 3000)
        self.assertEqual((d["enabled"], d["mode"], d["app_target"]), (True, "on", "Weather"))
        self.assertIn("+// >>> iOS Studio", d["diff"])
        self.assertEqual(d["package_path"], f"{TPROJ}/Package.swift")
        steps = " ".join(d["next_steps"])
        self.assertIn(f"ios_hot_reload_start(project_path='{TPROJ}')", steps)
        self.assertIn("@ObserveHotReload", steps)
        self.assertIn("release", steps)

    async def test_dry_run_writes_nothing_and_a_cut_diff_points_at_the_file(self):
        self.helper.reply("hotreload", "enable", {"enabled": True, "mode": "on", "changed": True, "dryRun": True,
                                                  "diff": "+ line\n" * 400, "truncated": True,
                                                  "fullDiffPath": f"{HOME}/.cache/ios-studio/diffs/x.diff"})
        res = await self.call(HR, "ios_hot_reload_set", project_path=PROJ, enabled=True, dry_run=True)
        self.assertLessEqual(size_of(res), 12_000)
        d = res.structured_content
        self.assertEqual(self.helper.flag(self.helper.calls("hotreload", "enable")[0], "dry_run"), "")
        self.assertTrue(d["dry_run"] and d["truncated"])
        self.assertEqual(d["full_output_path"], "~/.cache/ios-studio/diffs/x.diff")
        self.assertIn("nothing was written", d["message"])
        self.assertIn("without dry_run", d["next_steps"][0])

    async def test_off_to_stub_lists_the_importing_files(self):
        users = [f"Sources/Weather/V{i}.swift" for i in range(14)]
        self.helper.reply("hotreload", "disable", {"enabled": False, "mode": "stub", "importedBy": users, "changed": True,
                                                   "diff": "-  .unsafeFlags\n", "sessionStopped": True})
        d = await self.data(HR, "ios_hot_reload_set", project_path="Weather", enabled=False)
        self.assertEqual((d["enabled"], d["mode"], d["imported_by_count"]), (False, "stub", 14))
        self.assertEqual(len(d["imported_by"]), 10)
        self.assertTrue(d["session_stopped"])
        self.assertIn("still `import HotReload`", d["message"])
        self.assertIn("session was stopped", d["message"])

    async def test_off_stops_this_servers_session_first(self):
        started = await self.data(HR, "ios_hot_reload_start", project_path="Weather")
        self.assertEqual(started["state"], "watching")
        self.helper.reply("hotreload", "disable", {"enabled": False, "mode": "off", "importedBy": [], "changed": True,
                                                   "diff": "-block\n", "sessionStopped": False})
        d = await self.data(HR, "ios_hot_reload_set", project_path="Weather", enabled=False)
        self.assertEqual(d["mode"], "off")
        self.assertTrue(d["session_stopped"])
        self.assertIn('{"cancel": true}', self.stdin_lines())
        self.assertEqual(self.state.jobs.get(started["job_id"]).state, "cancelled")
        self.assertIn("were removed", d["message"])

    async def test_errors_name_the_next_step(self):
        self.helper.fail("hotreload", "enable", "block-damaged", "The hot reload lines were changed by hand.")
        e = await self.error(HR, "ios_hot_reload_set", project_path="Weather", enabled=True)
        self.assertIn("[block-damaged]", e)
        self.assertIn("Next: Ask the user to restore", e)
        self.helper.fail("hotreload", "enable", "changed", "Package.swift changed.")
        e = await self.error(HR, "ios_hot_reload_set", project_path="Weather", enabled=True)
        self.assertIn("ios_hot_reload_set again", e)
        self.assertNotIn("ios_package_remove", e)      # the packages feature's hint for the same code is replaced


class StartTool(HotReloadCase):
    async def test_start_returns_once_watching_and_stop_ends_it(self):
        d = await self.data(HR, "ios_hot_reload_start", project_path="Weather", udid="U1")
        argv = self.helper.calls("hotreload", "run")[0]
        self.assertEqual((self.helper.flag(argv, "project"), self.helper.flag(argv, "udid")), (PROJ, "U1"))
        self.assertIsNone(self.helper.flag(argv, "preset"))
        self.assertEqual((d["state"], d["started_by"], d["running"]), ("watching", "mcp", True))
        self.assertTrue(d["job_id"].startswith("hr-"))
        self.assertEqual(d["last_seq"], 3)                       # patchSeq continues the previous session's
        self.assertIn("after_seq=3", " ".join(d["next_steps"]))
        self.assertIn("@ObserveHotReload", " ".join(d["next_steps"]))
        again = await self.data(HR, "ios_hot_reload_start", project_path=PROJ)
        self.assertEqual(again["job_id"], d["job_id"])
        self.assertIn("already runs", again["message"])
        self.assertEqual(len(self.helper.calls("hotreload", "run")), 1)
        out = await self.data(HR, "ios_hot_reload_control", project_path="Weather", action="stop")
        self.assertEqual((out["state"], out["stop_reason"], out["running"]), ("stopped", "stopped", False))
        self.assertEqual(out["history_total"], 1)
        self.assertIn('{"cancel": true}', self.stdin_lines())
        self.assertFalse(self.helper.calls("hotreload", "stop"))   # our own job: cancelled, no helper stop
        self.assertIn("ios_hot_reload_start", " ".join(out["next_steps"]))

    async def test_slow_start_returns_running_then_job_id_waits(self):
        self.helper.reply("hotreload", "status", {"project": f"{HOME}/Developer/Slow", "enabled": True, "mode": "on",
                                                  "session": None})
        self.helper.reply("projects", "presets", {"presets": []})
        with mock.patch.object(HR, "MAX_WAIT", 1.5):
            d = await self.data(HR, "ios_hot_reload_start", project_path="Slow", preset="German")
        self.assertEqual(self.helper.flag(self.helper.calls("hotreload", "run")[0], "preset"), "German")
        self.assertIn(d["state"], ("building", "running"))
        self.assertEqual(d["progress"]["fraction"], 0.3)
        self.assertIn(f"ios_hot_reload_start(job_id='{d['job_id']}')", " ".join(d["next_steps"]))
        Path(f"{self.helper.exe}.go").write_text("")
        d2 = await self.data(HR, "ios_hot_reload_start", job_id=d["job_id"])
        self.assertEqual(d2["state"], "watching")
        job = self.state.jobs.get(d["job_id"])
        self.assertEqual(job.tool, "ios_hot_reload_start")
        await self.state.jobs.cancel(job)                         # ios_job_cancel does this
        self.assertEqual(job.state, "cancelled")                  # its final snapshot is kept as the result
        self.assertEqual(job.result["stopReason"], "stopped")
        self.assertIn('{"cancel": true}', self.stdin_lines())

    async def test_failed_build_ends_with_diagnostics_and_a_hint(self):
        self.helper.reply("hotreload", "status", {"project": f"{HOME}/Developer/Broken", "enabled": True, "mode": "on",
                                                  "session": None})
        d = await self.data(HR, "ios_hot_reload_start", project_path="Broken")
        self.assertEqual((d["state"], d["stop_reason"]), ("stopped", "build-failed"))
        self.assertEqual(d["error"]["code"], "build-failed")
        self.assertEqual(d["last_error"]["diagnostics"][0]["location"], "Sources/Broken/A.swift:3:7")
        self.assertEqual(d["last_error"]["log_file"], "~/.local/state/b.log")
        self.assertIn("ios_build_diagnostics(project_path='~/Developer/Broken')", " ".join(d["next_steps"]))

    async def test_not_enabled_fails_before_starting(self):
        self.helper.reply("hotreload", "status", {"project": PROJ, "enabled": False, "mode": "off", "session": None})
        e = await self.error(HR, "ios_hot_reload_start", project_path="Weather")
        self.assertIn("[not-enabled]", e)
        self.assertIn(f"ios_hot_reload_set(project_path='{TPROJ}', enabled=true)", e)
        self.assertFalse(self.helper.calls("hotreload", "run"))

    async def test_a_session_from_the_app_is_reported_or_restarted(self):
        self.helper.reply("hotreload", "status", {"project": PROJ, "enabled": True, "mode": "on",
                                                  "session": snapshot(lastReload=RELOAD)})
        d = await self.data(HR, "ios_hot_reload_start", project_path="Weather")
        self.assertEqual((d["state"], d["started_by"]), ("watching", "app"))
        self.assertIn("iOS Studio window", d["message"])
        self.assertFalse(self.helper.calls("hotreload", "run"))
        self.helper.reply("hotreload", "stop", {"stopped": True, "session": snapshot("stopped")})
        d = await self.data(HR, "ios_hot_reload_start", project_path="Weather", restart=True)
        self.assertEqual(len(self.helper.calls("hotreload", "stop")), 1)
        self.assertEqual((d["state"], d["started_by"]), ("watching", "mcp"))

    async def test_needs_a_project_or_job_id(self):
        e = await self.error(HR, "ios_hot_reload_start")
        self.assertIn("ios_project_list", e)
        e = await self.error(HR, "ios_hot_reload_start", job_id="hr-99")
        self.assertIn("Unknown job_id", e)


class StatusTool(HotReloadCase):
    BUDGET = None           # the real 25 s budget, so wait_seconds reaches the helper

    async def test_after_seq_wait_reports_the_reload_with_timings(self):
        self.helper.reply("hotreload", "status", {
            "project": PROJ, "enabled": True, "mode": "on", "waited": 2.4, "timedOut": False,
            "session": snapshot("reloaded", message="Reloaded ContentView.swift in 2.9 s", lastReload=RELOAD,
                                history=[{"seq": 4, "ok": True, "files": [SRC], "ms": 2100, "swapped": 3, "time": 1},
                                         {"seq": 5, "ok": True, "files": [SRC], "ms": 2900, "swapped": 4, "time": 2}])})
        d = await self.data(HR, "ios_hot_reload_status", project_path="Weather", after_seq=4, wait_seconds=20)
        argv = self.helper.calls("hotreload", "status")[0]
        self.assertEqual(self.helper.flag(argv, "after_seq"), "4")
        self.assertLessEqual(int(self.helper.flag(argv, "wait_seconds")), 20)
        self.assertGreater(int(self.helper.flag(argv, "wait_seconds")), 0)
        self.assertEqual(d["reload_result"], "reloaded")
        self.assertEqual(d["last_seq"], 5)
        r = d["last_reload"]
        self.assertEqual((r["seq"], r["ms"], r["swapped"], r["reverted"]), (5, 2900, 4, 1))
        self.assertEqual(r["steps"]["compile"], 900)
        self.assertEqual([h["seq"] for h in d["history"]], [5, 4])       # newest first
        self.assertEqual(d["waited_seconds"], 2.4)
        self.assertIn("after_seq=5", " ".join(d["next_steps"]))

    async def test_failed_reload_is_bounded(self):
        self.helper.reply("hotreload", "status", {
            "project": PROJ, "enabled": True, "mode": "on",
            "session": snapshot("failed", lastError=failed_error(21), patchSeq=21, history=history(21),
                                notice={"id": "needs-run", "message": "Info.plist changed. " * 40})})
        res = await self.call(HR, "ios_hot_reload_status", project_path="Weather", after_seq=20)
        self.assertLessEqual(size_of(res), 12_000)
        d = res.structured_content
        self.assertEqual(d["reload_result"], "failed")
        e = d["last_error"]
        self.assertEqual((e["code"], e["seq"], e["location"]), ("unsupported-change", 21, f"{SRC}:12"))
        self.assertLessEqual(len(e["blockers"]), 10)
        self.assertLessEqual(len(e["diagnostics"]), 5)
        self.assertEqual(e["blockers"][0]["location"], SRC)                  # line 0: no line
        self.assertEqual(e["blockers"][1]["location"], f"{SRC}:1")
        self.assertNotIn("detail", e)                     # blockers say it; the 4000-char detail stays out
        self.assertLessEqual(len(d["history"]), 5)
        self.assertEqual(d["history_total"], 21)
        self.assertEqual((d["history"][0]["seq"], d["history"][0]["result"]), (21, "failed"))
        self.assertEqual(d["history"][1]["result"], "reloaded")
        steps = " ".join(d["next_steps"])
        self.assertIn("[unsupported-change]", steps)
        self.assertIn("restart=true", steps)
        self.assertTrue(steps.startswith("Notice: Info.plist changed."))
        self.assertNotIn("data:image", json.dumps(d))

    async def test_more_history_when_asked_and_waiting(self):
        self.helper.reply("hotreload", "status", {"project": PROJ, "enabled": True, "mode": "on", "timedOut": True,
                                                  "waited": 20.0, "session": snapshot(history=history(12))})
        d = await self.data(HR, "ios_hot_reload_status", project_path="Weather", history=12, after_seq=12)
        self.assertEqual(len(d["history"]), 12)
        self.assertEqual(d["reload_result"], "waiting")
        self.assertTrue(d["timed_out"])
        self.assertIsNone(self.helper.flag(self.helper.calls("hotreload", "status")[0], "wait_seconds"))

    async def test_no_session_says_what_to_do(self):
        d = await self.data(HR, "ios_hot_reload_status", project_path="Weather")
        self.assertEqual((d["state"], d["enabled"]), ("off", True))
        self.assertIn("ios_hot_reload_start(project_path=", d["next_steps"][0])
        self.helper.reply("hotreload", "status", {"project": PROJ, "enabled": False, "mode": "off", "session": None})
        d = await self.data(HR, "ios_hot_reload_status", project_path="Weather")
        self.assertIn("enabled=true", d["next_steps"][0])

    async def test_overview_lists_sessions_and_enabled_projects(self):
        sessions = [snapshot(path=f"{HOME}/Developer/P{i}", project=f"P{i}") for i in range(14)]
        projects = {f"{HOME}/Developer/P{i}": {"enabled": i % 2 == 0, "mode": ["on", "off", "stub"][i % 3]}
                    for i in range(60)}
        self.helper.reply("hotreload", "status", {"sessions": sessions, "projects": projects})
        res = await self.call(HR, "ios_hot_reload_status")
        self.assertLessEqual(size_of(res), 12_000)
        d = res.structured_content
        self.assertEqual(self.helper.calls("hotreload", "status")[0][2:], [])
        self.assertEqual(d["state"], "overview")
        self.assertEqual(len(d["sessions"]), 10)
        self.assertEqual(d["sessions"][0]["started_by"], "app")
        self.assertEqual(len(d["enabled_projects"]), 20)
        self.assertTrue(all(p["mode"] in ("on", "stub") for p in d["enabled_projects"]))
        self.assertTrue(d["truncated"])
        self.assertTrue(d["enabled_projects"][0]["project_path"].startswith("~/"))

    async def test_this_servers_session_is_marked(self):
        started = await self.data(HR, "ios_hot_reload_start", project_path="Weather")
        self.helper.reply("hotreload", "status", {"project": PROJ, "enabled": True, "mode": "on",
                                                  "session": snapshot()})
        d = await self.data(HR, "ios_hot_reload_status", project_path="Weather")
        self.assertEqual((d["job_id"], d["started_by"]), (started["job_id"], "mcp"))


class ControlTool(HotReloadCase):
    async def test_pause_and_resume(self):
        self.helper.reply("hotreload", "pause", {"paused": True, "session": snapshot(
            "paused", paused=True, pending=[f"Sources/Weather/F{i}.swift" for i in range(5)], pendingCount=9)})
        d = await self.data(HR, "ios_hot_reload_control", project_path="Weather", action="pause")
        self.assertEqual(self.helper.flag(self.helper.calls("hotreload", "pause")[0], "project"), PROJ)
        self.assertEqual((d["state"], d["paused"], d["pending_count"], len(d["pending"])), ("paused", True, 9, 5))
        self.assertIn("action='resume'", " ".join(d["next_steps"]))
        self.helper.reply("hotreload", "resume", {"paused": False, "session": snapshot("reloading", patchSeq=6)})
        d = await self.data(HR, "ios_hot_reload_control", project_path="Weather", action="resume")
        self.assertEqual((d["state"], d["paused"], d["last_seq"]), ("reloading", False, 5))
        self.assertIn("Resumed", d["message"])
        self.assertIn("after_seq=5", " ".join(d["next_steps"]))

    async def test_not_running_points_at_start(self):
        self.helper.fail("hotreload", "pause", "not-running", "Hot reload isn't running for this project.",
                         fix={"label": "Start hot reload", "page": "projects", "action": "hot-reload-start"})
        e = await self.error(HR, "ios_hot_reload_control", project_path="Weather", action="pause")
        self.assertIn("[not-running]", e)
        self.assertIn(f"ios_hot_reload_start(project_path='{TPROJ}')", e)

    async def test_stop_a_session_from_the_app(self):
        self.helper.reply("hotreload", "stop", {"stopped": True, "session": snapshot("stopped", stopReason="stopped",
                                                                                      running=False)})
        d = await self.data(HR, "ios_hot_reload_control", project_path="Weather", action="stop")
        self.assertEqual((d["state"], d["message"]), ("stopped", "Hot reload stopped."))
        self.helper.reply("hotreload", "stop", {"stopped": False, "session": None})
        d = await self.data(HR, "ios_hot_reload_control", project_path="Weather", action="stop")
        self.assertEqual((d["state"], d["message"]), ("off", "Hot reload wasn't running for this project."))


class Views(unittest.TestCase):
    def test_last_seq(self):
        self.assertEqual(HR.last_seq({"patchSeq": 7, "history": []}), 7)
        self.assertEqual(HR.last_seq({"patchSeq": 8, "state": "reloading", "history": [{"seq": 7}]}), 7)
        self.assertEqual(HR.last_seq({"patchSeq": 0, "lastError": {"seq": 3}}), 3)

    def test_app_not_running_while_watching(self):
        v = HR.to_view(snapshot(lastError={"code": "app-not-running", "message": "The app isn't running."}))
        steps = HR._next_steps(v, PROJ)
        self.assertIn("ios_app_launch", " ".join(steps))


class DemoRoundTrip(unittest.IsolatedAsyncioTestCase):
    """The real helper in demo mode: start -> watching, status waits for the scripted reload, pause, resume, stop."""

    async def test_demo(self):
        from iosstudio_mcp.state import AppState
        from support.mcp_harness import registered
        tmp = tempfile.mkdtemp(prefix="mcp-hr-")
        env = {"IOSSTUDIO_DEMO": "ready", "IOSSTUDIO_DEMO_DELAY": "0.2", "IOSSTUDIO_RUNTIME_DIR": tmp + "/run",
               "XDG_STATE_HOME": tmp + "/state", "XDG_CONFIG_HOME": tmp + "/config", "XDG_CACHE_HOME": tmp + "/cache"}
        os.makedirs(tmp + "/run")
        with mock.patch.dict(os.environ, env):
            rec = registered(HR)
            state = AppState()
            from types import SimpleNamespace
            ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=state))
            weather = str(Path.home() / "Developer/WeatherGlance")
            try:
                d = (await rec.tools["ios_hot_reload_start"](ctx=ctx, project_path=weather)).structured_content
                for _ in range(5):
                    if d["state"] not in HR.STARTING + ("running",):
                        break
                    d = (await rec.tools["ios_hot_reload_start"](ctx=ctx, job_id=d["job_id"])).structured_content
                self.assertEqual(d["state"], "watching", d)
                seq = d["last_seq"]
                st = (await rec.tools["ios_hot_reload_status"](ctx=ctx, project_path=weather, after_seq=seq,
                                                               wait_seconds=20)).structured_content
                self.assertEqual(st["reload_result"], "reloaded", st)
                self.assertEqual(st["started_by"], "mcp")
                self.assertGreater(st["last_reload"]["steps"]["compile"], 0)
                p = (await rec.tools["ios_hot_reload_control"](ctx=ctx, project_path=weather,
                                                               action="pause")).structured_content
                self.assertTrue(p["paused"], p)
                r = (await rec.tools["ios_hot_reload_control"](ctx=ctx, project_path=weather,
                                                               action="resume")).structured_content
                self.assertFalse(r["paused"], r)
            finally:
                s = (await rec.tools["ios_hot_reload_control"](ctx=ctx, project_path=weather,
                                                               action="stop")).structured_content
                await state.jobs.shutdown()
            self.assertEqual(s["state"], "stopped", s)


if __name__ == "__main__":
    unittest.main()
