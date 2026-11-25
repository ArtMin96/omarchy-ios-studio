"""`test run`: a project's test targets built and run with `swift test` on this computer, as JSON lines.

Steps (contract in docs/features and helper/iosstudio/demo/testing.py, which answers the same in demo mode):
  prepare  cached package facts (features/testing/project.py); no test target -> "no_tests"; Apple-only imports in
           the tested code -> "host_unsupported" (before anything is built); the per-project test lock -> "busy";
           the first run seeds .build/host-tests with the packages xtool already downloaded into .build.
  build    `swift build --scratch-path .build/host-tests --product <Package>PackageTests --build-system native`,
           niced; compile errors become diagnostic events (features/diagnostics.py), "build_failed".
  run      `swift test --skip-build` with stdout and stderr read apart (XCTest's console, features/testing/xctest.py)
           and swift-testing's event stream (features/testing/events.py) polled while it runs; one "test" event per
           finished test and a "test" diagnostic per failure; done ok, or "tests_failed".
The separate scratch path keeps this off xtool's .build: a test run never waits for, or breaks, an iPhone build.
Cancel: {"cancel": true} on stdin or SIGTERM kills swift's whole process group ("cancelled").
"""
from __future__ import annotations

import os
import re
import select
import shutil
import subprocess
import time
from contextlib import ExitStack
from pathlib import Path

from iosstudio.core import fileio, output, subproc
from iosstudio.core.output import HelperError
from iosstudio.features import diagnostics

from . import project, store
from .events import EventStreamReader
from .results import TestResult, clip, failure_diagnostic
from .xctest import XCTestParser

SCRATCH = Path(".build") / "host-tests"
SEED_ITEMS = ("repositories", "checkouts", "workspace-state.json")
SEED_TIMEOUT = 120
BUILD_TIMEOUT = 20 * 60
RUN_TIMEOUT = 10 * 60
EXIT_GRACE = 30           # seconds swift may linger after closing its output
NICE = 10                 # builds and tests run at a lower priority so the desktop stays responsive
POLL_EVERY = 0.25         # seconds between event-stream reads (and the longest wait for a cancel)
PROGRESS_EVERY = 0.5
MAX_LOG_EVENTS = 2000     # per run; the log file has every line
MAX_DIAG_EVENTS = 200
MAX_TEST_EVENTS = 10_000  # counts stay exact past it
FAILURES_MAX = 50
FILTER_MAX = 500
MAX_LINE = 64 * 1024

DEPRECATED_NATIVE = re.compile(r"'--build-system native' has been deprecated")
NATIVE_REJECTED = re.compile(r"(?i)^(?:error|Error):.*(?:--build-system|build.system|'native')")
NO_MODULE = re.compile(r"no such module '(?P<module>[A-Za-z_]\w*)'")
NO_PRODUCT = re.compile(r"no product named '")
NO_TESTS_FOUND = "error: no tests found"
BUILD_PROGRESS = re.compile(r"^\[(\d+)/(\d+)\] (.+)$")


def _timeout(name: str, default: float) -> float:
    try:
        return max(1.0, float(os.environ.get(name, default)))
    except ValueError:
        return default


def _child() -> None:  # pragma: no cover - runs in the child
    """preexec_fn: a lower priority, and SIGTERM when the helper dies."""
    try:
        os.nice(NICE)
    except OSError:
        pass
    subproc.pdeathsig_term()


def parse_filter(flags: dict) -> str | None:
    """--filter as given, None when absent; HelperError "bad_filter" for a pattern that isn't a valid regex."""
    v = flags.get("filter")
    if v in (None, True) or not str(v).strip():
        return None
    filt = str(v).strip()
    if len(filt) > FILTER_MAX:
        raise HelperError("bad_filter", f"The filter is too long (at most {FILTER_MAX} characters).")
    try:
        re.compile(filt)
    except re.error as exc:
        raise HelperError("bad_filter", f"The filter isn't a valid pattern ({exc}). Use a suite or test name, "
                                        "like MathTests or MathTests/testAdd.") from None
    return filt


# ================================================================ one run

class Run:
    """The state of one run: steps, capped events, the log file and the tally that becomes RESULT."""

    def __init__(self, root: Path, filt: str | None, runner: subproc.Runner):
        self.root = root
        self.filter = filt
        self.runner = runner
        self.started = time.monotonic()
        self.package: str | None = None
        self.log: store.RunLog | None = None
        self.log_path: str | None = None
        self.step: tuple[str, str] | None = None
        self.log_events = 0
        self.diag_events = 0
        self.test_events = 0
        self.counts = {"total": 0, "passed": 0, "failed": 0, "skipped": 0}
        self.failures: list[dict] = []
        self.first_failure: TestResult | None = None
        self.crashed: str | None = None
        self.no_tests_matched = False
        self._progress_at = 0.0

    # ---- steps and events
    def begin(self, sid: str, title: str) -> None:
        self.step = (sid, title)
        output.step(sid, title, "running")

    def end(self, state: str = "done") -> None:
        if self.step:
            output.step(*self.step, state)
            self.step = None

    def open_log(self, package: str, argv_lines: list[str]) -> None:
        self.package = package
        self.log = store.RunLog(package)
        for ln in argv_lines:
            self.log.write(ln)

    def line(self, text: str, event: bool = True) -> None:
        if self.log:
            self.log.write(text)
        if not event:
            return
        self.log_events += 1
        if self.log_events < MAX_LOG_EVENTS:
            output.log(text)
        elif self.log_events == MAX_LOG_EVENTS:
            output.log("… more output is in the log file.")

    def diagnostic(self, d: dict) -> None:
        self.diag_events += 1
        if self.diag_events <= MAX_DIAG_EVENTS:
            output.event("diagnostic", **d)

    def progress(self, fraction: float, detail: str) -> None:
        now = time.monotonic()
        if now - self._progress_at >= PROGRESS_EVERY:
            self._progress_at = now
            output.progress(round(fraction, 3), detail[:200])

    def test(self, r: TestResult) -> None:
        self.counts["total"] += 1
        if r.status in self.counts:
            self.counts[r.status] += 1
        self.test_events += 1
        if self.test_events <= MAX_TEST_EVENTS:
            output.event("test", **r.to_event())
        if r.status == "failed":
            self.first_failure = self.first_failure or r
            if len(self.failures) < FAILURES_MAX:
                self.failures.append(r.to_failure())
            self.diagnostic(failure_diagnostic(r))

    # ---- the end
    def result(self) -> dict:
        return {"project": str(self.root), "package": self.package, "filter": self.filter, "counts": dict(self.counts),
                "duration": round(time.monotonic() - self.started, 1), "failures": self.failures,
                "crashed": self.crashed, "no_tests_matched": self.no_tests_matched, "log_path": self.close_log()}

    def close_log(self) -> str | None:
        if self.log is not None:
            self.log_path = self.log.close()
            self.log = None
        return self.log_path

    def finish(self, ok: bool, error: HelperError | None = None, remember: bool = True) -> int:
        res = self.result()
        if remember:
            store.remember(self.root, store.last_entry(res, ok))
        return output.done(True, res) if ok else output.done(False, data=res, error=error)


def run(flags: dict) -> int:
    try:
        root = project.project_root(flags)
        filt = parse_filter(flags)
    except HelperError as exc:
        return output.done(False, error=exc)
    with subproc.Runner(watch_stdin=True) as runner:
        r = Run(root, filt, runner)
        try:
            return _run(r)
        except subproc.Cancelled:
            runner.quiet()
            r.end("failed")
            r.line("Test run cancelled.")
            err = HelperError("cancelled", "Test run cancelled.")
            return r.finish(False, err, remember=False) if r.package else output.done(False, error=err)
        except HelperError as exc:
            r.end("failed")
            if r.log is not None:
                r.line(exc.message)
                return r.finish(False, exc)
            return output.done(False, error=exc)


def _run(r: Run) -> int:
    r.begin("prepare", "Checking the test targets")
    swift = project.find_swift()
    with ExitStack() as stack:
        try:
            stack.enter_context(fileio.lock(store.lock_file(r.root), blocking=False))
        except BlockingIOError:
            raise HelperError("busy", "This project's tests are already running. Wait for them to finish, or "
                                      "cancel that run.") from None
        return _locked(r, swift)


def _locked(r: Run, swift: str) -> int:
    facts = project.package_facts(r.root, swift)
    r.runner.poll()
    if not facts.test_targets:
        r.end("failed")
        return output.done(False, error=HelperError(
            "no_tests", f"{facts.name} has no unit tests yet. Add a test target in a Tests/ folder (for example "
            f"Tests/{facts.name}Tests) that depends on a library target with your app's logic. New projects made "
            "with the \"App with unit tests\" template already have one.", project.open_fix()))
    blockers = project.host_blockers(r.root, facts)
    if blockers:
        r.end("failed")
        for b in blockers:
            r.diagnostic(_blocker_diag(b))
        return output.done(False, error=_host_unsupported(blockers))
    scratch = r.root / SCRATCH
    seeded = seed(r.root, scratch)
    r.end()

    sig = project.swift_signature(swift)
    known = project.build_system(sig)
    native = known != "default"
    r.open_log(facts.name, [f"# {facts.name}: unit tests" + (f" matching {r.filter}" if r.filter else "")])
    if seeded:
        r.line(f"Reused the packages already downloaded for this project ({seeded}).", event=False)

    r.begin("build", "Building the tests")
    rc, b = _build(r, swift, facts, native)
    if rc != 0 and native and b.native_rejected and known is None:
        project.remember_build_system(sig, "default")
        native = False
        r.line("This Swift doesn't accept --build-system native: building with the default build system.")
        rc, b = _build(r, swift, facts, native)
    elif rc == 0 and native and known is None:
        project.remember_build_system(sig, "native")
    if rc != 0:
        r.end("failed")
        return _build_failed(r, facts, b)
    r.end()

    r.begin("run", "Running the tests")
    return _test(r, swift, native)


# ================================================================ prepare

def _blocker_diag(b: dict) -> dict:
    return {"severity": "error", "category": "compiler", "file": b["file"], "line": b["line"], "col": 0,
            "message": f"import {b['module']}: {b['module']} only exists on Apple platforms, so this code can't be "
                       "built for tests on the computer",
            "hint": "Move the logic you want to test into a library target that only imports Foundation."}


def _host_unsupported(blockers: list[dict]) -> HelperError:
    targets = list(dict.fromkeys(b["target"] for b in blockers))
    mods = list(dict.fromkeys(b["module"] for b in blockers))
    names = " and ".join(mods[:3]) + (" …" if len(mods) > 3 else "")
    who = " and ".join(targets[:2]) + (" …" if len(targets) > 2 else "")
    where = "\n".join(f"{b['file']}:{b['line']}: import {b['module']}" for b in blockers)
    return HelperError(
        "host_unsupported", f"{who} {'imports' if len(targets) == 1 else 'import'} {names}, and UIKit or SwiftUI code "
        "can't run on the computer. Move the logic you want to test into a library target that only imports "
        "Foundation, and test that instead.", project.open_fix(), where)


def seed(root: Path, scratch: Path) -> str | None:
    """First run: copy the packages xtool's build already downloaded (.build/repositories, checkouts,
    workspace-state.json) into the test scratch folder, so SwiftPM doesn't fetch them again. Reflinked where the
    file system can (btrfs, xfs), else copied. Any failure just leaves SwiftPM to fetch. Returns what was copied."""
    src = root / ".build"
    if (scratch / "workspace-state.json").exists() or not (src / "workspace-state.json").is_file():
        return None
    items = [src / n for n in SEED_ITEMS if (src / n).exists()]
    cp = shutil.which("cp")
    if not cp:
        return None
    try:
        scratch.mkdir(parents=True, exist_ok=True)
        res = subproc.run_group([cp, "-a", "--reflink=auto", *map(str, items), str(scratch)], timeout=SEED_TIMEOUT)
        ok = res.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        ok = False
    if not ok:
        for n in SEED_ITEMS:
            t = scratch / n
            if t.is_dir() and not t.is_symlink():
                shutil.rmtree(t, ignore_errors=True)
            else:
                t.unlink(missing_ok=True)
        return None
    return ", ".join(p.name for p in items)


# ================================================================ build

class BuildOutput:
    """What the build's lines said, beyond the diagnostics: the facts that change the error."""

    def __init__(self) -> None:
        self.native_rejected = False
        self.no_product = False
        self.missing_modules: list[tuple[str, str | None, int | None]] = []   # (module, file, line)
        self.first_error: dict | None = None


def _build(r: Run, swift: str, facts: project.PackageFacts, native: bool) -> tuple[int, BuildOutput]:
    argv = [swift, "build", "--scratch-path", str(r.root / SCRATCH), "--product", facts.product,
            *(["--build-system", "native"] if native else []), "--no-color-diagnostics"]
    b = BuildOutput()
    parser = diagnostics.Parser()
    r.line("$ " + " ".join(["swift", *argv[1:]]), event=False)

    def emit(diags: list[dict]) -> None:
        for d in diags:
            if d.get("severity") == "error" and b.first_error is None:
                b.first_error = d
            m = NO_MODULE.search(d.get("message") or "")
            if m and m["module"] in project.APPLE_ONLY:
                b.missing_modules.append((m["module"], d.get("file"), d.get("line")))
            r.diagnostic(d)

    def on_line(text: str) -> None:
        if DEPRECATED_NATIVE.search(text):
            r.line(text, event=False)
            return
        r.line(text)
        s = text.strip()
        if s.startswith(("error:", "Error:")):
            if NATIVE_REJECTED.match(s):
                b.native_rejected = True
            if NO_PRODUCT.search(s):
                b.no_product = True
        if m := BUILD_PROGRESS.match(s):
            r.progress(int(m[1]) / max(1, int(m[2])), m[3])
        emit(parser.feed(text))

    r.runner.on_line = on_line
    timeout = _timeout("IOSSTUDIO_TEST_BUILD_TIMEOUT", BUILD_TIMEOUT)
    try:
        rc = r.runner.run(argv, cwd=r.root, env=subproc.tool_env("swift"), preexec_fn=_child, reaper=True,
                          timeout=timeout, exit_grace=EXIT_GRACE, kill_grace=2.0)
    except FileNotFoundError:
        raise project.toolchain_error() from None
    except HelperError as exc:
        if exc.code == "timeout":
            raise HelperError("timeout", f"Building the tests took longer than {_minutes(timeout)} and was "
                                         "stopped.") from None
        raise
    finally:
        r.runner.on_line = None
    emit(parser.finish(failed=rc != 0))
    return rc, b


def _build_failed(r: Run, facts: project.PackageFacts, b: BuildOutput) -> int:
    if b.missing_modules:
        mods = list(dict.fromkeys(m for m, _f, _l in b.missing_modules))
        where = "\n".join(f"{f}:{ln}: no such module '{m}'" for m, f, ln in b.missing_modules if f)
        return r.finish(False, HelperError(
            "host_unsupported", f"The tests need {' and '.join(mods[:3])}, and UIKit or SwiftUI code can't run on the "
            "computer. Move the logic you want to test into a library target that only imports Foundation, and test "
            "that instead.", project.open_fix(), where or None))
    if b.no_product:
        return r.finish(False, HelperError(
            "no_tests", f"Swift found no tests to build in {facts.name}. Add a test target in a Tests/ folder that "
            "depends on a library target with your app's logic.", project.open_fix()))
    d = b.first_error
    if d:
        where = f"{Path(d['file']).name}:{d.get('line')}: " if d.get("file") else ""
        msg = f"The tests didn't build: {where}{clip(d.get('message') or '', 200)}"
    else:
        tail = next((ln.strip() for ln in reversed(r.runner.tail) if ln.strip()), "")
        msg = "The tests didn't build." + (f" {clip(tail, 200)}" if tail else "")
    return r.finish(False, HelperError("build_failed", msg, output.fix("Open in editor", "projects", action="open")))


def _minutes(seconds: float) -> str:
    return f"{int(seconds // 60)} minutes" if seconds >= 120 else f"{int(seconds)} s"


# ================================================================ run

def _test(r: Run, swift: str, native: bool) -> int:
    scratch = r.root / SCRATCH
    events = scratch / "events.jsonl"
    try:
        scratch.mkdir(parents=True, exist_ok=True)
        events.unlink(missing_ok=True)
    except OSError:
        pass
    r.runner.tail.clear()
    argv = [swift, "test", "--scratch-path", str(scratch), "--skip-build",
            *(["--build-system", "native"] if native else []), *(["--filter", r.filter] if r.filter else []),
            "--experimental-event-stream-output", str(events), "--experimental-event-stream-version", "0"]
    r.line("$ " + " ".join(["swift", *argv[1:]]), event=False)
    xp = XCTestParser(root=r.root)
    reader = EventStreamReader(events, root=r.root)
    no_tests_found = False

    def on_line(text: str) -> None:
        nonlocal no_tests_found
        if DEPRECATED_NATIVE.search(text):
            r.line(text, event=False)
            return
        r.line(text)
        if text.startswith(NO_TESTS_FOUND):
            no_tests_found = True
        for t in xp.feed(text):
            r.test(t)

    timeout = _timeout("IOSSTUDIO_TEST_RUN_TIMEOUT", RUN_TIMEOUT)
    try:
        rc = _stream_two(r, argv, on_line, lambda: [r.test(t) for t in reader.poll()], timeout)
    except HelperError as exc:
        if exc.code != "timeout":
            raise
        for t in [*xp.finish(None), *reader.poll()]:
            r.test(t)
        raise HelperError("timeout", f"The tests took longer than {_minutes(timeout)} and were stopped (a test that "
                                     "never finishes?). The log shows the last test that started.") from None
    for t in [*xp.finish(rc), *reader.finish(fatals=xp.fatals)]:
        r.test(t)
    try:
        events.unlink(missing_ok=True)
    except OSError:
        pass
    r.crashed = xp.crashed or reader.crashed
    c = r.counts
    if c["failed"]:
        r.end("failed")
        first = r.first_failure
        msg = f"{c['failed']} of {c['total']} tests failed."
        if r.crashed:
            msg = f"{r.crashed} crashed the test process, so the tests after it didn't run. " + msg
        elif first:
            msg += f" First: {first.suite}.{first.name}." if first.suite else f" First: {first.name}."
        return r.finish(False, HelperError("tests_failed", msg))
    if rc == 0:
        r.no_tests_matched = c["total"] == 0
        r.end()
        return r.finish(True)
    r.end("failed")
    if no_tests_found:
        return r.finish(False, HelperError("no_tests", "Swift found no tests in this project's test targets.",
                                           project.open_fix()))
    tail = next((ln.strip() for ln in reversed(r.runner.tail) if ln.strip().startswith("error:")), "")
    return r.finish(False, HelperError("tests_failed", "The test run failed before any test reported a result."
                                       + (f" {clip(tail, 200)}" if tail else "")))


def _stream_two(r: Run, argv: list[str], on_line, on_poll, timeout: float) -> int:
    """Run argv with stdout and stderr read as separate pipes (lines from the two never mix mid-line), calling
    on_poll every POLL_EVERY s, checking our stdin for a cancel; the child's whole group dies on any exception."""
    try:
        proc = subprocess.Popen(argv, cwd=str(r.root), env=subproc.tool_env("swift", {"SWIFT_BACKTRACE": "enable=no"}),
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
                                start_new_session=True, preexec_fn=_child)
    except FileNotFoundError:
        raise project.toolchain_error() from None
    pgid = proc.pid
    watchdog = subproc.start_reaper(pgid)
    assert proc.stdout is not None and proc.stderr is not None
    bufs = {proc.stdout.fileno(): b"", proc.stderr.fileno(): b""}
    tail = r.runner.tail
    deadline = time.monotonic() + timeout
    last_poll = 0.0

    def emit(raw: bytes) -> None:
        text = raw.decode("utf-8", "replace").rstrip("\r")
        tail.append(text)
        on_line(text)

    try:
        while bufs:
            r.runner.poll()
            try:
                ready, _, _ = select.select(list(bufs), [], [], POLL_EVERY)
            except InterruptedError:
                ready = []
            for fd in ready:
                chunk = os.read(fd, 65536)
                if not chunk:
                    if bufs[fd]:
                        emit(bufs[fd])
                    del bufs[fd]
                    continue
                *lines, rest = (bufs[fd] + chunk).split(b"\n")
                while len(rest) > MAX_LINE:
                    lines.append(rest[:MAX_LINE])
                    rest = rest[MAX_LINE:]
                bufs[fd] = rest
                for ln in lines:
                    emit(ln)
            now = time.monotonic()
            if now - last_poll >= POLL_EVERY:
                last_poll = now
                on_poll()
            if now >= deadline:
                raise HelperError("timeout", "swift test took too long.")
        try:
            return proc.wait(timeout=max(1.0, min(EXIT_GRACE, deadline - time.monotonic())))
        except subprocess.TimeoutExpired:
            raise HelperError("timeout", "swift test closed its output but didn't exit.") from None
    except BaseException:
        subproc.kill_group(pgid, 1.0)
        subproc.kill_proc(proc)
        raise
    finally:
        subproc.stop_reaper(watchdog)
        subproc.kill_group(pgid, 0)
        for f in (proc.stdout, proc.stderr):
            try:
                f.close()
            except OSError:
                pass
