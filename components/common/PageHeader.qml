import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons

// Top of every page: title, one-line subtitle, optional status pill and
// trailing actions.
//   PageHeader {
//     title: "iPhone"; subtitle: "Alex's iPhone · iOS 18.1"
//     status: StatusPill { level: "ok"; text: "Connected" }     // optional
//     actions: [ ActionButton { text: "Refresh"; glyph: Model.Glyph.refresh; kind: "ghost" } ]
//   }
Item {
  id: root
  property string title: ""
  property string subtitle: ""
  property string glyph: ""
  property alias actions: actionsRow.data
  property alias status: statusSlot.data

  implicitHeight: Math.max(textCol.implicitHeight, actionsRow.implicitHeight)
  implicitWidth: textCol.implicitWidth + actionsRow.implicitWidth

  RowLayout {
    anchors.fill: parent
    spacing: Style.spacing.xxl

    ColumnLayout {
      id: textCol
      Layout.fillWidth: true
      spacing: Style.spacing.xs
      RowLayout {
        spacing: Style.spacing.lg
        Icon {
          visible: root.glyph !== ""
          glyph: root.glyph
          size: Style.font.display
          color: Commons.Color.accent
        }
        AppText {
          text: root.title
          size: "display"
          strong: true
          Layout.fillWidth: false
        }
        Item { id: statusSlot; implicitWidth: childrenRect.width; implicitHeight: childrenRect.height; Layout.alignment: Qt.AlignVCenter; Layout.leftMargin: Style.spacing.sm }
      }
      AppText {
        Layout.fillWidth: true
        visible: root.subtitle !== ""
        text: root.subtitle
        tone: "dim"
        size: "subtitle"
        wrapMode: Text.Wrap
        maximumLineCount: 2
        elide: Text.ElideRight
      }
    }
    // A RowLayout (not a Row) so mixed actions (text, icon and labelled buttons) share one centre line.
    RowLayout {
      id: actionsRow
      spacing: Style.spacing.md
      Layout.alignment: Qt.AlignTop | Qt.AlignRight
    }
  }
}
