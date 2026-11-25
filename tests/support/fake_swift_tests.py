#!/usr/bin/env python3
"""A fake `swift` for the helper's `test` group (point IOSSTUDIO_SWIFT at this file).

It answers from the real Swift 6.4.2 output in tests/fixtures/swift-test/ (paths /home/user/Projects/CrashLab are
rewritten to the current folder, the project):
  swift package [--scratch-path P] dump-package   a dump of the folder: Sources/<T> regular targets, Tests/<T> test
                                                  targets depending on FAKE_SWIFT_TESTED (comma list, default
                                                  AppCore); the name: from Package.swift
  swift build ...                                 build-ok.txt (or the scenario's failure)
  swift test ... --experimental-event-stream-output F
                                                  XCTest's console on stdout, swift's own lines on stderr, and the
                                                  swift-testing event stream written line by line to F
Environment:
  FAKE_SWIFT_SCENARIO  pass (default) | fail | crash | compile | nomodule | noproduct | notests | nonative |
                       nofound | slow (build sleeps, for cancel) | hang (a test that never ends)
  FAKE_SWIFT_ARGV      append each call's argv (JSON list) to this file
  FAKE_SWIFT_PIDS      append each call's pid to this file (tests check nothing outlives a cancel)
A --filter of NOTHING matches no test; any other --filter runs one XCTest test (filter-one-xctest.txt).
"""
import json
import os
import re
import sys
import time
from pathlib import Path

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "swift-test"
ORIG = "/home/user/Projects/CrashLab"
CWD = os.getcwd()
SCENARIO = os.environ.get("FAKE_SWIFT_SCENARIO", "pass")
ARGS = sys.argv[1:]


def record():
    for var, value in (("FAKE_SWIFT_ARGV", json.dumps(ARGS)), ("FAKE_SWIFT_PIDS", str(os.getpid()))):
        if os.environ.get(var):
            with open(os.environ[var], "a") as fh:
                fh.write(value + "\n")


def fixture(name):
    return (FIX / name).read_text().replace(ORIG, CWD).replace(ORIG.replace("/", "\\/"), CWD.replace("/", "\\/"))


def out(text, err=False):
    stream = sys.stderr if err else sys.stdout
    stream.write(text if text.endswith("\n") else text + "\n")
    stream.flush()


def dump_package():
    text = Path("Package.swift").read_text()
    m = re.search(r'name:\s*"([^"]+)"', text)
    tested = [t for t in os.environ.get("FAKE_SWIFT_TESTED", "AppCore").split(",") if t]
    targets = []
    for d in sorted(p.name for p in Path("Sources").iterdir() if p.is_dir()) if Path("Sources").is_dir() else []:
        targets.append({"name": d, "type": "regular", "dependencies": []})
    if SCENARIO != "notests" and Path("Tests").is_dir():
        for d in sorted(p.name for p in Path("Tests").iterdir() if p.is_dir()):
            targets.append({"name": d, "type": "test",
                            "dependencies": [{"byName": [t, None]} for t in tested]})
    print(json.dumps({"name": m.group(1) if m else Path(CWD).name, "targets": targets,
                      "packageKind": {"root": [CWD]}}, indent=2))
    return 0


def build():
    if SCENARIO == "nonative" and "--build-system" in ARGS:
        out("error: Invalid value 'native' for '--build-system <build-system>'", err=True)
        return 64
    if SCENARIO == "slow":
        out("Building for debugging...")
        time.sleep(60)
        return 0
    if SCENARIO == "compile":
        out(fixture("build-compile-error.txt"))
        return 1
    if SCENARIO == "nomodule":
        out("Building for debugging...")
        out(f"{CWD}/Sources/AppCore/Shapes.swift:1:8: error: no such module 'SwiftUI'")
        out(" 1 | import SwiftUI")
        out("   |        `- error: no such module 'SwiftUI'")
        return 1
    if SCENARIO == "noproduct":
        out("error: no product named 'CrashLabPackageTests'", err=True)
        return 1
    out(fixture("build-ok.txt"))
    return 0


def split_console(name):
    """A merged console fixture -> (stdout lines, stderr lines): swift's own lines go to stderr."""
    so, se = [], []
    for ln in fixture(name).splitlines():
        (se if ln.startswith(("[0/1]", "warning: '--build-system", "error: Process")) else so).append(ln)
    return so, se


def run_tests():
    events = ARGS[ARGS.index("--experimental-event-stream-output") + 1]
    filt = ARGS[ARGS.index("--filter") + 1] if "--filter" in ARGS else None
    ev_name, rc = None, 0
    if SCENARIO == "nofound":
        out("error: no tests found; create a target in the 'Tests' directory", err=True)
        return 1
    if SCENARIO == "hang":
        out("Test Suite 'All tests' started at 2026-11-25 22:04:19.476")
        out("Test Case 'MathTests.testHang' started at 2026-11-25 22:04:19.480")
        time.sleep(120)
        return 0
    if filt == "NOTHING":
        so, se = split_console("filter-none.txt")
    elif filt:
        so, se = split_console("filter-one-xctest.txt")
    elif SCENARIO == "fail":
        (so, se), ev_name, rc = split_console("xctest-failing.txt"), "events-failing.jsonl", 1
    elif SCENARIO == "crash":
        so, se = fixture("crash-stdout.txt").splitlines(), fixture("crash-stderr.txt").splitlines()
        ev_name, rc = "events-crash-truncated.jsonl", 1
    else:
        (so, se), ev_name = split_console("xctest-passing.txt"), "events-passing.jsonl"
    for ln in se:
        out(ln, err=True)
    for ln in so:
        out(ln)
    with open(events, "w") as fh:
        for ln in (fixture(ev_name).splitlines() if ev_name else []):
            fh.write(ln + "\n")
            fh.flush()
    return rc


def main():
    record()
    if ARGS[:1] == ["--version"]:
        print("Swift version 6.4.2 (fake)")
        return 0
    if ARGS[:1] == ["package"] and "dump-package" in ARGS:
        return dump_package()
    if ARGS[:1] == ["build"]:
        return build()
    if ARGS[:1] == ["test"]:
        return run_tests()
    out(f"fake swift: unsupported {ARGS}", err=True)
    return 2


if __name__ == "__main__":
    sys.exit(main())
