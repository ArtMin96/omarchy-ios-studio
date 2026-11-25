import QtQuick
import QtTest
import "../../lib/Model.js" as Model

// lib/Model.js test* helpers behind the unit tests sheet (components/projects/ProjectTestsSheet.qml) and
// services/TestService.qml: the summary line, durations, "Run failed only"'s filter, the rows and the sheet state.
// Run by tests/qml/test_qml_core.py.
TestCase {
  name: "UnitTests"

  function test_duration() {
    compare(Model.testDuration(0.005), "5 ms")
    compare(Model.testDuration(0.0001), "1 ms")
    compare(Model.testDuration(0.4), "400 ms")
    compare(Model.testDuration(4.2), "4.2 s")
    compare(Model.testDuration(9.96), "10 s")
    compare(Model.testDuration(75), "1 min")
    compare(Model.testDuration(null), "")
    compare(Model.testDuration(undefined), "")
    compare(Model.testDuration(-1), "")
  }

  function test_summary() {
    compare(Model.testSummary({ total: 15, passed: 12, failed: 2, skipped: 1 }, 4.2), "12 passed · 2 failed · 1 skipped · 4.2 s")
    compare(Model.testSummary({ passed: 12, failed: 0, skipped: 0 }, 3.1), "12 passed · 3.1 s")
    compare(Model.testSummary({ passed: 0, failed: 3 }), "3 failed")
    compare(Model.testSummary({ total: 0, passed: 0, failed: 0, skipped: 0 }, 0.4), "No tests ran · 400 ms")
    compare(Model.testSummary(null), "No tests ran")
  }

  function test_progress() {
    compare(Model.testProgressText({ finished: 0 }), "")
    compare(Model.testProgressText({ finished: 8, passed: 7, failed: 1 }), "8 finished · 1 failed")
    compare(Model.testProgressText({ passed: 3, skipped: 1 }), "4 finished")
    compare(Model.testProgressText(null), "")
  }

  function test_failedFilterEscapesAndAnchors() {
    var f = Model.testFailedFilter([
      { id: "HabitCoreTests.StreakCalculatorTests/testStreakResetsAfterMissedDay" },
      { id: "HabitCoreSwiftTests.ReminderScheduleTests/weekendRemindersSkipSaturday()" },
      { id: "HabitCoreTests.StreakCalculatorTests/testStreakResetsAfterMissedDay" },   // repeated: once
      { name: "no id" }
    ])
    compare(f, "^(?:HabitCoreTests\\.StreakCalculatorTests/testStreakResetsAfterMissedDay"
               + "|HabitCoreSwiftTests\\.ReminderScheduleTests/weekendRemindersSkipSaturday\\(\\))(?:/|$)")
    var rx = new RegExp(f)
    verify(rx.test("HabitCoreTests.StreakCalculatorTests/testStreakResetsAfterMissedDay"))
    verify(rx.test("HabitCoreSwiftTests.ReminderScheduleTests/weekendRemindersSkipSaturday()"))
    verify(!rx.test("HabitCoreTests.StreakCalculatorTests/testStreakResetsAfterMissedDayTwice"))
    verify(!rx.test("HabitCoreTests.StreakCalculatorTests/testCountsConsecutiveDays"))
    verify(!rx.test("XHabitCoreTests.StreakCalculatorTests/testStreakResetsAfterMissedDay"))
    compare(Model.testFailedFilter([]), "")
    compare(Model.testFailedFilter(null), "")
    compare(Model.regexEscape("a.b*c+d?e^f$g{h}i(j)k|l[m]n\\o"), "a\\.b\\*c\\+d\\?e\\^f\\$g\\{h\\}i\\(j\\)k\\|l\\[m\\]n\\\\o")
  }

  function test_libraryLabel() {
    compare(Model.testLibraryLabel("xctest"), "XCTest")
    compare(Model.testLibraryLabel("swift-testing"), "Swift Testing")
    compare(Model.testLibraryLabel(""), "")
  }

  function test_rowsFailuresFirstPassedCollapsed() {
    var r = { failed: [{ id: "f1" }], skipped: [{ id: "s1" }], passed: [{ id: "p1" }, { id: "p2" }], passedTotal: 5 }
    var rows = Model.testRows(r, false)
    compare(rows.map(function(x) { return x.kind }), ["failed", "skipped", "passedGroup"])
    compare(rows[2].count, 5)
    compare(rows[2].shown, 2)
    var open = Model.testRows(r, true)
    compare(open.map(function(x) { return x.kind + ":" + (x.test ? x.test.id : "") }),
            ["failed:f1", "skipped:s1", "passedGroup:", "passed:p1", "passed:p2"])
    // nothing passed: no group row
    compare(Model.testRows({ failed: [{ id: "f" }], skipped: [], passed: [] }, true).length, 1)
    compare(Model.testRows(null, true).length, 0)
  }

  function test_sheetState() {
    compare(Model.testSheetState({ state: "running" }, "ready", {}), "running")
    compare(Model.testSheetState({ state: "done", ok: true, result: { no_tests_matched: false } }, "ready", {}), "passed")
    compare(Model.testSheetState({ state: "done", ok: true, result: { no_tests_matched: true } }, "ready", {}), "no_match")
    compare(Model.testSheetState({ state: "done", ok: false, error: { code: "tests_failed" } }, "ready", {}), "failed")
    var codes = ["build_failed", "no_tests", "host_unsupported", "cancelled", "toolchain"]
    for (var i = 0; i < codes.length; i++)
      compare(Model.testSheetState({ state: "done", ok: false, error: { code: codes[i] } }, "ready", {}), codes[i])
    compare(Model.testSheetState({ state: "done", ok: false, error: { code: "timeout" } }, "ready", {}), "error")
    compare(Model.testSheetState({ state: "done", ok: false, error: null }, "ready", {}), "error")
    // before a run: from `test info`
    compare(Model.testSheetState(null, "loading", null), "loading")
    compare(Model.testSheetState(null, "idle", null), "loading")
    compare(Model.testSheetState(null, "error", null), "info_error")
    compare(Model.testSheetState(null, "ready", { has_tests: false, host_blockers: [] }), "no_tests")
    compare(Model.testSheetState(null, "ready", { has_tests: true, host_blockers: [{ file: "/x.swift", line: 1 }] }), "host_unsupported")
    compare(Model.testSheetState(null, "ready", { has_tests: true, host_blockers: [] }), "idle")
  }
}
