import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../Model.js" as Model

// Every thread of a captured backtrace (helper `debug backtrace` data: {threads, crashedThread,
// selectedThread}). Threads collapse; the crashed one (else the main one) starts open. Clicking a
// frame copies it.
ColumnLayout {
  id: root
  required property var app
  property var backtrace: null
  property var open: ({})                     // thread index -> expanded

  readonly property var threads: backtrace && backtrace.threads ? backtrace.threads : []
  readonly property int focusIndex: backtrace ? (backtrace.crashedThread !== null && backtrace.crashedThread !== undefined
                                                  ? backtrace.crashedThread : (backtrace.selectedThread || 1)) : -1

  function isOpen(t) { return root.open[t.index] !== undefined ? root.open[t.index] : t.index === root.focusIndex }
  function toggle(t) { var o = Object.assign({}, root.open); o[t.index] = !root.isOpen(t); root.open = o }
  function setAll(v) { var o = {}; for (var i = 0; i < threads.length; i++) o[threads[i].index] = v; root.open = o }
  function copy(text, what) { Util.execArgv(["wl-copy", "--", text]); root.app.toast("Copied " + what) }

  function asText() {
    var out = []
    for (var i = 0; i < threads.length; i++) {
      var t = threads[i]
      out.push("Thread " + t.index + (t.crashed ? " CRASHED" : "") + (t.name ? " " + t.name : "") + (t.queue ? " (" + t.queue + ")" : "")
               + (t.description ? ": " + t.description : ""))
      for (var j = 0; j < (t.frames || []).length; j++) {
        var f = t.frames[j]
        out.push("  #" + f.index + " " + (f.module || "?") + "`" + (f.function || f.pc) + (f.file ? " at " + f.file + ":" + f.line : ""))
      }
    }
    return out.join("\n")
  }

  onBacktraceChanged: root.open = ({})
  spacing: Style.spacing.sm

  RowLayout {
    Layout.fillWidth: true
    spacing: Style.spacing.md
    AppText {
      Layout.fillWidth: true
      text: root.threads.length + " threads" + (root.backtrace && root.backtrace.crashedThread ? " · thread " + root.backtrace.crashedThread + " stopped on an exception" : "")
      size: "small"; tone: "dim"; elide: Text.ElideRight
    }
    ActionButton { text: "Expand all"; compact: true; kind: "ghost"; onClicked: root.setAll(true) }
    ActionButton { text: "Collapse all"; compact: true; kind: "ghost"; onClicked: root.setAll(false) }
    ActionButton { text: "Copy all"; glyph: Model.Glyph.copy; compact: true; onClicked: root.copy(root.asText(), "the backtrace") }
  }

  Repeater {
    model: root.threads
    delegate: ColumnLayout {
      id: th
      required property var modelData
      readonly property bool expanded: root.isOpen(modelData)
      readonly property bool crashed: !!modelData.crashed
      Layout.fillWidth: true
      spacing: 0

      Item {
        Layout.fillWidth: true
        implicitHeight: head.implicitHeight + Style.spacing.sm * 2
        Rectangle {
          anchors.fill: parent; radius: Style.cornerRadius
          color: th.crashed ? Theme.levelFill("error") : (headHover.hovered ? Theme.surfaceRaised : Theme.surface)
        }
        HoverHandler { id: headHover; cursorShape: Qt.PointingHandCursor }
        TapHandler { onTapped: root.toggle(th.modelData) }
        RowLayout {
          id: head
          anchors.fill: parent
          anchors.leftMargin: Style.spacing.md; anchors.rightMargin: Style.spacing.md
          spacing: Style.spacing.md
          Icon { glyph: th.expanded ? Model.Glyph.chevronDown : Model.Glyph.chevronRight; size: Style.font.body; color: Theme.dim }
          AppText { text: "Thread " + th.modelData.index; strong: true }
          AppText {
            Layout.fillWidth: true
            text: [th.modelData.name, th.modelData.queue].filter(function(x) { return !!x }).join(" · ")
            size: "small"; tone: "dim"; elide: Text.ElideRight
          }
          StatusPill {
            visible: th.crashed
            level: "error"; quiet: true
            text: th.modelData.description || "Exception"
          }
          AppText { text: th.modelData.numFrames + " frames"; size: "small"; tone: "faint" }
        }
      }

      ColumnLayout {
        Layout.fillWidth: true
        Layout.leftMargin: Style.spacing.lg
        Layout.topMargin: Style.spacing.xs
        Layout.bottomMargin: Style.spacing.sm
        visible: th.expanded
        spacing: 0
        Repeater {
          model: th.expanded ? th.modelData.frames : []
          delegate: DebugFrameRow {
            required property var modelData
            Layout.fillWidth: true
            frame: modelData
            onActivated: root.copy(copyText, "frame #" + modelData.index)
          }
        }
        AppText {
          visible: th.modelData.numFrames > (th.modelData.frames || []).length
          Layout.leftMargin: Style.spacing.md
          text: (th.modelData.numFrames - (th.modelData.frames || []).length) + " more frames not shown"
          size: "small"; tone: "faint"
        }
      }
    }
  }
}
