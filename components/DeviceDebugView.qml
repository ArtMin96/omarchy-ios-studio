import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import ".."
import "../Model.js" as Model

// iPhone › Debug: LLDB on your own apps. Pick an app, capture a backtrace of every thread, or run a
// breakpoint session (DebugSessionPanel). The symbols card downloads the iPhone's Swift runtime once
// so lldb reads Swift values right. Helper group `debug`, daemon `debug.symbols`.
ColumnLayout {
  id: root
  required property var app
  readonly property var svc: app.device

  // your own apps, running ones first (attaching needs a running app)
  readonly property var devApps: svc.apps.filter(function(a) { return a.developer })
                                         .sort(function(a, b) { return a.running === b.running ? 0 : (a.running ? -1 : 1) })
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
    if (root.checkStatus === "unknown") root.check()
    root.refreshSymbols()
    if (root.svc.appsStatus === "unknown" && root.svc.connected) root.svc.refreshApps()
    root.loadSources()
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

  function downloadSymbols() {
    var s = root.symbols || {}
    var need = s.estimatedBytes || 0
    root.app.confirm({
      title: "Download debug symbols?",
      message: "iOS Studio copies your iPhone's system libraries (about " + Model.formatBytes(need) + ") over the connection, keeps only the Swift runtime (a few hundred MB) and deletes the rest. It needs that much free space while it runs, and takes 10–30 minutes over Wi-Fi. Keep the iPhone awake and this window open.",
      confirmText: "Download", glyph: Model.Glyph.download,
      onConfirm: function() {
        root.symBusy = true
        root.svc.request("debug.symbols", { action: "download" }, function(ok, d) {
          root.symBusy = false
          if (ok) { root.symbols = d; root.symbolsError = null }
          else root.symbolsError = d
        }, 120000)
      }
    })
  }
  function cancelSymbols() {
    root.svc.request("debug.symbols", { action: "cancel" }, function(ok, d) { if (ok) root.symbols = d })
  }

  onVisibleChanged: if (visible) root.activate()
  onCurrentChanged: { root.sources = { project: null, files: [], localBuild: null }; root.loadSources() }
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
      if (data.phase === "done" || data.phase === "failed" || data.phase === "cancelled") root.refreshSymbols()
    }
    function onConnStateChanged() { if (root.svc.connected && !root.symbols) root.refreshSymbols() }
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
    Layout.fillWidth: true
    title: "Debug symbols"
    glyph: Model.Glyph.layers
    glyphColor: root.symbols && root.symbols.ready ? Theme.success : Theme.dim
    subtitle: !root.symbols ? "Checking…"
              : (root.symbols.ready ? "Ready for iOS " + root.symbols.ios + " (" + root.symbols.build + ")" + (root.symbols.sizeOnDisk ? " · " + Model.formatBytes(root.symbols.sizeOnDisk) : "")
                 : (root.symbols.ios ? "Not downloaded for iOS " + root.symbols.ios + " (" + root.symbols.build + ")" : "Not downloaded"))


    AppText {
      Layout.fillWidth: true
      visible: !!(root.symbols && root.symbols.ready)
      text: "lldb uses them by itself for this iOS version. A phone on another iOS version needs its own download."
      size: "small"; tone: "dim"; wrapMode: Text.WordWrap
    }
    AppText {
      Layout.fillWidth: true
      visible: !(root.symbols && root.symbols.ready)
      text: "Optional, once per iOS version. Without them, backtraces and breakpoints still work, but Swift values such as String and Array can read wrong and system frames show only library names."
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
      visible: !!(root.symP && (root.symP.phase === "failed" || root.symP.phase === "cancelled")) && !root.symDownloading
      text: root.symP ? (root.symP.message || "") : ""
      size: "small"; tone: root.symP && root.symP.phase === "failed" ? "error" : "dim"; wrapMode: Text.WordWrap
    }
    AppText {
      Layout.fillWidth: true
      visible: !!root.symbols && !root.symbols.ready && !root.symDownloading && !root.symEnough
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
    RowLayout {
      Layout.fillWidth: true
      spacing: Style.spacing.md
      visible: !!root.symbols && !root.symbols.ready
      AppText {
        Layout.fillWidth: true
        text: root.symbols && !root.symbols.downloading ? "About " + Model.formatBytes(root.symbols.estimatedBytes || 0) + " to download · " + Model.formatBytes(root.symbols.freeBytes || 0) + " free" : ""
        size: "small"; tone: "faint"
      }
      ActionButton {
        visible: !root.symDownloading
        text: "Download debug symbols"; glyph: Model.Glyph.download
        enabled: root.svc.connected && root.symEnough
        busy: root.symBusy
        onClicked: root.downloadSymbols()
      }
      ActionButton {
        visible: root.symDownloading
        text: "Cancel"; glyph: Model.Glyph.close; kind: "ghost"
        onClicked: root.cancelSymbols()
      }
    }
  }
}
