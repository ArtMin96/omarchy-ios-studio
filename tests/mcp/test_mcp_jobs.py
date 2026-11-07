"""MCP jobs (mcp/iosstudio_mcp/jobs.py), the output budget's last resort and ios_build_log's file reading.

The jobs run small Python scripts in place of the helper, so nothing real is built, installed or launched.
Needs the MCP server's dependencies (the dev venv; skipped otherwise):
    python -m pytest tests/mcp/test_mcp_jobs.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from iosstudio_mcp.core import budget as B
from iosstudio_mcp.core import jobs as J
from iosstudio_mcp.core.jobs import Job, JobManager
from iosstudio_mcp.tools import build as TB

TMP = Path(tempfile.mkdtemp(prefix="ios-studio-mcp-tests-"))


async def spawn(*events, sleep: float = 0.0, big: int = 0) -> asyncio.subprocess.Process:
    """A stand-in helper: prints a log event `big` characters long (if any), then each event as a JSON
    line, then sleeps."""
    script = ("import sys, time, json\n"
              "n = int(sys.argv[1])\n"
              "if n: print(json.dumps({'event': 'log', 'text': 'x' * n}), flush=True)\n"
              "for e in json.loads(sys.argv[2]):\n"
              "    print(json.dumps(e), flush=True)\n"
              "time.sleep(float(sys.argv[3]))\n")
    return await asyncio.create_subprocess_exec(
        sys.executable, "-c", script, str(big), json.dumps(list(events)), str(sleep), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, start_new_session=True, limit=64 * 1024)


async def run_job(mgr: JobManager, proc, finisher=None, info: dict | None = None) -> Job:
    job = Job(id="b-1", kind="build", action="run", title="t", project_path="/p", finisher=finisher)
    job.info.update(info or {})
    job.proc = proc
    mgr.jobs[job.id] = job
    job._task = asyncio.create_task(mgr._pump(job))
    return job


async def until(cond, timeout: float = 10.0) -> None:
    end = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > end:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.02)


OK_DONE = {"event": "done", "ok": True, "data": {"needsInstall": True, "udid": "U", "ipa": "/x.ipa", "bundleId": "b"}}


class Cancel(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_while_installing_stops_the_launch(self):
        mgr = JobManager()
        launched = []

        async def finisher(job: Job) -> None:
            job.check_cancel()
            job.set_step("install", "Installing", "running")
            await asyncio.sleep(30)               # the install over Wi-Fi
            job.check_cancel()
            launched.append(True)

        job = await run_job(mgr, await spawn(OK_DONE), finisher)
        await until(lambda: job.steps.get("install", {}).get("state") == "running")
        self.assertIsNone(mgr.running_for("/missing"))
        self.assertIs(mgr.running_for("/p"), job)
        t0 = time.monotonic()
        await mgr.cancel(job)
        self.assertLess(time.monotonic() - t0, 5)
        self.assertEqual(job.state, "cancelled")
        self.assertIn("won't be launched", job.error["message"])
        self.assertEqual(job.steps["install"]["state"], "cancelled")
        self.assertTrue(job._task.done())
        self.assertTrue(job._finisher_task.done())
        await asyncio.sleep(0.1)
        self.assertEqual(launched, [])

    async def test_cancel_between_install_and_launch(self):
        mgr = JobManager()
        steps = []
        gate = asyncio.Event()

        async def finisher(job: Job) -> None:
            steps.append("install")
            await gate.wait()
            job.check_cancel()
            steps.append("launch")

        job = await run_job(mgr, await spawn(OK_DONE), finisher)
        await until(lambda: steps == ["install"])
        job.cancel_requested = True       # cancel() arriving just as the install returns
        gate.set()
        await asyncio.wait({job._task}, timeout=5)
        self.assertEqual(job.state, "cancelled")
        self.assertEqual(steps, ["install"])

    async def test_cancel_before_the_finisher_starts(self):
        mgr = JobManager()
        ran = []

        async def finisher(job: Job) -> None:
            ran.append(True)

        job = await run_job(mgr, await spawn(OK_DONE, sleep=0.5), finisher)
        await until(lambda: job.result is not None)    # the helper said ok but hasn't exited yet
        await mgr.cancel(job)
        self.assertEqual(job.state, "cancelled")
        self.assertEqual(ran, [])

    async def test_terminal_only_after_the_finisher(self):
        mgr = JobManager()
        gate = asyncio.Event()

        async def finisher(job: Job) -> None:
            await gate.wait()
            job.result["launched"] = True

        job = await run_job(mgr, await spawn(OK_DONE), finisher)
        await until(lambda: job._finisher_task is not None)
        self.assertFalse(job.finished)
        gate.set()
        await asyncio.wait({job._task}, timeout=5)
        self.assertEqual(job.state, "succeeded")
        self.assertIsNone(job.finisher)


class MalformedEvents(unittest.IsolatedAsyncioTestCase):
    async def test_string_error_and_bad_numbers(self):
        mgr = JobManager()
        job = await run_job(mgr, await spawn(
            {"event": "progress", "fraction": "half", "eta": "soon", "detail": {"x": 1}},
            {"event": "step", "id": "compile", "state": "failed", "error": "plain text"},
            {"event": "diagnostic", "severity": "error", "message": "m", "details": "not a list"},
            {"event": "done", "ok": False, "error": "it broke"}))
        await asyncio.wait({job._task}, timeout=10)
        self.assertEqual(job.state, "failed")
        self.assertEqual(job.error["message"], "it broke")
        self.assertIsNone(job.progress)
        self.assertEqual(job.steps["compile"]["error"], "plain text")
        self.assertEqual(len(job.diagnostics), 1)

    async def test_done_with_odd_build(self):
        mgr = JobManager()
        job = await run_job(mgr, await spawn({"event": "done", "ok": False,
                                              "error": {"code": "x", "message": "m", "build": {"diagnostics": "?"}}}))
        await asyncio.wait({job._task}, timeout=10)
        self.assertEqual((job.state, job.error["code"]), ("failed", "x"))

    async def test_refused_build_becomes_a_diagnostic(self):
        mgr = JobManager()
        job = await run_job(mgr, await spawn({"event": "done", "ok": False, "error": {
            "code": "generated-project", "message": "This project generates its xtool.yml."}}))
        await asyncio.wait({job._task}, timeout=10)
        self.assertEqual(job.state, "failed")
        self.assertEqual(len(job.diagnostics), 1)
        d = job.diagnostics[0]
        self.assertEqual((d["severity"], d["category"], d["code"]), ("error", "config", "generated-project"))
        self.assertIn("ios_project_build_settings", d["hint"])

    async def test_build_failure_from_output_adds_no_refusal(self):
        mgr = JobManager()
        job = await run_job(mgr, await spawn(
            {"event": "diagnostic", "severity": "error", "category": "linker", "message": "undefined symbol"},
            {"event": "done", "ok": False, "error": {"code": "not-signed-in", "message": "m"}}))
        await asyncio.wait({job._task}, timeout=10)
        self.assertEqual([d["category"] for d in job.diagnostics], ["linker"])
        job2 = await run_job(mgr, await spawn({"event": "done", "ok": False,
                                               "error": {"code": "build-failed", "message": "m"}}))
        await asyncio.wait({job2._task}, timeout=10)
        self.assertEqual(job2.diagnostics, [])

    async def test_over_long_line_is_skipped(self):
        mgr = JobManager()
        job = await run_job(mgr, await spawn({"event": "log", "text": "after"}, {"event": "done", "ok": True},
                                             big=200_000))
        await asyncio.wait({job._task}, timeout=10)
        self.assertEqual(job.state, "succeeded")
        self.assertIn("after", list(job.lines))
        self.assertTrue(any("too long" in ln for ln in job.lines))

    async def test_unexpected_error_fails_the_job_and_stops_the_helper(self):
        mgr = JobManager()
        proc = await spawn({"event": "log", "text": "a"}, {"event": "done", "ok": True}, sleep=60)
        with mock.patch.object(mgr, "_apply", side_effect=RuntimeError("bad event")), \
                self.assertLogs("iosstudio.jobs", "ERROR"):
            job = await run_job(mgr, proc)
            await asyncio.wait({job._task}, timeout=15)
        self.assertEqual(job.state, "failed")
        self.assertEqual(job.error["code"], "internal")
        self.assertIn("bad event", job.error["message"])
        self.assertIsNotNone(proc.returncode)      # killed, not left writing into a full pipe


class FinishedJobMemory(unittest.IsolatedAsyncioTestCase):
    async def test_lines_are_trimmed_and_spilled(self):
        mgr = JobManager()
        job = Job(id="t-9", kind="toolchain-install", action="all", title="t")
        for i in range(5000):
            job.add_line(f"line {i}")
        with mock.patch.object(B, "CACHE", TMP / "cache"):
            mgr._finish(job, "succeeded")
        self.assertEqual(len(job.lines), J.FINISHED_LINES)
        self.assertEqual(job.first_line, 5000 - J.FINISHED_LINES)
        spill = Path(job.info["spillFile"])
        self.assertEqual(spill.read_text().splitlines()[0], "line 0")
        self.assertIsNone(job.finisher)

    async def test_jobs_with_a_log_file_do_not_spill(self):
        mgr = JobManager()
        job = Job(id="b-9", kind="build", action="compile", title="t", info={"logFile": "/nowhere.log"})
        for i in range(3000):
            job.add_line(str(i))
        mgr._finish(job, "failed")
        self.assertNotIn("spillFile", job.info)
        self.assertEqual(list(job.lines)[-1], "2999")


class BudgetNested(unittest.TestCase):
    def test_long_nested_string_is_cut(self):
        data = {"result": {"text": "x" * 60_000, "next_cursor": "c" * 300}, "items": [{"msg": "y" * 30_000}]}
        out, text = B.enforce(data, (), 12_000)
        self.assertLessEqual(B.result_size(out, ()), 12_000)
        self.assertTrue(out["truncated"])
        self.assertEqual(out["result"]["next_cursor"], "c" * 300)    # cursors pass through untouched

    def test_lists_inside_list_items_shrink(self):
        data = {"groups": [{"details": [f"detail {i} " + "z" * 100 for i in range(400)]}]}
        out, _ = B.enforce(data, (), 12_000)
        self.assertLessEqual(B.result_size(out, ()), 12_000)
        self.assertLess(len(out["groups"][0]["details"]), 400)

    def test_small_results_untouched(self):
        data = {"a": "b" * 100, "l": [1, 2, 3]}
        out, _ = B.enforce(dict(data), (), 12_000)
        self.assertEqual(out, data)


class BuildLogFiles(unittest.TestCase):
    def setUp(self):
        self.logs = TMP / "state" / "ios-studio" / "build-logs"
        self.logs.mkdir(parents=True, exist_ok=True)
        p = mock.patch.object(TB, "BUILD_LOGS", self.logs)
        p.start()
        self.addCleanup(p.stop)

    def test_only_files_in_the_log_folder(self):
        f = self.logs / "1-App-compile.log"
        f.write_text("a\n")
        self.assertEqual(TB.build_log_path(str(f)), f.resolve())
        self.assertEqual(TB.build_log_path("1-App-compile.log"), f.resolve())
        outside = TMP / "secret.log"
        outside.write_text("no")
        for bad in (str(outside), "../../secret.log", str(self.logs / ".." / ".." / ".." / "secret.log"),
                    str(self.logs / "missing.log")):
            self.assertIsNone(TB.build_log_path(bad), bad)

    def test_tail_of_a_big_log_keeps_file_line_numbers(self):
        f = self.logs / "big.log"
        f.write_text("".join(f"line {i}\n" for i in range(10_000)))
        lines, first = TB.read_log(f, max_read=1_000)
        self.assertEqual(lines[0], f"line {first}")
        self.assertEqual(lines[-1], "line 9999")
        whole, first0 = TB.read_log(f)
        self.assertEqual((len(whole), first0), (10_000, 0))
        self.assertIsNone(TB.read_log(self.logs / "nope.log"))


if __name__ == "__main__":
    unittest.main()
