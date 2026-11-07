import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import "../core"
import "../lib/Model.js" as Model

// Watch mode, compile on save (issue #7; helper/iosstudio/features/watch.py, docs/features/watch-mode.md).
// One watcher per user: starting one stops the other, and agents (MCP ios_watch_start/stop) can
// start or stop it too. Every change of the watcher is a snapshot in <runtime dir>/watch.json:
//   {seq, pid, state: watching|changed|compiling|ok|failed|stopped, reason, message, project (name), path,
//    autoRun, changed[], progress?, lastBuild?: {id, result, duration, time, errors, warnings, logFile,
//    diagnostics[≤5]}, run?: {state, message, buildId, forBuild (the compile it followed), time}, stopReason?, replaced?}
// The app follows that file (and, for a watch it started, the `watch run` stream, which is also what
// keeps the watcher alive: it stops when none of its owners is left). So a watch an agent started
// shows up here too, without polling the helper.
//
// Public surface (app.watch):
//   current: null | { path, name, state, autoRun, owned, pid, seq, message, reason, changed[], progress,
//                     last: {id, ok, errors, warnings, duration, time (ms), logFile, diagnostics[], errorMessage},
//                     run: {state, message, buildId, forBuild, time} | null, mode, modeReason }
//   active (bool), state ("off" when nothing is watched), busy ("" | "starting" | "stopping"),
//   now (ms; ticks while active, for "compiled 12 s ago"), lastError
//   forProject(p) → current when p is the watched project (or being started), else null · isWatching(p)
//   start(p, autoRun) · stop() · toggle(p) · setAutoRun(p, on) · showLast()
//   autoRunBlocker → "" or why auto-run can't work right now (no iPhone, no Apple ID); autoRunBlockerShort (menu)
//   label(w) → { level, glyph, text, tip } for the card and the sidebar
// After each watch build the result goes to app.projects.showWatchBuild(…), so the build panel's
// Problems tab refreshes by itself (unless a manual build owns the panel right now).
Item {
  id: root
  required property var app

  // Scaffold contract (status is never "loading", so the sidebar doesn't spin for this).
  property string status: "ready"
  property var summary: ({ title: "Watch", detail: "", level: "info", badge: "" })

  property var current: null
  readonly property bool active: current !== null
  readonly property string state: current ? current.state : "off"
  property string busy: ""              // "starting" | "stopping" while we wait for the helper
  property string busyPath: ""
  property real now: Date.now()
  property var lastError: null

  readonly property string runtimeDir: (Quickshell.env("IOSSTUDIO_RUNTIME_DIR") || Quickshell.env("XDG_RUNTIME_DIR") || "/tmp")
                                       + "/" + (app.demo !== "" ? "ios-studio-demo" : "ios-studio")

  readonly property string autoRunBlocker: {
    var p = app.projects
    if (p && !p.canRun) return "Connect your iPhone first"
    var a = app.account
    if (a && a.status === "ready" && !a.signedIn) return "Sign in with your Apple ID first"
    return ""
  }

  // The same, short enough for the Run menu's detail column.
  readonly property string autoRunBlockerShort: autoRunBlocker === "" ? "" : (autoRunBlocker.indexOf("iPhone") >= 0 ? "connect your iPhone" : "sign in to your Apple ID")

  function _path(p) { return p && p.path ? p.path : (p || "") }
  function forProject(p) {
    var path = _path(p)
    if (!path) return null
    if (root.current && root.current.path === path) return root.current
    // Starting: show it on the card at once, before the watcher's first snapshot.
    if (root.busy === "starting" && root.busyPath === path)
      return { path: path, name: _nameOf(path), state: "starting", autoRun: false, owned: true, last: null, run: null }
    return null
  }
  function isWatching(p) { return forProject(p) !== null }

  // ------------------------------------------------------------ actions
  property bool _userStop: false

  function start(p, autoRun) {
    var path = _path(p)
    if (!path) return
    var argv = ["watch", "run", "--project", path]
    if (autoRun === true && root.autoRunBlocker === "") argv.push("--auto-run")
    root.lastError = null
    root._userStop = false
    root.busy = "starting"
    root.busyPath = path
    // Our follower already runs for another project: replace it (the new one stops that watcher).
    stream.start(argv)
  }

  function stop() {
    var c = root.current
    if (!c) return
    root.busy = "stopping"
    root.busyPath = c.path
    root._userStop = true
    // Our follower stops the watcher for us ({"cmd": "stop"}); else ask the helper (an agent's watch).
    if (stream.running && stream.write({ cmd: "stop" })) return
    app.cli.call(["watch", "stop", "--project", c.path], "", function(d) {
      root._onSnapshot(Object.assign({}, d, { state: "stopped", path: c.path, pid: c.pid, seq: (c.seq || 0) + 1 }), false)
      root.busy = ""
      root._userStop = false
    }, function(e) {
      root.busy = ""
      root._userStop = false
      app.toast(Model.errorMessage(e), "error")
    }, { timeout: 20000 })
  }

  function toggle(p) { if (isWatching(p)) stop(); else start(p, false) }

  // Auto-run changes on the running watcher (no restart): `watch run --detach` with the new setting.
  function setAutoRun(p, on) {
    if (on && root.autoRunBlocker !== "") { app.toast(root.autoRunBlocker + " to run after each compile.", "warn"); return }
    var w = root.current && root.current.path === _path(p) ? root.current : null
    if (!w) { start(p, on); return }
    if (w.autoRun === on) return
    var argv = ["watch", "run", "--detach", "--project", w.path]
    if (on) argv.push("--auto-run")
    app.cli.call(argv, "", function(d) {
      root._onSnapshot(d, false)
      app.toast(on ? "Runs " + w.name + " on your iPhone after each clean compile" : "Watching " + w.name + " without running it", "ok")
    }, function(e) { app.toast(Model.errorMessage(e), "error") }, { timeout: 20000 })
  }

  function _nameOf(path) { var pr = app.projects ? app.projects.projectByPath(path) : null; return pr ? pr.name : String(path).split("/").pop() }

  // ------------------------------------------------------------ the watch we started
  Stream {
    id: stream
    program: [app.helperPath]
    onEvent: function(ev) {
      if (ev.event === "watch") root._onSnapshot(ev, true)
      else if (ev.event === "watch-start" && ev.replaced && ev.replaced.project)
        app.toast("Stopped watching " + ev.replaced.project + ". One project is watched at a time.", "info")
    }
    onFinished: function(ok, data, error) {
      if (!ok && error && error.code !== "cancelled") {
        root.busy = ""
        root.lastError = error
        app.toast("Couldn't watch for changes: " + Model.errorMessage(error), "error")
      }
      if (ok && data && data.state) root._onSnapshot(data, true)
    }
    onExited: function(code) {
      Qt.callLater(function() {
        if (stream.running) return                    // replaced by a new follower (another project)
        if (root.busy !== "") root.busy = ""
        root._userStop = false
        statusFile.reload()                           // the watcher may live on (an agent owns it too)
      })
    }
  }

  // ------------------------------------------------------------ watch.json (any watch, ours or an agent's)
  FileView {
    id: statusFile
    path: root.runtimeDir + "/watch.json"
    watchChanges: true
    printErrors: false
    onFileChanged: reload()
    onLoaded: {
      try { root._onSnapshot(JSON.parse(text() || "{}"), false) } catch (e) {}
    }
    onLoadFailed: if (!stream.running && root.current && !root.current.owned) root.current = null
  }
  // The watcher replaces watch.json atomically, which a file watch can miss: re-read it now and then
  // while the window is focused (a small file read, no process).
  Timer {
    interval: 2000
    repeat: true
    running: root.app.windowFocused
    onTriggered: statusFile.reload()
  }
  // A watcher that crashed leaves a non-stopped watch.json behind; the helper's `watch status` checks
  // its lock. Asked rarely, only while something seems watched and we don't follow it ourselves.
  Timer {
    interval: 15000
    repeat: true
    running: root.app.windowFocused && root.active && !stream.running
    onTriggered: root.app.cli.call(["watch", "status"], "", function(d) {
      var c = root.current
      if (d && d.watching === false && c && !stream.running)
        root._onSnapshot(Object.assign({}, d, { state: "stopped", path: c.path, pid: c.pid, seq: (c.seq || 0) + 1 }), false)
    }, function(e) {}, { timeout: 10000 })
  }

  property string _shownBuild: ""       // lastBuild.id already sent to the build panel
  readonly property real _startedAt: Date.now()
  property string _handledRun: ""       // the auto-run (id|state|time) the project list was refreshed for
  property int _seq: -1
  property int _pid: 0

  function _onSnapshot(s, fromStream) {
    if (!s || typeof s !== "object" || !s.state) return
    var pid = Number(s.pid || 0), seq = Number(s.seq || 0)
    // The file and the stream report the same snapshots: skip what we already have.
    if (pid && pid === root._pid && seq && seq <= root._seq) return
    if (pid) { root._pid = pid; root._seq = seq }
    var st = String(s.state)
    if (st === "stopped" || st === "off") {
      var was = root.current
      if (root.busy === "stopping") root.busy = ""
      if (was && (!s.path || was.path === s.path)) {
        root.current = null
        if (root._userStop) app.toast("Stopped watching " + was.name, "ok")
        else if (s.stopReason !== "replaced" && was.owned)
          app.toast("Stopped watching " + was.name + (s.message && s.message !== "Stopped" ? ": " + s.message : ""), "info")
        if (app.projects) app.projects.loadHistory()
      }
      return
    }
    if (!s.path) return
    if (root.busy === "starting" && root.busyPath === s.path) root.busy = ""
    var lb = s.lastBuild || null
    var last = lb ? {
      id: lb.id || "", ok: lb.result === "ok", errors: Number(lb.errors || 0), warnings: Number(lb.warnings || 0),
      duration: Number(lb.duration || 0), time: lb.time ? lb.time * 1000 : Date.now(), logFile: lb.logFile || "",
      diagnostics: lb.diagnostics || [], errorMessage: lb.errorMessage || "", errorCode: lb.errorCode || ""
    } : null
    var owned = fromStream || (root.current && root.current.path === s.path && root.current.owned)
                || (stream.running && stream.args.indexOf(s.path) >= 0)
    root.current = {
      path: s.path, name: s.project || _nameOf(s.path), state: st, autoRun: s.autoRun === true, owned: !!owned,
      pid: pid, seq: seq, message: s.message || "", reason: s.reason || "", changed: s.changed || [],
      progress: s.progress || null, last: last, run: s.run || null, mode: s.mode || "", modeReason: s.modeReason || ""
    }
    // Auto-run finished (launched or failed): the list's days-left chip and Recent builds change.
    var run = s.run || null
    if (run && (run.state === "launched" || run.state === "failed")) {
      var key = (run.buildId || "") + "|" + run.state + "|" + (run.time || "")
      // (one that ended before the app started, read from watch.json, doesn't count)
      if (key !== root._handledRun) {
        root._handledRun = key
        if (app.projects && (!run.time || run.time * 1000 >= root._startedAt)) app.projects.refresh()
      }
    }
    if (last && last.id && last.id !== root._shownBuild && st !== "compiling") {
      root._shownBuild = last.id
      root._publish(root.current)
    }
  }

  // A finished watch build: fill Problems. The snapshot carries the first few diagnostics; the full
  // list is in the build history (the watcher runs the same `build compile`), matched by buildId / log file.
  function _publish(c) {
    if (!c || !c.last || !app.projects) return
    var last = c.last
    var show = function(diags) {
      app.projects.showWatchBuild({ path: c.path, name: c.name, ok: last.ok, errors: last.errors, warnings: last.warnings,
                                    duration: last.duration, logFile: last.logFile, buildId: last.id, diagnostics: diags,
                                    errorMessage: last.errorMessage, errorCode: last.errorCode })
    }
    if (!last.logFile && !last.id) { show(last.diagnostics); return }
    app.cli.call(["build", "history", "--path", c.path, "--limit", "3"], "", function(d) {
      var hit = (d.builds || []).filter(function(e) { return e && ((last.id && e.buildId === last.id) || e.logFile === last.logFile) })[0]
      show(hit && hit.diagnostics ? hit.diagnostics : last.diagnostics)
    }, function() { show(last.diagnostics) }, { timeout: 15000 })
  }

  // Show the watched project's last result in the build panel again (after a manual build replaced it).
  function showLast() { if (root.current && root.current.last) _publish(root.current) }

  // "compiled 12 s ago": ticks only while something is watched and the window is focused.
  Timer {
    interval: 5000
    repeat: true
    running: root.active && root.app.windowFocused
    triggeredOnStart: true
    onTriggered: root.now = Date.now()
  }

  // ------------------------------------------------------------ labels
  function ago(ms) {
    var s = Math.max(0, Math.round((root.now - ms) / 1000))
    if (s < 10) return "just now"
    if (s < 60) return s + " s ago"
    return Model.relativeTime(ms, root.now)
  }

  // { level, glyph ("" = the level's, spinning when busy), text, tip } for a watch state.
  function label(w) {
    if (!w) return null
    var last = w.last
    var run = w.run
    var tip = "Watching " + w.name + ": it compiles when you save." + (w.autoRun ? " A clean compile then runs it on your iPhone." : "")
              + (w.owned ? "" : " Started by an agent.") + " Turn it off in the Run menu (▾)."
    if (w.mode === "poll" && w.modeReason) tip += " " + w.modeReason
    if (root.busy === "stopping" && root.busyPath === w.path) return { level: "busy", glyph: "", text: "Stopping…", tip: tip }
    if (w.state === "starting") return { level: "busy", glyph: "", text: "Starting watch…", tip: tip }
    // While it compiles again, the last failed result stays in view until the new one arrives.
    var was = last && !last.ok ? " · last: " + (last.errors > 0 ? Model.plural(last.errors, "error") : "failed") : ""
    if (w.state === "changed" && w.reason === "busy") return { level: "busy", glyph: "", text: "Waiting for your build…" + was, tip: (w.message ? w.message + ". " : "") + tip }
    if (w.state === "changed") return { level: "busy", glyph: "", text: "Change seen…" + was, tip: (w.message ? w.message + ". " : "") + tip }
    if (w.state === "compiling") return { level: "busy", glyph: "", text: "Compiling…" + was, tip: (w.message ? w.message + ". " : "") + tip }
    if (run && ["checking", "building", "installing", "launching"].indexOf(run.state) >= 0)
      return { level: "busy", glyph: "", text: "Running on iPhone…", tip: (run.message ? run.message + ". " : "") + tip }
    if (last && !last.ok) {
      var n = last.errors || 0
      return { level: "error", glyph: Model.Glyph.eye,
               text: n > 0 ? "Watching · " + Model.plural(n, "error") : "Watching · build failed",
               tip: "The last compile on save failed " + ago(last.time) + (last.errorMessage && n === 0 ? ": " + last.errorMessage : "") + ". Click to see the problems." }
    }
    if (last && last.ok) {
      // The run has its own build id; run.forBuild names the compile it followed (older helpers: by time).
      var mine = run && (run.forBuild ? run.forBuild === last.id : (run.time ? run.time * 1000 >= last.time : false))
      var ranOk = mine && run.state === "launched"
      var runBad = mine && run.state === "failed"
      var wn = last.warnings ? " · " + Model.plural(last.warnings, "warning") : ""
      var runTip = mine && run.message ? " " + run.message + "." : ""
      return { level: runBad || last.warnings ? "warn" : "ok", glyph: Model.Glyph.eye,
               text: "Watching · " + (ranOk ? "ran " : runBad ? "run failed " : "compiled ") + ago(last.time) + wn,
               tip: "Compiled on save in " + Model.formatDuration(last.duration) + "." + runTip + " " + tip + (last.warnings ? " Click to see the warnings." : "") }
    }
    return { level: "info", glyph: Model.Glyph.eye, text: "Watching for changes", tip: tip }
  }

  Component.onCompleted: statusFile.reload()
}
