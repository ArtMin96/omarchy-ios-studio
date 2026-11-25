"""The real `test` group through the helper as a subprocess, with a fake swift (tests/support/fake_swift_tests.py)
replaying real Swift 6.4.2 output: info, passing / failing / crashing runs, compile errors, no test target, Apple-only
imports (before the build and from the build's errors), busy, --filter, cancel (stdin and SIGTERM), timeout, a
missing toolchain, the native build-system fallback, and the logs and last.json the runs leave."""
from __future__ import annotations

import fcntl
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from iosstudio.core import fileio
from support import HELPER_PY as HELPER
from support import ROOT, isolated_env

FAKE = ROOT / "tests" / "support" / "fake_swift_tests.py"
RESULT_KEYS = {"project", "package", "filter", "counts", "duration", "failures", "crashed", "no_tests_matched", "log_path"}

FILES = {
    "Package.swift": '// swift-tools-version: 6.0\nimport PackageDescription\nlet package = Package(name: "CrashLab")\n',
    "xtool.yml": "version: 1\nbundleID: com.example.CrashLab\n",
    "Sources/AppCore/Math.swift": "import Foundation\npublic enum Math {}\n",
    "Sources/CrashLab/ContentView.swift": "import SwiftUI\nstruct ContentView: View { var body: some View { Text(\"x\") } }\n",
    "Tests/AppCoreTests/MathTests.swift": "import XCTest\n@testable import AppCore\n",
    "Tests/AppCoreSwiftTests/GreetingTests.swift": "import Testing\n@testable import AppCore\n",
}


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:   # a zombie isn't running
        return Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"
    except OSError:
        return False


class SwiftTestCli(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="swift-test-cli-")).resolve()
        self.root = self.tmp / "CrashLab"
        for rel, text in FILES.items():
            p = self.root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        self.argv_file = self.tmp / "argv.jsonl"
        self.pids_file = self.tmp / "pids"
        self.env = isolated_env(self.tmp, IOSSTUDIO_SWIFT=str(FAKE), FAKE_SWIFT_ARGV=str(self.argv_file),
                                FAKE_SWIFT_PIDS=str(self.pids_file), IOSSTUDIO_RUNTIME_DIR=str(self.tmp / "rt"))
        self.state = self.tmp / "xdg_state_home" / "ios-studio" / "test-runs"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- helpers
    def helper(self, *args: str, timeout: float = 60, **env: str) -> list[dict]:
        r = subprocess.run(HELPER + list(args), env={**self.env, **env}, capture_output=True, text=True,
                           timeout=timeout, stdin=subprocess.DEVNULL)
        return [json.loads(ln) for ln in r.stdout.splitlines() if ln.startswith("{")]

    def info(self, **env: str) -> dict:
        return self.helper("test", "info", "--project", str(self.root), **env)[-1]

    def run_tests(self, *extra: str, **env: str) -> tuple[list[dict], dict]:
        events = self.helper("test", "run", "--project", str(self.root), *extra, **env)
        done = events[-1]
        self.assertEqual(done["event"], "done", events[-5:])
        return events, done

    def calls(self) -> list[list[str]]:
        if not self.argv_file.exists():
            return []
        return [json.loads(ln) for ln in self.argv_file.read_text().splitlines()]

    def calls_of(self, verb: str) -> list[list[str]]:
        return [c for c in self.calls() if c[:1] == [verb]]

    def of(self, events: list[dict], name: str) -> list[dict]:
        return [e for e in events if e.get("event") == name]

    def assert_result(self, data: dict) -> None:
        self.assertEqual(set(data), RESULT_KEYS)
        self.assertEqual(data["project"], str(self.root))
        self.assertEqual(data["package"], "CrashLab")
        self.assertTrue(data["log_path"].startswith(str(self.state)), data["log_path"])
        self.assertTrue(Path(data["log_path"]).is_file())

    # ---- info
    def test_info(self):
        out = self.info()
        self.assertTrue(out["ok"], out)
        d = out["data"]
        self.assertEqual(set(d), {"project", "package", "has_tests", "test_targets", "tested_targets",
                                  "host_blockers", "last_run"})
        self.assertEqual(d["project"], str(self.root))
        self.assertEqual(d["package"], "CrashLab")
        self.assertTrue(d["has_tests"])
        self.assertEqual(d["test_targets"], [{"name": "AppCoreSwiftTests", "path": "Tests/AppCoreSwiftTests"},
                                             {"name": "AppCoreTests", "path": "Tests/AppCoreTests"}])
        self.assertEqual(d["tested_targets"], ["AppCore"])
        self.assertEqual(d["host_blockers"], [])     # the app target imports SwiftUI, but no test needs it
        self.assertIsNone(d["last_run"])
        self.info()
        self.assertEqual(len(self.calls()), 1, "the second info reads the cache")
        (self.root / "Package.swift").write_text(FILES["Package.swift"] + "// changed\n")
        self.info()
        self.assertEqual(len(self.calls()), 2, "a changed Package.swift is read again")
        self.assertIn("--scratch-path", self.calls()[0])
        _, done = self.run_tests()
        last = self.info()["data"]["last_run"]
        self.assertEqual(set(last), {"at", "ok", "counts", "duration", "filter"})
        self.assertTrue(last["ok"])
        self.assertEqual(last["counts"], done["data"]["counts"])

    def test_info_errors(self):
        out = self.helper("test", "info")[-1]
        self.assertEqual(out["error"]["code"], "not_project")
        out = self.helper("test", "info", "--project", str(self.tmp))[-1]
        self.assertEqual(out["error"]["code"], "not_project")
        out = self.info(IOSSTUDIO_SWIFT=str(self.tmp / "no-swift"))
        self.assertEqual(out["error"]["code"], "toolchain")
        self.assertEqual(out["error"]["fix"]["page"], "setup")

    # ---- runs
    def test_passing_run(self):
        events, done = self.run_tests()
        self.assertTrue(done["ok"], done)
        self.assert_result(done["data"])
        self.assertEqual(done["data"]["counts"], {"total": 6, "passed": 6, "failed": 0, "skipped": 0})
        self.assertFalse(done["data"]["no_tests_matched"])
        steps = [(e["id"], e["state"]) for e in self.of(events, "step")]
        self.assertEqual(steps, [("prepare", "running"), ("prepare", "done"), ("build", "running"), ("build", "done"),
                                 ("run", "running"), ("run", "done")])
        tests = self.of(events, "test")
        self.assertEqual({t["library"] for t in tests}, {"xctest", "swift-testing"})
        self.assertIn({"event": "test", "id": "MathTests/testAdd", "name": "testAdd", "suite": "MathTests", "library": "xctest",
                       "status": "passed", "duration": 0.007}, tests)
        self.assertNotIn("deprecated", json.dumps(self.of(events, "log")), "the native warning is not news")
        self.assertEqual(self.of(events, "diagnostic"), [])
        build, = self.calls_of("build")
        scratch = str(self.root / ".build" / "host-tests")
        self.assertEqual(build, ["build", "--scratch-path", scratch, "--product", "CrashLabPackageTests",
                                 "--build-system", "native", "--no-color-diagnostics"])
        test, = self.calls_of("test")
        self.assertEqual(test[:5], ["test", "--scratch-path", scratch, "--skip-build", "--build-system"])
        self.assertIn("--experimental-event-stream-version", test)
        self.assertNotIn("--filter", test)
        self.assertFalse((self.root / ".build" / "host-tests" / "events.jsonl").exists(), "parsed, then deleted")
        log = Path(done["data"]["log_path"]).read_text()
        self.assertIn("Test Case 'MathTests.testAdd' passed", log)
        self.assertIn("$ swift build --scratch-path", log)
        last = json.loads((self.state / "last.json").read_text())
        self.assertEqual(list(last), [str(self.root)])

    def test_failing_run(self):
        events, done = self.run_tests(FAKE_SWIFT_SCENARIO="fail")
        self.assertFalse(done["ok"])
        self.assertEqual(done["error"]["code"], "tests_failed")
        self.assertIn("tests failed", done["error"]["message"])
        data = done["data"]
        self.assert_result(data)
        self.assertGreaterEqual(data["counts"]["failed"], 3)
        self.assertEqual(data["counts"]["failed"], len(data["failures"]))
        failed = {t["id"]: t for t in self.of(events, "test") if t["status"] == "failed"}
        x = failed["FailingTests/testAddIsWrong"]
        self.assertEqual((x["file"], x["line"]), (str(self.root / "Tests/AppCoreTests/FailingTests.swift"), 6))
        self.assertIn("XCTAssertEqual failed", x["message"])
        s = next(t for t in failed.values() if t["library"] == "swift-testing")
        self.assertTrue(s["file"].startswith(str(self.root / "Tests/AppCoreSwiftTests")), s)
        self.assertGreater(s["line"], 0)
        diags = self.of(events, "diagnostic")
        self.assertEqual(len(diags), len(failed))
        self.assertTrue(all(d["category"] == "test" and d["severity"] == "error" for d in diags))
        self.assertIn({"event": "diagnostic", "severity": "error", "category": "test", "file": x["file"], "line": 6,
                       "col": 0, "message": "FailingTests.testAddIsWrong: " + x["message"]}, diags)
        self.assertFalse(self.info()["data"]["last_run"]["ok"])

    def test_crash(self):
        events, done = self.run_tests(FAKE_SWIFT_SCENARIO="crash")
        self.assertEqual(done["error"]["code"], "tests_failed")
        self.assertEqual(done["data"]["crashed"], "CrashTests/testForceUnwrapCrash")
        self.assertIn("crashed the test process", done["error"]["message"])
        crash = next(t for t in self.of(events, "test") if t["id"] == "CrashTests/testForceUnwrapCrash")
        self.assertIn("Crashed: Fatal error", crash["message"])

    def test_build_failed(self):
        events, done = self.run_tests(FAKE_SWIFT_SCENARIO="compile")
        self.assertEqual(done["error"]["code"], "build_failed")
        self.assertIn("BrokenTests.swift:6", done["error"]["message"])
        self.assert_result(done["data"])
        self.assertEqual(done["data"]["counts"], {"total": 0, "passed": 0, "failed": 0, "skipped": 0})
        diags = [d for d in self.of(events, "diagnostic") if d["severity"] == "error"]
        self.assertEqual([(Path(d["file"]).name, d["line"], d["col"], d["category"]) for d in diags],
                         [("BrokenTests.swift", 6, 22, "compiler"), ("BrokenTests.swift", 7, 29, "compiler")])
        self.assertEqual(self.calls_of("test"), [])
        self.assertEqual([(e["id"], e["state"]) for e in self.of(events, "step")][-1], ("build", "failed"))

    def test_no_tests(self):
        events, done = self.run_tests(FAKE_SWIFT_SCENARIO="notests")
        self.assertEqual(done["error"]["code"], "no_tests")
        self.assertIn("App with unit tests", done["error"]["message"])
        self.assertEqual(done["error"]["fix"]["page"], "projects")
        self.assertEqual(self.calls_of("build"), [])
        self.assertFalse(self.info(FAKE_SWIFT_SCENARIO="notests")["data"]["has_tests"])
        _, done = self.run_tests(FAKE_SWIFT_SCENARIO="noproduct")
        self.assertEqual(done["error"]["code"], "no_tests")
        _, done = self.run_tests(FAKE_SWIFT_SCENARIO="nofound")
        self.assertEqual(done["error"]["code"], "no_tests")

    def test_host_unsupported_before_building(self):
        (self.root / "Sources/AppCore/Shapes.swift").write_text("// shapes\nimport Foundation\nimport SwiftUI\n")
        (self.root / "Sources/AppCore/Haptics.swift").write_text("#if canImport(UIKit)\nimport UIKit\n#endif\n")
        blockers = self.info()["data"]["host_blockers"]
        self.assertEqual(blockers, [{"target": "AppCore", "file": str(self.root / "Sources/AppCore/Shapes.swift"),
                                     "line": 3, "module": "SwiftUI"}])
        events, done = self.run_tests()
        self.assertEqual(done["error"]["code"], "host_unsupported")
        self.assertIn("AppCore imports SwiftUI", done["error"]["message"])
        self.assertIn("Shapes.swift:3: import SwiftUI", done["error"]["detail"])
        self.assertNotIn("data", done)
        d, = self.of(events, "diagnostic")
        self.assertEqual((d["file"], d["line"]), (blockers[0]["file"], 3))
        self.assertEqual(self.calls_of("build"), [])

    def test_host_unsupported_from_the_build(self):
        events, done = self.run_tests(FAKE_SWIFT_SCENARIO="nomodule")
        self.assertEqual(done["error"]["code"], "host_unsupported")
        self.assertIn("SwiftUI", done["error"]["message"])
        self.assertIn("Shapes.swift:1: no such module 'SwiftUI'", done["error"]["detail"])
        self.assertTrue(any(d.get("line") == 1 for d in self.of(events, "diagnostic")))

    def test_busy(self):
        lock = self.state / "locks" / f"{fileio.path_key(self.root)}.lock"
        lock.parent.mkdir(parents=True)
        with open(lock, "a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _, done = self.run_tests()
        self.assertEqual(done["error"]["code"], "busy")
        self.assertEqual(self.calls(), [])
        _, done = self.run_tests()
        self.assertTrue(done["ok"])

    def test_filter(self):
        events, done = self.run_tests("--filter", "MathTests/testAdd$")
        self.assertTrue(done["ok"])
        self.assertEqual(done["data"]["filter"], "MathTests/testAdd$")
        self.assertEqual(done["data"]["counts"]["total"], 1)
        test, = self.calls_of("test")
        self.assertEqual(test[test.index("--filter") + 1], "MathTests/testAdd$")
        _, done = self.run_tests("--filter", "NOTHING")
        self.assertTrue(done["ok"])
        self.assertTrue(done["data"]["no_tests_matched"])
        self.assertEqual(done["data"]["counts"]["total"], 0)
        n = len(self.calls())
        events, done = self.run_tests("--filter", "Math(")
        self.assertEqual(done["error"]["code"], "bad_filter")
        self.assertEqual(len(events), 1, "nothing ran before the filter was checked")
        self.assertEqual(len(self.calls()), n)

    def test_native_fallback(self):
        _, done = self.run_tests(FAKE_SWIFT_SCENARIO="nonative")
        self.assertTrue(done["ok"], done)
        builds = self.calls_of("build")
        self.assertEqual(["--build-system" in b for b in builds], [True, False])
        self.assertNotIn("--build-system", self.calls_of("test")[0])
        self.argv_file.unlink()
        _, done = self.run_tests(FAKE_SWIFT_SCENARIO="nonative")
        self.assertTrue(done["ok"])
        self.assertEqual(["--build-system" in b for b in self.calls_of("build")], [False], "the choice is remembered")

    def test_toolchain_missing(self):
        events, done = self.run_tests(IOSSTUDIO_SWIFT=str(self.tmp / "no-swift"))
        self.assertEqual(done["error"]["code"], "toolchain")
        self.assertEqual(done["error"]["fix"], {"label": "Install Swift", "page": "setup", "step": "swift"})

    def test_timeout(self):
        t0 = time.monotonic()
        events, done = self.run_tests(FAKE_SWIFT_SCENARIO="hang", IOSSTUDIO_TEST_RUN_TIMEOUT="2")
        self.assertLess(time.monotonic() - t0, 30)
        self.assertEqual(done["error"]["code"], "timeout")
        self.assert_result(done["data"])
        hung, = self.of(events, "test")
        self.assertEqual((hung["id"], hung["status"]), ("MathTests/testHang", "failed"))
        self.assertIn("Didn't finish", hung["message"])
        self.assert_gone()

    # ---- cancel
    def start(self, **env: str) -> subprocess.Popen:
        proc = subprocess.Popen(HELPER + ["test", "run", "--project", str(self.root)], env={**self.env, **env},
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if '"id":"build","title"' in line and '"running"' in line:
                time.sleep(0.5)
                return proc
        proc.kill()
        self.fail("the build never started")

    def finish(self, proc: subprocess.Popen) -> dict:
        out, _ = proc.communicate(timeout=20)
        return json.loads(out.strip().splitlines()[-1])

    def assert_gone(self):
        pids = [int(p) for p in self.pids_file.read_text().split()]
        deadline = time.monotonic() + 5
        while any(alive(p) for p in pids) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse([p for p in pids if alive(p)], "swift outlived the run")

    def test_cancel_by_stdin(self):
        proc = self.start(FAKE_SWIFT_SCENARIO="slow")
        t0 = time.monotonic()
        proc.stdin.write('{"cancel": true}\n')
        proc.stdin.flush()
        done = self.finish(proc)
        self.assertLess(time.monotonic() - t0, 10)
        self.assertEqual(done["error"]["code"], "cancelled")
        self.assert_result(done["data"])
        self.assert_gone()
        self.assertFalse((self.state / "last.json").exists(), "a cancelled run isn't the last run")

    def test_cancel_by_sigterm(self):
        proc = self.start(FAKE_SWIFT_SCENARIO="slow")
        t0 = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        done = self.finish(proc)
        self.assertLess(time.monotonic() - t0, 10)
        self.assertEqual(done["error"]["code"], "cancelled")
        self.assert_gone()

    # ---- what runs leave
    def test_seed_and_logs_pruned(self):
        build = self.root / ".build"
        (build / "checkouts" / "swift-collections").mkdir(parents=True)
        (build / "checkouts" / "swift-collections" / "Package.swift").write_text("// x\n")
        (build / "repositories" / "swift-collections-1").mkdir(parents=True)
        (build / "workspace-state.json").write_text("{}\n")
        self.state.mkdir(parents=True)
        for i in range(25):
            f = self.state / f"20200101-0000{i:02d}-000-Old.log"
            f.write_text("old\n")
            os.utime(f, (1_600_000_000 + i, 1_600_000_000 + i))
        _, done = self.run_tests()
        scratch = build / "host-tests"
        self.assertTrue((scratch / "checkouts" / "swift-collections" / "Package.swift").is_file())
        self.assertTrue((scratch / "repositories" / "swift-collections-1").is_dir())
        self.assertTrue((scratch / "workspace-state.json").is_file())
        self.assertIn("Reused the packages", Path(done["data"]["log_path"]).read_text())
        logs = sorted(self.state.glob("*.log"))
        self.assertEqual(len(logs), 20)
        self.assertIn(Path(done["data"]["log_path"]), logs)


if __name__ == "__main__":
    unittest.main()
