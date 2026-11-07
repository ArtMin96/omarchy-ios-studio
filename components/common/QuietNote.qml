import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons

// A slim, left-aligned "nothing here yet" line for small dashboard sections,
// where a full EmptyState would outweigh the lists beside it.
//   QuietNote { glyph: Model.Glyph.hammer; title: "No builds yet"; text: "Builds you run show up here." }
// `level` (ok/warn/error) tints the glyph; empty = quiet.
Item {
  id: root
  property string glyph: ""
  property string title: ""
  property string text: ""
  property string level: ""

  implicitHeight: row.implicitHeight + Style.spacing.lg * 2
  implicitWidth: Style.space(280)

  RowLayout {
    id: row
    anchors.fill: parent
    anchors.leftMargin: Style.spacing.rowPaddingX
    anchors.rightMargin: Style.spacing.md
    spacing: Style.spacing.xxl
    // the glyph centred on the title's first line
    FontMetrics { id: titleMetrics; font: titleText.font }
    Item {
      visible: root.glyph !== ""
      Layout.preferredWidth: Style.font.iconLarge * 1.4
      Layout.preferredHeight: titleMetrics.height
      Layout.alignment: Qt.AlignTop
      Icon {
        anchors.centerIn: parent
        glyph: root.glyph
        size: Style.font.iconLarge
        color: root.level !== "" ? Theme.levelColor(root.level) : Theme.faint
      }
    }
    ColumnLayout {
      Layout.fillWidth: true
      Layout.alignment: Qt.AlignTop
      spacing: Style.spacing.xxs
      AppText { id: titleText; Layout.fillWidth: true; text: root.title; tone: "dim"; wrapMode: Text.WordWrap }
      AppText { Layout.fillWidth: true; visible: root.text !== ""; text: root.text; size: "small"; tone: "faint"; wrapMode: Text.WordWrap }
    }
  }
}
