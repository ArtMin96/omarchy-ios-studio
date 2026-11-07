import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import "../core"
import "../lib/Model.js" as Model

// Setup slice service. Owns `setup check` (step states), the install stream
// (`setup install --step/--all`), its prompts, and the Xcode .xip watcher.
//
//   app.setup.steps            [{id, title, description, group, state, detail, size, time, needsRoot,
//                                interactive, canAutoInstall, essential, optional, action, ...}]
//   app.setup.summary          {title, detail, level, badge, done, total, ready, essentialMissing}
//   app.setup.essentialMissing ids; non-empty -> status "needs-setup" (AppState opens Setup on first run)
//   app.setup.install(id) / installAll() / cancel()
//   app.setup.runs[id]         {state: running|done|failed, fraction, detail, eta, error}
//   app.setup.logLines(id)     [lines] so far (last 400); new ones arrive as stepLog(id, text),
//                              and stepLogCleared(id) ("" = every step) when an install starts
//   app.setup.prompt           pending helper prompt {id, kind, message, options} or null; answer(value)
//   app.setup.xip              watcher state {state: idle|watching|downloading|checking|found|rejected, ...}
Item {
  id: root
  required property var app

  // "unknown" | "loading" | "ready" | "needs-setup" | "error"
  property string status: "unknown"
  property var summary: ({ title: "Setup", detail: "Checking…", level: "info", badge: "" })
  property var error: null

  property var steps: []
  property var raw: null                // last check data
  property var xcode: ({})              // {needed, major, swift, expectedSize, downloadUrl, pageUrl, file}
  property var essentialMissing: []
  property bool checking: false
  property double checkedAt: 0
  readonly property bool loaded: checkedAt > 0

  // installs
  property var runs: ({})
  // step id -> [lines]. Changed in place (no change signal); readers follow stepLog instead,
  // so a log line doesn't copy every step's log and rebuild the row's view.
  property var _logs: ({})
  property bool installing: installer.running
  property string runningStep: ""
  property bool runningAll: false
  property var prompt: null
  property var lastResult: null

  // xip watcher
  property var xip: ({ state: "idle" })
  readonly property bool watchingXip: watcher.running

  signal installFinished(string stepId, bool ok, var error)
  signal stepLog(string stepId, string text)
  signal stepLogCleared(string stepId)

  readonly property var groups: ["System", "Toolchain", "Apple", "iPhone"]

  function refresh() {
    if (root.checking) return
    root.checking = true
    if (!root.loaded) root.status = "loading"
    root.app.cli.call(["setup", "check"], "", function(data) {
      root.checking = false
      root.raw = data
      root.xcode = data.xcode || {}
      root.steps = root._merge(data.steps || [])
      root.checkedAt = Date.now()
      root.error = null
      root._recompute()
    }, function(err) {
      root.checking = false
      root.error = err
      root.status = "error"
      root.summary = { title: "Setup", detail: Model.errorMessage(err), level: "error", badge: "!" }
    }, { timeout: 30000 })
  }

  function step(id) {
    for (var i = 0; i < root.steps.length; i++) if (root.steps[i].id === id) return root.steps[i]
    return null
  }

  // Live device knowledge beats the last report on disk.
  function _merge(list) {
    var d = root.app.device
    var out = []
    for (var i = 0; i < list.length; i++) {
      var s = Object.assign({}, list[i])
      if (d && d.connected && d.info) {
        if (s.id === "developer-mode" && d.info.developerMode !== undefined && d.info.developerMode !== null) {
          s.state = d.info.developerMode ? "done" : "missing"
          s.detail = d.info.developerMode ? "On" : "Off: Settings › Privacy & Security › Developer Mode"
        }
        if (s.id === "iphone" && s.state !== "done") {
          s.state = "done"
          s.detail = "Connected" + (d.current && d.current.name ? ": " + d.current.name : "")
        }
      }
      out.push(s)
    }
    return out
  }

  function _recompute() {
    var counted = 0, done = 0, missing = []
    for (var i = 0; i < root.steps.length; i++) {
      var s = root.steps[i]
      if (s.id !== "platform" && !s.optional && s.state !== "optional") {
        counted++
        if (s.state === "done") done++
      }
      if (s.essential && (s.state === "missing" || s.state === "broken")) missing.push(s.id)
    }
    root.essentialMissing = missing
    var broken = missing.filter(function(id) { var s = root.step(id); return s && s.state === "broken" })
    var level = missing.length === 0 ? "ok" : (broken.length ? "error" : "warn")
    root.summary = {
      title: missing.length === 0 ? "Ready to build" : (missing.length === 1 ? "1 step left" : missing.length + " steps left"),
      detail: done + " of " + counted + " ready",
      level: level,
      badge: missing.length ? String(missing.length) : "",
      done: done, total: counted, ready: missing.length === 0, essentialMissing: missing,
      next: root.nextStep() ? root.nextStep().id : ""
    }
    if (root.status !== "needs-setup" && missing.length > 0) root.status = "needs-setup"
    else if (missing.length === 0) root.status = "ready"
  }

  // The first step a person should look at: broken essentials, then missing ones in order.
  function nextStep() {
    var firstMissing = null
    for (var i = 0; i < root.steps.length; i++) {
      var s = root.steps[i]
      if (!s.essential) continue
      if (s.state === "broken") return s
      if (s.state === "missing" && !firstMissing) firstMissing = s
    }
    return firstMissing
  }

  readonly property int autoCount: {
    var n = 0
    for (var i = 0; i < root.steps.length; i++) if (root.steps[i].canAutoInstall && !root.steps[i].optional) n++
    return n
  }

  // ------------------------------------------------------------------ installs
  function _setRun(id, patch) {
    var r = Object.assign({}, root.runs)
    r[id] = Object.assign({}, r[id] || {}, patch)
    root.runs = r
  }

  function logLines(id) { return (root._logs[id] || []).slice() }

  function _log(id, text) {
    if (!id) id = root.runningStep || "_all"
    var arr = root._logs[id] || (root._logs[id] = [])
    arr.push(text)
    if (arr.length > 450) arr.splice(0, arr.length - 400)
    root.stepLog(id, text)
  }

  function _clearLog(id) {
    if (id) delete root._logs[id]
    else root._logs = ({})
    root.stepLogCleared(id || "")
  }

  function install(id, extraArgs) {
    if (root.installing) { root.app.toast("Another step is still installing.", "warn"); return }
    root._clearLog(id)
    root._setRun(id, { state: "running", fraction: -1, detail: "Starting…", eta: null, error: null })
    root.runningStep = id
    root.runningAll = false
    installer.start(["setup", "install", "--step", id].concat(extraArgs || []))
  }

  function installAll() {
    if (root.installing) return
    root.runningAll = true
    root.runningStep = ""
    root._clearLog("")
    installer.start(["setup", "install", "--all"])
  }

  function cancel() {
    if (!root.installing) return
    installer.stop()
    if (root.runningStep) root._setRun(root.runningStep, { state: "failed", error: { message: "Cancelled." } })
    root.prompt = null
    root.runningAll = false
    root.runningStep = ""
    Qt.callLater(root.refresh)
  }

  function answer(value) {
    if (!root.prompt) return
    installer.write({ prompt: root.prompt.id, value: String(value) })
    root.prompt = null
  }

  function decline() {
    if (!root.prompt) return
    installer.write({ prompt: root.prompt.id, cancel: true })
    root.prompt = null
  }

  // Follow a step's action: install, open a page, or start the Xcode flow.
  function act(s) {
    if (!s) return
    var a = s.action || {}
    if (a.kind === "page") { root.app.nav(a.page, { action: a.action || "", from: "setup" }); return }
    if (s.id === "xcode") { root.openXcodeDownload(); return }
    if (a.kind === "open-url" && a.url) { Qt.openUrlExternally(a.url); return }
    root.install(s.id)
  }

  Stream {
    id: installer
    program: [root.app.helperPath]
    onEvent: function(ev) {
      var sid = ev.step || ev.id || root.runningStep
      switch (ev.event) {
      case "step":
        if (ev.state === "running") { root.runningStep = ev.id; root._setRun(ev.id, { state: "running", fraction: -1, detail: "", error: null }) }
        else if (ev.state === "done") root._setRun(ev.id, { state: "done", fraction: 1, detail: "" })
        else if (ev.state === "failed") root._setRun(ev.id, { state: "failed", error: ev.error || null })
        else if (ev.state === "skipped") root._setRun(ev.id, { state: "skipped", detail: ev.reason || "" })
        break
      case "progress":
        if (sid) root._setRun(sid, { fraction: ev.fraction === null || ev.fraction === undefined ? -1 : ev.fraction, detail: ev.detail || "", eta: ev.eta })
        break
      case "log":
        root._log(ev.step || root.runningStep, ev.text || "")
        break
      case "prompt":
        root.prompt = ev
        if (!root.app.windowFocused) root.app.notify("iOS Studio needs you", ev.message, Model.Glyph.help)
        break
      }
    }
    onFinished: function(ok, data, error) {
      var id = root.runningAll ? "_all" : root.runningStep
      root.lastResult = { ok: ok, data: data, error: error, step: id }
      if (!ok && root.runningStep) root._setRun(root.runningStep, { state: "failed", error: error })
      root.prompt = null
      if (ok) {
        root.app.toast(root.runningAll ? "Setup finished" : "Done: " + (root.step(id) ? root.step(id).title : id), "ok")
        root.app.notify("Setup finished", root.runningAll ? "Everything that could be installed is ready." : "", Model.Glyph.checkCircle)
      } else if (error && error.code !== "cancelled") {
        root.app.notify("Setup step failed", Model.errorMessage(error), Model.Glyph.error)
      }
      root.installFinished(id, ok, error)
      root.runningStep = ""
      root.runningAll = false
      root.refresh()
      if (root.app.toolchain && typeof root.app.toolchain.refresh === "function") root.app.toolchain.refresh()
    }
  }

  // ------------------------------------------------------------------ Xcode .xip
  function openXcodeDownload(pageOnly) {
    root.app.cli.call(["setup", "xip", "--open"].concat(pageOnly ? ["--page"] : []), "", function(data) {
      root.app.toast("Opened Apple's download page in your browser", "info")
    }, function(err) { root.app.toast(Model.errorMessage(err), "error") })
    root.watchXip()
  }

  function watchXip() {
    if (watcher.running) return
    root.xip = { state: "watching" }
    watcher.start(["setup", "xip", "--watch"])
  }

  function stopWatch() { watcher.stop(); root.xip = { state: "idle" } }

  function useXip(path) {
    if (!path) return
    var p = String(path).replace(/^file:\/\//, "")
    p = decodeURIComponent(p)
    if (watcher.running) watcher.stop()
    root.xip = { state: "checking", file: p.split("/").pop() }
    watcher.start(["setup", "xip", "--use", p])
  }

  Stream {
    id: watcher
    program: [root.app.helperPath]
    onEvent: function(ev) {
      switch (ev.event) {
      case "watching": root.xip = Object.assign({}, root.xip, { state: "watching", dir: ev.dir, needed: ev.needed, expected: ev.expectedSize }); break
      case "download": root.xip = Object.assign({}, root.xip, { state: "downloading", file: ev.file, size: ev.size, expected: ev.expected, fraction: ev.fraction, rate: ev.rate, eta: ev.eta }); break
      case "found": root.xip = Object.assign({}, root.xip, { state: "checking", file: ev.file, size: ev.size, fraction: -1 }); break
      case "progress": if (root.xip.state === "checking") root.xip = Object.assign({}, root.xip, { fraction: ev.fraction, detail: ev.detail, eta: ev.eta }); break
      case "rejected": root.xip = Object.assign({}, root.xip, { state: "rejected", message: ev.message, file: ev.xip ? ev.xip.name : "" }); break
      }
    }
    onFinished: function(ok, data, error) {
      if (ok) {
        root.xip = { state: "found", file: data.xip ? data.xip.name : "", version: data.xip ? data.xip.version : "", path: data.xip ? data.xip.path : "" }
        root.app.toast("Found Xcode " + (data.xip && data.xip.version ? data.xip.version : "") + ". Ready to build the iOS SDK.", "ok")
        root.app.notify("Xcode download found", "iOS Studio can build the iOS SDK now.", Model.Glyph.checkCircle)
        root.refresh()
      } else if (error && error.code !== "cancelled" && error.code !== "exited") {
        root.xip = { state: "rejected", message: Model.errorMessage(error) }
      } else if (root.xip.state !== "rejected") {
        root.xip = { state: "idle" }
      }
    }
  }

  // ------------------------------------------------------------------ lifecycle
  Connections {
    target: root.app.device
    ignoreUnknownSignals: true
    function onConnectedChanged() { if (root.loaded) { root.steps = root._merge(root.raw ? root.raw.steps : []); root._recompute() } }
    function onInfoChanged() { if (root.loaded) { root.steps = root._merge(root.raw ? root.raw.steps : []); root._recompute() } }
  }

  // Re-check now and then while the window has focus, so a step finished elsewhere (browser
  // download, sign-in) shows up. A check runs ~17 commands (~0.8 s CPU), so not in the
  // background: coming back to the window re-checks if the last check is a minute old.
  readonly property int recheckInterval: 60000
  Timer {
    interval: root.recheckInterval; repeat: true
    running: !root.installing && root.app.windowFocused
    onTriggered: root.refresh()
  }
  Connections {
    target: root.app
    function onWindowFocusedChanged() {
      if (root.app.windowFocused && !root.installing && root.loaded && Date.now() - root.checkedAt > root.recheckInterval) root.refresh()
    }
  }

  Component.onCompleted: root.refresh()
}
