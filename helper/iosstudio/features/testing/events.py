"""swift-testing's JSON event stream (`--experimental-event-stream-output F --experimental-event-stream-version 0`)
-> TestResults, read while `swift test` writes it (stdlib only).

    r = EventStreamReader(path, root=project_dir)
    while running: emit(r.poll())               # tests finished since the last poll
    emit(r.finish(fatals=xctest_parser.fatals)) # tests that started and never ended (the process crashed)

Each line is one JSON record {"kind": "test"|"event", "payload": {...}, "version": 0}:
  test   {id, kind: "suite"|"function", name, displayName?, sourceLocation{_filePath, fileID, line, column}}
  event  {kind, testID?, messages: [{symbol, text}], issue?, instant{absolute}}  kinds: runStarted,
         testStarted, testCaseStarted, testCaseEnded, issueRecorded, testSkipped, testEnded, runEnded
Pass/fail lives only in messages[].symbol: pass, passWithKnownIssue, fail, skip (default/details are prose).
One result per test function: a parameterized test fails when any of its cases recorded an issue. Suites are
not results. Ids drop the source location: "AppCoreSwiftTests.GreetingTests/add()".

poll() reads only the bytes appended since the last call (a byte offset), never a partial last line, skips
blank and non-JSON lines, and skips lines over MAX_LINE bytes without holding them.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .results import MAX_MESSAGE, TestResult, clip
from .xctest import Fatal, parse_fatal, resolve_path

MAX_LINE = 1 << 20
CHUNK = 1 << 16
MAX_TESTS = 100_000          # test records / running tests kept (a cap, not a real-world limit)
MAX_DETAILS = 3              # "details" messages appended to a failure message

LOCATION = re.compile(r"/[^/]+\.swift:\d+:\d+$")
AFTER = re.compile(r"after (\d+(?:\.\d+)?) seconds")
SKIP_REASON = re.compile(r"skipped: (.+?)\.?$")
_decode = json.JSONDecoder().decode


@dataclass(slots=True)
class _Test:
    name: str
    is_suite: bool
    file_id: str | None = None
    file: str | None = None
    line: int | None = None


@dataclass(slots=True)
class _Running:
    started: float | None
    file: str | None = None
    line: int | None = None
    message: str | None = None
    more: int = 0
    failed: bool = False


class EventStreamReader:
    def __init__(self, path: Path, root: Path | None = None):
        self.path = Path(path)
        self.root = root
        self.run_ended = False
        self.crashed: str | None = None      # id of the test the crash was pinned on (finish)
        self._offset = 0
        self._skipping = False               # inside a line longer than MAX_LINE
        self._tests: dict[str, _Test] = {}
        self._running: dict[str, _Running] = {}

    def poll(self) -> list[TestResult]:
        out: list[TestResult] = []
        try:
            f = self.path.open("rb")
        except OSError:
            return out
        with f:
            f.seek(self._offset)
            buf = bytearray()
            while chunk := f.read(CHUNK):
                buf += chunk
                start = 0
                while (nl := buf.find(b"\n", start)) >= 0:
                    if self._skipping:
                        self._skipping = False
                    elif nl > start:
                        out.extend(self._line(bytes(buf[start:nl])))
                    start = nl + 1
                self._offset += start
                del buf[:start]
                if self._skipping or len(buf) > MAX_LINE:
                    self._skipping = True
                    self._offset += len(buf)
                    buf.clear()
        return out

    def finish(self, fatals: Iterable[Fatal | str] = ()) -> list[TestResult]:
        """The final poll, then every test function that started and never ended counts as failed.

        Without runEnded the process crashed: a trap line (XCTestParser.fatals, or raw lines) whose file is
        a running test's file pins the crash on that test; with one running test it gets the crash anyway.
        """
        out = self.poll()
        running = [(tid, st) for tid, st in self._running.items() if not self._is_suite(tid)]
        self._running.clear()
        if not running:
            return out
        traps = [f if isinstance(f, Fatal) else parse_fatal(f) for f in fatals]
        traps = [f for f in traps if f is not None]
        pinned: dict[str, Fatal | None] = {}
        if not self.run_ended:
            for tid, _ in running:
                t = self._tests.get(tid)
                for trap in traps:
                    if t and t.file_id and (trap.file == t.file_id or (t.file or "").endswith("/" + trap.file)):
                        pinned[tid] = trap
                        break
            if not pinned and len(running) == 1:
                pinned[running[0][0]] = traps[0] if traps else None
        for tid, st in running:
            r = self._result(tid, "failed", None)
            r.file, r.line = st.file, st.line
            if self.run_ended:
                r.message = "Didn't finish"
            elif tid in pinned:
                self.crashed = self.crashed or r.id
                trap = pinned[tid]
                if trap is not None:
                    t = self._tests.get(tid)
                    same_file = t is not None and t.file and trap.file == t.file_id
                    r.file, r.line = (t.file if same_file else trap.resolve(self.root)), trap.line
                    r.message = clip(f"Crashed: {trap.message}")
                else:
                    r.message = "Crashed"
            elif pinned:
                r.message = "Didn't finish: the test process crashed"
            else:
                r.message = "Crashed"
            out.append(r)
        return out

    # ------------------------------------------------------------ records

    def _line(self, raw: bytes) -> list[TestResult]:
        if b'"kind":"testCase' in raw:          # per-case start/end: issues carry the function's testID
            return []
        try:
            rec = _decode(raw.decode("utf-8", "replace"))
        except ValueError:
            return []
        if not isinstance(rec, dict) or not isinstance(payload := rec.get("payload"), dict):
            return []
        if rec.get("kind") == "test":
            self._record(payload)
            return []
        if rec.get("kind") != "event":
            return []
        kind, tid = payload.get("kind"), payload.get("testID")
        if kind == "runEnded":
            self.run_ended = True
            return []
        if not isinstance(tid, str):
            return []
        if kind == "testStarted":
            if not self._is_suite(tid) and len(self._running) < MAX_TESTS:
                self._running[tid] = _Running(_instant(payload))
        elif kind == "issueRecorded":
            self._issue(tid, payload)
        elif kind == "testEnded":
            return self._ended(tid, payload)
        elif kind == "testSkipped" and not self._is_suite(tid):
            r = self._result(tid, "skipped", None)
            m = SKIP_REASON.search(_text(payload))
            r.message = clip(m[1].strip().strip('"')) if m else None
            return [r]
        return []

    def _record(self, p: dict) -> None:
        tid = p.get("id")
        if not isinstance(tid, str) or len(self._tests) >= MAX_TESTS:
            return
        name = p.get("displayName") or p.get("name") or tid
        loc = p.get("sourceLocation") if isinstance(p.get("sourceLocation"), dict) else {}
        t = _Test(str(name), p.get("kind") == "suite")
        if loc:
            t.file_id = loc.get("fileID") if isinstance(loc.get("fileID"), str) else None
            t.file = self._path(loc)
            t.line = loc.get("line") if isinstance(loc.get("line"), int) else None
        self._tests[tid] = t

    def _issue(self, tid: str, p: dict) -> None:
        issue = p.get("issue") if isinstance(p.get("issue"), dict) else {}
        if issue.get("isKnown"):
            return
        messages = [m for m in p.get("messages") or [] if isinstance(m, dict)]
        symbols = {m.get("symbol") for m in messages}
        if messages and "fail" not in symbols:
            return                              # a warning or known issue, not a failure
        st = self._running.get(tid)
        if st is None:
            if self._is_suite(tid) or len(self._running) >= MAX_TESTS:
                return
            st = self._running[tid] = _Running(None)
        st.failed = True
        if st.message is not None:
            st.more += 1
            return
        loc = issue.get("sourceLocation") or p.get("_sourceLocation")
        if isinstance(loc, dict):
            st.file = self._path(loc)
            st.line = loc.get("line") if isinstance(loc.get("line"), int) else None
        head = next((str(m.get("text", "")) for m in messages if m.get("symbol") == "fail"), "") or "Issue recorded"
        details = [str(m.get("text", "")) for m in messages if m.get("symbol") == "details"][:MAX_DETAILS]
        st.message = clip(head + (f" ({'; '.join(details)})" if details else ""))

    def _ended(self, tid: str, p: dict) -> list[TestResult]:
        st = self._running.pop(tid, None)
        if self._is_suite(tid):
            return []
        symbol = next((m.get("symbol") for m in p.get("messages") or [] if isinstance(m, dict)
                       and m.get("symbol") not in (None, "default", "details")), None)
        failed = (st is not None and st.failed) or symbol == "fail"
        status = "failed" if failed else "skipped" if symbol == "skip" else "passed"
        m = AFTER.search(_text(p))
        if m:
            duration: float | None = float(m[1])
        else:
            end = _instant(p)
            duration = round(end - st.started, 6) if st and st.started is not None and end is not None else None
        r = self._result(tid, status, duration)
        if failed:
            if st is not None:
                r.file, r.line = st.file, st.line
                r.message = _with_more(st.message or "failed", st.more)
            else:
                r.message = "failed"
        return [r]

    # ------------------------------------------------------------ helpers

    def _is_suite(self, tid: str) -> bool:
        t = self._tests.get(tid)
        return t.is_suite if t is not None else not LOCATION.search(tid)

    def _path(self, loc: dict) -> str | None:
        for key in ("_filePath", "filePath"):
            if isinstance(v := loc.get(key), str) and v:
                return v
        v = loc.get("fileID")
        return resolve_path(v, self.root) if isinstance(v, str) and v else None

    def _result(self, tid: str, status: str, duration: float | None) -> TestResult:
        rid = LOCATION.sub("", tid)
        module, _, rest = rid.partition(".")
        parts = rest.split("/") if rest else [module]
        t = self._tests.get(tid)
        name = t.name if t is not None else parts[-1]
        suite = ".".join(parts[:-1]) or module
        return TestResult(rid, name, suite, "swift-testing", status, duration)


def _instant(p: dict) -> float | None:
    inst = p.get("instant")
    v = inst.get("absolute") if isinstance(inst, dict) else None
    return float(v) if isinstance(v, (int, float)) else None


def _text(p: dict) -> str:
    for m in p.get("messages") or []:
        if isinstance(m, dict) and isinstance(m.get("text"), str):
            return m["text"]
    return ""


def _with_more(message: str, more: int) -> str:
    if not more:
        return message
    suffix = f" (+{more} more)"
    return clip(message, MAX_MESSAGE - len(suffix)) + suffix
