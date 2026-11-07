import QtQuick
import qs.Commons
import qs.Commons as Commons

// Compact segmented control (for filters in toolbars). For forms prefer qs.Ui ButtonGroup.
//   ButtonGroupLite { options: [{value: "", label: "All"}, {value: "error", label: "Errors"}]; value: v; onPicked: function(v) { … } }
Rectangle {
  id: root
  property var options: []
  property string value: ""
  signal picked(string value)

  implicitWidth: row.implicitWidth + Style.spacing.xxs * 2
  implicitHeight: row.implicitHeight + Style.spacing.xxs * 2
  radius: Style.cornerRadius
  color: Theme.surface
  border.width: Style.normalBorderWidth
  border.color: Theme.hairline

  Row {
    id: row
    anchors.centerIn: parent
    spacing: Style.spacing.xxs
    Repeater {
      model: root.options
      delegate: Rectangle {
        required property var modelData
        readonly property bool sel: root.value === modelData.value
        implicitWidth: lbl.implicitWidth + Style.spacing.xl * 2
        implicitHeight: lbl.implicitHeight + Style.spacing.sm * 2
        radius: Style.cornerRadius
        color: sel ? Style.selectedAccentFill : (h.hovered ? Style.hoverFill : "transparent")
        Behavior on color { ColorAnimation { duration: 100 } }
        AppText { id: lbl; anchors.centerIn: parent; text: modelData.label; size: "small"; strong: sel; tone: sel ? "primary" : "dim" }
        HoverHandler { id: h; cursorShape: Qt.PointingHandCursor }
        TapHandler { onTapped: root.picked(modelData.value) }
      }
    }
  }
}
