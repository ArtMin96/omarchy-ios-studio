"""Unit tests on the computer (#15), MCP side (mcp/iosstudio_mcp/tools/testing.py): ios_test_run against a fake helper
that streams `test run` events in the shape of the helper contract: start, running, followed by job_id to the end;
failing tests capped and cut to the budget; compile errors grouped; no_tests / host_unsupported with next steps; a
crashed test; a second call while a run is going returns that run; --filter passed through; and a huge run
(thousands of test events and failures) kept under the budget without storing per-test events in the job.

    python -m pytest tests/mcp/test_mcp_testing.py
"""
from __future__ import annotations

from mcp.server.mcpserver.exceptions import ToolError

from iosstudio_mcp.core import budget as B
from iosstudio_mcp.core import jobs as J
from iosstudio_mcp.tools import testing as M
from support.mcp_harness import FakeHelper, ToolCase, size_of

PROJ = "/home/u/Developer/CrashLab"
LOG = PROJ + "-state/test-runs/20261125-CrashLab.log"


def result(**over) -> dict:
    r = {"project": PROJ, "package": "CrashLab", "filter": None,
         "counts": {"total": 5, "passed": 5, "failed": 0, "skipped": 0}, "duration": 1.4, "failures": [],
         "crashed": None, "no_tests_matched": False, "log_path": LOG}
    r.update(over)
    return r


def failure(i: int, message: str = "XCTAssertEqual failed: (\"4\") is not equal to (\"5\")") -> dict:
    return {"id": f"AppCoreTests.MathTests/testAdd{i}", "name": f"testAdd{i}", "suite": "MathTests",
            "library": "xctest", "file": f"{PROJ}/Tests/AppCoreTests/MathTests.swift", "line": 6 + i,
            "message": message}


def tev(i: int, status: str = "passed") -> dict:
    return {"event": "test", "id": f"AppCoreTests.MathTests/test{i}", "name": f"test{i}", "suite": "MathTests",
            "library": "xctest", "status": status, "duration": 0.001}


STEPS = [{"event": "step", "id": "prepare", "title": "Prepare", "state": "done"},
         {"event": "step", "id": "build", "title": "Build tests", "state": "done"},
         {"event": "step", "id": "run", "title": "Run tests", "state": "running"}]


def failed(code: str, message: str, data: dict | None = None) -> dict:
    ev = {"event": "done", "ok": False, "error": {"code": code, "message": message}}
    if data is not None:
        ev["data"] = data
    return ev


class TestRunTool(ToolCase):
    DAEMON = False

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.helper.reply("projects", "list", {"projects": [{"name": "CrashLab", "path": PROJ}]})

    async def test_a_run_returns_running_then_is_followed_to_success(self):
        self.helper.stream("test", "run", STEPS + [tev(i) for i in range(5)]
                           + [{"event": "done", "ok": True, "data": result()}], delay=1)
        first = await self.data(M, "ios_test_run", project_path="CrashLab", wait_seconds=0)
        self.assertEqual(first["state"], "running")
        self.assertTrue(first["job_id"].startswith("test-"))
        self.assertIn(f"ios_test_run(job_id='{first['job_id']}'", first["next_steps"][0])
        argv = (await self.until(lambda: self.helper.calls("test", "run"), what="helper started"))[0]
        self.assertEqual(FakeHelper.flag(argv, "project"), PROJ)
        self.assertIsNone(FakeHelper.flag(argv, "filter"))
        done = await self.poll(M, "ios_test_run", first["job_id"], project_path="CrashLab", wait_seconds=3)
        self.assertEqual(done["state"], "succeeded")
        self.assertEqual(done["counts"], {"total": 5, "passed": 5, "failed": 0, "skipped": 0})
        self.assertEqual(done["duration_seconds"], 1.4)
        self.assertTrue(done["full_output_path"].endswith("20261125-CrashLab.log"))
        self.assertIn("All 5 tests passed", done["next_steps"][0])
        self.assertNotIn("error", done)
        job = self.state.jobs.get(first["job_id"])
        self.assertEqual((job.kind, job.tool), ("test", "ios_test_run"))
        self.assertEqual(job.data["tests"], {"passed": 5})

    async def test_the_filter_is_passed_to_the_helper_and_a_miss_is_explained(self):
        self.helper.stream("test", "run", [{"event": "done", "ok": True, "data": result(
            filter="Nope", counts={"total": 0, "passed": 0, "failed": 0, "skipped": 0}, no_tests_matched=True)}])
        d = await self.data(M, "ios_test_run", project_path=PROJ, filter=" Nope ", wait_seconds=3)
        self.assertEqual(FakeHelper.flag(self.helper.calls("test", "run")[0], "filter"), "Nope")
        self.assertEqual(d["state"], "succeeded")
        self.assertTrue(d["no_tests_matched"])
        self.assertEqual(d["filter"], "Nope")
        self.assertIn("matched no test", d["next_steps"][0])

    async def test_failing_tests_are_listed_capped_with_a_note(self):
        fails = [failure(i) for i in range(30)]
        self.helper.stream("test", "run", STEPS + [failed("tests_failed", "30 of 35 tests failed.", result(
            counts={"total": 35, "passed": 5, "failed": 30, "skipped": 0}, failures=fails))])
        d = await self.data(M, "ios_test_run", project_path="CrashLab", filter="MathTests", wait_seconds=3)
        self.assertEqual(d["state"], "failed")
        self.assertEqual(d["error"]["code"], "tests_failed")
        self.assertEqual(d["failures_total"], 30)
        self.assertLessEqual(len(d["failures"]), M.FAILURES_CAP)
        self.assertEqual(d["failures"][0], "AppCoreTests.MathTests/testAdd0 — Tests/AppCoreTests/MathTests.swift:6 — "
                                           "XCTAssertEqual failed: (\"4\") is not equal to (\"5\")")
        self.assertTrue(d["truncated"])
        self.assertIn("of 30 failing tests", d["note"])
        self.assertIn("ios_test_run(project_path=", d["next_steps"][0])
        self.assertIn("filter='MathTests'", d["next_steps"][0])

    async def test_a_build_failure_returns_grouped_compile_errors(self):
        diag = {"event": "diagnostic", "severity": "error", "category": "compiler",
                "message": "cannot find 'adder' in scope", "file": f"{PROJ}/Tests/AppCoreTests/MathTests.swift"}
        self.helper.stream("test", "run", [
            {"event": "step", "id": "build", "title": "Build tests", "state": "failed"},
            *[{**diag, "line": n, "col": 9} for n in (4, 8, 12)],
            {"event": "diagnostic", "severity": "error", "category": "compiler", "message": "missing return",
             "file": f"{PROJ}/Sources/AppCore/Math.swift", "line": 3},
            failed("build_failed", "The tests didn't compile.",
                   result(counts={"total": 0, "passed": 0, "failed": 0, "skipped": 0}))])
        d = await self.data(M, "ios_test_run", project_path="CrashLab", wait_seconds=3)
        self.assertEqual((d["state"], d["error"]["code"]), ("failed", "build_failed"))
        self.assertEqual(d["diagnostics_total"], 2)
        top = d["diagnostics"][0]
        self.assertEqual((top["count"], top["category"]), (3, "compiler"))
        self.assertEqual(top["locations"][0], "Tests/AppCoreTests/MathTests.swift:4:9")
        self.assertIn("compile errors", d["next_steps"][0])
        self.assertNotIn("failures", d)

    async def test_no_tests_and_host_unsupported_say_what_to_do(self):
        self.helper.stream("test", "run", [failed("no_tests", "CrashLab has no test target.")])
        d = await self.data(M, "ios_test_run", project_path="CrashLab", wait_seconds=3)
        self.assertEqual(d["error"], {"code": "no_tests", "message": "CrashLab has no test target."})
        self.assertIn("App with unit tests", d["next_steps"][0])
        self.assertIn("Tests/", d["next_steps"][0])
        self.helper.stream("test", "run", [failed("host_unsupported", "AppCore imports SwiftUI.")])
        d = await self.data(M, "ios_test_run", project_path="CrashLab", wait_seconds=3)
        self.assertEqual(d["error"]["code"], "host_unsupported")
        self.assertIn("Foundation-only library target", d["next_steps"][0])

    async def test_a_crashed_test_is_named(self):
        crash = failure(1, "Fatal error: Unexpectedly found nil while unwrapping an Optional value")
        self.helper.stream("test", "run", [failed("tests_failed", "A test crashed.", result(
            counts={"total": 2, "passed": 1, "failed": 1, "skipped": 0}, failures=[crash],
            crashed=crash["id"]))])
        d = await self.data(M, "ios_test_run", project_path="CrashLab", wait_seconds=3)
        self.assertEqual(d["crashed"], crash["id"])
        self.assertFalse(d.get("truncated", False))
        self.assertTrue(any("crashed the test process" in s for s in d["next_steps"]))

    async def test_a_second_call_while_running_returns_the_running_job(self):
        self.helper.stream("test", "run", [{"event": "done", "ok": True, "data": result()}], delay=30)
        first = await self.data(M, "ios_test_run", project_path="CrashLab", wait_seconds=0)
        again = await self.data(M, "ios_test_run", project_path=PROJ, filter="Other", wait_seconds=0)
        self.assertEqual(again["job_id"], first["job_id"])
        self.assertTrue(again["busy"])
        self.assertEqual(again["state"], "running")
        self.assertIn("already going", again["note"])
        await self.until(lambda: self.helper.calls("test", "run"), what="helper started")
        self.assertEqual(len(self.helper.calls("test", "run")), 1)
        await self.state.jobs.cancel(self.state.jobs.get(first["job_id"]))
        d = await self.data(M, "ios_test_run", project_path="CrashLab", job_id=first["job_id"], wait_seconds=0)
        self.assertEqual(d["state"], "cancelled")
        self.assertIn("Call ios_test_run again", d["next_steps"][0])

    async def test_another_kind_of_job_is_refused(self):
        self.helper.stream("build", "compile", [{"event": "done", "ok": True, "data": {}}])
        job = await self.state.jobs.start("build", "build", "compile", {}, title="Compile", prefix="b")
        text = await self.error(M, "ios_test_run", project_path="CrashLab", job_id=job.id)
        self.assertIn("not test", text)

    async def test_a_huge_run_stays_in_budget_and_out_of_the_jobs_memory(self):
        n = 3000
        long_msg = "XCTAssertEqual failed: " + "x" * 3000
        events = [tev(i, "failed" if i % 2 else "passed") for i in range(n)]
        events += [{"event": "diagnostic", "severity": "error", "category": "test",
                    "message": f"testAdd{i}: {long_msg}", "file": f"{PROJ}/Tests/T.swift", "line": i}
                   for i in range(1500)]
        events.append(failed("tests_failed", "1500 tests failed.", result(
            counts={"total": n, "passed": n // 2, "failed": n // 2, "skipped": 0},
            failures=[failure(i, long_msg) for i in range(200)])))
        self.helper.stream("test", "run", events)
        res = await self.call(M, "ios_test_run", project_path="CrashLab", wait_seconds=10)
        self.assertLessEqual(size_of(res), B.DEFAULT_BYTES)
        d = res.structured_content
        self.assertEqual(d["state"], "failed")
        self.assertEqual(d["failures_total"], n // 2)
        self.assertTrue(d["truncated"])
        self.assertNotIn("diagnostics", d)       # test failures aren't repeated as diagnostics
        job = self.state.jobs.get(d["job_id"])
        self.assertEqual(job.data["tests"], {"passed": n // 2, "failed": n // 2})
        self.assertLessEqual(len(job.diagnostics), J.MAX_DIAGNOSTICS)
        self.assertFalse(any("event" in x for x in [job.result]))
        small = await self.call(M, "ios_test_run", project_path="CrashLab", job_id=d["job_id"], max_bytes=4000)
        self.assertLessEqual(size_of(small), 4000)
        big = await self.data(M, "ios_test_run", project_path="CrashLab", job_id=d["job_id"], max_bytes=48000)
        self.assertEqual(len(big["failures"]), M.FAILURES_CAP)

    async def test_a_bad_project_is_refused_before_the_helper_runs(self):
        with self.assertRaises(ToolError):
            await self.call(M, "ios_test_run", project_path="NoSuchApp")
        self.assertFalse(self.helper.calls("test", "run"))
