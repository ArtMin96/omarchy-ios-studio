"""Feature tools on the shared job manager: each starts its work as a job, returns state='running' with a job_id
when it outlasts wait_seconds, and answers the same call with job_id; ios_job_status / ios_job_respond /
ios_job_cancel (tools/jobs.py) work on every one of them. Covers ios_app_install, ios_files_pull/push,
ios_launch_profile, ios_screen_record, ios_pcap_capture and ios_project_create with the device and helper
faked; app data and the screenshot matrix have their own test files.

Needs the MCP server's dependencies (the dev venv; skipped otherwise):
    python -m pytest tests/mcp/test_mcp_job_tools.py
"""
from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mcp.server.mcpserver.exceptions import ToolError

from iosstudio_mcp.backends import device as D
from iosstudio_mcp.core.jobs import Job
from iosstudio_mcp.state import AppState
from iosstudio_mcp.tools import device as TD
from iosstudio_mcp.tools import files as TF
from iosstudio_mcp.tools import jobs as TJ
from iosstudio_mcp.tools import metrics as TM
from iosstudio_mcp.tools import network as TN
from iosstudio_mcp.tools import projects as TP
from iosstudio_mcp.tools import screen as TS
from support.mcp_fakes import FakeMCP

HOME = str(Path.home())


class Ctx(SimpleNamespace):
    async def report_progress(self, *a, **k):
        self.progress.append(a)


class Base(unittest.IsolatedAsyncioTestCase):
    MODULES = (TJ,)

    async def asyncSetUp(self):
        mcp = FakeMCP()
        for mod in self.MODULES + (TJ,):
            with mock.patch.object(mod, "tool", lambda m, *a, **k: m.tool(**k)):   # bare functions: models
                mod.register(mcp)
        self.tools = mcp.tools
        self.state = AppState()
        self.ctx = Ctx(request_context=SimpleNamespace(lifespan_context=self.state), progress=[])
        self.calls: list = []

    async def asyncTearDown(self):
        await self.state.jobs.shutdown()

    def patch(self, target, attr, value):
        p = mock.patch.object(target, attr, value)
        p.start()
        self.addCleanup(p.stop)

    async def call(self, tool_name, **kw):
        return await self.tools[tool_name](self.ctx, **kw)


class GenericTools(Base):
    async def test_status_respond_cancel_on_a_feature_job(self):
        async def work(job: Job) -> dict:
            job.report(0.25, "asking")
            v = await job.ask({"id": "p1", "message": "Overwrite?", "options": [
                {"value": "yes", "label": "Overwrite", "destructive": True}, {"value": "no"}]})
            return {"answer": v}

        job = self.state.jobs.run(work, "transfer", title="copy", tool="ios_files_push")
        st = await self.call("ios_job_status", job_id=job.id, wait_seconds=5)
        self.assertEqual((st.state, st.pending_prompt.id, st.tool), ("needs_input", "p1", "ios_files_push"))
        self.assertIn("ios_job_respond", " ".join(st.next_steps))
        with self.assertRaises(ToolError):       # destructive needs confirm
            await self.call("ios_job_respond", job_id=job.id, prompt_id="p1", value="yes")
        with self.assertRaises(ToolError):       # not an option
            await self.call("ios_job_respond", job_id=job.id, prompt_id="p1", value="maybe")
        st = await self.call("ios_job_respond", job_id=job.id, prompt_id="p1", value="yes", confirm=True)
        self.assertEqual((st.state, st.detail), ("succeeded", {"answer": "yes"}))

        slow = self.state.jobs.run(lambda j: asyncio.sleep(30), "pcap", title="slow")
        st = await self.call("ios_job_cancel", job_id=slow.id)
        self.assertEqual(st.state, "cancelled")
        with self.assertRaises(ToolError):
            await self.call("ios_job_status", job_id="b-404", wait_seconds=0)


class Install(Base):
    MODULES = (TD,)

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.gate = asyncio.Event()

        async def install_ipa(state, udid, path, on_progress=None):
            on_progress(40.0, "copying")
            await self.gate.wait()
            return {"bundleId": "XTL-T.com.example.app", "seconds": 3}

        async def package_for(state, path, project_path):
            return "/tmp/App.ipa", None

        self.patch(D, "install_ipa", install_ipa)
        self.patch(TD, "package_for", package_for)

    async def test_install_job(self):
        r = await self.call("ios_app_install", path="/tmp/App.ipa", wait_seconds=0.2)
        self.assertEqual((r.state, r.progress), ("running", 0.4))
        self.assertTrue(r.job_id.startswith("install-"))
        self.assertIn(f"ios_app_install(job_id='{r.job_id}'", r.next_steps[0])
        st = await self.call("ios_job_status", job_id=r.job_id, wait_seconds=0)
        self.assertEqual((st.kind, st.tool, st.progress), ("install", "ios_app_install", 0.4))
        self.gate.set()
        done = await self.call("ios_app_install", job_id=r.job_id, wait_seconds=5)
        self.assertEqual((done.state, done.installed, done.bundle_id), ("succeeded", True, "XTL-T.com.example.app"))
        with self.assertRaises(ToolError):       # a job of another kind
            other = self.state.jobs.run(lambda j: asyncio.sleep(0), "matrix", title="m")
            await self.call("ios_app_install", job_id=other.id)

    async def test_cancelled_install_says_so(self):
        r = await self.call("ios_app_install", path="/tmp/App.ipa", wait_seconds=0)
        await self.call("ios_job_cancel", job_id=r.job_id)
        with self.assertRaises(ToolError):
            await self.call("ios_app_install", job_id=r.job_id, wait_seconds=0)


class FakeDevice:
    def __init__(self, test):
        self.test = test
        self.release = asyncio.Event()
        self.outcome: Exception | dict = {"files": [HOME + "/Downloads/a.txt"], "bytes": 10, "dest": HOME + "/Downloads"}

    async def request(self, cmd, args, timeout=60):
        self.test.calls.append((cmd, args))
        await self.release.wait()
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class Files(Base):
    MODULES = (TF,)

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.dev = FakeDevice(self)

        async def client(state):
            return self.dev

        async def resolve_app(state, bundle_id, udid=None):
            return {"bundleId": bundle_id, "name": "App", "developer": True}

        async def dcall(state, cmd, args=None, **kw):
            self.calls.append((cmd, args))
            if cmd == "transfer.cancel":
                self.dev.outcome = ToolError("Cancelled [cancelled]")
                self.dev.release.set()
            return {}

        self.patch(TF, "client", client)
        self.patch(TF, "resolve_app", resolve_app)
        self.patch(TF, "dcall", dcall)
        self.patch(TF.helper, "inside_home", lambda p: True)

    def emit(self, name, data):
        for fn in list(D.hub(self.state)["listeners"]):
            fn(name, data)

    async def test_pull_progress_and_result(self):
        r = await self.call("ios_files_pull", bundle_id="com.example.app", paths=["/Documents/a.txt"], wait_seconds=0)
        self.assertEqual((r.state, r.kind), ("running", "container.pull"))
        await asyncio.sleep(0.01)
        self.assertEqual(self.calls[0][1]["transferId"], r.job_id)          # the daemon's id is the job id
        self.emit("transfer", {"id": r.job_id, "bytes": 5, "total": 10, "file": "a.txt", "files": 0})
        st = await self.call("ios_job_status", job_id=r.job_id, wait_seconds=0)
        self.assertEqual((st.progress, st.progress_detail), (0.5, "a.txt"))
        with self.assertRaises(ToolError):       # a pull isn't polled with the push tool
            await self.call("ios_files_push", job_id=r.job_id)
        self.dev.release.set()
        done = await self.call("ios_files_pull", job_id=r.job_id, wait_seconds=5)
        self.assertEqual((done.state, done.files_total), ("succeeded", 1))

    async def test_cancel_tells_the_daemon(self):
        r = await self.call("ios_files_pull", bundle_id="com.example.app", paths=["/Documents"], wait_seconds=0)
        st = await self.call("ios_job_cancel", job_id=r.job_id)
        self.assertEqual(st.state, "cancelled")
        self.assertIn(("transfer.cancel", {"transferId": r.job_id}), self.calls)
        done = await self.call("ios_files_pull", job_id=r.job_id, wait_seconds=0)
        self.assertEqual(done.state, "cancelled")


class Profiles(Base):
    MODULES = (TM,)

    async def test_launch_profile_job(self):
        gate = asyncio.Event()

        async def dcall(state, cmd, args=None, **kw):
            await gate.wait()
            return {"verdict": "Launch took 0.8 s", "totalMs": 800}

        async def resolve_app(state, bundle_id, udid=None):
            return {"bundleId": bundle_id, "name": "App"}

        self.patch(TM, "dcall", dcall)
        self.patch(TM, "resolve_app", resolve_app)
        r = await self.call("ios_launch_profile", bundle_id="com.example.app", confirm=True, wait_seconds=0)
        self.assertEqual((r.state, r.kind), ("running", "launch-profile"))
        with self.assertRaises(ToolError):       # one at a time
            await self.call("ios_launch_profile", bundle_id="com.example.app", confirm=True, wait_seconds=0)
        gate.set()
        done = await self.call("ios_launch_profile", job_id=r.job_id, wait_seconds=5)
        self.assertEqual((done.state, done.message), ("succeeded", "Launch took 0.8 s"))
        with self.assertRaises(ToolError):
            await self.call("ios_memory_leaks", job_id=r.job_id)


class Recording(Base):
    MODULES = (TS,)

    async def test_record_job_and_early_stop(self):
        status = {"state": "recording", "path": "/tmp/r.mp4", "seconds": 1}

        async def dcall(state, cmd, args=None, **kw):
            self.calls.append(cmd)
            if cmd == "record.start":
                return {"path": "/tmp/r.mp4", "mode": "screenshot", "state": "recording"}
            if cmd == "record.stop":
                status.update(state="done", seconds=2, bytes=2000)
                return dict(status)
            return dict(status)

        self.patch(TS, "dcall", dcall)
        r = await self.call("ios_screen_record", seconds=30, wait_seconds=0, thumbnail=False)
        info = r.structured_content
        self.assertEqual(info["state"], "recording")
        self.assertTrue(info["job_id"].startswith("recording-"))
        st = await self.call("ios_job_cancel", job_id=info["job_id"])
        self.assertEqual(st.state, "cancelled")
        self.assertIn("record.stop", self.calls)
        r = await self.call("ios_screen_record", job_id=info["job_id"], wait_seconds=0, thumbnail=False)
        self.assertEqual((r.structured_content["state"], r.structured_content["seconds"]), ("done", 2))


class Pcap(Base):
    MODULES = (TN,)

    async def test_capture_job_stops_early(self):
        st = {"file": "/tmp/cap-1.pcapng", "running": True, "maxSeconds": 60, "seconds": 1, "packets": 3}

        async def dcall(state, cmd, args=None, **kw):
            self.calls.append(cmd)
            if cmd == "pcap.stop":
                st.update(running=False, stopReason="stopped", summary={"protocols": {"TCP": 3}})
            return dict(st)

        self.patch(TN, "dcall", dcall)
        r = await self.call("ios_pcap_capture", seconds=60, wait_seconds=0)
        self.assertEqual(r.state, "running")
        self.assertTrue(r.job_id.startswith("pcap-"))
        self.assertIn("ios_pcap_capture(job_id=", r.next_steps[0])
        st = await self.call("ios_job_cancel", job_id=r.job_id)
        self.assertEqual((st.state, st.detail["stopReason"]), ("cancelled", "stopped"))   # stopped early, result kept
        self.assertIn("pcap.stop", self.calls)
        done = await self.call("ios_pcap_capture", job_id=r.job_id, wait_seconds=0)
        self.assertEqual((done.state, done.stop_reason, done.protocols), ("done", "stopped", {"TCP": 3}))


class CreateProject(Base):
    MODULES = (TP,)

    async def test_create_job_is_shared_by_repeat_calls(self):
        gate = asyncio.Event()
        news = []

        async def hcall(group, action, flags=None, **kw):
            if action == "templates":
                return {"templates": [{"id": "swiftui-app"}]}
            news.append(flags)
            await gate.wait()
            return {"name": "Glance", "path": HOME + "/Developer/Glance", "bundleId": "com.example.Glance"}

        self.patch(TP.helper, "call", hcall)
        self.patch(TP, "_is_demo_stub", lambda d: False)
        r = await self.call("ios_project_create", name="Glance", wait_seconds=0)
        self.assertFalse(r.done)
        self.assertTrue(r.job_id.startswith("project-create-"))
        again = await self.call("ios_project_create", name="Glance", wait_seconds=0.1)
        self.assertEqual((again.job_id, len(news)), (r.job_id, 1))          # waits for the same creation
        gate.set()
        done = await self.call("ios_project_create", job_id=r.job_id, wait_seconds=5)
        self.assertTrue(done.done)
        self.assertIn("Created Glance", done.message)


class OneIdName(unittest.TestCase):
    def test_no_tool_takes_a_per_kind_id(self):
        from iosstudio_mcp.server import mcp
        old = {"build_id", "install_id", "transfer_id", "matrix_id", "recording_id", "capture_id", "test_id"}
        tools = mcp._tool_manager.list_tools()
        names = {t.name for t in tools}
        self.assertLessEqual({"ios_job_status", "ios_job_respond", "ios_job_cancel"}, names)
        self.assertFalse(names & {"ios_build_status", "ios_build_respond", "ios_build_cancel"})
        for t in tools:
            self.assertFalse(old & set(t.parameters.get("properties", {})), t.name)

    def test_no_result_names_an_id_build_id(self):
        """Results name the ids the tools take: job_id, never build_id."""
        from iosstudio_mcp.server import mcp

        def keys(schema):
            if isinstance(schema, dict):
                for k, v in schema.items():
                    if k == "properties" and isinstance(v, dict):
                        yield from v
                    yield from keys(v)
            elif isinstance(schema, list):
                for x in schema:
                    yield from keys(x)
        for t in mcp._tool_manager.list_tools():
            self.assertNotIn("build_id", set(keys(t.fn_metadata.output_schema or {})), t.name)


if __name__ == "__main__":
    unittest.main()
