import QtQuick
import QtTest
import "../../lib/Model.js" as Model

// lib/Model.js hotReload*: the state machine behind services/HotReloadService.qml (the `hotreload run` stream's
// snapshots and `hotreload status` → the entry a project card shows) and HotReloadBar's text.
TestCase {
  name: "HotReload"

  readonly property var on: ({ enabled: true, mode: "on" })
  readonly property var off: ({ enabled: false, mode: "off" })

  function snap(seq, state, extra) {
    return Object.assign({ event: "hotreload", seq: seq, state: state, message: "", project: "CrashLab",
                           path: "/p/CrashLab", pid: 4242, reloads: 0, paused: false, pending: [], pendingCount: 0,
                           progress: null, lastReload: null, lastError: null, notice: null, stopReason: null }, extra || {})
  }
  function feed(e, events) {
    for (var i = 0; i < events.length; i++) e = Model.hotReloadEvent(e, events[i])
    return e
  }
  function started() {
    var e = Model.hotReloadEntry("/p/CrashLab", "CrashLab")
    e.action = "starting"
    return e
  }

  function test_offShowsNothingOnShowsNotRunning() {
    compare(Model.hotReloadLabel(null, off, 0), null)
    compare(Model.hotReloadLabel(null, null, 0), null)
    var l = Model.hotReloadLabel(null, on, 0)
    compare(l.state, "idle"); verify(!l.live); compare(l.text, "Hot reload is on, but not running")
    compare(Model.hotReloadLabel(started(), on, 0).state, "starting")
  }

  function test_buildingWithProgress() {
    var e = feed(started(), [snap(1, "starting", { message: "Starting hot reload" }),
                             snap(2, "building", { message: "Building the app", progress: { fraction: 0.35, detail: "Compiling CrashLab" } })])
    compare(e.action, ""); compare(e.session.state, "building"); verify(e.session.event === undefined)
    var l = Model.hotReloadLabel(e, on, 0)
    compare(l.level, "busy"); verify(l.busy); verify(l.live)
    compare(l.pill, "Building 35%"); compare(l.text, "Building the app"); compare(l.detail, "Compiling CrashLab")
    compare(l.progress, 0.35); verify(!l.indeterminate)
    verify(!l.canPause); verify(l.pauseTip !== "")
    var z = Model.hotReloadLabel(feed(e, [snap(3, "building", { progress: { fraction: 0, detail: "" } })]), on, 0)
    compare(z.pill, "Building"); verify(z.indeterminate)
    compare(Model.hotReloadLabel(feed(e, [snap(4, "installing", { message: "Installing on your iPhone" })]), on, 0).pill, "Installing")
  }

  function test_olderSnapshotsAreDropped() {
    var e = feed(started(), [snap(5, "watching")])
    compare(Model.hotReloadEvent(e, snap(4, "building")), e)
    compare(Model.hotReloadEvent(e, snap(5, "building")), e)
    // a new session (another pid) starts its own count
    compare(Model.hotReloadEvent(e, snap(1, "starting", { pid: 9 })).session.state, "starting")
    compare(Model.hotReloadEvent(e, { event: "log", text: "x" }), e)
    compare(Model.hotReloadEvent(null, snap(1, "watching")), null)
  }

  function test_watchingThenReloaded() {
    var e = feed(started(), [snap(1, "watching", { message: "Watching for saves" })])
    var l = Model.hotReloadLabel(e, on, 0)
    compare(l.state, "watching"); compare(l.level, "ok"); compare(l.text, "Watching for saves"); verify(l.canPause)
    verify(l.detail.indexOf("Save a Swift file") === 0)
    var t = new Date(2026, 10, 12, 12, 4, 31).getTime() / 1000
    var r = { seq: 2, ok: true, files: ["Sources/CrashLab/ContentView.swift"], fileCount: 1, ms: 1630, time: t }
    e = feed(e, [snap(2, "reloaded", { lastReload: r, reloads: 1 })])
    l = Model.hotReloadLabel(e, on, t * 1000 + 1000)
    compare(l.pill, "Reloaded"); compare(l.text, "Reloaded ContentView.swift · 1.6 s · 12:04:31 · just now")
    e = feed(e, [snap(3, "watching", { message: "Watching for saves", lastReload: r })])
    compare(Model.hotReloadLabel(e, on, t * 1000 + 12500).text, "Reloaded ContentView.swift · 1.6 s · 12:04:31 · 12 s ago")
    compare(Model.hotReloadLabel(e, on, t * 1000 + 190000).text, "Reloaded ContentView.swift · 1.6 s · 12:04:31 · 3 min ago")
    r = Object.assign({}, r, { files: ["Sources/A/A.swift", "Sources/A/B.swift"], fileCount: 3 })
    compare(Model.hotReloadReloadText(r, null), "Reloaded A.swift and 2 more · 1.6 s · 12:04:31")
    var back = feed(e, [snap(4, "watching", { message: "Back to the built version", lastReload: r })])
    compare(Model.hotReloadLabel(back, on, 0).text, "Back to the built version")
  }

  function test_reloadingShowsTheStep() {
    var e = feed(started(), [snap(1, "reloading", { message: "Reloading ContentView.swift", progress: { step: "compile", detail: "Compiling" } })])
    var l = Model.hotReloadLabel(e, on, 0)
    compare(l.pill, "Reloading"); compare(l.text, "Reloading ContentView.swift"); compare(l.detail, "Compiling…")
    verify(l.canPause); verify(l.busy)
  }

  function test_failedWithBlockers() {
    var blockers = []
    for (var i = 0; i < 5; i++)
      blockers.push({ kind: "stored-property", type: "ContentView", name: "p" + i, file: "Sources/CrashLab/ContentView.swift",
                      line: 10 + i, message: "Adds the stored property p" + i + " to ContentView." })
    var err = { code: "unsupported-change", message: "These changes need a full build.", blockers: blockers,
                fix: { label: "Run again", page: "projects", action: "hot-reload-restart" } }
    var e = feed(started(), [snap(1, "failed", { message: err.message, lastError: err })])
    var l = Model.hotReloadLabel(e, on, 0)
    compare(l.state, "failed"); compare(l.level, "error"); verify(l.live); verify(l.canPause)
    compare(l.text, "These changes need a full build.")
    compare(l.blockers.length, 3); compare(l.moreBlockers, 2)
    compare(l.blockers[0].where, "ContentView.swift:10"); compare(l.blockers[0].text, "Adds the stored property p0 to ContentView.")
    compare(l.error.fix.action, "hot-reload-restart")
    // one blocker that the message already says: only its place
    var one = { code: "unsupported-change", message: "Adds the stored property count to ContentView. This change needs a full build.",
                blockers: [{ kind: "stored-property", file: "Sources/CrashLab/ContentView.swift", line: 12,
                             message: "Adds the stored property count to ContentView." }] }
    var l1 = Model.hotReloadLabel(feed(started(), [snap(1, "failed", { lastError: one })]), on, 0)
    compare(l1.blockers.length, 1); compare(l1.blockers[0].where, "ContentView.swift:12"); compare(l1.blockers[0].text, "")
    // the reply lists 10 of 25: "and 22 more" counts blockersTotal, not the list
    var many = []
    for (var k = 0; k < 10; k++)
      many.push({ kind: "stored-property", file: "Sources/CrashLab/ContentView.swift", line: 20 + k, message: "Adds the stored property q" + k + "." })
    var cut = { code: "unsupported-change", message: "These changes need a full build.", blockers: many, blockersTotal: 25 }
    var l25 = Model.hotReloadLabel(feed(started(), [snap(1, "failed", { lastError: cut })]), on, 0)
    compare(l25.blockers.length, 3); compare(l25.moreBlockers, 22)
    // an older reply without blockersTotal: the list length
    delete cut.blockersTotal
    compare(Model.hotReloadLabel(feed(started(), [snap(1, "failed", { lastError: cut })]), on, 0).moreBlockers, 7)
  }

  function test_compileFailedShowsTheFirstError() {
    var err = { code: "compile-failed", message: "ContentView.swift doesn't compile.",
                diagnostics: [{ severity: "warning", message: "unused", file: "Sources/A/X.swift", line: 1 },
                              { severity: "error", message: "cannot find 'foo' in scope\n  foo()", file: "Sources/A/ContentView.swift", line: 14 }] }
    var l = Model.hotReloadLabel(feed(started(), [snap(1, "failed", { lastError: err })]), on, 0)
    compare(l.diagnostic, "ContentView.swift:14: cannot find 'foo' in scope")
    var same = { code: "compile-failed", message: "ContentView.swift doesn't compile: cannot find 'foo' in scope",
                 diagnostics: [{ severity: "error", message: "cannot find 'foo' in scope", file: "Sources/A/ContentView.swift", line: 14 }] }
    compare(Model.hotReloadLabel(feed(started(), [snap(1, "failed", { lastError: same })]), on, 0).diagnostic, "")
  }

  function test_pausedWithPending() {
    var e = feed(started(), [snap(1, "watching")])
    e.action = "pausing"
    e = feed(e, [snap(2, "paused", { paused: true, pending: ["Sources/A/ContentView.swift", "Sources/A/ForecastView.swift"], pendingCount: 2 })])
    compare(e.action, "")
    var l = Model.hotReloadLabel(e, on, 0)
    compare(l.pill, "Paused"); compare(l.text, "2 saved files waiting")
    compare(l.pending, ["ContentView.swift", "ForecastView.swift"])
    compare(l.detail, "ContentView.swift, ForecastView.swift reload together when you resume.")
    verify(l.canResume); verify(!l.canPause)
    e.action = "resuming"
    verify(!Model.hotReloadLabel(e, on, 0).canResume)
    compare(Model.hotReloadLabel(feed(started(), [snap(1, "paused", { paused: true })]), on, 0).text, "Saves wait until you resume")
  }

  function test_noticesAndStartProblems() {
    var n = { id: "watch-stopped", level: "info", message: "Watch mode was stopped for this project.", time: 1000 }
    var e = feed(started(), [snap(1, "watching", { notice: n })])
    compare(Model.hotReloadLabel(e, on, 1000 * 1000 + 5000).note.text, n.message)
    compare(Model.hotReloadLabel(e, on, 1000 * 1000 + 400000).note, null)      // an old info notice goes
    var w = Object.assign({}, n, { id: "needs-run", level: "warn", fix: { label: "Run again", action: "hot-reload-restart" } })
    var lw = Model.hotReloadLabel(feed(e, [snap(2, "watching", { notice: w })]), on, 1000 * 1000 + 400000)
    compare(lw.note.level, "warn"); compare(lw.note.fix.label, "Run again")
    var err = { code: "app-not-running", message: "Open it on your iPhone.", fix: { label: "Run again", action: "hot-reload-restart" } }
    var la = Model.hotReloadLabel(feed(e, [snap(3, "watching", { lastError: err })]), on, 0)
    compare(la.state, "watching"); compare(la.note.text, "Open it on your iPhone.")
    // the last failed reload's error (it has a seq) isn't a note while the next one runs
    var lr = Model.hotReloadLabel(feed(e, [snap(4, "reloading", { lastError: Object.assign({ seq: 3 }, err) })]), on, 0)
    verify(lr.note === null || lr.note.text !== err.message)
  }

  function test_doneEndsIt() {
    var e = feed(started(), [snap(1, "watching")])
    e.action = "stopping"
    compare(Model.hotReloadLabel(e, on, 0).state, "stopping")
    e = feed(e, [snap(2, "stopped", { stopReason: "stopped", message: "Stopped" }),
                 { event: "done", ok: true, data: Object.assign(snap(2, "stopped", { stopReason: "stopped", message: "Stopped" }), { history: [1] }) }])
    compare(e.action, ""); verify(!Model.hotReloadLive(e)); verify(e.session.history === undefined)
    var l = Model.hotReloadLabel(e, on, 0)
    compare(l.state, "stopped"); compare(l.level, "info"); verify(!l.live); verify(!l.canPause)
    // turned off elsewhere: a stopped session no longer shows
    compare(Model.hotReloadLabel(e, off, 0), null)
  }

  function test_buildFailedKeepsItsFix() {
    var err = { code: "build-failed", message: "The build failed: 2 errors.", fix: { label: "Show build", page: "projects", action: "build" } }
    var e = feed(started(), [snap(1, "building"),
      { event: "done", ok: false, error: err, data: snap(3, "stopped", { stopReason: "build-failed", message: "Stopped: the build failed", lastError: err }) }])
    var l = Model.hotReloadLabel(e, on, 0)
    compare(l.state, "stopped"); compare(l.level, "error"); compare(l.pill, "Build failed")
    compare(l.text, err.message); compare(l.error.fix.label, "Show build")
  }

  function test_startFailureWithoutSnapshot() {
    var err = { code: "busy", message: "Hot reload is already running for this project (process 7).",
                fix: { label: "Stop it", page: "projects", action: "hot-reload-stop" } }
    var e = feed(started(), [{ event: "done", ok: false, error: err }])
    compare(e.session, null); compare(e.error.code, "busy"); compare(e.errorAction, "start"); compare(e.action, "")
    var l = Model.hotReloadLabel(e, on, 0)
    compare(l.state, "error"); compare(l.pill, "Didn't start"); compare(l.text, err.message); compare(l.error.fix.label, "Stop it")
    var en = Object.assign(Model.hotReloadEntry("/p/A", "A"), { error: { code: "manifest-invalid", message: "Bad" }, errorAction: "enable" })
    compare(Model.hotReloadLabel(en, off, 0).pill, "Couldn't turn on")
    var c = feed(started(), [{ event: "done", ok: false, error: { code: "cancelled", message: "x" } }])
    compare(c.error, null)
  }

  function test_enablingDisabling() {
    var e = Model.hotReloadEntry("/p/A", "A")
    e.action = "enabling"
    compare(Model.hotReloadLabel(e, off, 0).state, "enabling")
    e.action = "disabling"
    var l = Model.hotReloadLabel(e, on, 0)
    compare(l.state, "disabling"); verify(l.busy)
    // a session's done while it turns off keeps "disabling"
    compare(Model.hotReloadEvent(e, { event: "done", ok: true, data: {} }).action, "disabling")
  }

  function test_fromStatus() {
    var e = Model.hotReloadFromStatus(Object.assign(snap(9, "watching"), { running: true, history: [] }), "")
    verify(!e.owned); compare(e.name, "CrashLab"); compare(e.path, "/p/CrashLab"); verify(Model.hotReloadLive(e))
    verify(e.session.history === undefined)
    var dead = Model.hotReloadFromStatus(Object.assign(snap(9, "watching"), { running: false }), "")
    verify(!Model.hotReloadLive(dead))
  }

  function test_formatters() {
    compare(Model.hotReloadMs(1630), "1.6 s"); compare(Model.hotReloadMs(420), "0.4 s"); compare(Model.hotReloadMs(12400), "12 s")
    compare(Model.hotReloadMs(undefined), "")
    compare(Model.hotReloadAgo(100, 100 * 1000 + 61000), "1 min ago")
    compare(Model.hotReloadFiles([], 0), "your changes")
    compare(Model.hotReloadFiles(["Sources/A/X.swift"], 1), "X.swift")
    compare(Model.hotReloadBlocker({ file: "Sources/A/X.swift", message: "m" }).where, "X.swift")
    verify(Model.SHORTCUTS.some(function(s) { return s.keys === "H" }))
  }
}
