import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.components
import ".."
import "../Model.js" as Model

// Projects and builds (projects slice). Owns the one build stream, so a build keeps running
// while the user moves between pages; the Projects page only renders this state.
//
// Public surface:
//   status, summary, error, refresh()
//   items: [Project]             (helper `projects list` objects)
//   convertible, templates, history, recent (top 4 projects, for Home)
//   build: {active, kind, path, name, steps[], fraction, detail, eta, result, started}
//   diagnostics: ListModel of {n} rows, errors first; diagnostic(n) is the object · errorCount, warningCount
//   building (bool), canRun (an iPhone is connected)
//   run(p) · buildIpa(p) · compile(p) · renew(p) · reinstall(p) · cancel()
//   create(opts, onDone, onFail) · addFolder(path, …) · remove(p) · openInEditor(p, file, line, col)
//   reveal(p) · setAutoRenew(p, on) · convert(path)
//   signals: buildLog(string text, string level) · buildStarted() · buildFinished(bool ok) · diagnosticAdded(d)
Item {
  id: root
  required property var app

  property string status: "unknown"
  property var summary: ({ title: "Projects", detail: "Looking for your apps…", level: "info", badge: "" })
  property var error: null

  property var items: []
  property var convertible: []
  property var missing: []
  property var templates: []
  property string bundlePrefix: "com.example"
  property string defaultDir: ""        // where "New app" creates projects (~/Developer unless Settings says otherwise)
  property var roots: []                // folders scanned for projects (Settings, or the detected defaults)
  property var defaultRoots: []         // detected defaults: ~/Developer, ~/Projects, ~/Code, … that exist
  property var history: []
  property var autorenew: null
  readonly property var recent: items.slice(0, 4)
  property bool loadedOnce: false

  // ------------------------------------------------------------ build state
  property var build: ({ active: false })
  readonly property bool building: build.active === true
  property var logBuffer: []           // last lines of the current/last build, for a page that loads late
  property var pendingPrompt: null
  readonly property string deviceUdid: {
    var d = app.device ? app.device.current : null
    if (d && d.udid) return d.udid
    return app.setting("defaultDevice", "") || ""
  }
  // Run, reinstall and renew install on the phone: without a connection they'd build for
  // minutes and then fail. (Build and .ipa need no phone.)
  readonly property bool canRun: !!(app.device && app.device.connected && app.device.current && app.device.current.udid)

  // Diagnostics of the current/last build. The objects live in _diags (they never change once
  // added); the ListModel holds their index, errors before warnings before notes, so a new one
  // is one insert instead of a re-sorted copy of the whole list.
  ListModel { id: diagModel }
  readonly property alias diagnostics: diagModel
  property var _diags: []
  property var _rankCounts: [0, 0, 0, 0]
  property int errorCount: 0
  property int warningCount: 0
  readonly property var _severityRank: ({ error: 0, warning: 1, note: 2 })
  function diagnostic(n) { return root._diags[n] || null }
  function diagnosticList() { return root._diags.slice() }

  signal buildLog(string text, string level)
  signal buildStarted()
  signal buildFinished(bool ok)
  signal diagnosticAdded(var d)

  function refresh() {
    if (!root.loadedOnce) root.status = "loading"
    app.cli.call(["projects", "list"], "", function(data) {
      root.items = data.projects || []
      root.convertible = data.convertible || []
      root.missing = data.missing || []
      root.roots = data.roots || []
      root.defaultRoots = data.defaultRoots || []
      if (data.defaultDir) root.defaultDir = data.defaultDir
      root.loadedOnce = true
      root.error = null
      root.status = data.xtool === false ? "needs-setup" : "ready"
      root._updateSummary()
    }, function(err) {
      root.error = err
      root.status = "error"
      root._updateSummary()
    }, { timeout: 30000 })
    loadHistory()
    loadAutoRenew()
    if (root.templates.length === 0) {
      app.cli.call(["projects", "templates"], "", function(d) {
        root.templates = d.templates || []
        root.bundlePrefix = d.bundlePrefix || "com.example"
        root.defaultDir = d.defaultDir || ""
      }, function(e) {})
    }
  }

  function loadHistory() {
    app.cli.call(["build", "history", "--limit", "40"], "", function(d) { root.history = d.builds || []; root._updateSummary() }, function(e) {})
  }

  function loadAutoRenew() {
    app.cli.call(["projects", "autorenew", "--status"], "", function(d) { root.autorenew = d }, function(e) {})
  }

  function projectByPath(path) {
    for (var i = 0; i < root.items.length; i++) if (root.items[i].path === path) return root.items[i]
    return null
  }

  // ------------------------------------------------------------ summary (Home, sidebar)
  function _updateSummary() {
    var b = root.build
    if (b.active) {
      var pct = b.fraction > 0 ? " · " + Math.round(b.fraction * 100) + "%" : ""
      root.summary = { title: "Building " + b.name, detail: (b.detail || stepTitle(b.current) || "Working…") + pct, level: "info", badge: "…" }
      return
    }
    if (root.status === "error") { root.summary = { title: "Projects", detail: Model.errorMessage(root.error), level: "error", badge: "!" }; return }
    if (root.status === "needs-setup") { root.summary = { title: "Projects", detail: "Install xtool to build apps", level: "warn", badge: "" }; return }
    var expiring = 0
    for (var i = 0; i < root.items.length; i++) {
      var s = root.items[i].signing
      if (s && s.daysLeft !== null && s.daysLeft !== undefined && s.daysLeft < 2) expiring++
    }
    var last = root.history.length ? root.history[0] : null
    if (expiring > 0) {
      root.summary = { title: "Projects", detail: Model.plural(expiring, "app") + " need re-signing soon", level: "warn", badge: String(expiring) }
    } else if (last && last.result === "failed") {
      root.summary = { title: "Last build failed", detail: last.name + " · " + Model.relativeTime(last.time), level: "error", badge: "!" }
    } else if (last) {
      root.summary = { title: "Projects", detail: "Last build " + (last.result === "ok" ? "succeeded" : last.result) + " · " + Model.relativeTime(last.time), level: last.result === "ok" ? "ok" : "info", badge: "" }
    } else {
      root.summary = { title: "Projects", detail: root.items.length ? Model.plural(root.items.length, "app") : "No apps yet", level: "info", badge: "" }
    }
  }

  readonly property var _stepTitles: ({ resolve: "Resolving packages", compile: "Compiling", link: "Linking", sign: "Signing",
                                        "package": "Packaging", install: "Installing", launch: "Launching" })
  function stepTitle(id) { return root._stepTitles[id] || "" }

  // ------------------------------------------------------------ build actions
  function run(p) { _start(p, "run") }
  function buildIpa(p) { _start(p, "ipa") }
  function compile(p) { _start(p, "compile") }
  function renew(p) { _start(p, "renew") }
  function reinstall(p) { _start(p, "install") }
  function runAgain() { if (root.build.path) _start(projectByPath(root.build.path) || { path: root.build.path, name: root.build.name }, root.build.kind) }

  function _needsDevice(kind) { return kind === "run" || kind === "renew" || kind === "install" }

  function _start(p, kind) {
    if (!p || !p.path) return
    if (root.building) { app.toast("Another build is running. Wait for it or cancel it first.", "warn"); return }
    if (_needsDevice(kind) && !root.canRun) {
      app.toast("Connect your iPhone first.", "warn")
      app.nav("device")
      return
    }
    var argv
    if (kind === "renew") argv = ["projects", "renew", "--path", p.path]
    else argv = ["build", kind, "--path", p.path]
    if (_needsDevice(kind)) argv.push("--udid", root.deviceUdid)
    var cfg = app.setting("buildConfig", "debug")
    if (kind !== "install" && cfg === "release") argv.push("--config", "release")

    root.logBuffer = []
    root._cancelling = false
    _clearDiagnostics()
    root.build = {
      active: true, kind: kind, path: p.path, name: p.name || "", bundleId: p.bundleId || "",
      steps: [], current: "", fraction: 0, detail: "Starting…", eta: null,
      result: null, started: Date.now(), logFile: "", udid: _needsDevice(kind) ? root.deviceUdid : ""
    }
    root.buildStarted()
    _updateSummary()
    stream.start(argv)
  }

  property bool _cancelling: false
  function cancel() {
    if (!root.building) return
    root._cancelling = true
    _closeRevoke()
    root.pendingPrompt = null
    stream.write({ cancel: true })
    cancelKill.restart()        // the helper kills the process group; force it if it doesn't exit
  }

  function answerPrompt(value) {
    if (!root.pendingPrompt) return
    stream.write({ prompt: root.pendingPrompt.id, value: value })
    root.pendingPrompt = null
  }

  Timer { id: cancelKill; interval: 3000; onTriggered: if (stream.running) stream.stop() }

  // Wi-Fi install progress from the device daemon (app.install → installProgress events).
  Connections {
    target: root.app.device
    ignoreUnknownSignals: true
    function onDaemonEvent(name, data) {
      if (name !== "installProgress" || !root.building || !data || data.path !== root.build.ipaPath) return
      var text = data.status === "uploading" ? "Sending the app to your iPhone over Wi-Fi"
               : data.status === "done" ? "Installed" : "Installing on your iPhone… " + data.percent + "%"
      root._setStep("install", "running", text)
      root._patch({ detail: text })
    }
  }

  function _patch(fields) {
    var b = Object.assign({}, root.build)
    for (var k in fields) b[k] = fields[k]
    root.build = b
  }

  function _setStep(id, state, detail, extra) {
    var steps = root.build.steps ? root.build.steps.slice() : []
    var found = false
    for (var i = 0; i < steps.length; i++) {
      if (steps[i].id === id) {
        var s = Object.assign({}, steps[i]); s.state = state
        if (detail !== undefined && detail !== "") s.detail = detail
        if (state === "running" && !s.startedAt) s.startedAt = Date.now()
        if ((state === "done" || state === "failed") && s.startedAt) s.duration = (Date.now() - s.startedAt) / 1000
        steps[i] = s; found = true; break
      }
    }
    if (!found) steps.push(Object.assign({ id: id, title: stepTitle(id) || id, state: state, detail: detail || "" }, extra || {}))
    var patch = { steps: steps }
    if (state === "running") patch.current = id
    _patch(patch)
  }

  function _clearDiagnostics() {
    diagModel.clear()
    root._diags = []
    root._rankCounts = [0, 0, 0, 0]
    root.errorCount = 0
    root.warningCount = 0
  }

  function _addDiagnostic(d) {
    var rank = root._severityRank[d.severity]
    if (rank === undefined) rank = 3
    var n = root._diags.length
    root._diags.push(d)
    var at = 0
    for (var r = 0; r <= rank; r++) at += root._rankCounts[r]
    root._rankCounts[rank]++
    diagModel.insert(at, { n: n })
    if (d.severity === "error") root.errorCount++
    else if (d.severity === "warning") root.warningCount++
  }

  function _log(text, level) {
    var buf = root.logBuffer
    buf.push({ text: text, level: level || "" })
    if (buf.length > 4000) buf.splice(0, buf.length - 4000)
    root.buildLog(text, level || "")
  }

  Stream {
    id: stream
    program: [app.helperPath]
    onEvent: function(ev) {
      if (!root.building) return
      switch (ev.event) {
      case "build":
        root._patch({ logFile: ev.logFile || "", bundleId: ev.bundleId || root.build.bundleId, name: ev.project || root.build.name, estimate: ev.estimate })
        break
      case "step":
        root._setStep(ev.id, ev.state, ev.detail, { owner: ev.owner || "" })
        if (ev.state === "running" && ev.detail) root._patch({ detail: ev.detail })
        root._updateSummary()
        break
      case "progress":
        root._patch({ fraction: Math.max(root.build.fraction || 0, ev.fraction || 0), detail: ev.detail || root.build.detail, eta: ev.eta })
        root._updateSummary()
        break
      case "log":
        root._log(ev.text, ev.stream === "stderr" ? "" : "")
        break
      case "diagnostic":
        root._addDiagnostic(ev)
        root.diagnosticAdded(ev)
        break
      case "prompt":
        root.pendingPrompt = ev
        root._askRevoke(ev)
        break
      }
    }
    onFinished: function(ok, data, error) { root._helperDone(ok, data, error) }
    // Cancelled (stop() never reports `done`), or the helper ended without saying how: don't
    // leave the build "running" forever. Later, so a `done` or exit error from Stream goes first.
    onExited: function(code) {
      Qt.callLater(function() {
        if (!root.building || stream.running || stream.lastDone) return
        if (root._cancelling) root._helperDone(false, {}, { code: "cancelled", message: "Cancelled." })
        else root._helperDone(false, {}, { code: "exited", message: "The build stopped without reporting a result (exit code " + code + ")." })
      })
    }
  }

  // xtool wants to revoke the signing certificate. Destructive: Cancel ("Don't revoke") is the default.
  // The dialog closes by itself when the build ends first (cancelled, or the helper gave up).
  property var _revokeDialog: null
  function _askRevoke(ev) {
    app.notify("iOS Studio needs your decision", "Signing " + root.build.name + " needs a new certificate.", Model.Glyph.cert)
    _closeRevoke()
    root._revokeDialog = app.confirm({
      title: ev.title || "Replace your signing certificate?",
      message: ev.message,
      confirmText: "Revoke and continue",
      cancelText: "Don't revoke",
      destructive: true,
      glyph: Model.Glyph.cert,
      onConfirm: function() { root._revokeDialog = null; root.answerPrompt("yes") },
      onCancel: function() { root._revokeDialog = null; root.answerPrompt("no") }
    })
  }
  function _closeRevoke() {
    if (root._revokeDialog) app.closeConfirm(root._revokeDialog)
    root._revokeDialog = null
  }

  function _helperDone(ok, data, error) {
    cancelKill.stop()
    _closeRevoke()
    root.pendingPrompt = null
    if (!ok) { _finish(false, (error && error.build) || data || {}, error); return }
    // Wi-Fi: the helper built and signed the .ipa; the device daemon installs it.
    if (data.needsInstall) { _installViaDaemon(data); return }
    if (data.launch && data.launch.bundleId) { _launch(data); return }
    _finish(true, data, null)
  }

  function _daemonAvailable() { return app.device && typeof app.device.request === "function" }

  function _installViaDaemon(data) {
    _setStep("install", "running", "Sending the app to your iPhone over Wi-Fi")
    _patch({ detail: "Sending the app to your iPhone over Wi-Fi" })
    if (!_daemonAvailable()) {
      _setStep("install", "failed")
      _finish(false, data, { code: "no-daemon", message: "The iPhone service isn't running, so the app was built but not installed.", fix: { label: "Open iPhone", page: "device" } })
      return
    }
    _patch({ ipaPath: data.ipa })
    app.device.request("app.install", { path: data.ipa, udid: data.udid }, function(ok, res) {
      if (!root.building) return
      if (!ok) {
        _setStep("install", "failed")
        var err = res || {}
        if (err.code === "bad-args" && /Unknown command/.test(err.message || ""))   // an old daemon still running
          err = { code: "wifi-install-unavailable", message: "The iPhone service needs a restart to install over Wi-Fi. Reopen iOS Studio and press Run again.", fix: { label: "Open iPhone", page: "device" } }
        _finish(false, data, err)
        return
      }
      _setStep("install", "done")
      app.cli.call(["projects", "record-install", "--path", data.path, "--udid", data.udid, "--ipa", data.ipa, "--via", "wifi"], "", function(r) {}, function(e) {})
      data.installed = true
      if (data.launch && data.launch.bundleId) _launch(data)
      else _finish(true, data, null)
    })
  }

  // Free teams install apps as XTL-<TEAM>.<bundleID>; find the real id on the phone first.
  function _launch(data) {
    _setStep("launch", "running", "Opening the app on your iPhone")
    _patch({ detail: "Opening the app on your iPhone" })
    if (!_daemonAvailable()) {
      _setStep("launch", "done")
      _finish(true, Object.assign({}, data, { launchNote: "Installed. Open it on your iPhone." }), null)
      return
    }
    var want = data.launch.bundleId
    app.device.request("apps.list", { udid: data.udid, icons: false, sizes: false }, function(ok, apps) {
      var bid = want
      if (ok && apps && apps.length) {
        for (var i = 0; i < apps.length; i++) {
          var a = apps[i].bundleId || ""
          if (a === want || a.slice(-(want.length + 1)) === "." + want) { bid = a; break }
        }
      }
      // console: its print() output shows on Logs › Console (falls back to a plain launch if unavailable)
      app.device.request("app.launch", { bundleId: bid, udid: data.udid, console: true }, function(ok2, res) {
        if (!root.building) return
        if (ok2) {
          _setStep("launch", "done")
          _finish(true, data, null)
        } else {
          _setStep("launch", "failed")
          // Installed fine; only the launch failed. Keep the build green, explain the launch.
          _finish(true, Object.assign({}, data, { launchError: res }), null)
        }
      })
    })
  }

  function _finish(ok, data, error) {
    var b = Object.assign({}, root.build)
    var steps = (b.steps || []).map(function(s) {
      if (s.state === "running") return Object.assign({}, s, { state: ok ? "done" : "failed" })
      if (!ok && s.state === "pending") return Object.assign({}, s, { state: "skipped" })
      return s
    })
    b.steps = steps
    b.active = false
    b.fraction = ok ? 1 : b.fraction
    b.detail = ok ? "Done" : (error ? error.message : "Stopped")
    b.result = { ok: ok, data: data || {}, error: error, duration: data && data.duration ? data.duration : (Date.now() - b.started) / 1000 }
    if (data && data.diagnostics && data.diagnostics.length > root._diags.length) {
      _clearDiagnostics()
      for (var i = 0; i < data.diagnostics.length; i++) _addDiagnostic(data.diagnostics[i])
    }
    root._cancelling = false
    root.build = b
    root.buildFinished(ok)

    var cancelled = error && error.code === "cancelled"
    if (!cancelled) {
      var what = b.kind === "run" ? "is running on your iPhone" : b.kind === "renew" ? "was re-signed for another week"
               : b.kind === "ipa" ? ".ipa is ready" : b.kind === "install" ? "was reinstalled" : "built successfully"
      if (ok) {
        if (data && data.launchError) app.toast(b.name + " is installed, but didn't open: " + Model.errorMessage(data.launchError), "warn")
        else app.toast(b.name + " " + what, "ok")
        app.notify(b.name + " " + what, "Finished in " + Model.formatDuration(b.result.duration), Model.Glyph.checkCircle)
      } else {
        app.notify("Build failed: " + b.name, error ? error.message : "", Model.Glyph.error)
      }
    }
    refresh()
  }

  // ------------------------------------------------------------ project actions
  function create(opts, onDone, onFail) {
    var argv = ["projects", "new", "--name", opts.name, "--template", opts.template || "swiftui-app"]
    if (opts.dir) argv.push("--dir", opts.dir)
    if (opts.bundleId) argv.push("--bundle-id", opts.bundleId)
    app.cli.call(argv, "", function(d) { refresh(); app.toast(d.name + " is ready", "ok"); if (onDone) onDone(d) },
                 function(e) { if (onFail) onFail(e) }, { timeout: 90000 })
  }

  function addFolder(path, onDone, onFail) {
    app.cli.call(["projects", "add", "--path", path], "", function(d) { refresh(); app.toast("Added " + d.name, "ok"); if (onDone) onDone(d) },
                 function(e) { if (onFail) onFail(e) })
  }

  function convert(path, onDone, onFail) {
    app.cli.call(["projects", "convert", "--path", path], "", function(d) { refresh(); app.toast(d.name + " can now be built as an app", "ok"); if (onDone) onDone(d) },
                 function(e) { if (onFail) onFail(e); else app.toast(Model.errorMessage(e), "error") })
  }

  function pickFolder(start, onDone) {
    app.cli.call(["projects", "pick-folder"].concat(start ? ["--start", start] : []), "", function(d) { if (onDone) onDone(d) },
                 function(e) { app.toast(Model.errorMessage(e), "warn") }, { timeout: 600000 })
  }

  function remove(p) {
    app.confirm({
      title: "Remove " + p.name + " from the list?",
      message: "The project stays on disk in " + p.displayPath + ". You can add it back any time with Add existing folder.",
      confirmText: "Remove from list",
      destructive: true,
      glyph: Model.Glyph.trash,
      onConfirm: function() {
        app.cli.call(["projects", "remove", "--path", p.path], "", function() { refresh(); app.toast("Removed " + p.name + " from the list", "ok") },
                     function(e) { app.toast(Model.errorMessage(e), "error") })
      }
    })
  }

  function openInEditor(p, file, line, col) {
    var argv = ["projects", "open", "--path", p.path || p]
    if (file) argv.push("--file", file)
    if (line) argv.push("--line", String(line))
    if (col) argv.push("--col", String(col))
    app.cli.call(argv, "", function(d) { app.toast("Opening in " + d.editor, "ok") }, function(e) { app.toast(Model.errorMessage(e), "error") })
  }

  function reveal(p) {
    app.cli.call(["projects", "reveal", "--path", p.path], "", function() {}, function(e) { app.toast(Model.errorMessage(e), "error") })
  }

  function setAutoRenew(p, on) {
    var argv = ["projects", "set", "--path", p.path, "--auto-renew", on ? "true" : "false"]
    if (root.deviceUdid) argv.push("--udid", root.deviceUdid)
    app.cli.call(argv, "", function() {
      refresh()
      var timerOn = root.autorenew && root.autorenew.enabled
      if (on && !timerOn) app.toast("Auto-renew marked. Turn on automatic renewal in Settings so it runs daily.", "info")
      else app.toast(on ? "Auto-renew on for " + p.name : "Auto-renew off for " + p.name, "ok")
    }, function(e) { app.toast(Model.errorMessage(e), "error") })
  }

  function openBuildLog(file) {
    app.cli.call(["projects", "open", "--path", app.stateDir, "--file", file], "", function() {}, function(e) { app.toast(Model.errorMessage(e), "error") })
  }

  // Settings › auto-renew toggles the systemd --user timer. Only reacts to a change the user
  // makes after settings loaded (never on startup).
  property var _lastAutoRenew: undefined
  function _applyAutoRenew(on) {
    app.cli.call(["projects", "autorenew", on ? "--enable" : "--disable"], "", function(d) {
      root.autorenew = d
      app.toast(on ? "Automatic renewal is on. iOS Studio checks every day at 10:00." : "Automatic renewal is off.", "ok")
    }, function(e) { app.toast(Model.errorMessage(e), "error") })
  }
  Connections {
    target: app
    function onSettingsChanged() {
      if (!app.settingsLoaded) return
      var v = app.settings.autoRenew === true
      if (root._lastAutoRenew === undefined) { root._lastAutoRenew = v; return }
      if (v === root._lastAutoRenew) return
      root._lastAutoRenew = v
      if (!v) { root._applyAutoRenew(false); return }
      // Turning it on installs a background job: say so, and let Cancel undo the toggle.
      app.confirm({
        title: "Re-sign apps automatically?",
        message: "iOS Studio adds a small background job (a systemd user timer) that checks once a day at 10:00. "
               + "When an app you marked for auto-renew has less than " + app.setting("autoRenewDays", 2) + " days left and your iPhone is reachable, "
               + "it rebuilds and reinstalls it. It never replaces certificates on its own; if that's needed it notifies you instead.",
        confirmText: "Turn on",
        glyph: Model.Glyph.timer,
        onConfirm: function() { root._applyAutoRenew(true) },
        onCancel: function() { root._lastAutoRenew = false; app.setSetting("autoRenew", false) }
      })
    }
    function onSettingsLoadedChanged() { if (app.settingsLoaded && root._lastAutoRenew === undefined) root._lastAutoRenew = app.settings.autoRenew === true }
  }

  Component.onCompleted: Qt.callLater(refresh)
}
