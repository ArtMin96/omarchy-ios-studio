import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../../lib/Model.js" as Model

// One wizard step (Setup page). Shows number/state, title, description,
// size/time meta, an action button, an optional progress bar and an
// expandable live log.
//   StepRow {
//     number: 2; title: "Swift toolchain"; description: "Compiles your app."
//     stepState: "missing"        // done | missing | running | failed | pending | optional | skipped
//     meta: "≈ 900 MB · 3 min"
//     actionText: "Install"; actionGlyph: Model.Glyph.download; onAction: svc.install("swift")
//     progress: 0.42; progressText: "Downloading 380 / 900 MB"; progressTrailing: "1 min left"   (-1 = hidden)
//     Component.onCompleted: setLog(svc.logLines("swift"))   // then appendLog(text) per new line,
//                                                         // clearLog() when the step starts again
//     errorText: "The download was interrupted."  // shown under the title when failed
//     hasCursor: page.cursor === index; onHovered: …
//   }
BorderSurface {
  id: root
  property int number: 0
  property string title: ""
  property string description: ""
  property string stepState: "pending"
  property string meta: ""
  property string actionText: ""
  property string actionGlyph: ""
  property string actionKind: stepState === "failed" || stepState === "missing" ? "primary" : "secondary"
  property bool actionEnabled: true
  property bool actionBusy: stepState === "running"
  property real progress: -1
  property bool progressIndeterminate: false
  property string progressText: ""
  property string progressTrailing: ""
  property int logCount: 0
  readonly property int maxLogLines: 400

  function setLog(lines) {
    stepLog.clear()
    var l = lines || []
    stepLog.appendMany(l.slice(-root.maxLogLines))
    root.logCount = Math.min(l.length, root.maxLogLines)
  }
  function appendLog(text) {
    stepLog.append(text)
    root.logCount = Math.min(root.logCount + 1, root.maxLogLines)
  }
  function clearLog() { stepLog.clear(); root.logCount = 0 }
  property string errorText: ""
  property bool expanded: false
  property bool hasCursor: false
  property alias extra: extraSlot.data       // additional controls under the description

  signal action()
  signal hovered(bool isHovered)
  signal toggled(bool expanded)

  // Narrow (tiled) windows: the action button and meta drop under the text
  // instead of squeezing it, so nothing gets clipped.
  readonly property bool narrow: width > 0 && width < Style.space(560)
  readonly property bool _done: stepState === "done"
  readonly property bool _running: stepState === "running"
  readonly property bool _failed: stepState === "failed"
  readonly property color _tint: _done ? Theme.success : (_failed ? Commons.Color.urgent : (_running ? Commons.Color.accent : Theme.dim))

  radius: Style.cornerRadius
  color: hasCursor ? Theme.surfaceRaised : (_running ? Util.alpha(Commons.Color.accent, 0.05) : Theme.surface)
  borderSpec: Border.flat(_running ? Util.alpha(Commons.Color.accent, 0.45) : (_failed ? Util.alpha(Commons.Color.urgent, 0.4) : (hasCursor ? Util.alpha(Commons.Color.foreground, 0.22) : Theme.hairline)), Style.normalBorderWidth)
  implicitHeight: col.implicitHeight + Style.spacing.xxl * 2
  implicitWidth: Style.space(560)
  Behavior on color { ColorAnimation { duration: 140 } }

  HoverHandler { onHoveredChanged: root.hovered(hovered) }

  ColumnLayout {
    id: col
    anchors.fill: parent
    anchors.margins: Style.spacing.xxl
    anchors.leftMargin: Style.spacing.huge
    spacing: Style.spacing.lg

    RowLayout {
      Layout.fillWidth: true
      spacing: Style.spacing.xxl

      // state disc
      Rectangle {
        Layout.alignment: Qt.AlignTop
        implicitWidth: Style.font.heading * 1.9
        implicitHeight: implicitWidth
        radius: Style.cornerRadius > 0 ? width / 2 : 0
        color: root._done ? Util.alpha(Theme.success, 0.15) : (root._failed ? Util.alpha(Commons.Color.urgent, 0.15) : "transparent")
        border.width: Style.normalBorderWidth
        border.color: Util.alpha(root._tint, root._done || root._failed || root._running ? 0.6 : 0.35)
        AppText {
          anchors.centerIn: parent
          visible: !root._done && !root._failed && !root._running
          text: root.number > 0 ? String(root.number) : ""
          size: "subtitle"; strong: true; tone: "dim"
        }
        Icon { anchors.centerIn: parent; visible: root._done; glyph: Model.Glyph.check; color: Theme.success; size: Style.font.title }
        Icon { anchors.centerIn: parent; visible: root._failed; glyph: Model.Glyph.close; color: Commons.Color.urgent; size: Style.font.title }
        Spinner { anchors.centerIn: parent; running: root._running; size: parent.width * 0.62 }
      }

      ColumnLayout {
        Layout.fillWidth: true
        spacing: Style.spacing.xs
        RowLayout {
          Layout.fillWidth: true
          spacing: Style.spacing.lg
          AppText { text: root.title; size: "title"; strong: true; tone: root.stepState === "skipped" ? "dim" : "primary"; elide: Text.ElideRight; Layout.maximumWidth: col.width * 0.6 }
          StatusPill {
            visible: root.stepState === "optional" || root.stepState === "skipped"
            level: "info"; showGlyph: false; text: root.stepState === "optional" ? "optional" : "skipped"
          }
          Item { Layout.fillWidth: true }
          AppText { visible: root.meta !== "" && !root.narrow; text: root.meta; size: "small"; tone: "faint" }
        }
        AppText { visible: root.meta !== "" && root.narrow; text: root.meta; size: "small"; tone: "faint" }
        AppText { Layout.fillWidth: true; visible: root.description !== ""; text: root.description; tone: "dim"; wrapMode: Text.WordWrap; lineHeight: 1.2 }
        AppText { Layout.fillWidth: true; visible: root._failed && root.errorText !== ""; text: root.errorText; tone: "error"; wrapMode: Text.WordWrap }
        ColumnLayout { id: extraSlot; Layout.fillWidth: true; visible: children.length > 0; spacing: Style.spacing.md }
        ActionButton {
          Layout.topMargin: Style.spacing.sm
          visible: root.actionText !== "" && root.narrow
          text: root.actionText
          glyph: root.actionGlyph
          kind: root.actionKind
          busy: root.actionBusy
          enabled: root.actionEnabled
          onClicked: root.action()
        }
      }

      ActionButton {
        Layout.alignment: Qt.AlignTop
        visible: root.actionText !== "" && !root.narrow
        text: root.actionText
        glyph: root.actionGlyph
        kind: root.actionKind
        busy: root.actionBusy
        enabled: root.actionEnabled
        onClicked: root.action()
      }
    }

    ProgressTrack {
      Layout.fillWidth: true
      Layout.leftMargin: Style.font.heading * 1.9 + Style.spacing.xxl
      visible: root.progress >= 0 || root.progressIndeterminate
      value: root.progress
      indeterminate: root.progressIndeterminate
      label: root.progressText
      trailing: root.progressTrailing
    }

    // log expander
    RowLayout {
      Layout.leftMargin: Style.font.heading * 1.9 + Style.spacing.xxl
      visible: root.logCount > 0
      spacing: Style.spacing.sm
      Icon { glyph: root.expanded ? Model.Glyph.chevronDown : Model.Glyph.chevronRight; size: Style.font.bodySmall; color: Theme.dim }
      AppText { text: root.expanded ? "Hide log" : "Show log (" + root.logCount + " lines)"; size: "small"; tone: logHover.hovered ? "primary" : "dim" }
      HoverHandler { id: logHover; cursorShape: Qt.PointingHandCursor }
      TapHandler { onTapped: { root.expanded = !root.expanded; root.toggled(root.expanded) } }
    }
    LogView {
      id: stepLog
      Layout.fillWidth: true
      Layout.leftMargin: Style.font.heading * 1.9 + Style.spacing.xxl
      Layout.preferredHeight: Style.space(180)
      visible: root.expanded && root.logCount > 0
      maxLines: root.maxLogLines
      showToolbar: false
    }
  }
}
