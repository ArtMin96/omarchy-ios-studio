import QtQuick
import QtTest
import "../../lib/Model.js" as Model

// lib/Model.js hotReloadReconcile (HotReloadService's local switches vs `hotreload status`), the Stop before a
// session's first snapshot, and hotReloadFileNames (the stub note and the Clean up toast).
TestCase {
  name: "HotReloadReconcile"

  readonly property string p: "/p/CrashLab"

  function local(enabled, mode, after) {
    var l = {}
    l[p] = { enabled: enabled, mode: mode, after: after }
    return l
  }
  function status(enabled, mode) {
    var s = {}
    s[p] = { enabled: enabled, mode: mode }
    return s
  }

  function test_aReplyFromAFetchStartedLaterWinsWhateverItSays() {
    // Turned on here after fetch 3 had started; an agent turned it off since: fetch 4 says so.
    var l = local(true, "on", 3)
    var out = Model.hotReloadReconcile(l, status(false, "stub"), 4)
    verify(out !== l)
    compare(Object.keys(out).length, 0)
    verify(l[p] !== undefined)          // the old map is unchanged
  }

  function test_anOlderReplyThatDisagreesKeepsTheLocalSwitch() {
    // Fetch 3 started before the change was done: it may have read the project before it.
    var l = local(true, "on", 3)
    compare(Model.hotReloadReconcile(l, status(false, "off"), 3), l)
    compare(Model.hotReloadReconcile(l, status(false, "off"), 2), l)
    compare(Model.hotReloadReconcile(l, {}, 3), l)
  }

  function test_anOlderReplyThatAgreesClearsIt() {
    var l = local(false, "stub", 5)
    compare(Object.keys(Model.hotReloadReconcile(l, status(false, "stub"), 5)).length, 0)
    // the mode counts too
    compare(Model.hotReloadReconcile(l, status(false, "off"), 5), l)
  }

  function test_aLaterReplyClearsItEvenWhenTheProjectIsMissing() {
    compare(Object.keys(Model.hotReloadReconcile(local(true, "on", 0), {}, 1)).length, 0)
  }

  function test_onlyTheAnsweredSwitchesGo() {
    var l = { "/p/A": { enabled: true, mode: "on", after: 1 }, "/p/B": { enabled: true, mode: "on", after: 7 } }
    var out = Model.hotReloadReconcile(l, {}, 4)
    compare(Object.keys(out), ["/p/B"])
    var empty = {}
    compare(Model.hotReloadReconcile(empty, null, 9), empty)
  }

  function test_stopWhileItStartsShowsStopping() {
    var e = Model.hotReloadEntry(p, "CrashLab")
    e.action = "starting"
    var on = { enabled: true, mode: "on" }
    var l = Model.hotReloadLabel(e, on, 0)
    compare(l.state, "starting"); verify(!l.live)
    e.action = "stopping"
    l = Model.hotReloadLabel(e, on, 0)
    compare(l.state, "stopping"); compare(l.pill, "Stopping"); verify(l.busy); verify(!l.live)
    // the stream ends without a snapshot: stopped
    var done = Model.hotReloadEvent(e, { event: "done", ok: true, data: { stopReason: "stopped", message: "Stopped" } })
    compare(done.action, "")
    compare(Model.hotReloadLabel(done, on, 0).state, "stopped")
  }

  function test_blockersDoNotDependOnTheClock() {
    var e = Model.hotReloadEntry(p, "CrashLab")
    e = Model.hotReloadEvent(e, { event: "hotreload", seq: 1, pid: 1, state: "failed", running: true,
                                  notice: { level: "info", message: "x", time: 100 },
                                  lastError: { code: "needs-rebuild", message: "Needs a full build.", seq: 3,
                                               blockers: [{ file: "Sources/A.swift", line: 4, message: "stored property" }] } })
    var a = Model.hotReloadLabel(e, { enabled: true }, 100000), b = Model.hotReloadLabel(e, { enabled: true }, 0)
    compare(a.blockers, b.blockers)
    compare(a.blockers[0].where, "A.swift:4")
  }

  function test_cleanUpLeavesTheBarHidden() {
    // Clean up (mode stub, action "cleaning"): the stub note shows the busy button, the bar stays away.
    var e = Model.hotReloadEntry(p, "CrashLab")
    e.action = "cleaning"
    compare(Model.hotReloadLabel(e, { enabled: false, mode: "stub" }, 0), null)
  }

  function test_fileNames() {
    compare(Model.hotReloadFileNames([]), "")
    compare(Model.hotReloadFileNames(null), "")
    compare(Model.hotReloadFileNames(["Sources/App/ContentView.swift"]), "ContentView.swift")
    compare(Model.hotReloadFileNames(["a/A.swift", "B.swift", "c/C.swift", "D.swift", "E.swift"]),
            "A.swift, B.swift, C.swift and 2 more")
  }
}
