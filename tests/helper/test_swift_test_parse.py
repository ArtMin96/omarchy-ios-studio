"""swift test output parsers (helper/iosstudio/features/testing/{results,xctest,events}.py).

    python -m pytest tests/helper/test_swift_test_parse.py

Fixtures in tests/fixtures/swift-test/ are real Swift 6.4.2 output from a CrashLab copy on Linux
(`swift test --skip-build --build-system native` with SWIFT_BACKTRACE=enable=no; paths shortened to
/home/user/Projects/CrashLab, the [timeit] trailer dropped):
  xctest-passing / xctest-failing / xctest-skip-multi / xctest-crash   console output (stderr + stdout)
  crash-stdout.txt / crash-stderr.txt                                  one crashing run, the two pipes apart
  filter-one-xctest / filter-one-swift-testing / filter-none           --filter runs
  events-failing / events-extra / events-crash-truncated               --experimental-event-stream-version 0
  events-crash-v640                                                    the same crash without the version flag
"""
from __future__ import annotations

import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from iosstudio.features.testing import results
from iosstudio.features.testing.events import MAX_LINE, EventStreamReader
from iosstudio.features.testing.results import failure_diagnostic, filter_pattern, summarize
from iosstudio.features.testing.xctest import XCTestParser, parse_fatal
from support import ROOT

FIXTURES = ROOT / "tests" / "fixtures" / "swift-test"
CRASHLAB = "/home/user/Projects/CrashLab"


def parse_xctest(name: str, root: Path | None = None, exit_code: int | None = 1) -> tuple[XCTestParser, list]:
    p = XCTestParser(root=root)
    out: list[results.TestResult] = []
    for line in (FIXTURES / name).read_text().splitlines(keepends=True):
        out += p.feed(line)
    out += p.finish(exit_code)
    return p, out


def read_events(name: str, fatals=(), root: Path | None = None) -> tuple[EventStreamReader, list]:
    r = EventStreamReader(FIXTURES / name, root=root)
    out = r.poll()
    out += r.finish(fatals=fatals)
    return r, out


def by_id(items: list[results.TestResult]) -> dict[str, results.TestResult]:
    return {r.id: r for r in items}


class XCTestFixtures(unittest.TestCase):
    def test_passing(self):
        p, out = parse_xctest("xctest-passing.txt", exit_code=0)
        self.assertEqual([r.id for r in out], ["MathTests/testAdd", "MathTests/testClamp", "MathTests/testGreeting"])
        self.assertTrue(all(r.status == "passed" and r.library == "xctest" for r in out))
        self.assertEqual(out[0].duration, 0.007)
        self.assertEqual((p.executed, p.failures, p.skipped, p.crashed, p.no_tests_matched), (3, 0, 0, None, False))
        self.assertEqual(out[0].to_event(), {"id": "MathTests/testAdd", "name": "testAdd", "suite": "MathTests",
                                             "library": "xctest", "status": "passed", "duration": 0.007})

    def test_failing_mixed_with_swift_testing(self):
        p, out = parse_xctest("xctest-failing.txt")
        res = by_id(out)
        self.assertEqual(len(out), 5)          # swift-testing's ✘/✔ lines are not XCTest results
        a = res["FailingTests/testAddIsWrong"]
        self.assertEqual((a.status, a.file, a.line), ("failed", f"{CRASHLAB}/Tests/AppCoreTests/FailingTests.swift", 6))
        self.assertEqual(a.message, 'XCTAssertEqual failed: ("4") is not equal to ("5") - two plus two')
        b = res["FailingTests/testNilUnwrap"]
        self.assertEqual((b.status, b.line), ("failed", 10))
        self.assertTrue(b.message.startswith("XCTUnwrap failed"))
        self.assertEqual(res["MathTests/testAdd"].status, "passed")
        self.assertEqual((p.executed, p.failures), (5, 2))    # the outermost summary, not each suite's
        self.assertEqual(summarize(out), {"total": 5, "passed": 3, "failed": 2, "skipped": 0})

    def test_skip_and_several_failures(self):
        p, out = parse_xctest("xctest-skip-multi.txt")
        res = by_id(out)
        skip = res["ExtraTests/testSkipped"]
        self.assertEqual((skip.status, skip.message, skip.file), ("skipped", "needs a network", None))
        two = res["ExtraTests/testTwoFailures"]
        self.assertEqual((two.status, two.line), ("failed", 7))     # the first failure, and a count of the rest
        self.assertEqual(two.message, 'XCTAssertEqual failed: ("2") is not equal to ("3") - first (+2 more)')
        self.assertEqual((p.executed, p.failures, p.skipped), (6, 3, 1))
        self.assertEqual(summarize(out), {"total": 6, "passed": 4, "failed": 1, "skipped": 1})

    def test_crash_resolves_the_module_relative_path(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        (root / "Tests" / "AppCoreTests").mkdir(parents=True)
        (root / "Tests" / "AppCoreTests" / "CrashTests.swift").write_text("")
        p, out = parse_xctest("xctest-crash.txt", root=root)
        self.assertEqual(len(out), 1)
        r = out[0]
        self.assertEqual((r.id, r.status), ("CrashTests/testForceUnwrapCrash", "failed"))
        self.assertEqual(r.message, "Crashed: Fatal error: Unexpectedly found nil while unwrapping an Optional value")
        self.assertEqual((r.file, r.line), (str(root / "Tests/AppCoreTests/CrashTests.swift"), 7))
        self.assertEqual(p.crashed, "CrashTests/testForceUnwrapCrash")
        self.assertEqual(p.signal, 4)
        self.assertIsNone(p.executed)      # the process died before any summary
        # the swift-testing run's trap is kept for EventStreamReader.finish
        self.assertEqual([(f.file, f.line) for f in p.fatals],
                         [("AppCoreTests/CrashTests.swift", 7), ("AppCoreSwiftTests/CrashSwiftTests.swift", 5)])

    def test_crash_without_root_keeps_the_relative_path(self):
        _, out = parse_xctest("crash-stdout.txt", root=None)
        self.assertEqual((out[0].file, out[0].line), ("AppCoreTests/CrashTests.swift", 6))

    def test_crash_from_separate_pipes(self):
        p = XCTestParser()
        out = []
        for name in ("crash-stdout.txt", "crash-stderr.txt"):
            for line in (FIXTURES / name).read_text().splitlines():
                out += p.feed(line)
        out += p.finish(1)
        self.assertEqual(p.crashed, "CrashTests/testForceUnwrapCrash")
        self.assertEqual(p.signal, 4)      # the XCTest process, not the swift-testing one
        self.assertEqual(len(out), 1)

    def test_unfinished_without_a_trap(self):
        p = XCTestParser()
        p.feed("Test Case 'A.testB' started at 2026-11-25 22:00:00.000\n")
        out = p.finish(None)
        self.assertEqual((out[0].status, out[0].message), ("failed", "Didn't finish"))
        self.assertIsNone(p.crashed)
        p.feed("Test Case 'A.testC' started at 2026-11-25 22:00:00.000")
        self.assertEqual(p.finish(-15)[0].message, "Didn't finish (stopped by signal 15)")
        p.feed("Test Case 'A.testD' started at 2026-11-25 22:00:00.000")
        p.feed("error: Process '/x/PkgTests.xctest' exited with unexpected signal code 11")
        r = p.finish(1)[0]
        self.assertEqual((r.message, p.crashed), ("Crashed (signal 11)", "A/testD"))

    def test_filters(self):
        p, out = parse_xctest("filter-one-xctest.txt", exit_code=0)
        self.assertEqual([r.id for r in out], ["MathTests/testAdd"])
        self.assertEqual((p.executed, p.failures, p.no_tests_matched), (1, 0, False))   # 'Selected tests'
        p, out = parse_xctest("filter-one-swift-testing.txt", exit_code=0)
        self.assertEqual((out, p.executed, p.no_tests_matched), ([], None, False))
        p, out = parse_xctest("filter-none.txt", exit_code=0)
        self.assertEqual((out, p.no_tests_matched), ([], True))

    def test_ignores_build_and_junk(self):
        p = XCTestParser()
        for line in ["[3/9] Compiling AppCore Math.swift", "/abs/X.swift:3:1: error: cannot find 'y' in scope",
                     "error: FailingTests.testX : not in a test", "", "\x00\xff garbage", "x" * 200_000,
                     "Test Case 'broken"]:
            self.assertEqual(p.feed(line), [])
        self.assertEqual(p.finish(1), [])

    def test_failure_without_location(self):
        p = XCTestParser()
        p.feed("Test Case 'A.testB' started at 2026-11-25 22:00:00.000")
        p.feed("<unknown>:0: error: A.testB : failed - boom")
        r = p.feed("Test Case 'A.testB' failed (0.001 seconds)")[0]
        self.assertEqual((r.file, r.line, r.message), (None, None, "failed - boom"))

    def test_message_is_capped(self):
        p = XCTestParser()
        p.feed("Test Case 'A.testB' started at 2026-11-25 22:00:00.000")
        p.feed("/abs/A.swift:3: error: A.testB : " + "x" * 5000)
        p.feed("/abs/A.swift:4: error: A.testB : second")
        r = p.feed("Test Case 'A.testB' failed (0.001 seconds)")[0]
        self.assertLessEqual(len(r.message), 300)
        self.assertTrue(r.message.endswith("(+1 more)"))


class EventStreamFixtures(unittest.TestCase):
    def test_failing(self):
        r, out = read_events("events-failing.jsonl")
        res = by_id(out)
        self.assertTrue(r.run_ended)
        self.assertIsNone(r.crashed)
        self.assertEqual(set(res), {"AppCoreSwiftTests.GreetingTests/greet()", "AppCoreSwiftTests.GreetingTests/add()",
                                    "AppCoreSwiftTests.GreetingTests/addZero(n:)", "AppCoreSwiftTests.greetingIsWrong()",
                                    "AppCoreSwiftTests.requireFails()"})   # suites are not results
        greet = res["AppCoreSwiftTests.GreetingTests/greet()"]
        self.assertEqual((greet.name, greet.suite, greet.status, greet.duration),
                         ("Greeting trims spaces", "GreetingTests", "passed", 0.001))
        wrong = res["AppCoreSwiftTests.greetingIsWrong()"]
        self.assertEqual((wrong.status, wrong.suite, wrong.library), ("failed", "AppCoreSwiftTests", "swift-testing"))
        self.assertEqual((wrong.file, wrong.line),
                         (f"{CRASHLAB}/Tests/AppCoreSwiftTests/FailingSwiftTests.swift", 5))
        self.assertTrue(wrong.message.startswith('Expectation failed: greeting(for: "Bob") == "Hi, Bob!"'))
        self.assertIn('"Hello, Bob!"', wrong.message)
        self.assertEqual(res["AppCoreSwiftTests.requireFails()"].message, "Expectation failed: v (v → nil)")
        self.assertEqual(res["AppCoreSwiftTests.GreetingTests/addZero(n:)"].status, "passed")   # one per function
        self.assertEqual(summarize(out), {"total": 5, "passed": 3, "failed": 2, "skipped": 0})

    def test_skip_known_issue_parameterized_nested(self):
        r, out = read_events("events-extra.jsonl")
        res = by_id(out)
        self.assertEqual(len(out), 7)
        skip = res["AppCoreSwiftTests.Outer/disabledTest()"]
        self.assertEqual((skip.status, skip.message, skip.suite), ("skipped", "not ready", "Outer"))
        self.assertEqual(res["AppCoreSwiftTests.Outer/knownIssue()"].status, "passed")     # withKnownIssue
        deep = res["AppCoreSwiftTests.Outer/Inner/deep()"]
        self.assertEqual((deep.suite, deep.name, deep.status), ("Outer.Inner", "deep()", "passed"))
        case = res["AppCoreSwiftTests.Outer/oneCaseFails(n:)"]
        self.assertEqual((case.status, case.line), ("failed", 10))
        self.assertIn("n → 2", case.message)
        self.assertEqual(summarize(out), {"total": 7, "passed": 5, "failed": 1, "skipped": 1})

    def test_crash_truncated_pins_the_trap(self):
        p, _ = parse_xctest("crash-stdout.txt")
        r, out = read_events("events-crash-truncated.jsonl", fatals=p.fatals)
        res = by_id(out)
        self.assertFalse(r.run_ended)
        self.assertEqual(r.crashed, "AppCoreSwiftTests.fatalCrash()")
        crash = res["AppCoreSwiftTests.fatalCrash()"]
        self.assertEqual(crash.message, "Crashed: Fatal error: boom from swift-testing")
        self.assertEqual((crash.file, crash.line), (f"{CRASHLAB}/Tests/AppCoreSwiftTests/CrashSwiftTests.swift", 4))
        others = [x for x in out if x.status == "failed" and x.id != crash.id]
        self.assertTrue(others)            # started alongside it, never ended
        self.assertTrue(all(x.message == "Didn't finish: the test process crashed" for x in others))
        self.assertEqual(res["AppCoreSwiftTests.GreetingTests/add()"].status, "passed")
        self.assertEqual(res["AppCoreSwiftTests.Outer/disabledTest()"].status, "skipped")

    def test_crash_without_a_trap_line(self):
        r, out = read_events("events-crash-v640.jsonl")
        failed = [x for x in out if x.status == "failed"]
        self.assertEqual([(x.id, x.message) for x in failed], [("AppCoreSwiftTests.fatalCrash()", "Crashed")])
        self.assertEqual(r.crashed, "AppCoreSwiftTests.fatalCrash()")
        self.assertEqual(sum(x.status == "passed" for x in out), 3)

    def test_crash_raw_trap_line(self):
        r, out = read_events("events-crash-v640.jsonl",
                             fatals=["AppCoreSwiftTests/CrashSwiftTests.swift:5: Fatal error: boom from swift-testing"])
        crash = by_id(out)["AppCoreSwiftTests.fatalCrash()"]
        self.assertEqual((crash.message, crash.line), ("Crashed: Fatal error: boom from swift-testing", 5))

    def test_missing_file(self):
        r = EventStreamReader(Path(tempfile.gettempdir()) / "no-such-events.jsonl")
        self.assertEqual(r.poll(), [])
        self.assertEqual(r.finish(), [])
        self.assertFalse(r.run_ended)


class EventStreamIncremental(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = self.dir / "events.jsonl"
        self.path.write_bytes(b"")

    def append(self, data: bytes) -> None:
        with self.path.open("ab") as f:
            f.write(data)

    def test_partial_writes_match_a_whole_read(self):
        data = (FIXTURES / "events-failing.jsonl").read_bytes()
        _, whole = read_events("events-failing.jsonl")
        r = EventStreamReader(self.path)
        got = []
        for i in range(0, len(data), 97):        # cuts lines (and UTF-8 characters) anywhere
            self.append(data[i:i + 97])
            got += r.poll()
        got += r.finish()
        self.assertEqual([x.to_event() for x in got], [x.to_event() for x in whole])

    def test_partial_last_line_waits(self):
        lines = (FIXTURES / "events-failing.jsonl").read_bytes().splitlines(keepends=True)
        end = next(i for i, ln in enumerate(lines) if b'"testEnded"' in ln and b'"pass"' in ln)
        r = EventStreamReader(self.path)
        self.append(b"".join(lines[:end]) + lines[end][:40])
        self.assertEqual(r.poll(), [])
        self.append(lines[end][40:])
        self.assertEqual(len(r.poll()), 1)
        self.assertEqual(r.poll(), [])           # nothing new

    def test_junk_blank_and_huge_lines(self):
        lines = (FIXTURES / "events-failing.jsonl").read_bytes().splitlines(keepends=True)
        junk = [b"\n", b"not json\n", b"[1, 2]\n", b'{"kind": "event"}\n', b'{"kind":"event","payload":[]}\n',
                b'{"kind":"event","payload":{"kind":"testEnded","testID":5}}\n', b"\xff\xfe\n",
                b'{"x": "' + b"a" * (MAX_LINE + 10) + b'"}\n']
        r = EventStreamReader(self.path)
        got = []
        for i, line in enumerate(lines):
            self.append(junk[i % len(junk)] + line)
            got += r.poll()
        got += r.finish()
        self.assertEqual(summarize(got), {"total": 5, "passed": 3, "failed": 2, "skipped": 0})
        self.assertTrue(r.run_ended)

    def test_huge_line_split_across_polls(self):
        r = EventStreamReader(self.path)
        self.append(b'{"x": "' + b"a" * (MAX_LINE + 5))
        self.assertEqual(r.poll(), [])
        self.append(b"b" * 1000 + b'"}\n')
        self.assertEqual(r.poll(), [])
        self.append(b'{"kind":"event","payload":{"kind":"runEnded","messages":[]}}\n')
        r.poll()
        self.assertTrue(r.run_ended)

    def test_issue_before_start_and_unknown_tests(self):
        recs = [
            {"kind": "event", "payload": {"kind": "issueRecorded", "testID": "M.t()/F.swift:1:2",
                                          "messages": [{"symbol": "fail", "text": "Caught error: E"}],
                                          "issue": {"isKnown": False, "sourceLocation": {"fileID": "M/F.swift", "line": 3}}}},
            {"kind": "event", "payload": {"kind": "testEnded", "testID": "M.t()/F.swift:1:2",
                                          "messages": [{"symbol": "fail", "text": "Test t() failed after 0.5 seconds"}]}},
            {"kind": "event", "payload": {"kind": "testEnded", "testID": "M.S",
                                          "messages": [{"symbol": "fail", "text": "Suite S failed"}]}},
            {"kind": "event", "payload": {"kind": "runEnded", "messages": []}},
        ]
        self.append("".join(json.dumps(x) + "\n" for x in recs).encode())
        out = EventStreamReader(self.path).finish()
        self.assertEqual(len(out), 1)            # "M.S" has no source location: a suite
        t = out[0]
        self.assertEqual((t.id, t.name, t.suite, t.status, t.duration), ("M.t()", "t()", "M", "failed", 0.5))
        self.assertEqual((t.file, t.line, t.message), ("M/F.swift", 3, "Caught error: E"))

    def test_ran_to_the_end_but_a_test_never_ended(self):
        recs = [{"kind": "event", "payload": {"kind": "testStarted", "testID": "M.t()/F.swift:1:2", "messages": []}},
                {"kind": "event", "payload": {"kind": "runEnded", "messages": []}}]
        self.append("".join(json.dumps(x) + "\n" for x in recs).encode())
        r = EventStreamReader(self.path)
        out = r.finish()
        self.assertEqual((out[0].status, out[0].message, r.crashed), ("failed", "Didn't finish", None))


class ResultShapes(unittest.TestCase):
    def test_event_failure_and_diagnostic(self):
        r = results.TestResult("FailingTests/testAddIsWrong", "testAddIsWrong", "FailingTests", "xctest", "failed", 0.003,
                       "/abs/F.swift", 6, "XCTAssertEqual failed")
        self.assertEqual(r.to_event()["file"], "/abs/F.swift")
        self.assertEqual(r.to_failure(), {"id": "FailingTests/testAddIsWrong", "name": "testAddIsWrong",
                                          "suite": "FailingTests", "library": "xctest", "file": "/abs/F.swift",
                                          "line": 6, "message": "XCTAssertEqual failed"})
        self.assertEqual(failure_diagnostic(r), {"severity": "error", "category": "test", "file": "/abs/F.swift",
                                                 "line": 6, "col": 0,
                                                 "message": "FailingTests.testAddIsWrong: XCTAssertEqual failed"})
        r.message, r.file = "y" * 1000, None
        d = failure_diagnostic(r)
        self.assertLessEqual(len(d["message"]), 300)
        self.assertNotIn("file", d)

    def test_filter_pattern(self):
        x = results.TestResult("MathTests/testAdd", "testAdd", "MathTests", "xctest", "passed")
        s = results.TestResult("AppCoreSwiftTests.GreetingTests/add()", "add()", "GreetingTests", "swift-testing", "passed")
        self.assertEqual(filter_pattern(x), "MathTests/testAdd$")
        self.assertEqual(filter_pattern(s), r"AppCoreSwiftTests\.GreetingTests/add\(\)")

    def test_parse_fatal(self):
        f = parse_fatal("Sources/AppCore/X.swift:12: Precondition failed: n > 0")
        self.assertEqual((f.file, f.line, f.message), ("Sources/AppCore/X.swift", 12, "Precondition failed: n > 0"))
        self.assertIsNone(parse_fatal("Test Case 'A.b' passed (0.1 seconds)"))


class Performance(unittest.TestCase):
    def test_100k_xctest_lines(self):
        block = (FIXTURES / "xctest-failing.txt").read_text().splitlines()
        lines = (block * (100_000 // len(block) + 1))[:100_000]
        p = XCTestParser()
        t0 = time.process_time()
        n = 0
        for line in lines:
            n += len(p.feed(line))
        elapsed = time.process_time() - t0
        self.assertGreater(n, 9_000)
        self.assertLess(elapsed, 1.0, f"{elapsed:.2f}s for 100k lines")

    def test_20k_event_lines(self):
        # json decoding is most of the cost (~5 µs a line on an idle machine); the reader adds little on top,
        # and the testCase* lines of parameterized tests are skipped without decoding.
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        path = tmp / "events.jsonl"
        recs = []
        for i in range(5_000):
            tid = f"M.S/t{i}()/F.swift:{i}:2"
            recs += [{"kind": "event", "payload": {"kind": "testStarted", "testID": tid, "messages": []}},
                     {"kind": "event", "payload": {"kind": "testCaseStarted", "testID": tid, "messages": []}},
                     {"kind": "event", "payload": {"kind": "testCaseEnded", "testID": tid, "messages": []}},
                     {"kind": "event", "payload": {"kind": "testEnded", "testID": tid,
                                                   "messages": [{"symbol": "pass", "text": "passed after 0.001 seconds."}]}}]
        path.write_text("".join(json.dumps(x, separators=(",", ":")) + "\n" for x in recs))
        t0 = time.process_time()
        r = EventStreamReader(path)
        out = r.poll() + r.finish()
        elapsed = time.process_time() - t0
        self.assertEqual(len(out), 5_000)
        self.assertLess(elapsed, 1.0, f"{elapsed:.2f}s for 20k event lines")


if __name__ == "__main__":
    unittest.main()
