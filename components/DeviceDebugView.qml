import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import ".."
import "../Model.js" as Model

// iPhone › Debug: LLDB on your own apps. Pick an app, capture a backtrace of every thread, or run a
// breakpoint session (DebugSessionPanel). The symbols card downloads the iPhone's Swift runtime and
// system libraries once per iOS build so lldb reads Swift values right and attaches quickly; it
// shows whether the set is ready, outdated, incomplete or failed and offers the one fix for each.
// Helper group `debug`, daemon `debug.symbols`.
ColumnLayout {
  id: root
  required property var app
  readonly property var svc: app.device
  property bool active: false               // the tab is on screen (DevicePage)

  // your own apps, running ones first (attaching needs a running app); rebuilt only while the tab is on
  // screen, hidden it keeps the last list (a live session keeps its app)
  property var devApps: []
  function updateDevApps() {
    root.devApps = svc.apps.filter(function(a) { return a.developer })
                           .sort(function(a, b) { return a.running === b.running ? 0 : (a.running ? -1 : 1) })
  }
  property string pick: ""
  readonly property var current: {
    for (var i = 0; i < devApps.length; i++) if (devApps[i].bundleId === pick) return devApps[i]
    return devApps.length ? devApps[0] : null
  }

  // debugger availability (`debug check`)
  property string checkStatus: "unknown"    // unknown | loading | ready | error
  property var checkError: null
  // sources of the picked app (`debug sources`)
  property var sources: ({ project: null, files: [], localBuild: null })
  // backtrace
  property string btPhase: "idle"           // idle | running | done | failed
  property string btStep: ""
  property var backtrace: null
  property var btError: null
  property var btNotices: []
  property string btApp: ""
  // symbols (`debug.symbols`)
  property var symbols: null
  property var symbolsError: null
  property var symProgress: null
  property bool symBusy: false
  readonly property var symP: symProgress
  readonly property bool symDownloading: !!(symbols && symbols.downloading) || (!!symP && (symP.phase === "downloading" || symP.phase === "extracting" || symP.phase === "starting"))
  readonly property bool symEnough: !symbols || !symbols.freeBytes || symbols.freeBytes >= (symbols.estimatedBytes || 0)
  // missing | downloading | extracting | ready | outdated | incomplete | failed
  readonly property string symState: !symbols ? "" : (symDownloading ? (symP && symP.phase === "extracting" ? "extracting" : "downloading")
                                                     : (symbols.state || (symbols.ready ? "ready" : "missing")))
  readonly property bool symCanUnpack: !!(symbols && symbols.canUnpack)
  readonly property bool symNeedsUpdate: !!(symbols && symbols.update && symbols.update.needed) && !symDownloading
  // the one fix the card offers: download | update | unpack | retry | "" (nothing to do)
  readonly property string symFix: {
    var st = root.symState
    if (st === "" || st === "ready" || st === "downloading" || st === "extracting") return ""
    if (root.symCanUnpack) return "unpack"
    return st === "missing" ? "download" : (st === "failed" ? "retry" : "update")
  }
  readonly property bool symOnDisk: !!symbols && ((symbols.sizeOnDisk || 0) > 0 || (symbols.downloadKept || 0) > 0)

  function check() {
    root.checkStatus = "loading"
    root.app.cli.call(["debug", "check"], "", function(d) {
      if (d.ready) { root.checkStatus = "ready"; root.checkError = null }
      else { root.checkStatus = "error"; root.checkError = d.error }
    }, function(e) { root.checkStatus = "error"; root.checkError = e })
  }
  function loadSources() {
    if (!root.current) return
    var bid = root.current.bundleId
    root.app.cli.call(["debug", "sources", "--bundle-id", bid], "", function(d) {
      if (root.current && root.current.bundleId === bid) root.sources = d
    }, function(e) { root.sources = { project: null, files: [], localBuild: null } })
  }
  function refreshSymbols() {
    if (!root.svc.connected) return
    root.svc.request("debug.symbols", { action: "status" }, function(ok, d) {
      if (ok) { root.symbols = d; root.symbolsError = null; root.symProgress = d.progress || null }
      else root.symbolsError = d
    })
  }
  function activate() {
    var before = root.current
    root.updateDevApps()   // a different app here loads its sources through onCurrentChanged
    if (root.checkStatus === "unknown") root.check()
    root.refreshSymbols()
    if (root.svc.appsStatus === "unknown" && root.svc.connected) root.svc.refreshApps()
    if (root.current === before) root.loadSources()
  }

  function captureBacktrace() {
    var a = root.current
    if (!a) return
    root.app.confirm({
      title: "Capture a backtrace?",
      message: "This pauses " + a.name + " for a few seconds (longer over Wi-Fi) while the debugger reads every thread, then lets it run again. Nothing else changes.",
      confirmText: "Capture", glyph: Model.Glyph.bug,
      onConfirm: function() {
        root.btPhase = "running"; root.btStep = ""; root.backtrace = null; root.btError = null; root.btNotices = []
        root.btApp = a.name
        bt.start(["debug", "backtrace", "--bundle-id", a.bundleId])
      }
    })
  }

  function symRequest(args, timeout) {
    root.symBusy = true
    root.svc.request("debug.symbols", args, function(ok, d) {
      root.symBusy = false
      if (ok) { root.symbols = d; root.symbolsError = null; root.symProgress = d.progress || null }
      else root.symbolsError = d
    }, timeout || 30000)
  }
  function downloadSymbols() {
    var s = root.symbols || {}
    var need = Model.formatBytes(s.estimatedBytes || 0)
    var update = root.symState === "outdated" || root.symState === "incomplete"
    var keep = s.keepDownload ? "keeps the download (Keep the download is on)" : "keeps only those libraries (a few hundred MB) and deletes the rest"
    root.app.confirm({
      title: update ? "Update debug symbols?" : "Download debug symbols?",
      message: "iOS Studio copies your iPhone's system libraries (about " + need + ") over the connection, unpacks the Swift runtime and the system libraries lldb needs, then " + keep + ". It needs that much free space while it runs and takes 10–30 minutes over Wi-Fi. Keep the iPhone awake and this window open."
               + (update ? " The symbols you have now keep working until the new ones are ready." : ""),
      confirmText: update ? "Update" : "Download", glyph: Model.Glyph.download,
      onConfirm: function() { root.symRequest({ action: "download", force: update }, 120000) }
    })
  }
  function unpackSymbols() { root.symRequest({ action: "unpack" }) }
  function fixSymbols() {
    if (root.symFix === "unpack") root.unpackSymbols()
    else root.downloadSymbols()
  }
  function removeSymbols() {
    var s = root.symbols || {}
    var size = (s.sizeOnDisk || 0) + (s.downloadKept || 0)
    root.app.confirm({
      title: "Remove debug symbols?",
      message: "This deletes the debug symbols for iOS " + s.ios + " (" + s.build + ")" + (s.downloadKept ? " and the kept download" : "") + ", " + Model.formatBytes(size) + " in all. Debugging still works without them, but Swift values can read wrong and attaching is slower. Getting them back means downloading again.",
      confirmText: "Remove", destructive: true, glyph: Model.Glyph.trash,
      onConfirm: function() { root.symRequest({ action: "remove" }) }
    })
  }
  function setKeepDownload(on) { root.symRequest({ action: "status", keepDownload: on }) }
  function cancelSymbols() {
    root.svc.request("debug.symbols", { action: "cancel" }, function(ok, d) { if (ok) root.symbols = d })
  }

  onActiveChanged: if (active) activate()
  Component.onCompleted: if (active) activate()
  onCurrentChanged: { root.sources = { project: null, files: [], localBuild: null }; if (root.active) root.loadSources() }
  spacing: Style.spacing.huge

  Stream {
    id: bt
    onEvent: function(ev) {
      if (ev.event === "step" && ev.state === "running") root.btStep = ev.title
      else if (ev.event === "notice") root.btNotices = root.btNotices.concat([ev])
      else if (ev.event === "backtrace") root.backtrace = ev
    }
    onFinished: function(ok, data, error) {
      if (ok) { root.btPhase = "done"; if (data.threads) root.backtrace = data }
      else { root.btPhase = "failed"; root.btError = error }
    }
  }

  Connections {
    target: root.svc
    function onDaemonEvent(name, data) {
      if (name !== "debugSymbols") return
      root.symProgress = data
      if (root.active && (data.phase === "done" || data.phase === "failed" || data.phase === "cancelled")) root.refreshSymbols()
    }
    function onConnStateChanged() { if (root.active && root.svc.connected && !root.symbols) root.refreshSymbols() }
  }
  Connections {
    target: root.svc
    enabled: root.active
    function onAppsChanged() { root.updateDevApps() }
  }

  // ---------------------------------------------------------------- debugger missing
  ErrorState {
    Layout.fillWidth: true
    visible: root.checkStatus === "error"
    app: root.app
    title: "The debugger isn't ready"
    error: root.checkError
    retryText: "Check again"
    onRetry: root.check()
  }

  // ---------------------------------------------------------------- which app
  ColumnLayout {
    Layout.fillWidth: true
    spacing: Style.spacing.md
    RowLayout {
      Layout.fillWidth: true
      spacing: Style.spacing.lg
      AppText { text: "App"; tone: "dim"; size: "small" }
      ButtonGroupLite {
        visible: root.devApps.length > 0
        enabled: bt.running === false && !session.live
        options: root.devApps.map(function(a) { return { value: a.bundleId, label: a.name + (a.running ? "" : " · not running") } })
        value: root.current ? root.current.bundleId : ""
        onPicked: function(v) { root.pick = v }
      }
      Item { Layout.fillWidth: true }
      ActionButton {
        visible: !!root.current && !root.current.running
        text: "Open on iPhone"; glyph: Model.Glyph.play; compact: true
        busy: !!root.current && root.svc.busyApps[root.current.bundleId] === "launch"
        onClicked: root.svc.launchApp(root.current)
      }
    }
    AppText {
      Layout.fillWidth: true
      text: "Only your own apps are listed: iOS lets a debugger attach only to apps built and signed for development, so App Store and system apps can't be debugged."
      size: "small"; tone: "faint"; wrapMode: Text.WordWrap
    }
  }

  // slow-attach hint: an outdated set makes every attach take minutes over Wi-Fi
  RowLayout {
    Layout.fillWidth: true
    visible: !!root.current && root.symNeedsUpdate
    spacing: Style.spacing.md
    Icon { glyph: Model.Glyph.warn; color: Theme.levelColor("warn"); Layout.alignment: Qt.AlignVCenter }
    AppText {
      Layout.fillWidth: true
      text: "Attaching may take minutes until the debug symbols are " + (root.symState === "incomplete" ? "unpacked again." : "updated.") + " The app stays paused meanwhile."
      size: "small"; tone: "warn"; wrapMode: Text.WordWrap
    }
    ActionButton {
      text: root.symFix === "unpack" ? "Unpack again" : "Update symbols"; glyph: root.symFix === "unpack" ? Model.Glyph.refresh : Model.Glyph.download
      compact: true; kind: "ghost"
      enabled: root.svc.connected && (root.symFix === "unpack" || root.symEnough)
      busy: root.symBusy
      onClicked: root.fixSymbols()
    }
  }

  Skeleton {
    Layout.fillWidth: true
    visible: root.svc.appsStatus === "loading" && root.devApps.length === 0
    lines: 2
  }
  EmptyState {
    Layout.fillWidth: true
    compact: true
    visible: root.svc.appsStatus !== "loading" && root.devApps.length === 0
    glyph: Model.Glyph.bug
    title: "None of your apps are on this iPhone"
    text: "Build and run one of your projects on the iPhone, then debug it here."
    actionText: "Open Projects"; actionGlyph: Model.Glyph.folder
    onAction: root.app.nav("projects")
  }

  // ---------------------------------------------------------------- backtrace
  ColumnLayout {
    Layout.fillWidth: true
    visible: !!root.current
    spacing: Style.spacing.lg

    SectionHeader { Layout.fillWidth: true; text: "Backtrace" }
    RowLayout {
      Layout.fillWidth: true
      spacing: Style.spacing.lg
      AppText {
        Layout.fillWidth: true
        text: root.current && root.current.running
              ? "See what every thread of " + root.current.name + " is doing right now, for a hang or a spinner that never ends."
              : (root.current ? root.current.name + " isn't running. Open it on the iPhone first." : "")
        tone: "dim"; wrapMode: Text.WordWrap
      }
      ActionButton {
        text: "Capture backtrace"; glyph: Model.Glyph.bug
        enabled: !!root.current && root.current.running && root.checkStatus !== "error" && !session.live
        busy: root.btPhase === "running"
        onClicked: root.captureBacktrace()
      }
    }
    LoadingState {
      Layout.fillWidth: true
      visible: root.btPhase === "running"
      text: root.btStep || "Starting the debugger…"
      detail: root.btApp + " is paused until this finishes."
    }
    Repeater {
      model: root.btPhase !== "idle" ? root.btNotices : []
      delegate: AppText {
        required property var modelData
        Layout.fillWidth: true
        text: modelData.message; size: "small"; wrapMode: Text.WordWrap
        tone: modelData.level === "warn" ? "warn" : "dim"
      }
    }
    ErrorState {
      Layout.fillWidth: true
      visible: root.btPhase === "failed"
      app: root.app
      error: root.btError
      compact: true
      onRetry: root.captureBacktrace()
    }
    DebugBacktraceView {
      Layout.fillWidth: true
      visible: root.btPhase === "done" && !!root.backtrace
      app: root.app
      backtrace: root.backtrace
    }
  }

  // ---------------------------------------------------------------- breakpoints
  DebugSessionPanel {
    id: session
    Layout.fillWidth: true
    visible: !!root.current
    app: root.app
    bundleId: root.current ? root.current.bundleId : ""
    appName: root.current ? root.current.name : ""
    appRunning: !!root.current && root.current.running
    sources: root.sources.files || []
    hasLocalBuild: !root.sources.project || !!root.sources.localBuild
    blocked: root.btPhase === "running" || root.checkStatus === "error"
  }

  // ---------------------------------------------------------------- symbols
  Card {
    id: symCard
    Layout.fillWidth: true
    title: "Debug symbols"
    glyph: Model.Glyph.layers
    glyphColor: root.symState === "ready" ? Theme.success : Theme.dim
    subtitle: {
      var s = root.symbols
      if (!s) return "Checking…"
      var v = s.ios ? "iOS " + s.ios + " (" + s.build + ")" : ""
      if (!s.sizeOnDisk) return v
      return v + " · " + Model.formatBytes(s.sizeOnDisk) + (s.libs ? " · " + s.libs + " libraries" : "")
    }
    actions: [
      StatusPill {
        visible: root.symState !== ""
        level: ({ ready: "ok", outdated: "warn", incomplete: "warn", failed: "error", downloading: "busy", extracting: "busy" })[root.symState] || "info"
        text: ({ ready: "Ready", outdated: "Needs an update", incomplete: "Incomplete", failed: "Failed",
                 downloading: "Downloading", extracting: "Unpacking", missing: "Not downloaded" })[root.symState] || ""
      }
    ]

    // what this state means
    AppText {
      Layout.fillWidth: true
      visible: root.symState === "ready"
      text: "lldb uses them by itself for this iOS version. A phone on another iOS version needs its own download."
      size: "small"; tone: "dim"; wrapMode: Text.WordWrap
    }
    AppText {
      Layout.fillWidth: true
      visible: root.symState === "missing"
      text: "Optional, once per iOS version. Without them, backtraces and breakpoints still work, but Swift values such as String and Array can read wrong, system frames show only library names and attaching is slow."
      size: "small"; tone: "dim"; wrapMode: Text.WordWrap
    }
    AppText {
      Layout.fillWidth: true
      visible: root.symNeedsUpdate
      text: (root.symbols && root.symbols.update && root.symbols.update.reason ? root.symbols.update.reason : "")
            + (root.symCanUnpack ? " The download is kept, so unpacking again takes about a minute." : " Updating downloads them again; the ones you have keep working meanwhile.")
      size: "small"; tone: "warn"; wrapMode: Text.WordWrap
    }
    AppText {
      Layout.fillWidth: true
      visible: !root.symDownloading && !!root.symbols && !!root.symbols.error
      text: root.symbols && root.symbols.error ? root.symbols.error.message : ""
      size: "small"; tone: "error"; wrapMode: Text.WordWrap
    }
    AppText {
      Layout.fillWidth: true
      visible: !!(root.symP && root.symP.phase === "cancelled") && !root.symDownloading
      text: root.symP ? (root.symP.message || "") : ""
      size: "small"; tone: "dim"; wrapMode: Text.WordWrap
    }

    ProgressTrack {
      Layout.fillWidth: true
      visible: root.symDownloading
      value: root.symP && root.symP.fraction !== undefined && root.symP.fraction !== null ? root.symP.fraction : 0
      indeterminate: !root.symP || root.symP.fraction === null || root.symP.fraction === undefined
      label: root.symP ? (root.symP.message || "") : "Starting…"
      trailing: root.symP && root.symP.phase === "downloading" && root.symP.total
                ? Model.formatBytes(root.symP.received || 0) + " of " + Model.formatBytes(root.symP.total) + (root.symP.eta ? " · " + Model.formatDuration(root.symP.eta) + " left" : "")
                : ""
    }
    AppText {
      Layout.fillWidth: true
      visible: root.symFix !== "" && root.symFix !== "unpack" && !root.symEnough
      text: "Needs about " + Model.formatBytes(root.symbols ? root.symbols.estimatedBytes : 0) + " free; this computer has " + Model.formatBytes(root.symbols ? root.symbols.freeBytes : 0) + "."
      size: "small"; tone: "warn"; wrapMode: Text.WordWrap
    }
    ErrorState {
      Layout.fillWidth: true
      visible: !!root.symbolsError
      app: root.app
      error: root.symbolsError
      banner: true
      onRetry: root.refreshSymbols()
    }

    // actions: the one fix for this state, Remove, Cancel while running
    RowLayout {
      Layout.fillWidth: true
      spacing: Style.spacing.md
      visible: !!root.symbols && (root.symFix !== "" || root.symDownloading || root.symOnDisk)
      AppText {
        Layout.fillWidth: true
        text: {
          var s = root.symbols
          if (!s || root.symDownloading) return ""
          if (root.symFix === "unpack") return "Unpacks the kept download · no download needed"
          if (root.symFix !== "") return "About " + Model.formatBytes(s.estimatedBytes || 0) + " to download · " + Model.formatBytes(s.freeBytes || 0) + " free"
          return ""
        }
        size: "small"; tone: "faint"; elide: Text.ElideRight
      }
      ActionButton {
        visible: root.symOnDisk && !root.symDownloading
        text: "Remove"; glyph: Model.Glyph.trash; kind: "ghost"
        enabled: !root.symBusy
        onClicked: root.removeSymbols()
      }
      ActionButton {
        visible: root.symFix !== "" && !root.symDownloading
        kind: "primary"
        text: ({ download: "Download debug symbols", update: "Update symbols", unpack: "Unpack again", retry: "Try again" })[root.symFix] || ""
        glyph: root.symFix === "unpack" || root.symFix === "retry" ? Model.Glyph.refresh : Model.Glyph.download
        enabled: root.svc.connected && (root.symFix === "unpack" || root.symEnough)
        busy: root.symBusy
        onClicked: root.fixSymbols()
      }
      ActionButton {
        visible: root.symDownloading
        text: "Cancel"; glyph: Model.Glyph.close; kind: "ghost"
        onClicked: root.cancelSymbols()
      }
    }

    // keep the raw download for a later "Unpack again"
    RowLayout {
      Layout.fillWidth: true
      visible: !!root.symbols
      spacing: Style.spacing.md
      ToggleSwitch {
        // track lines up with the text above it (the cursor-ring margin sits outside)
        Layout.leftMargin: cursorRing ? -cursorPad : 0
        checked: !!(root.symbols && root.symbols.keepDownload)
        enabled: !root.symBusy
        onToggled: root.setKeepDownload(!checked)
      }
      AppText {
        Layout.fillWidth: true
        text: {
          var s = root.symbols || {}
          var kept = s.downloadKept || 0
          return "Keep the download so symbols can be unpacked again without downloading"
                 + (kept ? " · " + Model.formatBytes(kept) + " kept" : " · about " + Model.formatBytes(s.estimatedBytes || 0))
        }
        size: "small"; tone: "dim"; wrapMode: Text.WordWrap
        TapHandler { onTapped: if (!root.symBusy) root.setKeepDownload(!(root.symbols && root.symbols.keepDownload)) }
      }
    }
  }
}
