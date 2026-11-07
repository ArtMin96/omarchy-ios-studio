"""Every tool call answers within its time budget (mcp/iosstudio_mcp/core/deadline.py), and the slow tools are
jobs (ios_a11y_audit, ios_toolchain_info, ios_app_run_and_capture).

Real code paths: tools run through the real @tool wrapper; the iPhone is a fake device daemon on a Unix socket
that speaks the real protocol (JSON lines, docs/deviced-protocol.md) to the real DeviceClient, with replies
that take as long as the test says; the helper is a fake `iosstudio-helper` executable run by the real
helper.call(). The budget is shortened (MAX_WAIT patched) so a test takes seconds.

Needs the MCP server's dependencies (the dev venv; skipped otherwise):
    python -m pytest tests/mcp/test_mcp_timeouts.py
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import stat
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mcp.server.mcpserver.exceptions import ToolError

from iosstudio_mcp import helper
from iosstudio_mcp.core import deadline
from iosstudio_mcp.core.registry import tool as real_tool
from iosstudio_mcp.models import ActionResult
from iosstudio_mcp.state import AppState
from iosstudio_mcp.tools import logs as LOGS
from iosstudio_mcp.tools import toolchain as TOOLCHAIN
from iosstudio_mcp.tools import ui_inspect as UI
from support.mcp_fakes import FakeMCP


class Recorder(FakeMCP):
    """FakeMCP that also takes resources (crash reports), which these tests don't call."""

    def resource(self, *a, **k):
        return lambda fn: fn


BUDGET = 2.0          # MAX_WAIT for these tests
SLACK = 0.6           # what a call may take beyond its budget (result building, scheduling)


class FakeDaemon:
    """The device daemon's request/reply protocol on a Unix socket. replies[cmd] = (delay seconds, data) or
    (delay, {"error": {...}}) or a callable(args) -> one of those. Like the real broker, a request runs as its own
    task and keeps going when the client stops waiting."""

    def __init__(self, path: str):
        self.path = path
        self.replies: dict = {}
        self.requests: list[tuple[str, dict]] = []
        self.server = None
        self.writers: list[asyncio.StreamWriter] = []

    async def start(self) -> None:
        self.server = await asyncio.start_unix_server(self._serve, path=self.path)

    async def stop(self) -> None:
        for w in self.writers:
            w.close()
        self.server.close()
        await self.server.wait_closed()

    async def _serve(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        self.writers.append(w)
        tasks = set()
        while line := await r.readline():
            msg = json.loads(line)
            t = asyncio.create_task(self._answer(w, msg))
            tasks.add(t)
            t.add_done_callback(tasks.discard)

    async def _answer(self, w: asyncio.StreamWriter, msg: dict) -> None:
        cmd, args = msg["cmd"], msg.get("args") or {}
        self.requests.append((cmd, args))
        if cmd in ("hello", "subscribe"):
            rep = (0, {"pid": os.getpid(), "version": 1})
        else:
            rep = self.replies.get(cmd, (0, {}))
            if callable(rep):
                rep = rep(args)
        delay, data = rep
        await asyncio.sleep(delay)
        out = {"id": msg["id"], "ok": False, "error": data["error"]} if isinstance(data, dict) and "error" in data \
            else {"id": msg["id"], "ok": True, "data": data}
        with contextlib.suppress(Exception):
            w.write((json.dumps(out) + "\n").encode())
            await w.drain()
        if cmd == "syslog.start":   # a little log after the subscription, like the phone
            await asyncio.sleep(0.2)
            ev = {"event": "syslog", "data": [{"ts": time.time(), "level": "error", "process": "App", "pid": 42,
                                               "message": "Could not load settings"}]}
            with contextlib.suppress(Exception):
                w.write((json.dumps(ev) + "\n").encode())
                await w.drain()

    def count(self, cmd: str) -> int:
        return sum(1 for c, _ in self.requests if c == cmd)


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp(prefix="iost-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.daemon = FakeDaemon(os.path.join(self.tmp, "d.sock"))
        await self.daemon.start()
        self.clients = []
        self.state = AppState()
        self.state.device = self.new_client("mcp", [])
        await self.state.device.connect()
        self.ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=self.state),
                                   report_progress=self._progress)
        patch = mock.patch.object(deadline, "MAX_WAIT", BUDGET)
        patch.start()
        self.addCleanup(patch.stop)

    async def asyncTearDown(self):
        await self.state.jobs.shutdown()
        for c in self.clients:
            with contextlib.suppress(Exception):
                await c.close()
        await self.daemon.stop()

    async def _progress(self, *a, **k):
        pass

    def new_client(self, name, events):
        mod = helper.device_client_module()
        c = mod.DeviceClient(name=name, events=events, spawn=False, reconnect=False, socket=self.daemon.path)
        self.clients.append(c)
        return c

    def tools(self, module):
        """The module's tools through the real @tool wrapper (budget, to_result, one-line errors)."""
        rec = Recorder()
        with mock.patch.object(module, "tool", lambda m, *a, **k: real_tool(rec, *a, **k)):
            module.register(rec)
        return rec.tools

    async def timed(self, coro):
        t0 = time.monotonic()
        try:
            return await coro, time.monotonic() - t0
        except ToolError as exc:
            return exc, time.monotonic() - t0


# ------------------------------------------------------------------ the budget itself

class Deadline(unittest.IsolatedAsyncioTestCase):
    async def test_outside_a_call_nothing_changes(self):
        self.assertIsNone(deadline.remaining())
        self.assertEqual(deadline.clamp(600), 600)
        self.assertEqual(deadline.wait_time(30), 30)

    async def test_a_call_cuts_every_step_to_what_is_left(self):
        with deadline.call_budget(5):
            self.assertLessEqual(deadline.clamp(600), 5 - deadline.RESERVE)
            self.assertEqual(deadline.clamp(2), 2)
            await asyncio.sleep(0.3)
            self.assertLess(deadline.clamp(600), 5 - deadline.RESERVE - 0.2)
            self.assertLessEqual(deadline.wait_time(25), 5 - deadline.RESERVE)
        self.assertIsNone(deadline.remaining())

    async def test_nothing_left_is_a_timeout_error(self):
        with deadline.call_budget(1.2):
            await asyncio.sleep(0.5)
            with self.assertRaises(ToolError) as cm:
                deadline.clamp(10, "the iPhone answered ui.tree")
            self.assertIn("[timeout]", str(cm.exception))
            self.assertIn("ui.tree", str(cm.exception))
            self.assertEqual(deadline.wait_time(10), 0)

    async def test_a_nested_budget_never_extends_the_outer_one(self):
        with deadline.call_budget(3):
            with deadline.call_budget(25):
                self.assertLessEqual(deadline.remaining(), 3)

    async def test_jobs_run_without_the_budget_of_the_call_that_started_them(self):
        state = AppState()
        seen = []

        async def work(job):
            seen.append(deadline.remaining())
            return 1
        with deadline.call_budget(3):
            job = state.jobs.run(work, "t", title="t")
            await state.jobs.wait(job, 2)
        self.assertEqual(seen, [None])

    async def test_a_long_poll_is_cut_to_the_budget(self):
        state = AppState()
        gate = asyncio.Event()

        async def work(job):
            await gate.wait()
        job = state.jobs.run(work, "t", title="t")
        t0 = time.monotonic()
        with deadline.call_budget(1.5):
            await state.jobs.wait(job, 25)
        self.assertLess(time.monotonic() - t0, 1.5)
        gate.set()
        await state.jobs.wait(job, 2)
        self.assertEqual(job.state, "succeeded")

    async def test_the_tool_wrapper_opens_a_budget(self):
        rec = FakeMCP()
        seen = []

        @real_tool(rec, "T", kind="read")
        async def probe() -> ActionResult:
            seen.append(deadline.remaining())
            return ActionResult(done=True, message="ok")
        await rec.tools["probe"]()
        self.assertTrue(deadline.MAX_WAIT - 0.5 < seen[0] <= deadline.MAX_WAIT)


class OwnResult(unittest.TestCase):
    def test_a_tool_that_builds_its_own_result_passes_through(self):
        """ios_screenshot returns a CallToolResult with an image; to_result must not wrap it as a model."""
        from mcp.types import CallToolResult, TextContent

        from iosstudio_mcp.core.registry import to_result
        res = CallToolResult(content=[TextContent(type="text", text="hi")], structured_content={"path": "/x"})
        self.assertIs(to_result(res), res)


class HelperCall(unittest.IsolatedAsyncioTestCase):
    """The real helper.call() against a fake helper executable that hangs."""

    async def test_a_hung_helper_is_stopped_at_the_budget(self):
        tmp = tempfile.mkdtemp(prefix="iost-")
        self.addCleanup(shutil.rmtree, tmp, True)
        fake = Path(tmp) / "iosstudio-helper"
        pidfile = Path(tmp) / "pid"
        fake.write_text(f"#!/bin/sh\necho $$ > {pidfile}\nexec sleep 30\n")
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        t0 = time.monotonic()
        with mock.patch.object(helper, "HELPER", fake), deadline.call_budget(3):
            with self.assertRaises(ToolError) as cm:
                await helper.call("toolchain", "info", timeout=60)
        self.assertLess(time.monotonic() - t0, 3)
        self.assertIn("[timeout]", str(cm.exception))
        pid = int(pidfile.read_text())
        with self.assertRaises(ProcessLookupError):   # killed with its process group
            os.kill(pid, 0)


# ------------------------------------------------------------------ capped calls

class CappedCalls(Base):
    async def test_ui_tree_answers_within_the_budget_when_the_phone_is_slow(self):
        self.daemon.replies["ui.tree"] = (10, {"elements": []})
        res, took = await self.timed(self.tools(UI)["ios_ui_tree"](self.ctx))
        self.assertIsInstance(res, ToolError)
        self.assertIn("[timeout]", str(res))
        self.assertLess(took, BUDGET + SLACK)

    async def test_ui_tree_in_time_is_unchanged(self):
        self.daemon.replies["ui.tree"] = (0.2, {"elements": [{"id": "e1", "role": "button", "label": "OK"}],
                                                "complete": True})
        res, took = await self.timed(self.tools(UI)["ios_ui_tree"](self.ctx))
        self.assertEqual(res.structured_content["elements"][0]["label"], "OK")

    async def test_two_slow_steps_share_one_budget(self):
        """ui.press after a slow front-app check: the second step gets what the first left, not its own 25 s."""
        self.daemon.replies["ui.press"] = (10, {})
        res, took = await self.timed(self.tools(UI)["ios_ui_press"](self.ctx, element_id="e1"))
        self.assertIsInstance(res, ToolError)
        self.assertLess(took, BUDGET + SLACK)

    async def test_a_slow_crash_pull_keeps_going_and_the_next_call_joins_it(self):
        from iosstudio_mcp.tools import crashes as CR
        ips = Path(self.tmp) / "App.ips"
        ips.write_text('{"app_name":"App"}\n{"threads": []}\n')
        self.daemon.replies["crash.pull"] = (BUDGET + 0.5, {"path": str(ips), "summary": {"process": "App"}})
        get = self.tools(CR)["ios_crash_get"]
        res, took = await self.timed(get(self.ctx, name="/App.ips"))
        self.assertIsInstance(res, ToolError)
        self.assertIn("[running]", str(res))
        self.assertIn("Call ios_crash_get again", str(res))
        self.assertLess(took, BUDGET + SLACK)
        await asyncio.sleep(1)          # the agent waits a moment, as told
        res, took = await self.timed(get(self.ctx, name="/App.ips"))
        self.assertLess(took, BUDGET + SLACK)
        self.assertEqual(res.structured_content["process"], "App")
        res, _ = await self.timed(get(self.ctx, name="/App.ips", section="raw"))   # right after: the same pull
        self.assertEqual(self.daemon.count("crash.pull"), 1)

    async def test_a_crash_pull_inside_a_job_waits_for_the_whole_pull(self):
        from iosstudio_mcp.backends import crashes as BCR
        ips = Path(self.tmp) / "App.ips"
        ips.write_text('{"app_name":"App"}\n{"threads": []}\n')
        self.daemon.replies["crash.pull"] = (BUDGET + 0.5, {"path": str(ips), "summary": {"process": "App"}})

        async def work(job):          # e.g. ios_app_run_and_capture reading the new crash
            return (await BCR.crash_detail(self.state, "/App.ips")).process
        job = self.state.jobs.run(work, "t", title="t")
        await asyncio.wait({job.task})
        self.assertEqual((job.state, job.result), ("succeeded", "App"))

    async def test_a_pulled_report_is_read_off_the_event_loop_and_bounded(self):
        from iosstudio_mcp.backends import crashes as BCR
        from iosstudio_mcp.tools import crashes as CR
        ips = Path(self.tmp) / "App.ips"
        ips.write_text('{"app_name":"App"}\n' + json.dumps({"threads": []}) + "\n" + "x" * 50_000)
        self.daemon.replies["crash.pull"] = (0, {"path": str(ips), "summary": {"process": "App"}})
        with mock.patch.object(BCR, "REPORT_CAP", 10_000):
            res, _ = await self.timed(self.tools(CR)["ios_crash_get"](self.ctx, name="/App.ips", section="raw"))
        self.assertEqual(res.structured_content["size"], ips.stat().st_size)
        self.assertLessEqual(len(res.content[0].text), 14_000)

    async def test_a_daemon_that_never_starts_is_a_timeout_not_a_hang(self):
        from iosstudio_mcp.backends import device as D
        self.state.device = None
        slow = self.new_client("slow", [])

        async def hang():
            await asyncio.sleep(30)
        slow.connect = hang
        with mock.patch.object(D, "new_client", lambda *a, **k: slow):
            res, took = await self.timed(self.tools(UI)["ios_ui_tree"](self.ctx))
        self.assertIsInstance(res, ToolError)
        self.assertIn("still starting", str(res))
        self.assertLess(took, BUDGET + SLACK)
        self.assertIsNone(self.state.device)      # half-open client dropped: the next call starts over


# ------------------------------------------------------------------ jobs

AUDIT = {"issues": [{"type": "contrast", "message": "Low contrast", "label": "Sign in"}],
         "supported": ["contrast"], "unsupported": [], "elapsedMs": 3000, "bundleId": "com.example.app", "pid": 42}


class A11yAuditJob(Base):
    async def test_a_slow_audit_returns_a_job_then_its_result(self):
        self.daemon.replies["ui.audit"] = (BUDGET + 1, AUDIT)       # longer than one call
        tools = self.tools(UI)
        res, took = await self.timed(tools["ios_a11y_audit"](self.ctx, bundle_id="com.example.app"))
        self.assertLess(took, BUDGET + SLACK)
        first = res.structured_content
        self.assertEqual(first["state"], "running")
        self.assertIn("ios_a11y_audit(job_id=", " ".join(first["next_steps"]))
        for _ in range(5):
            res, took = await self.timed(tools["ios_a11y_audit"](self.ctx, job_id=first["job_id"]))
            self.assertLess(took, BUDGET + SLACK)
            done = res.structured_content
            if done.get("state") != "running":
                break
        self.assertEqual((done["total"], done["counts"]), (1, {"contrast": 1}))
        self.assertEqual(self.daemon.count("ui.audit"), 1)                 # polling never audits again

    async def test_a_fast_audit_answers_in_one_call(self):
        self.daemon.replies["ui.audit"] = (0.1, AUDIT)
        res, _ = await self.timed(self.tools(UI)["ios_a11y_audit"](self.ctx))
        self.assertEqual(res.structured_content["total"], 1)

    async def test_a_daemon_error_is_the_tools_error(self):
        self.daemon.replies["ui.audit"] = (0, {"error": {"code": "locked", "message": "Your iPhone is locked."}})
        res, _ = await self.timed(self.tools(UI)["ios_a11y_audit"](self.ctx))
        self.assertIsInstance(res, ToolError)
        self.assertIn("[locked]", str(res))
        self.assertIn("unlock", str(res))

    async def test_wrong_job_kind_is_refused(self):
        job = self.state.jobs.run(lambda j: asyncio.sleep(0), "install", title="x")
        res, _ = await self.timed(self.tools(UI)["ios_a11y_audit"](self.ctx, job_id=job.id))
        self.assertIsInstance(res, ToolError)
        self.assertIn("not a11y-audit", str(res))


FAKE_HELPER = textwrap.dedent("""\
    #!/usr/bin/env python3
    import json, sys, time
    group, action = sys.argv[1], sys.argv[2]
    if (group, action) == ("toolchain", "info"):
        time.sleep(float(open(sys.argv[0] + ".delay").read()))
        print(json.dumps({"ok": True, "data": {"items": [{"id": "swift", "name": "Swift", "version": "6.2"}],
                                               "sdks": {"iPhoneOS": "26.0"}}}))
    elif (group, action) == ("toolchain", "disk"):
        print(json.dumps({"ok": True, "data": {"items": [{"id": "cache", "name": "Cache", "size": 300 * 2**20,
                                                          "cleanable": True, "safe": True}],
                                               "total": 300 * 2**20, "reclaimable": 300 * 2**20}}))
    else:
        print(json.dumps({"ok": False, "error": {"code": "usage", "message": "unexpected"}}))
""")


class ToolchainInfoJob(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = tempfile.mkdtemp(prefix="iost-")
        self.addCleanup(shutil.rmtree, tmp, True)
        self.fake = Path(tmp) / "iosstudio-helper"
        self.fake.write_text(FAKE_HELPER)
        self.fake.chmod(self.fake.stat().st_mode | stat.S_IEXEC)
        for p in (mock.patch.object(helper, "HELPER", self.fake), mock.patch.object(deadline, "MAX_WAIT", BUDGET)):
            p.start()
            self.addCleanup(p.stop)
        self.state = AppState()
        self.ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=self.state))
        rec = FakeMCP()
        with mock.patch.object(TOOLCHAIN, "tool", lambda m, *a, **k: real_tool(rec, *a, **k)):
            TOOLCHAIN.register(rec)
        self.tool = rec.tools["ios_toolchain_info"]

    async def asyncTearDown(self):
        await self.state.jobs.shutdown()

    async def test_slow_versions_come_back_as_a_job(self):
        Path(str(self.fake) + ".delay").write_text(str(BUDGET + 1))
        t0 = time.monotonic()
        first = (await self.tool(self.ctx)).structured_content
        self.assertLess(time.monotonic() - t0, BUDGET + SLACK)
        self.assertEqual(first["state"], "running")
        for _ in range(5):
            done = (await self.tool(self.ctx, job_id=first["job_id"])).structured_content
            if done.get("state") != "running":
                break
        self.assertEqual(done["tools"][0]["version"], "6.2")
        self.assertEqual(done["reclaimable_mb"], 300.0)
        self.assertIn("ios_toolchain_clean", done["next_steps"][0])

    async def test_fast_versions_in_one_call(self):
        Path(str(self.fake) + ".delay").write_text("0")
        done = (await self.tool(self.ctx)).structured_content
        self.assertNotEqual(done.get("state"), "running")
        self.assertEqual(done["sdks"], {"iPhoneOS": "26.0"})


APP = {"bundleId": "com.example.app", "executable": "App", "name": "App", "running": True, "developer": True}


class RunAndCaptureJob(Base):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.daemon.replies.update({
            "apps.list": (0, [APP]), "crash.list": (0, []), "app.kill": (0, {}),
            "app.launch": (0.5, {"pid": 42}), "processes.list": (0, [{"bundleId": "com.example.app", "pid": 42}]),
            "syslog.start": (0, {}), "syslog.stop": (0, {}),
        })
        p = mock.patch.object(LOGS, "new_client", lambda name, events: self.new_client(name, events))
        p.start()
        self.addCleanup(p.stop)

    async def test_a_run_longer_than_a_call_is_a_job(self):
        tools = self.tools(LOGS)
        res, took = await self.timed(tools["ios_app_run_and_capture"](self.ctx, bundle_id="com.example.app",
                                                                       seconds=2, console=False))
        self.assertLess(took, BUDGET + SLACK)
        first = res.structured_content
        self.assertEqual(first["state"], "running")
        self.assertTrue(first["job_id"].startswith("run-capture-"))
        out = None
        for _ in range(5):
            res, took = await self.timed(tools["ios_app_run_and_capture"](self.ctx, job_id=first["job_id"]))
            self.assertLess(took, BUDGET + SLACK)
            out = res.structured_content
            if out.get("state") != "running":
                break
        self.assertTrue(out["launched"])
        self.assertEqual((out["pid"], out["running_at_end"]), (42, True))
        self.assertEqual(out["log_counts"], {"error": 1})
        self.assertIn("Could not load settings", res.content[0].text)
        self.assertEqual(self.daemon.count("app.launch"), 1)
        self.assertEqual(self.daemon.count("syslog.stop"), 1)          # the capture's own session is closed

    async def test_unknown_app_fails_the_job_with_its_message(self):
        res, _ = await self.timed(self.tools(LOGS)["ios_app_run_and_capture"](self.ctx, bundle_id="com.nope",
                                                                               console=False))
        self.assertIsInstance(res, ToolError)
        self.assertIn("No app 'com.nope'", str(res))

    async def test_cancel_closes_the_capture(self):
        self.daemon.replies["app.launch"] = (10, {"pid": 42})
        res, _ = await self.timed(self.tools(LOGS)["ios_app_run_and_capture"](self.ctx, bundle_id="com.example.app",
                                                                               console=False))
        job = self.state.jobs.get(res.structured_content["job_id"])
        await self.state.jobs.cancel(job)
        self.assertEqual(job.state, "cancelled")
        self.assertEqual(LOGS._sessions(self.state), {})


# ------------------------------------------------------------------ slow reads that became jobs

STORES = {"running": False, "stores": [{"name": "default.store", "path": "/Library/Application Support/default.store",
                                         "size": 4096, "kind": "swiftdata",
                                         "tables": [{"name": "ZITEM", "entity": "Item", "rows": 2,
                                                     "columns": [{"name": "ZTITLE", "type": "VARCHAR"}]}]}]}


class SlowReads(Base):
    """Calls whose daemon step used to wait up to 10 minutes: now a job (or a joined job) that answers small
    reads at once and big ones with state='running' + job_id, never asking the iPhone twice."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.daemon.replies["apps.list"] = (0, [APP])

    async def poll(self, fn, **kw):
        out = None
        for _ in range(5):
            res, took = await self.timed(fn(self.ctx, **kw))
            self.assertLess(took, BUDGET + SLACK)
            out = res.structured_content
            if out.get("state") != "running":
                break
        return out

    async def test_a_big_store_summary_is_a_job(self):
        from iosstudio_mcp.tools import app_data as AD
        self.daemon.replies["appdata.stores"] = (BUDGET + 0.5, STORES)
        store = self.tools(AD)["ios_app_store"]
        res, took = await self.timed(store(self.ctx, bundle_id="com.example.app", wait_seconds=1))
        self.assertLess(took, BUDGET + SLACK)
        first = res.structured_content
        self.assertEqual(first["state"], "running")
        self.assertIn("ios_app_store(job_id=", " ".join(first["next_steps"]))
        done = await self.poll(store, job_id=first["job_id"])
        self.assertEqual(done["stores"][0]["tables"][0]["entity"], "Item")
        self.assertEqual(self.daemon.count("appdata.stores"), 1)

    async def test_a_small_store_read_answers_in_one_call(self):
        from iosstudio_mcp.tools import app_data as AD
        self.daemon.replies["appdata.stores"] = (0.1, STORES)
        self.daemon.replies["appdata.rows"] = (0.1, {"table": "ZITEM", "columns": [{"name": "ZTITLE", "type": "VARCHAR"}],
                                                     "rows": [["Milk"], ["Eggs"]], "total": 2})
        res, _ = await self.timed(self.tools(AD)["ios_app_store"](self.ctx, bundle_id="com.example.app", table="Item"))
        out = res.structured_content
        self.assertEqual((out["state"], out["table"], out["rows"]), ("succeeded", "ZITEM", [["Milk"], ["Eggs"]]))
        self.assertEqual(self.daemon.requests[-1][1]["store"], "/Library/Application Support/default.store")

    async def test_slow_rows_come_back_through_the_job(self):
        from iosstudio_mcp.tools import app_data as AD
        self.daemon.replies["appdata.rows"] = (BUDGET + 0.5, {"table": "ZITEM", "columns": [], "rows": [[1]] * 30,
                                                              "total": 90})
        store = self.tools(AD)["ios_app_store"]
        res, _ = await self.timed(store(self.ctx, bundle_id="com.example.app", store="/x/default.store",
                                        table="ZITEM", offset=30, limit=30))
        first = res.structured_content
        self.assertEqual(first["state"], "running")
        done = await self.poll(store, job_id=first["job_id"])
        self.assertEqual((done["offset"], done["count"], done["next_offset"]), (30, 30, 60))
        self.assertEqual(self.daemon.count("appdata.stores"), 0)      # a store path needs no summary first

    async def test_an_unknown_table_is_the_error_of_the_call(self):
        from iosstudio_mcp.tools import app_data as AD
        self.daemon.replies["appdata.stores"] = (0, STORES)
        res, _ = await self.timed(self.tools(AD)["ios_app_store"](self.ctx, bundle_id="com.example.app", table="Nope"))
        self.assertIsInstance(res, ToolError)
        self.assertIn("No table 'Nope'", str(res))

    async def test_a_slow_reset_dry_run_is_a_job(self):
        from iosstudio_mcp.tools import app_data as AD
        self.daemon.replies["appdata.reset"] = (BUDGET + 0.5, {"items": [{"path": "Documents", "kind": "dir",
                                                                          "size": 2000, "files": 3}],
                                                               "count": 1, "files": 3, "bytes": 2000})
        reset = self.tools(AD)["ios_app_reset"]
        res, took = await self.timed(reset(self.ctx, bundle_id="com.example.app"))
        self.assertLess(took, BUDGET + SLACK)
        first = res.structured_content
        self.assertEqual(first["state"], "running")
        done = await self.poll(reset, job_id=first["job_id"])
        self.assertEqual(done["state"], "dry_run")
        self.assertIn("This would delete 1 item(s)", done["message"])
        self.assertNotIn("confirm", self.daemon.requests[-1][1])       # still only a dry run

    async def test_a_slow_disk_image_mount_keeps_going_and_is_joined(self):
        from iosstudio_mcp.tools import device as DEV
        self.daemon.replies.update({"devmode.status": (0, {"enabled": True}), "ddi.status": (0, {"mounted": False}),
                                    "ddi.mount": (BUDGET + 0.5, {"mounted": True})})
        prepare = self.tools(DEV)["ios_device_prepare"]
        res, took = await self.timed(prepare(self.ctx))
        self.assertLess(took, BUDGET + SLACK)
        first = res.structured_content
        self.assertFalse(first["ready"])
        self.assertTrue(first["mount_job_id"])
        self.assertIn("call ios_device_prepare again", " ".join(first["next_steps"]))
        await asyncio.sleep(1)          # the agent waits a moment, as told
        res, took = await self.timed(prepare(self.ctx))
        self.assertLess(took, BUDGET + SLACK)
        self.assertTrue(res.structured_content["ddi_mounted"])
        self.assertTrue(res.structured_content["ready"])
        self.assertEqual(self.daemon.count("ddi.mount"), 1)


if __name__ == "__main__":
    unittest.main()
