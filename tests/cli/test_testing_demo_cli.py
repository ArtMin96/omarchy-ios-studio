"""The `test` group in demo mode through the helper as a subprocess (IOSSTUDIO_DEMO=1, private folders):
info shapes, passing and failing runs, no test target, SwiftUI in tested code, --filter, the last run, cancel."""
from __future__ import annotations

import json
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from support import HELPER_PY as HELPER
from support import isolated_env

HOME = str(Path.home())
COUNT_KEYS = {"total", "passed", "failed", "skipped"}
RESULT_KEYS = {"project", "package", "filter", "counts", "duration", "failures", "crashed", "no_tests_matched", "log_path"}


def project(name: str) -> str:
    return f"{HOME}/Developer/{name}"


class TestingDemoCli(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="test-demo-"))
        self.env = isolated_env(self.tmp, IOSSTUDIO_DEMO="1", IOSSTUDIO_DEMO_DELAY="0",
                                IOSSTUDIO_RUNTIME_DIR=str(self.tmp / "rt"))
        self.env.pop("IOSSTUDIO_DEMO_TESTS", None)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def helper(self, *args: str, env: dict | None = None) -> list[dict]:
        r = subprocess.run(HELPER + list(args), env=env or self.env, capture_output=True, text=True, timeout=60,
                           stdin=subprocess.DEVNULL)
        return [json.loads(ln) for ln in r.stdout.splitlines() if ln.startswith("{")]

    def info(self, name: str) -> dict:
        out = self.helper("test", "info", "--project", project(name))[-1]
        self.assertTrue(out["ok"], out)
        return out["data"]

    def run_tests(self, name: str, *extra: str) -> tuple[list[dict], dict]:
        events = self.helper("test", "run", "--project", project(name), *extra)
        done = events[-1]
        self.assertEqual(done["event"], "done")
        return events, done

    def assert_result(self, data: dict) -> None:
        self.assertEqual(set(data), RESULT_KEYS)
        self.assertEqual(set(data["counts"]), COUNT_KEYS)
        self.assertTrue(data["log_path"].startswith(str(self.tmp / "xdg_state_home" / "ios-studio" / "test-runs")))
        self.assertTrue(Path(data["log_path"]).is_file())

    def test_info_shapes(self):
        wg = self.info("WeatherGlance")
        self.assertEqual(set(wg), {"project", "package", "has_tests", "test_targets", "tested_targets", "host_blockers",
                                   "last_run"})
        self.assertTrue(wg["has_tests"])
        self.assertEqual(wg["project"], project("WeatherGlance"))
        self.assertEqual([t["name"] for t in wg["test_targets"]], ["WeatherCoreTests", "WeatherCoreSwiftTests"])
        self.assertEqual(wg["test_targets"][0]["path"], "Tests/WeatherCoreTests")
        self.assertEqual(wg["tested_targets"], ["WeatherCore"])
        self.assertEqual(wg["host_blockers"], [])
        self.assertIsNone(wg["last_run"])
        rb = self.info("RecipeBox")
        self.assertFalse(rb["has_tests"])
        self.assertEqual(rb["test_targets"], [])
        sp = self.info("SketchPad")
        self.assertEqual(sp["host_blockers"][0], {"target": "SketchCore", "module": "SwiftUI", "line": 2,
                                                  "file": project("SketchPad") + "/Sources/SketchCore/CanvasModel.swift"})
        other = self.info("SomethingElse")
        self.assertTrue(other["has_tests"])
        self.assertEqual(other["tested_targets"], ["SomethingElseCore"])
        missing = self.helper("test", "info")[-1]
        self.assertFalse(missing["ok"])
        self.assertEqual(missing["error"]["code"], "not_project")

    def test_passing_run_and_last_run(self):
        events, done = self.run_tests("WeatherGlance")
        self.assertTrue(done["ok"], done)
        steps = [(e["id"], e["state"]) for e in events if e["event"] == "step"]
        self.assertEqual(steps, [("prepare", "running"), ("prepare", "done"), ("build", "running"), ("build", "done"),
                                 ("run", "running"), ("run", "done")])
        logs = [e["text"] for e in events if e["event"] == "log"]
        self.assertIn("Build complete! (2.14s)", logs)
        tests = [e for e in events if e["event"] == "test"]
        self.assertEqual(len(tests), 12)
        self.assertEqual({t["library"] for t in tests}, {"xctest", "swift-testing"})
        self.assertTrue(all(t["status"] == "passed" and "file" not in t for t in tests))
        self.assertEqual(tests[0]["id"], "ForecastParserTests/testParsesHourly")        # XCTest on Linux: no module
        swift_testing = next(t for t in tests if t["library"] == "swift-testing")
        self.assertEqual(swift_testing["id"], "WeatherCoreSwiftTests.ConditionTests/symbolForEveryCondition()")
        data = done["data"]
        self.assert_result(data)
        self.assertEqual(data["counts"], {"total": 12, "passed": 12, "failed": 0, "skipped": 0})
        self.assertEqual(data["failures"], [])
        self.assertFalse(data["no_tests_matched"])
        self.assertIn("Build complete!", Path(data["log_path"]).read_text())
        last = self.info("WeatherGlance")["last_run"]
        self.assertEqual(last["counts"], data["counts"])
        self.assertTrue(last["ok"])
        self.assertIsNone(last["filter"])
        self.assertIsNone(self.info("HabitTracker")["last_run"])

    def test_failing_run(self):
        events, done = self.run_tests("HabitTracker")
        self.assertFalse(done["ok"])
        self.assertEqual(done["error"]["code"], "tests_failed")
        data = done["data"]
        self.assert_result(data)
        self.assertEqual(data["counts"], {"total": 10, "passed": 7, "failed": 2, "skipped": 1})
        root = project("HabitTracker")
        fx, fs = data["failures"]
        self.assertEqual((fx["library"], fx["file"], fx["line"]),
                         ("xctest", root + "/Tests/HabitCoreTests/StreakCalculatorTests.swift", 24))
        self.assertIn("XCTAssertEqual failed", fx["message"])
        self.assertEqual((fs["library"], fs["file"], fs["line"]),
                         ("swift-testing", root + "/Tests/HabitCoreSwiftTests/ReminderScheduleTests.swift", 17))
        self.assertIn("Expectation failed", fs["message"])
        diags = [e for e in events if e["event"] == "diagnostic"]
        self.assertEqual(len(diags), 2)
        for d, f in zip(diags, (fx, fs)):
            self.assertEqual((d["category"], d["severity"], d["file"], d["line"]), ("test", "error", f["file"], f["line"]))
            self.assertTrue(d["message"].startswith(f"{f['suite']}.{f['name']}: "))     # results.py failure_diagnostic
        failed = [e for e in events if e["event"] == "test" and e["status"] == "failed"]
        self.assertEqual([(t["file"], t["line"]) for t in failed], [(fx["file"], 24), (fs["file"], 17)])
        self.assertIn(("run", "failed"), [(e["id"], e["state"]) for e in events if e["event"] == "step"])
        self.assertFalse(self.info("HabitTracker")["last_run"]["ok"])

    def test_no_tests(self):
        events, done = self.run_tests("RecipeBox")
        self.assertFalse(done["ok"])
        self.assertEqual(done["error"]["code"], "no_tests")
        self.assertIn("App with unit tests", done["error"]["message"])
        self.assertEqual(done["error"]["fix"], {"label": "Show tests", "page": "projects", "action": "test",
                                                "path": project("RecipeBox")})
        self.assertFalse([e for e in events if e["event"] == "test"])

    def test_host_unsupported(self):
        _events, done = self.run_tests("SketchPad")
        self.assertEqual(done["error"]["code"], "host_unsupported")
        self.assertIn("SwiftUI", done["error"]["message"])
        self.assertIn("CanvasModel.swift:2", done["error"]["detail"])
        self.assertEqual(done["error"]["fix"], {"label": "Open the project folder", "page": "projects",
                                                "action": "reveal", "path": project("SketchPad")})
        forced = {**self.env, "IOSSTUDIO_DEMO_TESTS": "host"}
        out = self.helper("test", "run", "--project", project("WeatherGlance"), env=forced)[-1]
        self.assertEqual(out["error"]["code"], "host_unsupported")

    def test_filter(self):
        events, done = self.run_tests("WeatherGlance", "--filter", "UnitConverterTests|ConditionTests/rain")
        self.assertTrue(done["ok"])
        ids = [e["id"] for e in events if e["event"] == "test"]
        self.assertEqual(len(ids), 4)
        self.assertEqual(done["data"]["filter"], "UnitConverterTests|ConditionTests/rain")
        self.assertEqual(done["data"]["counts"]["total"], 4)
        _events, done = self.run_tests("HabitTracker", "--filter", "NoSuchTest")
        self.assertTrue(done["ok"])
        self.assertTrue(done["data"]["no_tests_matched"])
        self.assertEqual(done["data"]["counts"], {"total": 0, "passed": 0, "failed": 0, "skipped": 0})
        self.assertEqual(self.info("HabitTracker")["last_run"]["filter"], "NoSuchTest")
        events, done = self.run_tests("WeatherGlance", "--filter", "ForecastParserTests/testParsesHourly$")
        self.assertEqual([e["id"] for e in events if e["event"] == "test"], ["ForecastParserTests/testParsesHourly"])
        _events, done = self.run_tests("WeatherGlance", "--filter", "WeatherCoreTests\\.Unit")   # module-qualified
        self.assertEqual(done["data"]["counts"]["total"], 3)
        _events, done = self.run_tests("WeatherGlance", "--filter", "Forecast(")
        self.assertEqual(done["error"]["code"], "bad_filter")
        self.assertFalse(done["ok"])

    def cancel_run(self, how: str) -> list[dict]:
        env = {**self.env, "IOSSTUDIO_DEMO_DELAY": "1"}
        p = subprocess.Popen(HELPER + ["test", "run", "--project", project("WeatherGlance")], env=env, text=True,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        events = []
        try:
            for line in p.stdout:
                events.append(json.loads(line))
                if events[-1]["event"] == "test":
                    break
            t0 = time.monotonic()
            if how == "stdin":
                p.stdin.write('{"cancel": true}\n')
                p.stdin.flush()
            else:
                p.send_signal(signal.SIGTERM)
            events += [json.loads(ln) for ln in p.stdout]
            self.assertEqual(p.wait(timeout=10), 1)
            self.assertLess(time.monotonic() - t0, 1.0)
        finally:
            p.kill()
            p.wait()
        return events

    def test_cancel(self):
        for how in ("stdin", "sigterm"):
            with self.subTest(how=how):
                events = self.cancel_run(how)
                done = events[-1]
                self.assertEqual(done["event"], "done")
                self.assertFalse(done["ok"])
                self.assertEqual(done["error"]["code"], "cancelled")
                self.assertLess(len([e for e in events if e["event"] == "test"]), 12)
        self.assertIsNone(self.info("WeatherGlance")["last_run"])


if __name__ == "__main__":
    unittest.main()
