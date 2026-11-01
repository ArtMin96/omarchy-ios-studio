import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons

// Small uppercase label that introduces a group, with optional trailing items.
//   SectionHeader { text: "Installed apps"; detail: "12"; trailing: [ ActionButton { … } ] }
Item {
  id: root
  property string text: ""
  property string detail: ""
  property alias trailing: trail.data

  implicitHeight: Math.max(label.implicitHeight, trail.implicitHeight)
  implicitWidth: label.implicitWidth + trail.implicitWidth

  RowLayout {
    anchors.fill: parent
    spacing: Style.spacing.md
    AppText {
      id: label
      text: root.text.toUpperCase()
      size: "caption"
      strong: true
      tone: "dim"
      font.letterSpacing: Style.spaceReal(1.1)
    }
    AppText {
      visible: root.detail !== ""
      text: root.detail
      size: "caption"
      tone: "faint"
    }
    Rectangle {
      Layout.fillWidth: true
      Layout.alignment: Qt.AlignVCenter
      height: Style.spacing.hairline
      color: Theme.hairline
    }
    Row { id: trail; spacing: Style.spacing.xs; Layout.alignment: Qt.AlignVCenter }
  }
}
