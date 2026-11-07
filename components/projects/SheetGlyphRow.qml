import QtQuick
import QtQuick.Layouts
import qs.Commons
import "../common"

// A status line in a sheet's glyph column (projects sheets): the glyph, or a spinner, centred on the
// title's first line in a column `glyphWidth` wide (the sheet header glyph's), so the title starts on the
// sheet title's left edge. Children go under the title; `trailing` items (buttons) end the row.
//   SheetGlyphRow {
//     glyphWidth: root.glyphW; glyph: Model.Glyph.warn; glyphColor: Theme.warn
//     title: "Unresolved: …"; titleTone: "warn"; titleWraps: true
//     AppText { … }                                       // under the title
//     trailing: [ ActionButton { … } ]
//   }
RowLayout {
  id: row

  property real glyphWidth: 0
  property string glyph: ""
  property color glyphColor: Theme.dim
  property real glyphSize: Style.font.icon
  property bool spinner: false              // a running Spinner instead of the glyph
  property string title: ""
  property bool titleStrong: false
  property string titleTone: "primary"
  property bool titleWraps: false           // else it elides
  property int trailingAlignment: Qt.AlignVCenter

  default property alias details: col.data
  property alias trailing: tail.data

  spacing: Style.spacing.xxl

  FontMetrics { id: metrics; font: titleText.font }
  Item {
    Layout.alignment: Qt.AlignTop
    Layout.preferredWidth: row.glyphWidth
    Layout.preferredHeight: metrics.height
    Spinner { anchors.centerIn: parent; visible: row.spinner; running: row.spinner; size: row.glyphSize }
    Icon { anchors.centerIn: parent; visible: !row.spinner; glyph: row.glyph; size: row.glyphSize; color: row.glyphColor }
  }
  ColumnLayout {
    id: col
    Layout.fillWidth: true
    Layout.alignment: Qt.AlignTop
    spacing: Style.spacing.xxs
    AppText {
      id: titleText
      Layout.fillWidth: true
      text: row.title
      strong: row.titleStrong
      tone: row.titleTone
      wrapMode: row.titleWraps ? Text.WordWrap : Text.NoWrap
      elide: row.titleWraps ? Text.ElideNone : Text.ElideRight
    }
  }
  RowLayout {
    id: tail
    Layout.alignment: row.trailingAlignment
    visible: children.length > 0
    spacing: Style.spacing.xxl
  }
}
