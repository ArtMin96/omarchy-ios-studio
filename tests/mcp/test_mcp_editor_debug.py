"""tools/editor_debug.py: Debug in editor (ios_debug_editor_config / _start / _stop / _status).

- Against fakes (support/mcp_harness.py): `debug editor-config` and `debug editor-status` are FakeHelper replies;
  `debug editor` is an interactive fake below shaped like helper/iosstudio/features/editor_debug_bridge.py's
  stream (steps, notice, ready, editor, forward, then `done` after {"cancel": true} on stdin).
- The real helper in demo mode, with a stand-in device daemon where demo clients look (tests/support/fake_deviced.py).
- The real helper in real mode (the real bridge, state files and `editor-status`) against the stand-in daemon,
  skipped unless the helper's `debug check` says lldb is ready.

Needs the MCP server's dependencies (the dev venv; skipped otherwise):
    python -m pytest tests/mcp/test_mcp_editor_debug.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from iosstudio_mcp import helper as HELPER
from iosstudio_mcp.state import AppState
from iosstudio_mcp.tools import editor_debug as ED
from iosstudio_mcp.tools import jobs as JOBS
from support.fake_deviced import FakeDeviced
from support.lldb_ready import debugger_ready
from support.mcp_fakes import FakeMCP
from support.mcp_harness import FAKE_HELPER, ToolCase, size_of

HOME = os.path.expanduser("~")
PROJ = f"{HOME}/Developer/Weather"
INSTALLED = "XTL-TEAM123456.com.example.weather"
CFG = "Attach to Weather on iPhone (iOS Studio)"
APPS = [{"bundleId": INSTALLED, "name": "Weather", "developer": True, "running": True},
        {"bundleId": "com.apple.mobilesafari", "name": "Safari", "developer": False, "running": True}]

# The `debug editor` stream: <exe>.go releases a held ready (project ...Slow), <exe>.attach makes the editor attach,
# <exe>.move moves the forward to a new port; stdin lines are logged to <exe>.stdin.
EDITOR = textwrap.dedent("""\
    def editor(argv):
        import os, select, signal
        flags, i = {}, 3
        while i < len(argv):
            if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                flags[argv[i]] = argv[i + 1]; i += 2
            else:
                flags[argv[i]] = True; i += 1
        def out(name, **kw):
            print(json.dumps({"event": name, **kw}), flush=True)
        proj = flags.get("--project") or ""
        bid = flags.get("--bundle-id") or INSTALLED
        out("debug", mode="editor", bundleId=bid)
        out("step", id="lldb", title="Checking the debugger", state="done")
        if "--no-config" not in flags and proj:
            out("step", id="config", title="Writing the editor's debug configuration", state="done", detail="VS Code")
        out("step", id="forward", title="Starting the debugger on your iPhone", state="running")
        if proj.endswith("Missing"):
            out("done", ok=False, error={"code": "not-installed", "message": "Missing isn't installed on your iPhone.",
                                         "fix": {"page": "projects"}})
            return
        if bid.endswith("Busy") or proj.endswith("Busy"):
            out("done", ok=False, error={"code": "busy", "message": "Another debugger is attached to this app."})
            return
        while proj.endswith("Slow") and not os.path.exists(me + ".go"):
            time.sleep(0.02)
        launched = flags.get("--launch") == "suspended"
        out("step", id="forward", title="Starting the debugger on your iPhone", state="done", detail="port 52917")
        out("notice", id="no-symbols", level="info", message="This iPhone's debug symbols aren't downloaded. " * 20)
        if launched:
            out("notice", id="waiting", level="info", message="Weather waits for the debugger.")
        out("ready", bundleId=INSTALLED, appName="Weather", port=52917, host="127.0.0.1", pid=913, launched=launched,
            scriptPath=HOME + "/.local/state/ios-studio/editor-debug/com.example.weather.lldb",
            editors=[] if "--no-config" in flags or not proj else ["vscode"], configName=CFG, symbolsReady=False,
            localBuild=PROJ + "/xtool/Weather.app/Weather" if proj else None)
        stop = []
        signal.signal(signal.SIGTERM, lambda *a: stop.append("term"))
        attached = moved = False
        while not stop:
            if not attached and os.path.exists(me + ".attach"):
                attached = True
                out("editor", state="attached", connections=1)
            if not moved and os.path.exists(me + ".move"):
                moved = True
                out("forward", port=52999, host="127.0.0.1")
            try:
                r, _, _ = select.select([0], [], [], 0.05)
            except InterruptedError:
                continue
            if r:
                line = sys.stdin.readline()
                if not line:
                    break
                with open(me + ".stdin", "a") as f:
                    f.write(line)
                if json.loads(line).get("cancel"):
                    break
        out("done", ok=True, data={"ended": "cancelled", "bundleId": INSTALLED, "appName": "Weather", "pid": 913,
                                   "launched": launched, "attached": attached, "killed": launched and not attached,
                                   "scriptPath": HOME + "/.local/state/ios-studio/editor-debug/x.lldb"})
""")


class EditorCase(ToolCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.daemon.replies["apps.list"] = (0, APPS)
        self.helper.reply("projects", "list", {"projects": [
            {"name": n, "path": f"{HOME}/Developer/{n}"} for n in ("Weather", "Slow", "Missing", "Busy")]})
        self.helper.reply("debug", "editor-status", {"bridges": [], "checkedAt": 1})
        script = FAKE_HELPER.replace(
            'key = " ".join(sys.argv[1:3])\n',
            'key = " ".join(sys.argv[1:3])\n'
            'if key == "debug editor":\n    editor(sys.argv)\n    sys.exit(0)\n')
        script = script.replace("me = sys.argv[0]\n", f"me = sys.argv[0]\nimport time\nHOME = {HOME!r}\n"
                                f"PROJ = {PROJ!r}\nINSTALLED = {INSTALLED!r}\nCFG = {CFG!r}\n" + EDITOR)
        self.helper.exe.write_text(script)

    def flag_file(self, name: str) -> None:
        Path(f"{self.helper.exe}.{name}").write_text("")

    def stdin_lines(self) -> list[str]:
        p = Path(f"{self.helper.exe}.stdin")
        return p.read_text().splitlines() if p.exists() else []


class Annotations(EditorCase):
    async def test_kinds_and_registration(self):
        ann = self.rec(ED).annotations
        self.assertTrue(ann["ios_debug_editor_status"].read_only_hint)
        for name in ("ios_debug_editor_config", "ios_debug_editor_start", "ios_debug_editor_stop"):
            self.assertFalse(ann[name].read_only_hint, name)
            self.assertFalse(ann[name].destructive_hint, name)
        self.assertTrue(ann["ios_debug_editor_start"].open_world_hint)
        self.assertTrue(ann["ios_debug_editor_stop"].idempotent_hint)
        from iosstudio_mcp import tools
        self.assertIn("editor_debug", tools.MODULES)
        from iosstudio_mcp import resources
        guide = resources.GUIDE.read_text()
        for name in ("ios_debug_editor_config", "ios_debug_editor_start", "ios_debug_editor_status",
                     "ios_debug_editor_stop"):
            self.assertIn(name, guide)


class Config(EditorCase):
    def plan(self, done: bool) -> dict:
        return {"project": PROJ, "bundleId": "com.example.weather", "appName": "Weather", "configName": CFG,
                "scriptPath": f"{HOME}/.local/state/ios-studio/editor-debug/com.example.weather.lldb",
                "dryRun": not done, "applied": done, "notes": ["Zed: set dap.Swift.binary to lldb-dap " + "x" * 600],
                "editors": [
                    {"id": "vscode", "name": "VS Code", "path": f"{PROJ}/.vscode/launch.json", "state": "outdated",
                     "action": {"kind": "merge", "path": f"{PROJ}/.vscode/launch.json", "scope": "project",
                                "detail": f"Replaces “{CFG}”; every other configuration stays.",
                                "preview": "--- a\n+++ b\n" + "+ line\n" * 2000, "done": done}},
                    {"id": "zed", "name": "Zed", "path": f"{PROJ}/.zed/debug.json", "state": "present",
                     "action": {"kind": "skip", "path": f"{PROJ}/.zed/debug.json", "scope": "project",
                                "detail": "Already current."}}]}

    async def test_dry_run_shows_a_bounded_plan(self):
        self.helper.reply("debug", "editor-config", self.plan(False))
        res = await self.call(ED, "ios_debug_editor_config", project_path="Weather")
        self.assertLessEqual(size_of(res), 12_000)
        d = res.structured_content
        argv = self.helper.calls("debug", "editor-config")[0]
        self.assertEqual(self.helper.flag(argv, "project"), PROJ)
        self.assertEqual(self.helper.flag(argv, "dry_run"), "")
        self.assertIsNone(self.helper.flag(argv, "editor"))
        self.assertTrue(d["dry_run"])
        self.assertIn("Nothing was written", d["message"])
        self.assertIn("other configuration", d["message"])
        vs, zed = d["editors"]
        self.assertEqual((vs["state"], vs["action"], vs["path"]), ("outdated", "merge", "~/Developer/Weather/.vscode/launch.json"))
        self.assertLessEqual(len(vs["preview"]), ED.PREVIEW_CUT + 20)
        self.assertEqual((zed["state"], zed["action"]), ("present", "skip"))
        self.assertEqual(d["config_name"], CFG)
        self.assertTrue(d["attach_file"].startswith("~/"))
        self.assertTrue(any("confirm=true" in s for s in d["next_steps"]))
        self.assertTrue(any(s.startswith("For the user: Zed") for s in d["next_steps"]))

    async def test_confirm_writes(self):
        self.helper.reply("debug", "editor-config", self.plan(True))
        d = await self.data(ED, "ios_debug_editor_config", project_path=PROJ, editor="vscode", confirm=True)
        argv = self.helper.calls("debug", "editor-config")[0]
        self.assertIsNone(self.helper.flag(argv, "dry_run"))
        self.assertEqual(self.helper.flag(argv, "editor"), "vscode")
        self.assertFalse(d["dry_run"])
        self.assertTrue(d["applied"])
        self.assertIn("Written: VS Code", d["message"])
        self.assertTrue(any("ios_debug_editor_start" in s for s in d["next_steps"]))

    async def test_helper_error_is_one_line(self):
        self.helper.fail("debug", "editor-config", "no-bundle-id", "This project has no bundle ID yet.")
        e = await self.error(ED, "ios_debug_editor_config", project_path="Weather")
        self.assertIn("no bundle ID", e)


class Bridge(EditorCase):
    async def asyncTearDown(self):
        await self.state.jobs.shutdown()        # like the server: every bridge is asked to stop
        await super().asyncTearDown()

    async def test_start_ready_attached_status_stop(self):
        res = await self.call(ED, "ios_debug_editor_start", project_path="Weather")
        self.assertLessEqual(size_of(res), 12_000)
        d = res.structured_content
        self.assertEqual(d["state"], "ready", d)
        self.assertEqual((d["port"], d["pid"], d["config_name"], d["editors"]), (52917, 913, CFG, ["vscode"]))
        self.assertEqual(d["editor"], "waiting")
        self.assertEqual(d["bundle_id"], INSTALLED)
        self.assertFalse(d.get("symbols_ready", False))
        self.assertTrue(d["local_build"])
        self.assertTrue(d["job_id"].startswith("dbe-"))
        self.assertTrue(all(len(n) <= ED.NOTE_CUT + 20 for n in d["notices"]))
        self.assertTrue(any("F5" in s and CFG in s for s in d["next_steps"]))
        self.assertTrue(any("ios_debug_editor_stop" in s for s in d["next_steps"]))
        argv = self.helper.calls("debug", "editor")[0]
        self.assertEqual(self.helper.flag(argv, "project"), PROJ)
        self.assertEqual(self.helper.flag(argv, "launch"), "auto")
        self.assertIsNone(self.helper.flag(argv, "no_config"))
        job = d["job_id"]

        # a second start for the same app joins the running bridge
        again = await self.data(ED, "ios_debug_editor_start", project_path=PROJ)
        self.assertEqual(again["job_id"], job)
        self.assertIn("already runs", again["message"])
        self.assertEqual(len(self.helper.calls("debug", "editor")), 1)

        # the editor attaches, the forward moves: a poll with job_id reports both
        self.flag_file("attach")
        d = await self.data(ED, "ios_debug_editor_start", job_id=job, wait_seconds=3)
        if d["editor"] != "attached":
            d = await self.data(ED, "ios_debug_editor_start", job_id=job, wait_seconds=3)
        self.assertEqual((d["state"], d["editor"], d["attached"]), ("ready", "attached", True))
        self.flag_file("move")
        d = await self.data(ED, "ios_debug_editor_start", job_id=job, wait_seconds=3)
        self.assertEqual((d["port"], d["forward_moved"]), (52999, 1))

        st = await self.data(ED, "ios_debug_editor_status")
        self.assertEqual(len(st["bridges"]), 1)
        b = st["bridges"][0]
        self.assertEqual((b["job_id"], b["started_by"], b["editor"], b["port"]), (job, "mcp", "attached", 52999))

        # ios_job_status sees the job too
        js = await self.data(JOBS, "ios_job_status", job_id=job, wait_seconds=0)
        self.assertEqual((js["kind"], js["state"]), ("debug-editor", "running"))

        out = await self.data(ED, "ios_debug_editor_stop", job_id=job)
        self.assertTrue(out["done"])
        self.assertEqual(out["data"]["ended"], "cancelled")
        self.assertTrue(out["data"]["attached"])
        self.assertEqual(self.stdin_lines(), ['{"cancel": true}'])
        d = await self.data(ED, "ios_debug_editor_start", job_id=job)
        self.assertEqual((d["state"], d["ended"]), ("ended", "cancelled"))

        # idempotent: nothing left to stop
        out = await self.data(ED, "ios_debug_editor_stop")
        self.assertTrue(out["done"])
        self.assertIn("No Debug in editor bridge", out["message"])

    async def test_slow_start_is_a_job_polled_with_job_id(self):
        d = await self.data(ED, "ios_debug_editor_start", project_path="Slow", launch="suspended",
                            write_config=False, editor="zed")
        self.assertEqual(d["state"], "running")
        self.assertIn("Starting the debugger", d["message"])
        self.assertIn("job_id=", d["next_steps"][0])
        argv = self.helper.calls("debug", "editor")[0]
        self.assertEqual(self.helper.flag(argv, "no_config"), "")
        self.assertEqual(self.helper.flag(argv, "launch"), "suspended")
        self.assertEqual(self.helper.flag(argv, "editor"), "zed")
        self.flag_file("go")
        d = await self.data(ED, "ios_debug_editor_start", job_id=d["job_id"])
        self.assertEqual(d["state"], "ready")
        self.assertTrue(d["launched"])
        self.assertTrue(any("suspended" in s for s in d["next_steps"]))
        self.assertTrue(any("No editor config was written" in s for s in d["next_steps"]))
        # stop by bundle id; a suspended app nobody attached to is ended
        out = await self.data(ED, "ios_debug_editor_stop", bundle_id="com.example.weather")
        self.assertTrue(out["data"]["killed"])

    async def test_by_bundle_id(self):
        d = await self.data(ED, "ios_debug_editor_start", bundle_id="com.example.weather")
        self.assertEqual(d["state"], "ready")
        argv = self.helper.calls("debug", "editor")[0]
        self.assertEqual(self.helper.flag(argv, "bundle_id"), INSTALLED)       # the installed id
        self.assertFalse(d["local_build"])
        self.assertTrue(any("ios_run" in s for s in d["next_steps"]))
        e = await self.error(ED, "ios_debug_editor_start", bundle_id="com.apple.mobilesafari")
        self.assertIn("ios_app_list(scope='own')", e)
        e = await self.error(ED, "ios_debug_editor_start")
        self.assertIn("project_path", e)

    async def test_busy_and_not_installed_have_next_steps(self):
        d = await self.data(ED, "ios_debug_editor_start", project_path="Busy")
        self.assertEqual((d["state"], d["error"]["code"]), ("failed", "busy"))
        self.assertIn("ios_debug_session_end", d["next_steps"][0])
        d = await self.data(ED, "ios_debug_editor_start", project_path="Missing")
        self.assertEqual((d["state"], d["error"]["code"]), ("failed", "not-installed"))
        self.assertIn("ios_run", d["next_steps"][0])
        js = await self.data(JOBS, "ios_job_status", job_id=d["job_id"], wait_seconds=0)
        self.assertEqual(js["state"], "failed")

    async def test_ios_job_cancel_stops_it_the_same_way(self):
        d = await self.data(ED, "ios_debug_editor_start", project_path="Weather")
        await self.data(JOBS, "ios_job_cancel", job_id=d["job_id"])
        self.assertEqual(self.stdin_lines(), ['{"cancel": true}'])
        d = await self.data(ED, "ios_debug_editor_start", job_id=d["job_id"])
        self.assertEqual(d["state"], "ended")

    async def test_server_exit_stops_the_bridge(self):
        await self.data(ED, "ios_debug_editor_start", project_path="Weather")
        await self.state.jobs.shutdown()
        self.assertEqual(self.stdin_lines(), ['{"cancel": true}'])

    async def test_status_shows_a_bridge_from_the_app_window_and_stop_ends_it(self):
        other = subprocess.Popen(["sleep", "60"])
        self.addCleanup(other.kill)
        self.helper.reply("debug", "editor-status", {"bridges": [{
            "bundleId": "com.example.notes", "installedBundleId": "XTL-T.com.example.notes", "appName": "Notes",
            "project": f"{HOME}/Developer/Notes", "port": 50001, "host": "127.0.0.1", "pid": 77, "launched": False,
            "editor": "detached", "since": "2026-11-11T10:00:00Z", "helperPid": other.pid,
            "scriptPath": f"{HOME}/.local/state/ios-studio/editor-debug/com.example.notes.lldb"}], "checkedAt": 1})
        st = await self.data(ED, "ios_debug_editor_status")
        b = st["bridges"][0]
        self.assertEqual((b["started_by"], b["bundle_id"], b["project"]), ("app", "com.example.notes",
                                                                            "~/Developer/Notes"))
        self.assertNotIn("job_id", b)
        out = await self.data(ED, "ios_debug_editor_stop", bundle_id="com.example.notes")
        self.assertEqual(other.wait(5), -15)                         # SIGTERM, which the helper handles
        self.assertFalse(out["done"])                                # the fake status still lists it
        self.assertIn("still running", out["message"])


class Demo(unittest.IsolatedAsyncioTestCase):
    """The real helper in demo mode (no files written) with a stand-in demo daemon: start, attached, stop."""

    async def test_demo_round_trip(self):
        run = tempfile.mkdtemp(prefix="ied-", dir="/tmp")       # short enough for the socket path
        self.addCleanup(shutil.rmtree, run, True)
        replies = {"debug.start": {"bundleId": "XTL-A1B2C3D4E5.com.example.weatherglance", "port": 52917,
                                   "host": "127.0.0.1", "pid": 913, "launched": False, "symbolsReady": True},
                   "debug.stop": lambda a: {"bundleId": a["bundleId"], "stopped": True, "killed": None}}
        with mock.patch.dict(os.environ, {"IOSSTUDIO_DEMO": "1", "IOSSTUDIO_RUNTIME_DIR": run}), \
                FakeDeviced(Path(run), replies, demo=True) as daemon:
            mcp = FakeMCP()
            ED.register(mcp)
            state = AppState()
            ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=state),
                                  report_progress=None, info=None)
            try:
                projects = (await HELPER.call("projects", "list"))["projects"]
                proj = projects[0]["path"]
                res = await mcp.tools["ios_debug_editor_config"](ctx, project_path=proj)
                cfg = res.structured_content
                self.assertTrue(cfg["dry_run"])
                self.assertEqual(cfg["editors"][0]["id"], "vscode")
                res = await mcp.tools["ios_debug_editor_start"](ctx, project_path=proj)
                d = res.structured_content
                self.assertEqual((d["state"], d["port"]), ("ready", 52917), d)
                self.assertEqual(d["config_name"], cfg["config_name"])
                for _ in range(5):          # demo: the editor attaches right after ready
                    if d["editor"] == "attached":
                        break
                    d = (await mcp.tools["ios_debug_editor_start"](ctx, job_id=d["job_id"], wait_seconds=5)
                         ).structured_content
                self.assertEqual(d["editor"], "attached")
                st = (await mcp.tools["ios_debug_editor_status"](ctx)).structured_content
                self.assertEqual(st["bridges"][0]["job_id"], d["job_id"])
                out = (await mcp.tools["ios_debug_editor_stop"](ctx)).structured_content
                self.assertEqual(out["data"]["ended"], "cancelled")
                self.assertEqual(len(daemon.sent("debug.stop")), 1)
            finally:
                await state.jobs.shutdown()


@unittest.skipUnless(debugger_ready(), "needs the Swift toolchain's lldb with its Python (Setup › Debugger support)")
class RealHelper(unittest.IsolatedAsyncioTestCase):
    """The real `debug editor` bridge (attach file, state file, editor-status) against a stand-in daemon."""

    async def test_real_bridge(self):
        run = tempfile.mkdtemp(prefix="ied-", dir="/tmp")
        self.addCleanup(shutil.rmtree, run, True)
        proj = Path(tempfile.mkdtemp(prefix="Target-")) / "Target"
        self.addCleanup(shutil.rmtree, proj.parent, True)
        (proj / "Sources/Target").mkdir(parents=True)
        (proj / "Package.swift").write_text("// swift-tools-version:5.9\nimport PackageDescription\n"
                                            "let package = Package(name: \"Target\")\n")
        (proj / "xtool.yml").write_text("version: 1\nbundleID: com.example.target\nproduct: Target\n")
        installed = "XTL-TEAM123456.com.example.target"
        replies = {"apps.list": {"apps": [{"bundleId": installed}]},
                   "debug.start": {"bundleId": installed, "port": 1, "host": "127.0.0.1", "pid": 4242,
                                   "launched": False, "symbolsReady": False},
                   "debug.stop": lambda a: {"bundleId": a["bundleId"], "stopped": True, "killed": None},
                   "processes.list": [{"pid": 4242, "name": "Target", "isApp": True}]}
        env = {k: v for k, v in os.environ.items() if k != "IOSSTUDIO_DEMO"}
        env["IOSSTUDIO_RUNTIME_DIR"] = run
        with mock.patch.dict(os.environ, env, clear=True), FakeDeviced(Path(run), replies) as daemon:
            mcp = FakeMCP()
            ED.register(mcp)
            state = AppState()
            state._projects = (time.time(), {"projects": [{"name": "Target", "path": str(proj)}]})
            ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=state),
                                  report_progress=None, info=None)
            try:
                res = await mcp.tools["ios_debug_editor_config"](ctx, project_path="Target", editor="vscode")
                self.assertEqual(res.structured_content["editors"][0]["action"], "write")
                self.assertFalse((proj / ".vscode/launch.json").exists())          # a dry run writes nothing
                d = (await mcp.tools["ios_debug_editor_start"](ctx, project_path="Target", editor="vscode")
                     ).structured_content
                for _ in range(5):
                    if d["state"] != "running":
                        break
                    d = (await mcp.tools["ios_debug_editor_start"](ctx, job_id=d["job_id"])).structured_content
                self.assertEqual(d["state"], "ready", d)
                self.assertEqual((d["bundle_id"], d["pid"], d["editors"]), (installed, 4242, ["vscode"]))
                self.assertTrue((proj / ".vscode/launch.json").is_file())
                st = (await mcp.tools["ios_debug_editor_status"](ctx)).structured_content
                self.assertEqual(len(st["bridges"]), 1, st)
                b = st["bridges"][0]
                self.assertEqual((b["bundle_id"], b["job_id"], b["started_by"]), ("com.example.target", d["job_id"],
                                                                                  "mcp"))
                out = (await mcp.tools["ios_debug_editor_stop"](ctx, job_id=d["job_id"])).structured_content
                self.assertEqual(out["data"]["ended"], "cancelled")
                self.assertEqual(daemon.sent("debug.stop")[-1]["kill"], False)
                st = (await mcp.tools["ios_debug_editor_status"](ctx)).structured_content
                self.assertEqual(st.get("bridges", []), [])
            finally:
                await state.jobs.shutdown()
