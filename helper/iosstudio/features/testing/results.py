"""One test's outcome, shared by the XCTest and swift-testing parsers (stdlib only).

A TestResult becomes a `test` event (to_event), a RESULT.failures entry (to_failure) and, for a failure, a
diagnostic in the same shape as features/diagnostics.py (failure_diagnostic).
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

MAX_MESSAGE = 300
_REGEX_SPECIAL = re.compile(r"([\\.^$|?*+()\[\]{}])")


def clip(text: str, limit: int = MAX_MESSAGE) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


@dataclass(slots=True)
class TestResult:
    id: str                       # "MathTests/testAdd" (XCTest), "AppCoreSwiftTests.GreetingTests/add()" (swift-testing)
    name: str
    suite: str
    library: str                  # "xctest" | "swift-testing"
    status: str                   # "passed" | "failed" | "skipped"
    duration: float | None = None
    file: str | None = None
    line: int | None = None
    message: str | None = None

    def to_event(self) -> dict:
        """The fields of a `test` event: output.event("test", **r.to_event())."""
        d: dict = {"id": self.id, "name": self.name, "suite": self.suite, "library": self.library,
                   "status": self.status}
        for key in ("duration", "file", "line", "message"):
            value = getattr(self, key)
            if value is not None:
                d[key] = value
        return d

    def to_failure(self) -> dict:
        return {"id": self.id, "name": self.name, "suite": self.suite, "library": self.library,
                "file": self.file, "line": self.line, "message": self.message}


def failure_diagnostic(r: TestResult) -> dict:
    """The diagnostics.py shape ({severity, message, category, file?, line?, col?}) for a failed test."""
    d: dict = {"severity": "error", "message": clip(f"{r.suite}.{r.name}: {r.message or 'failed'}"),
               "category": "test"}
    if r.file:
        d["file"], d["line"], d["col"] = r.file, int(r.line or 0), 0
    return d


def summarize(results: Iterable[TestResult]) -> dict:
    counts = {"total": 0, "passed": 0, "failed": 0, "skipped": 0}
    for r in results:
        counts["total"] += 1
        if r.status in counts:
            counts[r.status] += 1
    return counts


def filter_pattern(r: TestResult) -> str:
    """A `swift test --filter` regex that selects exactly this test.

    XCTest matches "<Module>.<Class>/<method>", so an XCTest id is anchored at the end. swift-testing
    matches its own ids with the source location behind them, so the escaped id is left open at the end
    (a function name followed by "(" can't be the start of another test's name).
    """
    escaped = _REGEX_SPECIAL.sub(r"\\\1", r.id)
    return escaped + "$" if r.library == "xctest" else escaped
