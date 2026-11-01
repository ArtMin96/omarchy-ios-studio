import QtQuick
import qs.Commons
import qs.Commons as Commons
import "../Model.js" as Model

// In-page tabs with an accent underline.
//   PageTabs {
//     tabs: [ { key: "live", label: "Live log", glyph: Model.Glyph.console },
//             { key: "crashes", label: "Crashes", glyph: Model.Glyph.crash, count: 3, level: "warn" } ]
//     current: page.tab
//     onSelected: function(key) { page.tab = key }
//   }
Item {
  id: root
  property var tabs: []
  property string current: ""
  property int cursorIndex: -1
  signal selected(string key)

  implicitHeight: Style.space(34)
  implicitWidth: row.implicitWidth

  Rectangle {
    anchors.left: parent.left; anchors.right: parent.right; anchors.bottom: parent.bottom
    height: Style.spacing.hairline
    color: Theme.hairline
  }

  Row {
    id: row
    height: parent.height
    spacing: Style.spacing.xs

    Repeater {
      model: root.tabs
      delegate: Item {
        id: tab
        required property var modelData
        required property int index
        readonly property bool isCurrent: root.current === modelData.key
        readonly property bool hot: hover.hovered || root.cursorIndex === index
        width: content.implicitWidth + Style.spacing.xxl * 2
        height: row.height

        Rectangle {
          anchors.fill: parent
          anchors.bottomMargin: Style.space(2)
          radius: Style.cornerRadius
          color: tab.hot ? Style.hoverFill : "transparent"
          Behavior on color { ColorAnimation { duration: 100 } }
        }
        Row {
          id: content
          anchors.centerIn: parent
          spacing: Style.spacing.md
          Icon { visible: !!tab.modelData.glyph; glyph: tab.modelData.glyph || ""; size: Style.font.iconSmall; color: tab.isCurrent ? Commons.Color.foreground : Theme.dim; anchors.verticalCenter: parent.verticalCenter }
          AppText { text: tab.modelData.label; size: "body"; strong: tab.isCurrent; tone: tab.isCurrent ? "primary" : "dim"; anchors.verticalCenter: parent.verticalCenter }
          Badge {
            visible: tab.modelData.count !== undefined && tab.modelData.count !== null && tab.modelData.count !== ""
            text: String(tab.modelData.count === undefined ? "" : tab.modelData.count)
            level: tab.modelData.level || "info"
            anchors.verticalCenter: parent.verticalCenter
          }
        }
        Rectangle {
          anchors.left: parent.left; anchors.right: parent.right; anchors.bottom: parent.bottom
          anchors.leftMargin: Style.spacing.md; anchors.rightMargin: Style.spacing.md
          height: Style.space(2)
          color: Commons.Color.accent
          opacity: tab.isCurrent ? 1 : 0
          Behavior on opacity { NumberAnimation { duration: 140 } }
        }
        HoverHandler { id: hover; cursorShape: Qt.PointingHandCursor }
        TapHandler { onTapped: root.selected(tab.modelData.key) }
      }
    }
  }
}
