"""The one job manager (mcp/iosstudio_mcp/core/jobs.py) for in-process coroutine jobs, next to helper-stream
jobs: results and errors, progress, needs_input with ask()/respond(), cancel (with the on_cancel hook and
before the first step), shutdown, retention per kind and by age, and the JobStatus view of a coroutine job.
Helper-stream details (the pump, finishers, prompts over stdin) are in test_mcp_jobs.py.

Needs the MCP server's dependencies (the dev venv; skipped otherwise):
    python -m pytest tests/mcp/test_mcp_job_manager.py
"""
from __future__ import annotations

import asyncio
import sys
import time
import unittest

from mcp.server.mcpserver.exceptions import ToolError

from iosstudio_mcp.core import jobs as J
from iosstudio_mcp.core.job_status import job_status
from iosstudio_mcp.core.jobs import Job, JobManager
from support.waiting import until


class Fail(Exception):
    code = "boom"


class Coroutines(unittest.IsolatedAsyncioTestCase):
    async def test_result_progress_and_ids(self):
        mgr = JobManager()
        gate = asyncio.Event()

        async def work(job: Job) -> dict:
            job.report(0.5, "halfway")
            await gate.wait()
            return {"ok": True, "n": 3}

        job = mgr.run(work, "install", title="Install x", tool="ios_app_install", data={"pkg": "/x.ipa"})
        self.assertTrue(job.id.startswith("install-"))
        self.assertFalse(job.stream)
        await mgr.wait(job, 0.2)
        self.assertEqual((job.state, job.progress, job.progress_detail), ("running", 0.5, "halfway"))
        st = job_status(job)
        self.assertEqual((st.job_id, st.state, st.tool), (job.id, "running", "ios_app_install"))
        self.assertIn("ios_app_install(job_id=", st.next_steps[0])
        gate.set()
        await mgr.wait(job, 5)
        self.assertEqual((job.state, job.result), ("succeeded", {"ok": True, "n": 3}))
        st = job_status(job)
        self.assertEqual(st.detail, {"ok": True, "n": 3})
        self.assertIsNone(st.result)                       # BuildResult is for helper builds only
        self.assertIn("ios_app_install(job_id=", " ".join(st.next_steps))
        other = mgr.run(work, "transfer", title="t")
        self.assertNotEqual(other.id.split("-")[1], job.id.split("-")[1])   # one counter for every kind
        await mgr.cancel(other)

    async def test_big_result_detail_is_cut(self):
        mgr = JobManager()

        async def work(job: Job) -> dict:
            return {"path": "/a", "count": 2, "rows": ["x" * 100] * 100}

        job = mgr.run(work, "matrix", title="m", tool="ios_screenshot_matrix")
        await mgr.wait(job, 5)
        d = job_status(job).detail
        self.assertEqual((d["path"], d["count"]), ("/a", 2))
        self.assertNotIn("rows", d)
        self.assertIn("ios_screenshot_matrix", d["more"])

    async def test_errors(self):
        mgr = JobManager()

        async def tool_error(job: Job) -> None:
            raise ToolError("No iPhone. Next: connect it.")

        async def other(job: Job) -> None:
            raise Fail("disk on fire")

        a, b = mgr.run(tool_error, "x", title="a"), mgr.run(other, "x", title="b")
        await mgr.wait(a, 5)
        await mgr.wait(b, 5)
        self.assertEqual((a.state, a.error), ("failed", {"code": "failed", "message": "No iPhone. Next: connect it."}))
        self.assertIsInstance(a.exc, ToolError)
        self.assertEqual((b.state, b.error["code"], b.error["message"]), ("failed", "boom", "disk on fire"))
        self.assertEqual(job_status(a).error.code, "failed")

    async def test_ask_and_respond(self):
        mgr = JobManager()

        async def work(job: Job) -> str:
            v = await job.ask({"id": "p1", "title": "Replace?", "message": "Replace the file?",
                               "options": [{"value": "yes", "destructive": True}, {"value": "no"}]})
            return {"answer": v}

        job = mgr.run(work, "x", title="ask")
        await mgr.wait(job, 5)                              # returns early on needs_input
        self.assertEqual((job.state, job.pending_prompt["id"]), ("needs_input", "p1"))
        st = job_status(job)
        self.assertEqual(st.pending_prompt.id, "p1")
        self.assertTrue(st.pending_prompt.destructive)
        with self.assertRaises(ToolError):
            await mgr.respond(job, "p2", "no")
        await mgr.respond(job, "p1", "no")
        await mgr.wait(job, 5)
        self.assertEqual((job.state, job.result), ("succeeded", {"answer": "no"}))
        with self.assertRaises(ToolError):
            await mgr.respond(job, "p1", "no")

    async def test_cancel_runs_on_cancel_first(self):
        mgr = JobManager()
        seen = []
        stop = asyncio.Event()

        async def work(job: Job) -> None:
            await stop.wait()
            raise ToolError("The transfer was cancelled [cancelled]")    # what the daemon says

        async def on_cancel(job: Job) -> None:
            seen.append(job.id)
            stop.set()

        job = mgr.run(work, "transfer", title="copy", on_cancel=on_cancel)
        await asyncio.sleep(0)
        t0 = time.monotonic()
        await mgr.cancel(job)
        self.assertLess(time.monotonic() - t0, 2)
        self.assertEqual((seen, job.state, job.error["code"]), ([job.id], "cancelled", "cancelled"))
        self.assertTrue(job.task.done())
        self.assertIsNone(job.on_cancel)                    # released once finished
        self.assertIs(await mgr.cancel(job), job)           # idempotent

    async def test_cancel_without_hook_cancels_the_task(self):
        mgr = JobManager()
        cleaned = []

        async def work(job: Job) -> None:
            try:
                await asyncio.sleep(30)
            finally:
                cleaned.append(True)

        job = mgr.run(work, "x", title="slow")
        await asyncio.sleep(0)
        await mgr.cancel(job)
        self.assertEqual((job.state, cleaned), ("cancelled", [True]))

    async def test_cancel_while_asking(self):
        mgr = JobManager()

        async def work(job: Job) -> None:
            await job.ask({"id": "p1", "message": "?"})

        job = mgr.run(work, "x", title="ask")
        await mgr.wait(job, 5)
        await mgr.cancel(job)
        self.assertEqual((job.state, job.pending_prompt), ("cancelled", None))

    async def test_cancelled_before_first_step_still_ends(self):
        mgr = JobManager()

        async def work(job: Job) -> None:
            raise AssertionError("never runs")

        job = mgr.run(work, "x", title="never")
        job.task.cancel()
        await asyncio.wait({job.task})
        await asyncio.sleep(0)
        self.assertEqual(job.state, "cancelled")

    async def test_shutdown_cancels_coroutines_and_streams(self):
        mgr = JobManager()
        cleaned = []
        started = asyncio.Event()

        async def work(job: Job) -> None:
            try:
                started.set()
                await asyncio.sleep(30)
            finally:
                cleaned.append(job.id)

        a = mgr.run(work, "x", title="a")
        proc = await asyncio.create_subprocess_exec(sys.executable, "-c", "import time; time.sleep(30)",
                                                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.PIPE, start_new_session=True)
        b = Job(id="b-99", kind="build", action="compile", title="b", proc=proc)
        mgr.jobs[b.id] = b
        b._task = asyncio.create_task(mgr._pump(b))
        await started.wait()
        t0 = time.monotonic()
        await mgr.shutdown()
        self.assertLess(time.monotonic() - t0, 8)
        self.assertEqual((a.state, cleaned), ("cancelled", [a.id]))
        self.assertIsNotNone(proc.returncode)
        await until(lambda: b.finished, what="the stream job to finish")


class Lookup(unittest.IsolatedAsyncioTestCase):
    async def test_get_by_kind(self):
        mgr = JobManager()

        async def work(job: Job) -> None:
            return None

        job = mgr.run(work, "install", title="Install x", tool="ios_app_install")
        self.assertIs(mgr.get(f" {job.id} "), job)
        self.assertIs(mgr.get(job.id, ("install", "transfer")), job)
        with self.assertRaises(ToolError) as cm:
            mgr.get(job.id, "matrix")
        self.assertIn("ios_app_install(job_id=", str(cm.exception))
        with self.assertRaises(ToolError) as cm:
            mgr.get("nope-1")
        self.assertIn(job.id, str(cm.exception))            # names recent ids
        self.assertEqual(mgr.running("install"), [job])
        await mgr.wait(job, 5)
        self.assertEqual(mgr.running(), [])


class Retention(unittest.IsolatedAsyncioTestCase):
    async def test_newest_per_kind_and_ttl(self):
        mgr = JobManager()

        async def work(job: Job) -> None:
            return None

        installs = [mgr.run(work, "install", title=str(i)) for i in range(J.KEEP_PER_KIND + 5)]
        copies = [mgr.run(work, "transfer", title=str(i)) for i in range(3)]
        gate = asyncio.Event()

        async def slow(job: Job) -> None:
            await gate.wait()

        running = mgr.run(slow, "install", title="running")
        await asyncio.wait([j.task for j in installs + copies])
        mgr.prune()
        kept = [j for j in installs if j.id in mgr.jobs]
        self.assertEqual(kept, installs[-J.KEEP_PER_KIND:])          # the newest finished ones per kind
        self.assertTrue(all(j.id in mgr.jobs for j in copies))        # other kinds keep their own quota
        self.assertIn(running.id, mgr.jobs)                           # running jobs are never dropped
        mgr.prune(now=time.time() + J.FINISHED_TTL + 1)
        self.assertEqual(list(mgr.jobs), [running.id])                # finished jobs expire
        gate.set()
        await mgr.wait(running, 5)

    async def test_start_and_run_prune(self):
        mgr = JobManager()

        async def work(job: Job) -> None:
            return None

        old = mgr.run(work, "x", title="old")
        await mgr.wait(old, 5)
        old.ended = time.time() - J.FINISHED_TTL - 1
        mgr.run(work, "x", title="new")
        self.assertNotIn(old.id, mgr.jobs)


if __name__ == "__main__":
    unittest.main()
