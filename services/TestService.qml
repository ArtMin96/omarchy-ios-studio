pragma ComponentBehavior: Bound
import QtQuick
import Quickshell
import "../core"
import "../lib/Model.js" as Model

// Unit tests on this computer (issue #15; helper `test`, helper/iosstudio/features/testing/): `swift test` for a
// project's test targets, without the iPhone. One `test run` stream per project (several projects can test at
// once); a run keeps going when its sheet closes, and its outcome toasts when no sheet shows it.
//
// Public surface (app.tests):
//   forProject(p) → the run entry or null: { path, name, state: "running"|"done", filter, steps: [{id, title, state}],
//     step (the running one or null), counts: {finished, passed, failed, skipped}, ok, error, result (the helper's
//     RESULT), startedAt, endedAt, cancelling, rev (bumped when a failure or a skip arrives) }
//   isRunning(p) · results(p) → { failed[], skipped[], passed[], passedTotal, libraries: {xctest, "swift-testing"},
//     diagnostics[], log[] } (the last run's tests, capped: failures up to maxRows, then skipped and passed fill the
//     rest up to maxRows; passed ones beyond it are only counted) · logTail(p)
//   start(p, filter) · cancel(p) ({"cancel": true}, then the process is stopped if it doesn't end in 3 s) ·
//   forget(p) (drops a finished run's entry and results)
//   info: a Resource with `test info` for infoPath (test targets, host blockers, the last run); loadInfo(p) asks it
//     (the sheet does, when it opens) and a run of that project asks again when it ends. Nothing loads by itself.
//   viewing: the project path the tests sheet shows ("" = closed); its runs don't toast.
//   signal finished(path, ok)
// When a run ends with failing tests or compile errors, they go to the build panel's Problems
// (app.projects.showTestRun), like a finished build's.
// Bounded: maxRows rows and maxLog log lines per run, maxDiagnostics problems, keepProjects finished runs kept.
// Events are counted as they arrive; the entries the UI binds to are refreshed at most every 120 ms.
Item {
  id: root
  required property var app

  // Scaffold contract (status is never "loading", so the sidebar doesn't spin for this).
  property string status: "ready"
  property var summary: ({ title: "Tests", detail: "", level: "info", badge: "" })

  readonly property int maxRows: 2000
  readonly property int maxLog: 200
  readonly property int maxDiagnostics: 200
  readonly property int keepProjects: 8

  property var entries: ({})            // path → run entry (replaced, never changed in place)
  property string viewing: ""
  property int runningCount: 0
  property var _live: ({})              // path → the entry being filled (changed in place; flushed into entries)
  property var _store: ({})             // path → results (changed in place; entries[path].rev says when)
  property var _streams: ({})           // path → Stream
  property var _dirty: ({})

  signal finished(string path, bool ok)

  function _path(p) { return p && p.path ? String(p.path) : String(p || "") }
  function _project(p) {
    var path = _path(p)
    return p && p.path ? p : (app.projects ? app.projects.projectByPath(path) : null) || { path: path, name: path.split("/").pop() }
  }

  function forProject(p) { var path = _path(p); return path && root.entries[path] ? root.entries[path] : null }
  function isRunning(p) { var e = forProject(p); return !!e && e.state === "running" }
  function results(p) { return root._store[_path(p)] || null }
  function logTail(p) { var r = results(p); return r ? r.log.slice() : [] }

  // ------------------------------------------------------------ test info (cheap; only when asked)
  property string infoPath: ""
  property Resource info: Resource {
    fetch: function(done, fail) {
      root.app.cli.call(["test", "info", "--project", root.infoPath], "", done, fail, { timeout: 30000 })
    }
  }
  function loadInfo(p) {
    var path = _path(p)
    if (!path) return
    if (path !== root.infoPath) { root.info.reset(); root.infoPath = path }
    root.info.load(true)
  }

  // ------------------------------------------------------------ runs
  function start(p, filter) {
    var pr = _project(p)
    var path = pr.path
    if (!path) return false
    if (isRunning(path)) return false
    var f = String(filter || "").trim()
    var argv = ["test", "run", "--project", path]
    if (f !== "") argv.push("--filter", f)
    root._store[path] = { failed: [], skipped: [], passed: [], passedTotal: 0, skippedHidden: 0, failedHidden: 0,
                          libraries: {}, diagnostics: [], log: [] }
    var e = { path: path, name: pr.name || path.split("/").pop(), state: "running", filter: f, steps: [], step: null,
              counts: { finished: 0, passed: 0, failed: 0, skipped: 0 }, ok: false, error: null, result: null,
              startedAt: Date.now(), endedAt: 0, cancelling: false, rev: 0 }
    root._live[path] = e
    root._flush(path)
    var s = streamComp.createObject(root, { path: path })
    root._streams[path] = s
    s.start(argv)
    root._count()
    return true
  }

  function cancel(p) {
    var path = _path(p)
    var s = root._streams[path]
    var e = root._live[path]
    if (!s || !e || e.state !== "running" || e.cancelling) return
    e.cancelling = true
    root._flush(path)
    if (!s.write({ cancel: true })) s.stop()
    s.killLater()
  }

  function forget(p) {
    var path = _path(p)
    var e = root._live[path]
    if (!e || e.state === "running") return
    delete root._live[path]
    delete root._store[path]
    var copy = Object.assign({}, root.entries)
    delete copy[path]
    root.entries = copy
  }

  function _count() {
    var n = 0
    for (var k in root._streams) n++
    root.runningCount = n
  }

  // The entry the UI sees: a copy of the live one (counts and steps copied too).
  function _flush(path) {
    var e = root._live[path]
    if (!e) return
    delete root._dirty[path]
    var copy = Object.assign({}, root.entries)
    copy[path] = Object.assign({}, e, { counts: Object.assign({}, e.counts), steps: e.steps.slice() })
    root.entries = copy
  }
  function _flushSoon(path) {
    root._dirty[path] = true
    if (!flushTimer.running) flushTimer.start()
  }
  Timer {
    id: flushTimer
    interval: 120
    onTriggered: { for (var k in root._dirty) root._flush(k) }
  }

  function _step(e, ev) {
    var id = String(ev.id || ev.step || "")
    if (!id) return
    var st = String(ev.state || "running")
    var found = false
    for (var i = 0; i < e.steps.length; i++) {
      var s = e.steps[i]
      if (s.id === id) { e.steps[i] = { id: id, title: ev.title || s.title, state: st }; found = true }
      else if (st === "running" && s.state === "running") e.steps[i] = Object.assign({}, s, { state: "done" })
    }
    if (!found) e.steps.push({ id: id, title: String(ev.title || id), state: st })
    e.step = st === "running" ? { id: id, title: String(ev.title || id) } : null
  }

  function _test(path, e, ev) {
    var r = root._store[path]
    var status = ev.status === "failed" || ev.status === "skipped" ? ev.status : "passed"
    var t = { id: String(ev.id || ""), name: String(ev.name || ev.id || ""), suite: String(ev.suite || ""),
              library: String(ev.library || ""), status: status, duration: ev.duration }
    if (status === "failed") { t.file = ev.file || ""; t.line = ev.line || 0; t.message = String(ev.message || "") }
    if (t.library) r.libraries[t.library] = true
    e.counts.finished++
    e.counts[status]++
    var room = root.maxRows - r.failed.length - r.skipped.length - r.passed.length
    if (status === "failed") {
      if (r.failed.length < root.maxRows) { r.failed.push(t); e.rev++ } else r.failedHidden++
    } else if (status === "skipped") {
      if (room > 0) { r.skipped.push(t); e.rev++ } else r.skippedHidden++
    } else {
      if (room > 0) r.passed.push(t)
      r.passedTotal++
    }
  }

  function _event(path, ev) {
    var e = root._live[path]
    if (!e || e.state !== "running") return
    var r = root._store[path]
    switch (ev.event) {
    case "step":
      root._step(e, ev)
      root._flush(path)
      break
    case "log":
      if (ev.text === undefined) break
      r.log.push(String(ev.text))
      if (r.log.length > root.maxLog + 50) r.log.splice(0, r.log.length - root.maxLog)
      break
    case "diagnostic":
      if (r.diagnostics.length < root.maxDiagnostics) r.diagnostics.push(ev)
      break
    case "test":
      root._test(path, e, ev)
      root._flushSoon(path)
      break
    }
  }

  function _end(path, ok, data, error) {
    var e = root._live[path]
    if (!e || e.state !== "running") return
    var r = root._store[path]
    if (r.log.length > root.maxLog) r.log.splice(0, r.log.length - root.maxLog)
    var cancelled = !ok && (e.cancelling || (error && error.code === "cancelled"))
    if (cancelled && (!error || error.code !== "cancelled")) error = { code: "cancelled", message: "Test run cancelled." }
    e.steps = e.steps.map(function(s) { return s.state === "running" ? Object.assign({}, s, { state: ok ? "done" : "failed" }) : s })
    e.step = null
    e.state = "done"
    e.ok = ok
    e.error = ok ? null : (error || { code: "failed", message: "The test run failed." })
    e.result = data && data.counts ? data : null
    // the helper's counts, unless it stopped (cancelled) before counting what had finished
    if (e.result && !(cancelled && !(e.result.counts.total > 0))) {
      var c = e.result.counts
      e.counts = { finished: c.total !== undefined ? c.total : e.counts.finished, passed: c.passed || 0, failed: c.failed || 0,
                   skipped: c.skipped || 0 }
    }
    e.endedAt = Date.now()
    e.cancelling = false
    e.rev++
    root._flush(path)
    root._prune()
    if (path === root.infoPath) root.info.load(true)       // last_run changed
    root._toProblems(path, e, r)
    root._tell(e)
    root.finished(path, ok)
  }

  // Failing tests and compile errors in the build panel's Problems; a passing run clears a test run shown there.
  function _toProblems(path, e, r) {
    var p = app.projects
    if (!p || typeof p.showTestRun !== "function") return
    var code = e.error ? e.error.code : ""
    var shownHere = p.build && p.build.kind === "test" && p.build.path === path
    if (e.ok ? !shownHere : !((code === "tests_failed" || code === "build_failed") && r.diagnostics.length > 0)) return
    p.showTestRun({ path: path, name: e.name, ok: e.ok, code: code, message: e.error ? Model.errorMessage(e.error) : "",
                    counts: e.counts, duration: e.result ? e.result.duration : (e.endedAt - e.startedAt) / 1000,
                    diagnostics: r.diagnostics, log: r.log, logFile: e.result && e.result.log_path ? e.result.log_path : "",
                    filter: e.filter })
  }

  function _tell(e) {
    var code = e.error ? e.error.code : ""
    if (code === "cancelled") return
    var c = e.counts
    var title = e.ok ? (e.result && e.result.no_tests_matched ? "No test matched the filter in " + e.name : e.name + ": all tests passed")
              : code === "tests_failed" ? e.name + ": " + c.failed + " of " + c.finished + " tests failed"
              : code === "build_failed" ? e.name + ": the tests didn't compile"
              : e.name + ": " + Model.errorMessage(e.error)
    var body = e.result ? Model.testSummary(c, e.result.duration) : ""
    app.notify(title, body, e.ok ? Model.Glyph.checkCircle : Model.Glyph.error)
    if (root.viewing !== e.path) app.toast(title, e.ok ? "ok" : (code === "tests_failed" || code === "build_failed" ? "error" : "warn"))
  }

  // Keep the newest keepProjects finished runs (and every running one).
  function _prune() {
    var done = []
    for (var k in root._live) if (root._live[k].state === "done") done.push(root._live[k])
    if (done.length <= root.keepProjects) return
    done.sort(function(a, b) { return b.endedAt - a.endedAt })
    var copy = Object.assign({}, root.entries)
    for (var i = root.keepProjects; i < done.length; i++) {
      var path = done[i].path
      if (path === root.viewing) continue
      delete root._live[path]
      delete root._store[path]
      delete copy[path]
    }
    root.entries = copy
  }

  Component {
    id: streamComp
    Stream {
      id: s
      property string path: ""
      program: [root.app.helperPath]
      onEvent: function(ev) { root._event(s.path, ev) }
      onFinished: function(ok, data, error) { root._end(s.path, ok, data, error) }
      onExited: function(code) {
        var path = s.path
        // Stopped after Cancel (stop() reports no done), or it ended without saying how. Later, so a done goes first.
        Qt.callLater(function() {
          var e = root._live[path]
          if (e && e.state === "running")
            root._end(path, false, {}, e.cancelling ? { code: "cancelled", message: "Test run cancelled." }
                                                    : { code: "exited", message: "The test run stopped without a result (exit code " + code + ")." })
          if (root._streams[path] === s) delete root._streams[path]
          root._count()
          s.destroy()
        })
      }
      // After {"cancel": true} the helper stops swift and ends; this only catches a hung one.
      function killLater() { kill.restart() }
      Timer { id: kill; interval: 3000; onTriggered: if (s.running) s.stop() }
    }
  }
}
