"""XCTest's console output (Linux, swift-corelibs-xctest) -> TestResults, one line at a time (stdlib only).

    p = XCTestParser(root=project_dir)
    for line in output: emit(p.feed(line))     # the tests this line finished
    emit(p.finish(exit_code))                  # a test that started and never ended: it crashed

The lines it reads (everything else, swift-testing's ◇ ✔ ✘ ↳ lines and the build, is ignored):

    Test Case 'MathTests.testAdd' started at 2026-11-25 22:04:19.484
    /abs/Tests/AppCoreTests/FailingTests.swift:6: error: FailingTests.testAddIsWrong : XCTAssertEqual failed: …
    /abs/Tests/AppCoreTests/ExtraTests.swift:5: ExtraTests.testSkipped : Test skipped - needs a network
    Test Case 'MathTests.testAdd' passed (0.005 seconds)            (or failed / skipped)
    AppCoreTests/CrashTests.swift:7: Fatal error: Unexpectedly found nil while unwrapping an Optional value
    Test Suite 'All tests' failed at …                              ('Selected tests' with --filter)
    	 Executed 6 tests, with 1 test skipped and 3 failures (0 unexpected) in 0.002 (0.002) seconds
    warning: No matching test cases were run

Memory is O(1) per line: only the running test's state is kept (and at most MAX_FATALS fatal error lines).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .results import MAX_MESSAGE, TestResult, clip

MAX_FATALS = 10
MAX_LINE = 64 * 1024   # longer lines are cut before matching (a test can print anything)

CASE_START = re.compile(r"^Test Case '(?P<name>[^']+)' started\b")
CASE_END = re.compile(r"^Test Case '(?P<name>[^']+)' (?P<status>passed|failed|skipped) \((?P<dur>[\d.]+) seconds\)")
FAILURE = re.compile(r"^(?P<file>.+?):(?P<line>\d+): error: (?P<name>[^\s:]+) : (?P<msg>.*)$")
SKIP = re.compile(r"^(?P<file>.+?):(?P<line>\d+): (?P<name>[^\s:]+) : Test skipped(?: - (?P<msg>.*))?$")
FATAL = re.compile(r"(?P<file>[^\s:]+\.swift):(?P<line>\d+): (?P<kind>Fatal error|Precondition failed|"
                   r"Assertion failed)(?::\s?(?P<msg>.*))?$")
TOP_SUITE_END = re.compile(r"^Test Suite '(?:All tests|Selected tests)' (?:passed|failed) at\b")
SUMMARY = re.compile(r"^\s*Executed (?P<n>\d+) tests?, with (?:(?P<skipped>\d+) tests? skipped and )?"
                     r"(?P<failures>\d+) failures?\b")
NO_MATCH = "warning: No matching test cases were run"
# swift test's own line when the XCTest process traps (the swift-testing run's line names --testing-library).
SIGNAL = re.compile(r"^error: Process '.*' exited with unexpected signal code (?P<sig>\d+)")
SWIFT_TESTING_MARKS = "◇✔✘↳➜━⚠"


@dataclass(slots=True)
class Fatal:
    """A Swift runtime trap line: `<Module>/<File>.swift:7: Fatal error: <message>`."""
    file: str              # as printed: module-relative (the #fileID), sometimes absolute
    line: int
    message: str           # "Fatal error: Unexpectedly found nil …"

    def resolve(self, root: Path | None) -> str:
        return resolve_path(self.file, root)


def parse_fatal(line: str) -> Fatal | None:
    m = FATAL.search(line[:MAX_LINE])
    if not m:
        return None
    msg = m["kind"] + (f": {m['msg'].strip()}" if m["msg"] and m["msg"].strip() else "")
    return Fatal(m["file"], int(m["line"]), clip(msg))


def resolve_path(file: str, root: Path | None) -> str:
    """`AppCoreTests/X.swift` (a #fileID) -> <root>/Tests/AppCoreTests/X.swift or <root>/Sources/… if it exists."""
    if file.startswith("/") or root is None:
        return file
    for base in ("Tests", "Sources"):
        candidate = root / base / file
        if candidate.is_file():
            return str(candidate)
    return file


def test_id(name: str) -> tuple[str, str, str]:
    """'MathTests.testAdd' -> ('MathTests/testAdd', 'MathTests', 'testAdd'), usable as a --filter regex."""
    suite, _, method = name.rpartition(".")
    if not suite:
        return name, "", name
    return f"{suite}/{method}", suite, method


@dataclass(slots=True)
class _Running:
    name: str
    file: str | None = None
    line: int | None = None
    message: str | None = None
    more: int = 0          # failures after the first one
    skip: str | None = None
    fatal: Fatal | None = None


class XCTestParser:
    def __init__(self, root: Path | None = None):
        self.root = root
        self.crashed: str | None = None          # id of the test that was running when the process died
        self.no_tests_matched = False
        self.executed: int | None = None         # from the outermost 'All tests'/'Selected tests' summaries
        self.failures: int | None = None
        self.skipped: int | None = None
        self.fatals: list[Fatal] = []            # every trap line seen (also swift-testing's), first MAX_FATALS
        self.signal: int | None = None           # the XCTest process died on this signal
        self._current: _Running | None = None
        self._top_summary_next = False

    def feed(self, line: str) -> list[TestResult]:
        line = line[:MAX_LINE].rstrip("\r\n")
        if not line or line[0] in SWIFT_TESTING_MARKS:
            return []
        if line.startswith("Test Case '"):
            if m := CASE_START.match(line):
                out = self._flush("Didn't finish") if self._current else []
                self._current = _Running(m["name"])
                return out
            if m := CASE_END.match(line):
                return [self._end(m["name"], m["status"], float(m["dur"]))]
            return []
        if self._top_summary_next and (m := SUMMARY.match(line)):
            self._top_summary_next = False
            self.executed = (self.executed or 0) + int(m["n"])
            self.failures = (self.failures or 0) + int(m["failures"])
            self.skipped = (self.skipped or 0) + int(m["skipped"] or 0)
            return []
        if line.startswith("Test Suite '"):
            self._top_summary_next = bool(TOP_SUITE_END.match(line))
            return []
        if line.startswith(NO_MATCH):
            self.no_tests_matched = True
            return []
        if line.startswith("error: Process '") and "--testing-library swift-testing" not in line:
            if m := SIGNAL.match(line):
                self.signal = int(m["sig"])
            return []
        cur = self._current
        if cur is not None and " : " in line:
            if (m := FAILURE.match(line)) and m["name"] == cur.name:
                if cur.message is None:
                    cur.file = None if m["file"].startswith("<") else resolve_path(m["file"], self.root)
                    cur.line = int(m["line"]) if cur.file else None
                    cur.message = clip(m["msg"]) or "failed"
                else:
                    cur.more += 1
                return []
            if (m := SKIP.match(line)) and m["name"] == cur.name:
                cur.skip = clip(m["msg"] or "") or None
                return []
        if ".swift:" in line and (fatal := parse_fatal(line)):
            if len(self.fatals) < MAX_FATALS:
                self.fatals.append(fatal)
            if cur is not None and cur.fatal is None:
                cur.fatal = fatal
        return []

    def finish(self, exit_code: int | None = None) -> list[TestResult]:
        """Flush the test that never ended (the process crashed or was stopped)."""
        if self._current is None:
            return []
        if self.signal is not None:
            return self._flush(f"Crashed (signal {self.signal})", crashed=True)
        if exit_code is not None and exit_code < 0:
            return self._flush(f"Didn't finish (stopped by signal {-exit_code})")
        return self._flush("Didn't finish")

    # ------------------------------------------------------------ internals

    def _end(self, name: str, status: str, duration: float) -> TestResult:
        cur = self._current if self._current is not None and self._current.name == name else _Running(name)
        self._current = None
        tid, suite, method = test_id(name)
        r = TestResult(tid, method, suite, "xctest", status, duration)
        if status == "failed":
            r.file, r.line = cur.file, cur.line
            r.message = _with_more(cur.message or "failed", cur.more)
        elif status == "skipped" and cur.skip:
            r.message = cur.skip
        return r

    def _flush(self, reason: str, crashed: bool = False) -> list[TestResult]:
        cur, self._current = self._current, None
        assert cur is not None
        tid, suite, method = test_id(cur.name)
        r = TestResult(tid, method, suite, "xctest", "failed", None, cur.file, cur.line)
        if cur.fatal is not None:
            self.crashed = self.crashed or tid
            r.file, r.line = cur.fatal.resolve(self.root), cur.fatal.line
            r.message = clip(f"Crashed: {cur.fatal.message}")
        else:
            if crashed:
                self.crashed = self.crashed or tid
            r.message = _with_more(f"{reason}: {cur.message}", cur.more) if cur.message else reason
        return [r]


def _with_more(message: str, more: int) -> str:
    if not more:
        return message
    suffix = f" (+{more} more)"
    return clip(message, MAX_MESSAGE - len(suffix)) + suffix
