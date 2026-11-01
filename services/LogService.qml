import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.components
import ".."
import "../Model.js" as Model

// Logs slice: live device log (syslog over the device daemon), crash reports
// and build logs (from the projects slice's build history).
//
//   app.logs.status / summary / refresh()
//   Syslog:  streamWanted, viewVisible (set by LogsPage), keepStreaming, streaming,
//            processFilter, levelFilter, start(), stop(), signal entries(batch), signal cleared()
//   Crashes: crashes, crashStatus, crashError, newCrashCount, summaryFor(item), pullCrash(item, cb),
//            refreshCrashes(), beginCrashVisit(), isNew(item), isOwn(item)
//   Builds:  builds, refreshBuilds(), loadBuildLog(entry, cb), parseDiagnostics(text), rebuild(entry)
//   Files:   copyText(text), copyFile(path), writeFile(path, text, cb), downloadsDir
Item {
  id: root
  required property var app

  // ------------------------------------------------------------ public surface
  property string status: "loading"
  property var summary: _summary()
  property var error: null

  readonly property var dev: app.device
  readonly property bool deviceApi: !!dev && typeof dev.request === "function"
  readonly property bool deviceNeedsSetup: deviceApi && dev.status === "needs-setup"
  readonly property bool deviceConnected: deviceApi && dev.connected === true
  readonly property string deviceName: deviceApi && dev.current && dev.current.name ? dev.current.name : "your iPhone"
  readonly property string downloadsDir: app.home + "/Downloads"

  function refresh() {
    refreshCrashes()
    refreshBuilds()
  }

  // ============================================================ device log
  property bool streamWanted: true          // the Start / Stop button
  property bool viewVisible: false          // LogsPage sets this while the Device log tab is on screen
  readonly property bool keepStreaming: app.setting("logsKeepStreaming", false) === true
  readonly property bool shouldStream: streamWanted && deviceConnected && (viewVisible || keepStreaming)

  property bool streaming: false            // the daemon confirmed a running stream
  property bool starting: false
  property bool stopping: false
  property bool reconnecting: false
  property int dropped: 0
  property var syslogError: null
  property double streamSince: 0

  // "" = every process, "__mine" = all of the user's apps, else one executable name
  property string processFilter: app.setting("logsProcess", "__auto")
  // minimum level: debug < info < default < error < fault
  property string levelFilter: app.setting("logsLevel", "default")

  readonly property var levels: ["debug", "info", "default", "error", "fault"]
  function levelRank(lv) { var i = levels.indexOf(String(lv)); return i < 0 ? 2 : i }

  signal entries(var batch)
  signal cleared()

  // Executables of the apps the user builds: projects first, then dev-signed apps on the phone.
  readonly property var ownApps: {
    var out = [], seen = {}
    var items = app.projects && app.projects.items ? app.projects.items : []
    for (var i = 0; i < items.length; i++) {
      var p = items[i], exe = p.product || p.name
      if (exe && !seen[exe]) { seen[exe] = true; out.push({ process: exe, name: p.name || exe, bundleId: p.bundleId || "" }) }
    }
    var apps = deviceApi && dev.apps ? dev.apps : []
    for (var j = 0; j < apps.length; j++) {
      var a = apps[j]
      if (a.developer && a.executable && !seen[a.executable]) { seen[a.executable] = true; out.push({ process: a.executable, name: a.name || a.executable, bundleId: a.bundleId || "" }) }
    }
    return out
  }

  // "__auto" (never chosen) resolves to the user's first app, so the first stream isn't the
  // phone's 2000 lines/s firehose.
  readonly property string effectiveProcess: {
    if (processFilter !== "__auto") return processFilter
    return ownApps.length > 1 ? "__mine" : (ownApps.length ? ownApps[0].process : "")
  }

  readonly property var processList: {
    var p = effectiveProcess
    if (p === "") return []
    if (p === "__mine") return ownApps.map(function(a) { return a.process })
    return [p]
  }

  readonly property var filters: {
    var f = { minLevel: levelFilter }
    if (processList.length) f.processes = processList
    return f
  }

  function setProcess(v) { root.processFilter = v; app.setSetting("logsProcess", v) }
  function setLevel(v) { root.levelFilter = v; app.setSetting("logsLevel", v) }
  function setKeepStreaming(on) { app.setSetting("logsKeepStreaming", !!on) }

  function start() { root.syslogError = null; root.streamWanted = true; _sync() }
  function stop() { root.streamWanted = false; _sync() }
  function clear() { root.cleared() }

  property bool _syncQueued: false
  function _queueSync() { if (!_syncQueued) { _syncQueued = true; Qt.callLater(function() { root._syncQueued = false; root._sync() }) } }

  onShouldStreamChanged: _queueSync()
  onFiltersChanged: {
    if (root.streaming && deviceApi) dev.request("syslog.filter", { filters: root.filters }, function(ok, d) {
      if (!ok) root.app.toast(Model.errorMessage(d), "warn")
    })
  }

  function _sync() {
    if (!deviceApi) return
    if (root.shouldStream && !root.streaming && !root.starting) {
      root.starting = true
      dev.request("syslog.start", { filters: root.filters }, function(ok, d) {
        root.starting = false
        if (ok) {
          root.streaming = true
          root.streamSince = Date.now()
          root.dropped = 0
          root.syslogError = null
          // filters may have changed while we were starting
          if (JSON.stringify(d && d.filters ? d.filters : {}) !== JSON.stringify(root.filters))
            dev.request("syslog.filter", { filters: root.filters }, null)
        } else {
          root.streaming = false
          root.syslogError = d
          root.streamWanted = false     // don't hammer a failing phone; the user retries
        }
        if (!root.shouldStream) root._queueSync()
      })
    } else if (!root.shouldStream && (root.streaming || root.starting) && !root.stopping) {
      root.stopping = true
      dev.request("syslog.stop", {}, function() {
        root.stopping = false
        root.streaming = false
        root.reconnecting = false
        if (root.shouldStream) root._queueSync()
      })
    }
  }

  // ============================================================ crashes
  property var crashes: []                 // CrashItem[] newest first
  property string crashStatus: "unknown"   // unknown | loading | ready | error
  property var crashError: null
  property double crashesLoadedAt: 0
  property var summaries: ({})             // name -> CrashSummary
  property var localPaths: ({})            // name -> pulled file
  property var pulling: ({})               // name -> true

  // persisted (stateDir/logs.json)
  property double seenTs: 0                // crashes newer than this are "new"
  property double visitBaseline: -1        // seenTs when the Crashes tab was opened (keeps row badges during a visit)
  property var notified: []
  property bool stateLoaded: false

  readonly property int newCrashCount: {
    var n = 0
    for (var i = 0; i < crashes.length; i++) if (crashes[i].ts > seenTs && isOwn(crashes[i])) n++
    return n
  }

  function isOwn(c) {
    if (!c) return false
    for (var i = 0; i < ownApps.length; i++) {
      if (c.process === ownApps[i].process) return true
      var s = summaries[c.name]
      if (s && s.bundleId && ownApps[i].bundleId && String(s.bundleId).indexOf(ownApps[i].bundleId) >= 0) return true
    }
    return false
  }

  function isNew(c) {
    var base = visitBaseline >= 0 ? visitBaseline : seenTs
    return !!c && c.ts > base
  }

  function summaryFor(c) { return c ? (summaries[c.name] || null) : null }

  function kindLabel(kind) {
    switch (kind) {
    case "crash": return "Crash"
    case "fault": return "User fault"
    case "resource": return "Resource limit"
    case "jetsam": return "Out of memory"
    case "hang": return "Hang"
    default: return "Report"
    }
  }

  // Short text for list rows: "EXC_CRASH · SIGABRT", else the kind.
  function exceptionText(c) {
    var s = summaryFor(c)
    if (s && s.exceptionType) return s.exceptionType + (s.signal ? " · " + s.signal : "")
    return kindLabel(c ? c.kind : "")
  }

  property var _knownNames: null            // names seen this session (null until the first list)

  function refreshCrashes(quiet) {
    if (!deviceApi || !deviceConnected) return
    if (root.crashStatus === "loading") return
    if (!quiet || root.crashes.length === 0) root.crashStatus = "loading"
    dev.request("crash.list", { flush: true }, function(ok, d) {
      root.crashesLoadedAt = Date.now()
      if (!ok) {
        root.crashError = d
        root.crashStatus = root.crashes.length ? "ready" : "error"
        if (!quiet && root.crashes.length) root.app.toast(Model.errorMessage(d), "warn")
        return
      }
      var list = (d || []).slice().sort(function(a, b) { return b.ts - a.ts })
      root.crashError = null
      root.crashes = list
      root.crashStatus = "ready"
      root._announce(list)
      root._pullQueue()
    })
  }

  function _announce(list) {
    var first = root._knownNames === null
    var known = root._knownNames || {}
    var next = {}
    var fresh = []
    for (var i = 0; i < list.length; i++) {
      next[list[i].name] = true
      if (!first && !known[list[i].name] && isOwn(list[i]) && root.notified.indexOf(list[i].name) < 0) fresh.push(list[i])
    }
    root._knownNames = next
    if (!fresh.length) return
    var c = fresh[0]
    var title = fresh.length === 1 ? c.process + " crashed on your iPhone" : fresh.length + " new crashes on your iPhone"
    if (root.app.page !== "logs") root.app.toast(title, "warn")
    root.app.notify(title, "Open iOS Studio › Logs › Crashes to see what happened.", Model.Glyph.crash)
    root.notified = root.notified.concat(fresh.map(function(x) { return x.name })).slice(-200)
    _saveState()
  }

  // Pull summaries in the background (one at a time) for the user's crashes and the newest few.
  function _pullQueue() {
    for (var k in root.pulling) if (root.pulling[k]) return
    var budget = 0
    for (var i = 0; i < root.crashes.length; i++) {
      var c = root.crashes[i]
      if (root.summaries[c.name]) continue
      if (!(isOwn(c) || (i < 20 && (c.kind === "crash" || c.kind === "fault")))) continue
      pullCrash(c, function() { root._pullQueue() }, true)
      return
    }
  }

  function pullCrash(c, cb, background) {
    if (!c || !deviceApi) { if (cb) cb(false, null); return }
    if (root.summaries[c.name] && root.localPaths[c.name]) { if (cb) cb(true, { path: root.localPaths[c.name], summary: root.summaries[c.name] }); return }
    var p = Object.assign({}, root.pulling); p[c.name] = true; root.pulling = p
    dev.request("crash.pull", { name: c.name }, function(ok, d) {
      var q = Object.assign({}, root.pulling); delete q[c.name]; root.pulling = q
      if (ok && d) {
        var s = Object.assign({}, root.summaries); s[c.name] = d.summary || {}; root.summaries = s
        var l = Object.assign({}, root.localPaths); l[c.name] = d.path; root.localPaths = l
        root._saveStateSoon()
      } else if (!background) {
        root.app.toast(Model.errorMessage(d), "warn")
      }
      if (cb) cb(ok, d)
    })
  }

  // Called when the Crashes tab opens: rows keep their "New" badge for this visit, the
  // sidebar badge clears.
  function beginCrashVisit() {
    root.visitBaseline = root.seenTs
    markCrashesSeen()
    if (Date.now() - root.crashesLoadedAt > 60000) refreshCrashes(true)
  }
  function endCrashVisit() { root.visitBaseline = -1; markCrashesSeen() }

  function markCrashesSeen() {
    var newest = root.seenTs
    for (var i = 0; i < root.crashes.length; i++) newest = Math.max(newest, root.crashes[i].ts)
    if (newest !== root.seenTs) { root.seenTs = newest; _saveState() }
  }

  // Poll for new crashes while connected (crash.list is a cheap read-only listing).
  Timer {
    interval: 5 * 60 * 1000; repeat: true
    running: root.deviceConnected
    onTriggered: root.refreshCrashes(true)
  }
  onDeviceConnectedChanged: {
    if (deviceConnected) Qt.callLater(function() { root.refreshCrashes(true) })
    else { root.streaming = false; root.starting = false; root.reconnecting = false }
  }

  // ---- persisted state
  FileView {
    id: stateFile
    path: root.app.stateDir + "/logs.json"
    atomicWrites: true
    printErrors: false
    onLoaded: {
      try {
        var s = JSON.parse(text() || "{}")
        root.seenTs = Number(s.seenTs) || 0
        root.notified = s.notified || []
        root.summaries = Object.assign({}, s.summaries || {}, root.summaries)
        root.localPaths = Object.assign({}, s.paths || {}, root.localPaths)
      } catch (e) {}
      root.stateLoaded = true
    }
    onLoadFailed: {
      // First run: treat the last day as "new", so a fresh crash still shows up.
      root.seenTs = Date.now() / 1000 - 86400
      root.stateLoaded = true
      root._saveState()
    }
  }

  Timer { id: saveSoon; interval: 1500; onTriggered: root._saveState() }
  function _saveStateSoon() { saveSoon.restart() }

  function _saveState() {
    if (!root.stateLoaded) return
    // keep only the summaries of crashes still on the phone (or the last 150)
    var keep = {}, paths = {}, names = root.crashes.map(function(c) { return c.name })
    var all = Object.keys(root.summaries)
    for (var i = 0; i < all.length; i++) {
      var n = all[i]
      if (names.length && names.indexOf(n) < 0) continue
      keep[n] = root.summaries[n]
      if (root.localPaths[n]) paths[n] = root.localPaths[n]
    }
    stateFile.setText(JSON.stringify({ seenTs: root.seenTs, notified: root.notified, summaries: keep, paths: paths }) + "\n")
  }

  // ============================================================ builds
  readonly property var builds: app.projects && app.projects.history ? app.projects.history : _ownHistory
  property var _ownHistory: []

  function refreshBuilds() {
    if (app.projects && typeof app.projects.loadHistory === "function") { app.projects.loadHistory(); return }
    app.cli.call(["build", "history", "--limit", "60"], "", function(d) { root._ownHistory = d.builds || [] }, function(e) {})
  }

  function buildKindLabel(kind) {
    switch (kind) {
    case "run": return "Run on iPhone"
    case "ipa": return "Build .ipa"
    case "compile": return "Build"
    case "renew": return "Re-sign"
    case "install": return "Reinstall"
    default: return kind || "Build"
    }
  }

  // cb(ok, {text, file, truncated} | error)
  function loadBuildLog(entry, cb) {
    if (!entry || !entry.logFile) { cb(false, { code: "not-found", message: "This build has no saved log." }); return }
    app.cli.call(["build", "log", "--file", entry.logFile], "", function(d) { cb(true, d) }, function(e) { cb(false, e) })
  }

  // Same shape as the projects helper's DIAG regex.
  function parseDiagnostics(text) {
    var out = [], seen = {}
    var re = /^(\/[^:\n]+?):(\d+):(?:(\d+):)?\s*(error|warning|note|fatal error):\s*(.+)$/gm
    var m
    var s = String(text || "").replace(/\x1b\[[0-9;?]*[ -\/]*[@-~]/g, "")
    while ((m = re.exec(s)) !== null && out.length < 300) {
      var sev = m[4] === "fatal error" ? "error" : m[4]
      if (sev === "note") continue
      var msg = m[5].replace(/\s*\[#\w+\]$/, "").trim()
      var key = m[1] + ":" + m[2] + ":" + m[3] + sev + msg
      if (seen[key]) continue
      seen[key] = true
      out.push({ file: m[1], line: Number(m[2]), col: Number(m[3] || 0), severity: sev, message: msg })
    }
    out.sort(function(a, b) { return (a.severity === "error" ? 0 : 1) - (b.severity === "error" ? 0 : 1) })
    return out
  }

  function projectFor(entry) {
    if (!entry || !app.projects) return null
    if (typeof app.projects.projectByPath === "function") return app.projects.projectByPath(entry.path)
    return null
  }

  function rebuild(entry) {
    var ps = app.projects
    if (!ps || !entry) return
    var p = projectFor(entry) || { path: entry.path, name: entry.name || entry.project }
    var k = entry.kind
    if (k === "run" && ps.run) ps.run(p)
    else if (k === "ipa" && ps.buildIpa) ps.buildIpa(p)
    else if (k === "renew" && ps.renew) ps.renew(p)
    else if (k === "install" && ps.reinstall) ps.reinstall(p)
    else if (ps.compile) ps.compile(p)
    app.nav("projects")
  }

  // ============================================================ daemon events
  Connections {
    target: root.deviceApi ? root.dev : null
    ignoreUnknownSignals: true
    function onDaemonEvent(name, data) {
      if (name === "syslog") {
        if (root.streaming && data && data.length) root.entries(data)
      } else if (name === "syslogState") {
        if (!data) return
        if (data.running === false && !root.starting) root.streaming = false
        root.reconnecting = data.reconnecting === true
        if (data.dropped !== undefined) root.dropped = data.dropped
        if (data.error) root.syslogError = data.error
      } else if (name === "ready") {
        // daemon (re)started: any stream we had is gone
        root.streaming = false; root.starting = false; root.stopping = false
        root._queueSync()
      }
    }
    function onDaemonReadyChanged() { if (!root.dev.daemonReady) { root.streaming = false; root.starting = false; root.stopping = false } }
  }

  // ============================================================ files / clipboard
  Component {
    id: pipeComp
    Process {
      id: pp
      property string payload: ""
      property var cb: null
      onStarted: { if (pp.payload !== "") pp.write(pp.payload); pp.payload = ""; pp.stdinEnabled = false }
      onExited: function(code) { if (pp.cb) { try { pp.cb(code === 0) } catch (e) {} } pp.destroy() }
    }
  }

  function _pipe(argv, input, cb) {
    var p = pipeComp.createObject(root, { payload: String(input || ""), cb: cb || null })
    if (!p) { if (cb) cb(false); return }
    p.command = argv
    p.stdinEnabled = true
    p.running = true
  }

  // Large texts go over stdin (argv is limited to 128 KB per argument).
  function copyText(text, cb) { _pipe(["wl-copy"], text, cb) }
  function copyFile(path, cb) { _pipe(["sh", "-c", 'wl-copy < "$1"', "sh", path], "", cb) }
  function writeFile(path, text, cb) {
    _pipe(["sh", "-c", 'mkdir -p "$(dirname "$1")" && cat > "$1"', "sh", path], text, cb)
  }
  function saveCopy(src, dest, cb) {
    _pipe(["sh", "-c", 'mkdir -p "$(dirname "$2")" && cp -f "$1" "$2"', "sh", src, dest], "", cb)
  }

  function stamp() {
    var d = new Date()
    function z(n) { return (n < 10 ? "0" : "") + n }
    return d.getFullYear() + z(d.getMonth() + 1) + z(d.getDate()) + "-" + z(d.getHours()) + z(d.getMinutes()) + z(d.getSeconds())
  }

  function prettyPath(p) { return String(p).indexOf(app.home + "/") === 0 ? "~" + String(p).substring(app.home.length) : String(p) }

  // ============================================================ status / summary
  function _summary() {
    var n = newCrashCount
    if (n > 0) return { title: "Logs", detail: n === 1 ? "1 new crash" : n + " new crashes", level: "warn", badge: String(n) }
    if (streaming) return { title: "Logs", detail: "Streaming " + deviceName + "'s log", level: "info", badge: "" }
    if (crashStatus === "ready") return { title: "Logs", detail: "No new crashes", level: "ok", badge: "" }
    return { title: "Logs", detail: deviceConnected ? "Device log ready" : "Connect your iPhone to see its log", level: "info", badge: "" }
  }

  Binding {
    target: root; property: "status"
    value: !root.deviceApi ? "loading" : (root.deviceNeedsSetup ? "needs-setup" : (root.crashStatus === "error" && !root.crashes.length ? "error" : "ready"))
  }
  Binding { target: root; property: "error"; value: root.deviceNeedsSetup ? root.dev.error : root.crashError }

  Component.onCompleted: Qt.callLater(refreshBuilds)
}
