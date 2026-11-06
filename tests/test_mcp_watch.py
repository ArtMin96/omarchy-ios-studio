"""MCP side of watch mode (mcp/iosstudio_mcp/tools/watch.py): ios_watch_start / stop / status against a mocked
helper (flags, owner pid, bounded output with the first ≤ 5 errors, wait_seconds, another project watched),
ios_build_diagnostics(build_id='w-N') from the build history, and one round trip through the real helper in
demo mode. Also that the server registers the tools.

Needs the MCP server's dependencies (skipped otherwise):
    ~/.local/share/ios-studio/mcp-venv/bin/python -m unittest tests.test_mcp_watch
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mcp"))

try:
    from mcp.server.mcpserver.exceptions import ToolError
    from iosstudio_mcp import budget as B
    from iosstudio_mcp.state import AppState
    from iosstudio_mcp.tools import build as BT
    from iosstudio_mcp.tools import watch as T
except ImportError as exc:  # the system python has no `mcp` package
    raise unittest.SkipTest(f"MCP dependencies not installed ({exc})")

PROJ = "/home/u/Developer/Glance"


class FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self, **kw):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


def snapshot(state="failed", errors=2, **extra) -> dict:
    diags = [{"severity": "error", "category": "compiler", "file": f"{PROJ}/Sources/Glance/V{i}.swift", "line": 10 + i,
              "col": 5, "message": f"cannot find 'x{i}' in scope " + "very long " * 60} for i in range(errors)]
    diags.append({"severity": "warning", "category": "compiler", "file": f"{PROJ}/Sources/Glance/W.swift", "line": 1,
                  "message": "unused"})
    return {"event": "watch", "seq": 7, "state": state, "reason": "build-finished", "message": f"{errors} errors",
            "project": "Glance", "path": PROJ, "pid": 4321, "mode": "inotify", "autoRun": False, "debounceMs": 600,
            "watching": state != "stopped", "builds": 3, "superseded": 1, "buildId": "w-3",
            "changed": [f"Sources/Glance/V{i}.swift" for i in range(9)], "changedCount": 9,
            "lastBuild": {"id": "w-3", "result": "failed" if errors else "ok", "duration": 8.4, "time": 1_790_000_000,
                          "errors": errors, "warnings": 1, "logFile": "/home/u/.local/state/ios-studio/build-logs/x.log",
                          "diagnostics": diags[:5], "errorCode": "build-failed" if errors else None},
            **extra}


class WatchTools(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        mcp = FakeMCP()
        T.register(mcp)
        self.tools = mcp.tools
        self.state = AppState()
        self.ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=self.state))
        self.calls: list = []

    async def run_tool(self, name, result, **kw):
        async def fake_call(group, action, flags=None, **opts):
            self.calls.append((group, action, dict(flags or {}), opts))
            return result

        async def fake_resolve(state, project):
            return {"name": "Glance", "path": PROJ}

        with mock.patch.object(T.helper, "call", fake_call), mock.patch.object(T, "resolve_project", fake_resolve):
            res = await self.tools[name](self.ctx, **kw)
        text = "".join(c.text for c in res.content if getattr(c, "text", None))
        return res.structured_content, text

    async def test_start_detaches_with_this_server_as_owner(self):
        data, _ = await self.run_tool("ios_watch_start", snapshot("compiling", 0), project_path="Glance",
                                      auto_run=True, debounce_ms=900)
        (group, action, flags, opts), = self.calls
        self.assertEqual((group, action), ("watch", "run"))
        self.assertEqual(flags, {"project": PROJ, "auto_run": "true", "debounce_ms": 900, "detach": True,
                                 "owner": os.getpid()})
        self.assertGreaterEqual(opts["timeout"], 30)
        self.assertEqual(data["state"], "compiling")
        self.assertEqual(data["current_build_id"], "w-3")
        self.assertIn("wait_seconds=20", " ".join(data["next_steps"]))

    async def test_start_passes_only_given_settings(self):
        await self.run_tool("ios_watch_start", snapshot("ok", 0), project_path="Glance")
        await self.run_tool("ios_watch_start", snapshot("ok", 0), project_path="Glance", auto_run=False)
        a, b = self.calls[0][2], self.calls[1][2]
        self.assertNotIn("auto_run", a)
        self.assertNotIn("debounce_ms", a)
        self.assertEqual(b["auto_run"], "false")    # an explicit False reaches the helper (build_argv drops False)

    async def test_status_is_bounded_with_first_errors(self):
        data, text = await self.run_tool("ios_watch_status", snapshot("failed", 9), project_path="Glance")
        self.assertEqual(data["state"], "failed")
        self.assertEqual(data["last_build"]["errors"], 9)
        errs = data["first_errors"]
        self.assertLessEqual(len(errs), 5)
        self.assertEqual(errs[0]["location"], "Sources/Glance/V0.swift:10:5")      # relative to the project
        self.assertLessEqual(len(errs[0]["message"]), 215)   # 200 + the "…[+N]" marker
        self.assertEqual(len(data["changed_files"]), 5)
        self.assertEqual(data["last_build"]["log_file"], "~/.local/state/ios-studio/build-logs/x.log"
                         if str(Path.home()) == "/home/u" else data["last_build"]["log_file"])
        self.assertIn("ios_build_diagnostics(build_id='w-3')", " ".join(data["next_steps"]))
        self.assertLess(B.nbytes(text) + B.nbytes(json.dumps(data)), B.DEFAULT_BYTES)

    async def test_wait_seconds_goes_to_the_helper_with_a_longer_timeout(self):
        data, _ = await self.run_tool("ios_watch_status", snapshot("ok", 0, waited=4.2, timedOut=False),
                                      wait_seconds=20)
        (_, action, flags, opts), = self.calls
        self.assertEqual((action, flags), ("status", {"wait_seconds": 20}))
        self.assertGreater(opts["timeout"], 20)
        self.assertEqual((data["state"], data["waited_seconds"]), ("ok", 4.2))
        self.assertIn("ios_run(", " ".join(data["next_steps"]))

    async def test_status_names_the_other_watched_project(self):
        res = {"watching": False, "state": "off", "message": "Not watching this project",
               "other": {"project": "Other", "path": "/home/u/Developer/Other", "state": "ok"}}
        data, _ = await self.run_tool("ios_watch_status", res, project_path="Glance")
        self.assertFalse(data.get("watching"))
        self.assertTrue(data["other_project"].endswith("Developer/Other"))
        self.assertIn("ios_watch_start", data["next_steps"][0])

    async def test_stop(self):
        res = {**snapshot("stopped", 0), "stopped": True, "watching": False, "stopReason": "stopped"}
        data, _ = await self.run_tool("ios_watch_stop", res)
        (_, action, flags, _), = self.calls
        self.assertEqual((action, flags), ("stop", {}))
        self.assertTrue(data["stopped"])
        self.assertEqual((data["state"], data["stop_reason"]), ("stopped", "stopped"))

    async def test_auto_run_skip_is_explained(self):
        res = snapshot("ok", 0, autoRun=True, run={"state": "skipped", "message": "Not run: no iPhone has been "
                                                   "connected yet.", "buildId": "w-4"})
        data, _ = await self.run_tool("ios_watch_status", res)
        self.assertEqual(data["run"]["state"], "skipped")
        self.assertIn("Auto-run skipped: Not run: no iPhone", " ".join(data["next_steps"]))


class WatchDiagnostics(unittest.IsolatedAsyncioTestCase):
    async def test_w_ids_come_from_the_build_history(self):
        mcp = FakeMCP()
        BT.register(mcp)
        ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=AppState()))
        hist = [{"buildId": "w-3", "trigger": "watch", "project": "Glance", "path": PROJ, "result": "failed", "time": 2,
                 "diagnostics": [{"severity": "error", "category": "compiler", "file": f"{PROJ}/Sources/Glance/A.swift",
                                  "line": 3, "col": 1, "message": "cannot find 'y' in scope"}]},
                {"buildId": None, "project": "Glance", "path": PROJ, "result": "ok", "time": 1, "diagnostics": []}]

        async def fake_call(group, action, flags=None, **_):
            return {"builds": hist}

        with mock.patch.object(BT.helper, "call", fake_call):
            res = await mcp.tools["ios_build_diagnostics"](ctx, build_id="w-3")
            data = res.structured_content
            self.assertEqual((data["errors"], data["build_result"], data["build_id"]), (1, "failed", "w-3"))
            steps = " ".join(data["next_steps"])
            self.assertIn("ios_watch_status(wait_seconds=", steps)
            self.assertNotIn("ios_build_start", steps)
            res = await mcp.tools["ios_build_history"](ctx)
            first = res.structured_content["builds"][0]
            self.assertEqual(first.get("build_id"), "w-3")
            self.assertEqual(data["diagnostics"][0]["locations"], ["Sources/Glance/A.swift:3:1"])
            with self.assertRaises(ToolError):
                await mcp.tools["ios_build_diagnostics"](ctx, build_id="w-99")


class DiagnosticsPicksTheNewerBuild(unittest.IsolatedAsyncioTestCase):
    """ios_build_diagnostics(project_path): this session's latest job, unless a history entry (a watch build)
    started after that job ended."""

    async def run_diag(self, hist_time):
        from iosstudio_mcp.jobs import Job
        mcp = FakeMCP()
        BT.register(mcp)
        state = AppState()
        job = Job(id="b-1", kind="build", action="compile", title="Compile", project_path=PROJ, project="Glance")
        job.state, job.started, job.ended = "failed", 100.0, 110.0
        job.diagnostics = [{"severity": "error", "message": "from the job", "file": f"{PROJ}/Sources/A.swift",
                            "line": 1, "category": "compiler"}]
        state.jobs.jobs[job.id] = job
        ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=state))
        hist = [{"buildId": "w-7", "project": "Glance", "path": PROJ, "result": "failed", "time": hist_time,
                 "diagnostics": [{"severity": "error", "message": "from the watch build", "category": "compiler",
                                  "file": f"{PROJ}/Sources/B.swift", "line": 2}]}]

        async def fake_call(group, action, flags=None, **_):
            return {"builds": hist}

        async def fake_resolve(state, project):
            return {"name": "Glance", "path": PROJ}

        with mock.patch.object(BT.helper, "call", fake_call), mock.patch.object(BT, "resolve_project", fake_resolve):
            res = await mcp.tools["ios_build_diagnostics"](ctx, project_path="Glance")
        d = res.structured_content
        if d["source"] == "build b-1":   # a session job keeps its id and the rebuild advice
            self.assertEqual(d["build_id"], "b-1")
            self.assertIn("ios_build_start", " ".join(d["next_steps"]))
        return d["source"], d["diagnostics"][0]["message"]

    async def test_newer_watch_build_wins(self):
        self.assertEqual(await self.run_diag(120.0), ("history (latest build)", "from the watch build"))

    async def test_older_equal_or_missing_keeps_the_job(self):
        for t in (105.0, 110.0, None):
            self.assertEqual(await self.run_diag(t), ("build b-1", "from the job"), t)


class DemoRoundTrip(unittest.IsolatedAsyncioTestCase):
    """The real helper in demo mode: start, status, stop (a private runtime dir)."""

    async def test_demo(self):
        tmp = tempfile.mkdtemp(prefix="mcp-watch-")
        env = {"IOSSTUDIO_DEMO": "ready", "IOSSTUDIO_RUNTIME_DIR": tmp, "XDG_STATE_HOME": tmp + "/state"}
        with mock.patch.dict(os.environ, env):
            mcp = FakeMCP()
            T.register(mcp)
            ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=AppState()))
            habits = str(Path.home() / "Developer/HabitTracker")
            try:
                res = await mcp.tools["ios_watch_start"](ctx, project_path=habits)
                self.assertEqual(res.structured_content["state"], "watching")
                res = await mcp.tools["ios_watch_status"](ctx, wait_seconds=10)
                st = res.structured_content
                self.assertEqual(st["state"], "failed")
                self.assertEqual(st["first_errors"][0]["message"], "cannot find 'habbits' in scope")
                self.assertEqual(st["first_errors"][0]["location"], "Sources/HabitTracker/ContentView.swift:42:17")
            finally:
                res = await mcp.tools["ios_watch_stop"](ctx)
            self.assertTrue(res.structured_content["stopped"])


class Registration(unittest.TestCase):
    def test_server_lists_the_tools(self):
        from iosstudio_mcp import server
        names = {t.name for t in server.mcp._tool_manager.list_tools()}
        self.assertTrue({"ios_watch_start", "ios_watch_stop", "ios_watch_status"} <= names)
        self.assertIn("ios_watch_start", server.INSTRUCTIONS)


if __name__ == "__main__":
    unittest.main()
