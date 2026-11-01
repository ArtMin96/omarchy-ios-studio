import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../Model.js" as Model

// One stack frame from the debugger: "#3  ForecastModel.dailyHigh(for:)  ForecastModel.swift:58  WeatherGlance".
// Your own code is drawn in the accent colour; system frames are quieter. Clicking emits activated()
// (the Debug tab copies the frame, or selects it in a paused session).
Item {
  id: root
  property var frame: ({})
  property bool selected: false
  property string tooltip: "Click to copy"
  signal activated()

  readonly property bool inApp: !!frame.inApp
  readonly property string where: frame.file ? (String(frame.file).split("/").pop() + ":" + frame.line) : ""
  readonly property string copyText: (frame.function || frame.pc || "?") + (where ? " at " + (frame.file || "") + ":" + frame.line : "")
                                     + (frame.module ? " (" + frame.module + ")" : "")

  implicitHeight: row.implicitHeight + Style.spacing.xs * 2
  implicitWidth: row.implicitWidth

  Rectangle {
    anchors.fill: parent
    radius: Style.cornerRadius
    color: root.selected ? Theme.levelFill("info") : (hover.hovered ? Theme.surfaceRaised : "transparent")
  }
  HoverHandler { id: hover; cursorShape: Qt.PointingHandCursor }
  TapHandler { onTapped: root.activated() }
  PanelToolTip { visible: hover.hovered && root.tooltip !== ""; text: root.tooltip }

  RowLayout {
    id: row
    anchors.fill: parent
    anchors.leftMargin: Style.spacing.md
    anchors.rightMargin: Style.spacing.md
    spacing: Style.spacing.lg
    AppText {
      Layout.preferredWidth: Style.space(28)
      text: "#" + (root.frame.index === undefined ? "" : root.frame.index)
      size: "small"; tone: "faint"; mono: true
    }
    AppText {
      Layout.fillWidth: true
      text: root.frame.function || root.frame.pc || "?"
      size: "small"; mono: true; elide: Text.ElideRight
      tone: root.inApp ? "accent" : "primary"
      strong: root.inApp
    }
    AppText {
      visible: root.where !== ""
      text: root.where
      size: "small"; mono: true; tone: root.inApp ? "primary" : "dim"
    }
    AppText {
      Layout.maximumWidth: Style.space(170)
      text: root.frame.module || ""
      size: "small"; tone: "faint"; elide: Text.ElideRight
    }
  }
}
