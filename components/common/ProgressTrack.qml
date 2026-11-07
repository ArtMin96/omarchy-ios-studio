import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons

// Thin progress track.
//   ProgressTrack { value: 0.42 }                         // determinate (0…1)
//   ProgressTrack { indeterminate: true }                 // sliding segment
//   ProgressTrack { value: f; label: "1.2 / 2.8 GB"; trailing: "2 min left" }   // with caption row
//   level: "" (accent) | ok | warn | error
ColumnLayout {
  id: root
  property real value: 0
  property bool indeterminate: false
  property string level: ""
  property string label: ""
  property string trailing: ""
  property real thickness: Style.space(4)
  readonly property color tint: level === "" ? Commons.Color.accent : Theme.levelColor(level)

  spacing: Style.spacing.sm

  Rectangle {
    id: track
    Layout.fillWidth: true
    Layout.preferredHeight: root.thickness
    radius: Style.cornerRadius > 0 ? height / 2 : 0
    color: Util.alpha(Commons.Color.foreground, 0.08)
    clip: true

    Rectangle {
      visible: !root.indeterminate
      height: parent.height
      radius: parent.radius
      width: parent.width * Math.max(0, Math.min(1, root.value))
      color: root.tint
      Behavior on width { NumberAnimation { duration: 220; easing.type: Easing.OutCubic } }
    }

    Rectangle {
      id: seg
      visible: root.indeterminate
      height: parent.height
      radius: parent.radius
      width: parent.width * 0.3
      color: root.tint
      x: -width
      NumberAnimation on x {
        running: root.indeterminate && root.visible
        from: -seg.width; to: track.width
        duration: 1300
        loops: Animation.Infinite
        easing.type: Easing.InOutQuad
      }
    }
  }

  RowLayout {
    Layout.fillWidth: true
    visible: root.label !== "" || root.trailing !== ""
    AppText { Layout.fillWidth: true; text: root.label; size: "small"; tone: "dim"; elide: Text.ElideRight }
    AppText { text: root.trailing; size: "small"; tone: "faint" }
  }
}
