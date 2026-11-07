import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons

// Shimmering placeholder lines while content loads. Matches the rough shape
// of what's coming so the layout doesn't jump.
//   Skeleton { lines: 3 }                         // paragraph
//   Skeleton { lines: 4; rowHeight: Style.space(36); gap: Style.spacing.md }   // list rows
ColumnLayout {
  id: root
  property int lines: 3
  property real rowHeight: Style.font.body
  property real gap: Style.spacing.lg
  property bool running: true

  spacing: gap

  Repeater {
    model: root.lines
    delegate: Rectangle {
      required property int index
      Layout.fillWidth: index !== root.lines - 1 || root.lines === 1
      Layout.preferredWidth: index === root.lines - 1 && root.lines > 1 ? root.width * 0.6 : -1
      Layout.preferredHeight: root.rowHeight
      radius: Style.cornerRadius
      color: Theme.surfaceRaised
      SequentialAnimation on opacity {
        running: root.running && root.visible
        loops: Animation.Infinite
        NumberAnimation { from: 0.55; to: 1; duration: 700; easing.type: Easing.InOutSine }
        NumberAnimation { from: 1; to: 0.55; duration: 700; easing.type: Easing.InOutSine }
      }
    }
  }
}
