"""A helper job's kill_grace (mcp/iosstudio_mcp/core/jobs.py): a cancelled toolchain update gets time to put the
previous version back before SIGKILL, cancel() doesn't block the tool call while it does, and server shutdown asks
it to cancel and never SIGKILLs it. Other helper jobs keep the short ask -> SIGTERM -> SIGKILL escalation.

The fake helper behaves like `updates apply`: it ignores SIGTERM while it rolls back (_quiet_signals), turns
{"cancel": true} on stdin into a rollback and ends with done {ok: false, error: cancelled, data: {rolledBack}}.

    python -m pytest tests/mcp/test_mcp_job_kill_grace.py
"""
from __future__ import annotations

import asyncio
import signal
import sys
import time
import unittest
from unittest import mock

from iosstudio_mcp.core import jobs as J
from iosstudio_mcp.core.jobs import Job, JobManager
from support.waiting import until

FAKE_APPLY = r"""
import json, signal, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
print(json.dumps({"event": "step", "id": "ipsw", "title": "Updating ipsw", "state": "running"}), flush=True)
for line in sys.stdin:
    if json.loads(line).get("cancel"):
        break
time.sleep(float(sys.argv[1]))
print(json.dumps({"event": "done", "ok": False,
                  "error": {"code": "cancelled", "message": "Cancelled. The previous version is back."},
                  "data": {"applied": [], "rolledBack": ["ipsw"]}}), flush=True)
"""

FAST = {"CANCEL_ASK": 0.2, "CANCEL_TERM": 0.2, "CANCEL_SETTLE": 0.3}


class KillGrace(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for name, value in FAST.items():
            p = mock.patch.object(J, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.procs: list[asyncio.subprocess.Process] = []

    async def asyncTearDown(self):
        for p in self.procs:
            if p.returncode is None:
                p.kill()
                await p.wait()

    async def job(self, mgr: JobManager, rollback: float, grace: float, kind="toolchain-update") -> Job:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", FAKE_APPLY, str(rollback), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
        self.procs.append(proc)
        job = Job(id=f"t-{len(self.procs)}", kind=kind, action="apply", title="Update ipsw", proc=proc,
                  kill_grace=grace)
        mgr.jobs[job.id] = job
        job._task = asyncio.create_task(mgr._pump(job))
        await until(lambda: job.current_step == "ipsw", what="the update to start")
        return job

    async def test_without_grace_a_slow_rollback_is_killed(self):
        mgr = JobManager()
        job = await self.job(mgr, rollback=3, grace=0, kind="build")
        await mgr.cancel(job)
        self.assertEqual(job.state, "cancelled")
        self.assertEqual(self.procs[0].returncode, -signal.SIGKILL)
        self.assertIsNone(job.result)                 # it never reported what it put back

    async def test_with_grace_cancel_returns_and_the_rollback_finishes(self):
        mgr = JobManager()
        job = await self.job(mgr, rollback=1.2, grace=10)
        t0 = time.monotonic()
        await mgr.cancel(job)
        self.assertLess(time.monotonic() - t0, 1.0)  # the tool call isn't held for the whole rollback
        self.assertEqual(job.state, "running")
        self.assertTrue(job.cancel_requested)
        self.assertIn("previous version", job.progress_detail)
        await mgr.cancel(job)                          # a second cancel doesn't start a second escalation
        await until(lambda: job.finished, timeout=5, what="the rollback to finish")
        self.assertEqual(job.state, "cancelled")
        self.assertEqual(job.result["rolledBack"], ["ipsw"])
        self.assertEqual(job.error["code"], "cancelled")
        await until(lambda: self.procs[0].returncode is not None, what="the helper to exit")
        self.assertEqual(self.procs[0].returncode, 0)

    async def test_the_grace_is_bounded(self):
        mgr = JobManager()
        job = await self.job(mgr, rollback=30, grace=0.6)
        await mgr.cancel(job)
        await until(lambda: job.finished, timeout=5, what="SIGKILL after the grace period")
        self.assertEqual(self.procs[0].returncode, -signal.SIGKILL)

    async def test_shutdown_asks_and_waits_for_the_rollback(self):
        mgr = JobManager()
        job = await self.job(mgr, rollback=0.8, grace=120)
        other = await self.job(mgr, rollback=30, grace=0, kind="build")
        t0 = time.monotonic()
        await mgr.shutdown()
        self.assertLess(time.monotonic() - t0, 6)
        self.assertEqual((job.state, job.result["rolledBack"]), ("cancelled", ["ipsw"]))
        self.assertEqual(self.procs[0].returncode, 0)
        await until(lambda: other.finished, what="the other job to stop")
        self.assertEqual(self.procs[1].returncode, -signal.SIGKILL)

    async def test_shutdown_never_kills_an_update_that_outlives_it(self):
        mgr = JobManager()
        job = await self.job(mgr, rollback=30, grace=120)
        with mock.patch.object(J, "SHUTDOWN_SETTLE", 0.3):
            await mgr.shutdown()
        await asyncio.sleep(0.3)
        self.assertIsNone(self.procs[0].returncode)    # still putting things back on its own
        self.assertFalse(job.finished)
        job._task.cancel()                             # the event loop closing: SIGTERM only, never SIGKILL
        await asyncio.wait({job._task})
        await asyncio.sleep(0.2)
        self.assertIsNone(self.procs[0].returncode)


if __name__ == "__main__":
    unittest.main()
