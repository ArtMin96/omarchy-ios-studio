"""Demo mode of the `test` group (IOSSTUDIO_DEMO): made-up `swift test` runs for the demo projects.

Same commands and reply shapes as the real group in features/testing:
  test info --project P                   test targets, tested targets, host blockers, the last demo run
  test run --project P [--filter REGEX]   JSON lines: step prepare/build/run, log, test, diagnostic, done

Scripted per project folder name (anything else has tests that all pass):
  WeatherGlance  12 tests (XCTest + swift-testing), all pass
  HabitTracker   one XCTest assertion and one swift-testing #expect fail, one test is skipped
  RecipeBox      no test target                          -> done ok:false "no_tests"
  SketchPad      the tested code imports SwiftUI         -> done ok:false "host_unsupported"
IOSSTUDIO_DEMO_TESTS=pass|fail|none|host gives every project that behaviour (screenshots).

Nothing is built or run. Each run writes a small log to $XDG_STATE_HOME/ios-studio/test-runs/ (pruned like the
real logs) and its outcome to demo-last.json there (capped), so `test info` shows it as last_run.
A run stops at once on {"cancel": true} on stdin or SIGTERM (done ok:false "cancelled").
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from iosstudio.core import appdirs, fileio, output, subproc
from iosstudio.core.output import HelperError

LOG_KEEP, LOG_MAX_BYTES = 20, 50 * 1024 * 1024
LAST_MAX = 100          # projects remembered in demo-last.json
FAILURES_MAX = 50
TEST_PAUSE = 0.12       # seconds per test (IOSSTUDIO_DEMO_DELAY scales it)
SLICE = 0.05            # cancel is checked at least this often while pausing


@dataclass(frozen=True)
class Case:
    target: str
    suite: str
    name: str
    library: str                # "xctest" | "swift-testing"
    seconds: float
    fails: tuple[str, int, int, str] | None = None   # (file relative to the project, line, col, message)
    skipped: bool = False

    @property
    def id(self) -> str:
        return f"{self.target}.{self.suite}/{self.name}"


def _cases(target: str, suite: str, library: str, names: str, seconds: float = 0.004) -> list[Case]:
    return [Case(target, suite, n, library, seconds) for n in names.split()]


@dataclass(frozen=True)
class Scenario:
    core: str                   # the tested library target
    tests: tuple[Case, ...] = ()
    sources: tuple[str, ...] = ()
    blockers: tuple[tuple[str, int, str], ...] = ()   # (file relative to the project, line, module)
    duration: float = 3.4


def _scenarios() -> dict[str, Scenario]:
    weather = (
        _cases("WeatherCoreTests", "ForecastParserTests", "xctest", "testParsesHourly testParsesDaily testRejectsEmptyPayload")
        + _cases("WeatherCoreTests", "UnitConverterTests", "xctest", "testCelsiusToFahrenheit testRoundsToOneDecimal testKelvin")
        + _cases("WeatherCoreTests", "ForecastCacheTests", "xctest", "testExpiresAfterOneHour", 0.012)
        + _cases("WeatherCoreSwiftTests", "ConditionTests", "swift-testing",
                 "symbolForEveryCondition() rainChanceIsClamped() nightUsesMoonSymbols()", 0.001)
        + _cases("WeatherCoreSwiftTests", "LocationFormatterTests", "swift-testing", "shortNames() coordinatesFallback()", 0.001)
    )
    habit_fail_x = ("Tests/HabitCoreTests/StreakCalculatorTests.swift", 24, 0,
                    'XCTAssertEqual failed: ("3") is not equal to ("0") - a missed day resets the streak')
    habit_fail_s = ("Tests/HabitCoreSwiftTests/ReminderScheduleTests.swift", 17, 9,
                    "Expectation failed: (schedule.days → [.saturday, .sunday]).contains(.saturday) == false")
    habits = (
        _cases("HabitCoreTests", "StreakCalculatorTests", "xctest", "testCountsConsecutiveDays testIgnoresFutureDates")
        + [Case("HabitCoreTests", "StreakCalculatorTests", "testStreakResetsAfterMissedDay", "xctest", 0.006, habit_fail_x)]
        + _cases("HabitCoreTests", "HabitStoreTests", "xctest", "testSavesAndLoads testMigratesVersion1")
        + [Case("HabitCoreTests", "HabitStoreTests", "testSyncsWithCloud", "xctest", 0.001, skipped=True)]
        + _cases("HabitCoreSwiftTests", "ReminderScheduleTests", "swift-testing", "weekdayReminders()", 0.001)
        + [Case("HabitCoreSwiftTests", "ReminderScheduleTests", "weekendRemindersSkipSaturday()", "swift-testing", 0.002,
                habit_fail_s)]
        + _cases("HabitCoreSwiftTests", "HabitNameTests", "swift-testing", "trimsWhitespace() rejectsEmptyNames()", 0.001)
    )
    return {
        "pass": Scenario("WeatherCore", tuple(weather), ("ForecastParser.swift", "UnitConverter.swift", "ForecastCache.swift"),
                         duration=3.1),
        "fail": Scenario("HabitCore", tuple(habits), ("StreakCalculator.swift", "HabitStore.swift", "ReminderSchedule.swift"),
                         duration=3.8),
        "none": Scenario("RecipeCore"),
        "host": Scenario("SketchCore", tuple(_cases("SketchCoreTests", "CanvasModelTests", "xctest", "testUndo")),
                         blockers=(("Sources/SketchCore/CanvasModel.swift", 2, "SwiftUI"),
                                   ("Sources/SketchCore/BrushPalette.swift", 1, "SwiftUI"))),
    }


BY_NAME = {"WeatherGlance": "pass", "HabitTracker": "fail", "RecipeBox": "none", "SketchPad": "host"}
SCENARIOS = _scenarios()


def _project(flags: dict) -> tuple[str, str, str]:
    """(name, path, scenario key) for --project; a generic all-pass project for names that aren't scripted."""
    raw = str(flags.get("project") or flags.get("path") or "").rstrip("/")
    if not raw or raw == "True":
        raise HelperError("not_project", "Choose a project first.", output.fix("Open Projects", "projects"))
    name = raw.split("/")[-1]
    forced = os.environ.get("IOSSTUDIO_DEMO_TESTS", "").strip().lower()
    return name, raw, forced if forced in SCENARIOS else BY_NAME.get(name, "pass")


def _scenario(key: str, name: str) -> Scenario:
    if key == "pass" and name != "WeatherGlance":   # any other project: the same tests under its own name
        base = SCENARIOS["pass"]
        core = f"{name}Core"
        tests = tuple(Case(c.target.replace("WeatherCore", core), c.suite, c.name, c.library, c.seconds) for c in base.tests)
        return Scenario(core, tests, base.sources, duration=base.duration)
    return SCENARIOS[key]


def _targets(sc: Scenario) -> list[dict]:
    names = dict.fromkeys(c.target for c in sc.tests)
    return [{"name": n, "path": f"Tests/{n}"} for n in names]


# ---------------------------------------------------------------- state (logs and the last run)

def _runs_dir() -> Path:
    return appdirs.state_dir() / "test-runs"


def _last_file() -> Path:
    return _runs_dir() / "demo-last.json"


def _last_runs() -> dict:
    data = fileio.load_json(_last_file(), {})
    return data if isinstance(data, dict) else {}


def _remember(path: str, entry: dict) -> None:
    data = _last_runs()
    data.pop(path, None)
    data[path] = entry
    try:
        _runs_dir().mkdir(parents=True, exist_ok=True)
        fileio.save_json(_last_file(), dict(list(data.items())[-LAST_MAX:]))
    except OSError:
        pass


def _write_log(package: str, lines: list[str]) -> str | None:
    d = _runs_dir()
    stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"
    f = d / f"{stamp}-{package}.log"
    try:
        d.mkdir(parents=True, exist_ok=True)
        fileio.atomic_write(f, "\n".join(lines) + "\n")
        fileio.prune(d.glob("*.log"), LOG_KEEP, LOG_MAX_BYTES, protect=[f])
    except OSError:
        return None
    return str(f)


# ---------------------------------------------------------------- info

def info(flags: dict) -> int:
    try:
        name, path, key = _project(flags)
    except HelperError as exc:
        return output.fail(exc.code, exc.message, exc.fix, exc.detail)
    sc = _scenario(key, name)
    targets = _targets(sc)
    return output.ok({
        "project": path, "package": name, "has_tests": bool(targets), "test_targets": targets,
        "tested_targets": [sc.core] if targets else [],
        "host_blockers": [{"target": sc.core, "file": f"{path}/{f}", "line": ln, "module": m} for f, ln, m in sc.blockers],
        "last_run": _last_runs().get(path),
    })


# ---------------------------------------------------------------- run

class _Run:
    """One demo run: pauses that a cancel interrupts, and the log it writes."""

    def __init__(self, runner: subproc.Runner, name: str):
        self.runner = runner
        self.name = name
        self.lines: list[str] = []
        self.step: tuple[str, str] | None = None

    def pause(self, seconds: float) -> None:
        end = time.monotonic() + subproc.demo_delay(seconds)
        while True:
            self.runner.poll()                     # Cancelled on {"cancel": true}
            left = end - time.monotonic()
            if left <= 0:
                return
            time.sleep(min(SLICE, left))

    def log(self, text: str) -> None:
        self.lines.append(text)
        output.log(text)

    def begin(self, sid: str, title: str) -> None:
        self.step = (sid, title)
        output.step(sid, title, "running")

    def end(self, state: str = "done") -> None:
        if self.step:
            output.step(*self.step, state)
            self.step = None


def _result(path: str, name: str, filt: str | None, done: list[tuple[Case, str]], duration: float,
            log_path: str | None, matched: bool = True) -> dict:
    counts = {"total": len(done), "passed": 0, "failed": 0, "skipped": 0}
    for _c, status in done:
        counts[status] += 1
    failures = [_failure(c, path) for c, status in done if status == "failed"][:FAILURES_MAX]
    return {"project": path, "package": name, "filter": filt, "counts": counts, "duration": duration,
            "failures": failures, "crashed": None, "no_tests_matched": not matched, "log_path": log_path}


def _failure(c: Case, path: str) -> dict:
    f, line, _col, msg = c.fails or ("", 0, 0, "")
    return {"id": c.id, "name": c.name, "suite": c.suite, "library": c.library, "file": f"{path}/{f}", "line": line,
            "message": msg}


def _matcher(filt: str | None):
    if not filt:
        return lambda _id: True
    try:
        rx = re.compile(filt)
    except re.error:
        rx = re.compile(re.escape(filt))
    return lambda test_id: rx.search(test_id) is not None


def _console(c: Case, status: str, path: str) -> list[str]:
    """What swift test prints for one finished test (XCTest console or swift-testing symbols)."""
    if c.library == "xctest":
        out = []
        if c.fails:
            f, line, _col, msg = c.fails
            out.append(f"{path}/{f}:{line}: error: {c.suite}.{c.name} : {msg}")
        word = {"passed": "passed", "failed": "failed", "skipped": "skipped"}[status]
        return out + [f"Test Case '{c.suite}.{c.name}' {word} ({c.seconds:.3f} seconds)"]
    if c.fails:
        f, line, col, msg = c.fails
        return [f"✘ Test {c.name} recorded an issue at {Path(f).name}:{line}:{col}: {msg}",
                f"✘ Test {c.name} failed after {c.seconds:.3f} seconds with 1 issue."]
    return [f"✔ Test {c.name} passed after {c.seconds:.3f} seconds."]


def run(flags: dict) -> int:
    try:
        name, path, key = _project(flags)
    except HelperError as exc:
        return output.done(False, error=exc)
    filt = flags.get("filter")
    filt = str(filt) if filt not in (None, True, "") else None
    sc = _scenario(key, name)
    with subproc.Runner(watch_stdin=True) as runner:
        r = _Run(runner, name)
        try:
            return _play(r, sc, path, name, filt)
        except subproc.Cancelled:
            r.end("failed")
            r.log("Test run cancelled.")
            log_path = _write_log(name, r.lines)
            return output.done(False, data=_result(path, name, filt, [], 0.0, log_path),
                               error=HelperError("cancelled", "Test run cancelled."))


def _play(r: _Run, sc: Scenario, path: str, name: str, filt: str | None) -> int:
    open_fix = output.fix("Open in editor", "projects", action="open")
    r.begin("prepare", "Checking the test targets")
    r.pause(0.2)
    if not sc.tests:
        r.end("failed")
        return output.done(False, error=HelperError(
            "no_tests", f"{name} has no unit tests yet. Add a test target in a Tests/ folder (for example "
            f"Tests/{name}Tests) that depends on a library target with your app's logic. New projects made with the "
            "\"App with unit tests\" template already have one.", open_fix))
    if sc.blockers:
        r.end("failed")
        mods = sorted({m for _f, _l, m in sc.blockers})
        where = "\n".join(f"{path}/{f}:{ln}: import {m}" for f, ln, m in sc.blockers)
        return output.done(False, error=HelperError(
            "host_unsupported", f"{sc.core} imports {' and '.join(mods)}, and UIKit or SwiftUI code can't run on the "
            f"computer. Move the logic you want to test into a library target that only imports Foundation, and "
            f"test that instead.", open_fix, where))
    r.end()

    r.begin("build", "Building the tests")
    build = ["Building for debugging...",
             *(f"Compiling {sc.core} {s}" for s in sc.sources),
             f"Compiling {sc.tests[0].target} {sc.tests[0].suite}.swift",
             f"Linking {name}PackageTests.xctest",
             "Build complete! (2.14s)"]
    for line in build:
        r.pause(0.15)
        r.log(line)
    r.end()

    r.begin("run", "Running the tests")
    match = _matcher(filt)
    todo = [c for c in sc.tests if match(c.id)]
    if not todo:
        r.pause(0.2)
        r.log("warning: No matching test cases were run")
        r.end()
        res = _result(path, name, filt, [], 0.4, _write_log(name, r.lines), matched=False)
        _remember(path, _last(res, True))
        return output.done(True, res)
    done: list[tuple[Case, str]] = []
    for c in todo:
        r.pause(TEST_PAUSE)
        status = "failed" if c.fails else "skipped" if c.skipped else "passed"
        for line in _console(c, status, path):
            r.log(line)
        ev = {"id": c.id, "name": c.name, "suite": c.suite, "library": c.library, "status": status,
              "duration": c.seconds}
        if c.fails:
            f, line, col, msg = c.fails
            ev.update(file=f"{path}/{f}", line=line, message=msg)
            output.event("diagnostic", severity="error", message=f"{c.name}: {msg}", category="test",
                         file=f"{path}/{f}", line=line, col=col)
        output.event("test", **ev)
        done.append((c, status))
    failed = sum(1 for _c, s in done if s == "failed")
    r.log(f"Executed {len(done)} tests, with {failed} failures in {sc.duration - 2.1:.3f} seconds")
    ok = failed == 0
    if not ok:
        r.log("Note: Some test targets reported failures:")
    r.end("done" if ok else "failed")
    duration = sc.duration if len(todo) == len(sc.tests) else round(2.3 + 0.1 * len(todo), 1)
    res = _result(path, name, filt, done, duration, _write_log(name, r.lines))
    _remember(path, _last(res, ok))
    if ok:
        return output.done(True, res)
    first = res["failures"][0]
    return output.done(False, data=res, error=HelperError(
        "tests_failed", f"{failed} of {len(done)} tests failed. First: {first['suite']}.{first['name']}."))


def _last(res: dict, ok: bool) -> dict:
    return {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "ok": ok, "counts": res["counts"], "duration": res["duration"],
            "filter": res["filter"]}


ACTIONS = {"info": info, "run": run}


def demo(action: str, flags: dict) -> int:
    fn = ACTIONS.get(action)
    if fn is None:
        output.fail("usage", f"Unknown command 'test {action}'.")
        return output.EXIT_USAGE
    return fn(flags)
