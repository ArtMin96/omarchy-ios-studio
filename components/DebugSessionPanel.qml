import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import ".."
import "../Model.js" as Model

// Breakpoint session on one of your apps (helper `debug session`, a JSON-lines stream; commands go to
// its stdin as {cmd: ...}). Add breakpoints, start (attach, or restart the app paused), then step
// through stops: frames, locals, an expression box and lldb's console.
ColumnLayout {
  id: root
  required property var app
  property string bundleId: ""
  property string appName: ""
  property bool appRunning: false
  property var sources: []                  // [{path, rel, name, lines}] from `debug sources`
  property bool hasLocalBuild: true
  property bool blocked: false              // a backtrace capture is running

  // idle | starting | running | stopped | ended | failed
  property string phase: "idle"
  readonly property bool live: phase === "starting" || phase === "running" || phase === "stopped"
  property string stepTitle: ""
  property var specs: []                    // breakpoints before the session starts
  property var bps: []                      // [{id, spec, locations, resolved, hits}] while it runs
  property var stop: null                   // the last `stopped` event
  property var locals: null                 // {frame, function, variables}
  property int frameIndex: 0
  property var error: null
  property var notices: []
  property string launchMode: appRunning ? "attach" : "suspended"
  property var evalResult: null
  property bool waiting: false              // a step/continue was sent, no answer yet
  property var summary: null                // done data

  function reset() {
    root.bps = []; root.stop = null; root.locals = null; root.frameIndex = 0; root.error = null
    root.notices = []; root.evalResult = null; root.waiting = false; root.summary = null; root.stepTitle = ""
  }

  function addSpec(spec) {
    spec = String(spec || "").trim()
    if (spec === "") return
    if (root.live) { stream.write({ cmd: "breakpoint_add", spec: spec }); return }
    if (root.specs.indexOf(spec) >= 0) return
    root.specs = root.specs.concat([spec])
  }
  function removeSpec(item) {
    if (root.live && item.id !== undefined) { stream.write({ cmd: "breakpoint_delete", id: item.id }); return }
    root.specs = root.specs.filter(function(s) { return s !== item.spec })
  }

  function start() {
    if (!root.bundleId) return
    root.reset()
    root.phase = "starting"
    console_.clear()
    stream.start(["debug", "session", "--bundle-id", root.bundleId, "--launch", root.launchMode,
                  "--breakpoints", JSON.stringify(root.specs)])
  }
  function send(cmd, extra) {
    if (!stream.running) return
    var m = Object.assign({ cmd: cmd }, extra || {})
    if (["continue", "next", "step", "finish"].indexOf(cmd) >= 0) root.waiting = true
    stream.write(m)
  }
  function end(kill) {
    if (!stream.running) { root.phase = "idle"; return }
    if (kill) {
      root.app.confirm({ title: "Quit " + root.appName + "?", message: "The app closes on your iPhone, like swiping it away. Unsaved state in it is lost.",
                         confirmText: "Quit app", destructive: true, glyph: Model.Glyph.stop,
                         onConfirm: function() { root.send("kill") } })
      return
    }
    root.send("detach")
  }

  readonly property var listed: root.live ? root.bps : root.specs.map(function(s) { return { spec: s } })
  readonly property var frames: root.stop && root.stop.thread ? (root.stop.thread.frames || []) : []
  readonly property var f0: root.frames.length ? root.frames[0] : null

  spacing: Style.spacing.lg

  Stream {
    id: stream
    quitMessage: ({ cmd: "detach" })
    onEvent: function(ev) {
      var e = ev.event
      if (e === "step") { if (ev.state === "running") root.stepTitle = ev.title }
      else if (e === "console") console_.append(ev.text)
      else if (e === "log") console_.append(ev.text, "warn")
      else if (e === "notice") root.notices = root.notices.concat([ev])
      else if (e === "attached") root.stepTitle = ""
      else if (e === "breakpoints") root.bps = ev.items || []
      else if (e === "running") { root.phase = "running"; root.waiting = false }
      else if (e === "stopped") {
        root.phase = "stopped"; root.waiting = false
        root.stop = ev; root.locals = ev.locals || null; root.frameIndex = 0; root.evalResult = null
      }
      else if (e === "variables") { root.locals = ev; root.frameIndex = ev.frame || 0 }
      else if (e === "evaluated") root.evalResult = ev
      else if (e === "ack" && ev.ok === false) { root.waiting = false; root.app.toast(ev.message || "The debugger refused that.", "warn") }
      else if (e === "exited") root.notices = root.notices.concat([{ level: "info", message: root.appName + " exited." }])
    }
    onFinished: function(ok, data, error) {
      root.waiting = false
      root.summary = data
      if (ok) { root.phase = "ended"; root.error = null }
      else { root.phase = "failed"; root.error = error }
    }
  }

  // ---------------------------------------------------------------- breakpoints
  SectionHeader {
    Layout.fillWidth: true
    text: "Breakpoints"
    detail: root.listed.length ? String(root.listed.length) : ""
  }

  RowLayout {
    Layout.fillWidth: true
    spacing: Style.spacing.md
    visible: root.sources.length > 0
    Dropdown {
      id: filePick
      Layout.fillWidth: true
      showLabel: false
      options: root.sources.map(function(s) { return { value: s.name, label: s.rel + (s.lines ? "  ·  " + s.lines + " lines" : "") } })
      value: root.sources.length ? root.sources[0].name : ""
      onChanged: function(v) { filePick.value = v }
    }
    TextField {
      id: lineField
      Layout.preferredWidth: Style.space(90)
      placeholderText: "Line"
      validator: IntValidator { bottom: 1; top: 999999 }
      onAccepted: addLine.clicked()
    }
    ActionButton {
      id: addLine
      text: "Add"; glyph: Model.Glyph.plus
      enabled: filePick.value !== "" && lineField.text !== "" && root.phase !== "starting"
      onClicked: { root.addSpec(filePick.value + ":" + lineField.text); lineField.text = "" }
    }
  }
  RowLayout {
    Layout.fillWidth: true
    spacing: Style.spacing.md
    TextField {
      id: symField
      Layout.fillWidth: true
      placeholderText: root.sources.length ? "…or a function, e.g. ForecastModel.refresh" : "File.swift:42 or a function name, e.g. ForecastModel.refresh"
      onAccepted: addSym.clicked()
    }
    ActionButton {
      id: addSym
      text: "Add"; glyph: Model.Glyph.plus
      enabled: symField.text.trim() !== "" && root.phase !== "starting"
      onClicked: { root.addSpec(symField.text); symField.text = "" }
    }
  }

  ColumnLayout {
    Layout.fillWidth: true
    spacing: 0
    visible: root.listed.length > 0
    Repeater {
      model: root.listed
      delegate: RowLayout {
        required property var modelData
        Layout.fillWidth: true
        spacing: Style.spacing.md
        readonly property bool unresolved: modelData.locations === 0
        Icon { glyph: Model.Glyph.dot; size: Style.font.caption; color: parent.unresolved ? Theme.warn : Commons.Color.urgent }
        AppText { Layout.fillWidth: true; text: modelData.spec; mono: true; size: "small"; elide: Text.ElideMiddle }
        AppText {
          visible: modelData.locations !== undefined
          text: parent.unresolved ? "not found in the app" : (modelData.hits ? "hit " + modelData.hits + "×" : modelData.locations + (modelData.locations === 1 ? " location" : " locations"))
          size: "small"; tone: parent.unresolved ? "warn" : "faint"
        }
        IconButton { glyph: Model.Glyph.close; tooltip: "Remove breakpoint"; enabled: root.phase !== "starting"; onClicked: root.removeSpec(modelData) }
      }
    }
  }
  QuietNote {
    Layout.fillWidth: true
    visible: root.listed.length === 0
    glyph: Model.Glyph.circle
    title: "No breakpoints"
    text: "Without one, the app only stops when you press Pause or it hits an exception."
  }
  AppText {
    Layout.fillWidth: true
    visible: !root.hasLocalBuild
    text: "No build of this app was found on this computer, so file:line breakpoints won't resolve and your own frames show as addresses. Run it once from Projects first."
    size: "small"; tone: "warn"; wrapMode: Text.WordWrap
  }

  // ---------------------------------------------------------------- start / controls
  RowLayout {
    Layout.fillWidth: true
    spacing: Style.spacing.lg
    visible: !root.live
    ButtonGroupLite {
      options: [{ value: "attach", label: "Attach to the running app" }, { value: "suspended", label: "Restart it paused" }]
      value: root.launchMode
      onPicked: function(v) { root.launchMode = v }
    }
    Item { Layout.fillWidth: true }
    ActionButton {
      text: "Start debugging"; glyph: Model.Glyph.bug; kind: "primary"
      enabled: root.bundleId !== "" && !root.blocked && (root.launchMode !== "attach" || root.appRunning)
      tooltip: root.launchMode === "attach" && !root.appRunning ? "The app isn't running; open it first or restart it paused" : ""
      onClicked: root.start()
    }
  }
  AppText {
    Layout.fillWidth: true
    visible: !root.live
    text: root.launchMode === "attach"
          ? "The app pauses while the debugger attaches (about 30 s over Wi-Fi), then runs until a breakpoint."
          : "The app is closed and started again, paused until the debugger is ready, so breakpoints in startup code hit too."
    size: "small"; tone: "faint"; wrapMode: Text.WordWrap
  }

  LoadingState {
    Layout.fillWidth: true
    visible: root.phase === "starting"
    text: root.stepTitle || "Starting the debugger…"
    detail: "Connecting over Wi-Fi can take half a minute. The app stays paused until it's done."
  }

  RowLayout {
    Layout.fillWidth: true
    spacing: Style.spacing.md
    visible: root.phase === "running" || root.phase === "stopped"
    StatusPill {
      level: root.phase === "stopped" ? "warn" : (root.waiting ? "busy" : "ok")
      text: root.phase === "stopped" ? "Paused" : (root.waiting ? "Stepping…" : "Running")
    }
    ActionButton {
      text: "Continue"; glyph: Model.Glyph.play; kind: "primary"; compact: true
      enabled: root.phase === "stopped" && !root.waiting
      onClicked: root.send("continue")
    }
    ActionButton { text: "Step over"; glyph: Model.Glyph.arrowRight; compact: true; enabled: root.phase === "stopped" && !root.waiting; onClicked: root.send("next") }
    ActionButton { text: "Step in"; glyph: Model.Glyph.chevronDown; compact: true; enabled: root.phase === "stopped" && !root.waiting; onClicked: root.send("step") }
    ActionButton { text: "Step out"; glyph: Model.Glyph.chevronUp; compact: true; enabled: root.phase === "stopped" && !root.waiting; onClicked: root.send("finish") }
    ActionButton { text: "Pause"; glyph: Model.Glyph.pause; compact: true; visible: root.phase === "running"; onClicked: root.send("pause") }
    Item { Layout.fillWidth: true }
    ActionButton { text: "Detach"; glyph: Model.Glyph.linkOff; compact: true; kind: "ghost"; tooltip: "Stop debugging; the app keeps running"; onClicked: root.end(false) }
    IconButton { glyph: Model.Glyph.stop; tooltip: "Quit the app"; danger: true; onClicked: root.end(true) }
  }

  Repeater {
    model: root.live ? root.notices : []
    delegate: AppText {
      required property var modelData
      Layout.fillWidth: true
      text: modelData.message
      size: "small"; tone: modelData.level === "warn" ? "warn" : "dim"; wrapMode: Text.WordWrap
    }
  }

  ErrorState {
    Layout.fillWidth: true
    visible: root.phase === "failed"
    app: root.app
    error: root.error
    compact: true
    retryText: "Try again"
    onRetry: root.start()
  }
  QuietNote {
    Layout.fillWidth: true
    visible: root.phase === "ended"
    glyph: Model.Glyph.checkCircle
    level: "ok"
    title: root.summary && root.summary.ended === "killed" ? "Stopped, and the app was closed" : "Debugger detached"
    text: root.summary && root.summary.ended === "killed" ? "Open it again from the Apps tab." : root.appName + " keeps running normally."
  }

  // ---------------------------------------------------------------- the stop
  ColumnLayout {
    Layout.fillWidth: true
    visible: root.phase === "stopped" && !!root.stop
    spacing: Style.spacing.md

    RowLayout {
      Layout.fillWidth: true
      spacing: Style.spacing.md
      AppText {
        Layout.fillWidth: true
        text: root.f0 ? ((root.f0.file ? String(root.f0.file).split("/").pop() + ":" + root.f0.line + " in " : "In ") + (root.f0.function || root.f0.pc)) : ""
        size: "subtitle"; strong: true; elide: Text.ElideRight
      }
      StatusPill {
        level: root.stop && root.stop.reason === "exception" ? "error" : "info"
        quiet: true
        text: !root.stop ? "" : ({ breakpoint: "Breakpoint", step: "Step", paused: "Paused", exception: "Exception", signal: "Signal", watchpoint: "Watchpoint" }[root.stop.reason] || root.stop.reason)
      }
    }
    AppText {
      Layout.fillWidth: true
      visible: !!(root.stop && root.stop.reason === "exception" && root.stop.description)
      text: root.stop ? (root.stop.description || "") : ""
      size: "small"; tone: "error"; wrapMode: Text.WordWrap
    }

    SectionHeader { Layout.fillWidth: true; text: "Thread " + (root.stop && root.stop.thread ? root.stop.thread.index : ""); detail: root.stop && root.stop.thread ? root.stop.thread.numFrames + " frames" : "" }
    ColumnLayout {
      Layout.fillWidth: true
      spacing: 0
      Repeater {
        model: root.frames
        delegate: DebugFrameRow {
          required property var modelData
          Layout.fillWidth: true
          frame: modelData
          selected: modelData.index === root.frameIndex
          tooltip: "Show this frame's variables"
          onActivated: root.send("select_frame", { frame: modelData.index })
        }
      }
    }

    SectionHeader {
      Layout.fillWidth: true
      text: "Variables"
      detail: root.locals && root.locals.function ? root.locals.function : ""
    }
    QuietNote {
      Layout.fillWidth: true
      visible: !root.locals || !(root.locals.variables || []).length
      glyph: Model.Glyph.info
      title: "No variables here"
      text: "System frames have no debug info. Pick one of your own frames above."
    }
    Repeater {
      model: root.locals ? (root.locals.variables || []) : []
      delegate: ColumnLayout {
        id: v
        required property var modelData
        property bool expanded: (modelData.children || []).length > 0 && (modelData.children || []).length <= 6
        Layout.fillWidth: true
        spacing: 0
        Item {
          Layout.fillWidth: true
          implicitHeight: vrow.implicitHeight + Style.spacing.xs * 2
          HoverHandler { id: vh; cursorShape: (v.modelData.children || []).length ? Qt.PointingHandCursor : Qt.ArrowCursor }
          TapHandler { onTapped: v.expanded = !v.expanded }
          Rectangle { anchors.fill: parent; radius: Style.cornerRadius; color: vh.hovered ? Theme.surfaceRaised : "transparent" }
          RowLayout {
            id: vrow
            anchors.fill: parent; anchors.leftMargin: Style.spacing.md; anchors.rightMargin: Style.spacing.md
            spacing: Style.spacing.md
            Icon {
              glyph: v.expanded ? Model.Glyph.chevronDown : Model.Glyph.chevronRight
              size: Style.font.caption; color: Theme.dim
              opacity: (v.modelData.children || []).length ? 1 : 0
            }
            AppText { text: v.modelData.name; mono: true; size: "small"; strong: true }
            AppText { text: v.modelData.type || ""; size: "small"; tone: "faint"; elide: Text.ElideRight; Layout.maximumWidth: Style.space(200) }
            AppText {
              Layout.fillWidth: true
              text: v.modelData.value !== undefined ? "= " + v.modelData.value : (v.modelData.error || "")
              mono: true; size: "small"; tone: v.modelData.error ? "warn" : "primary"; elide: Text.ElideRight
            }
          }
        }
        Repeater {
          model: v.expanded ? (v.modelData.children || []) : []
          delegate: AppText {
            required property var modelData
            Layout.fillWidth: true
            Layout.leftMargin: Style.space(40)
            text: modelData.name + ": " + (modelData.type || "?") + (modelData.value !== undefined ? " = " + modelData.value : "")
            mono: true; size: "small"; tone: "dim"; elide: Text.ElideRight
          }
        }
      }
    }

    RowLayout {
      Layout.fillWidth: true
      spacing: Style.spacing.md
      TextField {
        id: exprField
        Layout.fillWidth: true
        placeholderText: "Evaluate a Swift expression in this frame, e.g. days.count"
        onAccepted: evalBtn.clicked()
      }
      ActionButton {
        id: evalBtn
        text: "Evaluate"; glyph: Model.Glyph.terminal
        enabled: exprField.text.trim() !== "" && !root.waiting
        onClicked: root.send("evaluate", { expr: exprField.text.trim() })
      }
    }
    AppText {
      Layout.fillWidth: true
      visible: !!root.evalResult
      text: !root.evalResult ? "" : (root.evalResult.ok
            ? root.evalResult.expr + " = " + (root.evalResult.result.value !== undefined ? root.evalResult.result.value : "(" + (root.evalResult.result.type || "no value") + ")")
            : (root.evalResult.result.error || "Couldn't evaluate that."))
      mono: true; size: "small"; tone: root.evalResult && root.evalResult.ok ? "primary" : "warn"; wrapMode: Text.WrapAnywhere
    }
  }

  // ---------------------------------------------------------------- console
  SectionHeader {
    Layout.fillWidth: true
    visible: root.phase !== "idle"
    text: "Console"
    detail: console_.count ? String(console_.count) : ""
  }
  LogView {
    id: console_
    Layout.fillWidth: true
    Layout.preferredHeight: Style.space(180)
    visible: root.phase !== "idle"
    showToolbar: false
    maxLines: 2000
    emptyText: "lldb's output shows up here."
    onLineCopied: function(t) { root.app.toast("Copied the line") }
  }
}
