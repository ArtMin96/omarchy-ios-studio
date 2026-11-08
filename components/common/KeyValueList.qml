import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons

// A stack of InfoRows from data, with hairline separators.
//   KeyValueList {
//     rows: [ { label: "Team", value: "Personal Team" },
//             { label: "Team ID", value: "ABCDE12345", copy: true },
//             { label: "Certificate", value: "expires in 3 days", level: "warn", glyph: Model.Glyph.cert } ]
//     onCopied: function(v) { app.toast("Copied") }
//   }
ColumnLayout {
  id: root
  property var rows: []
  property real labelWidth: Style.space(140)
  property bool separators: true
  signal copied(string value)

  spacing: 0

  Repeater {
    model: root.rows
    delegate: ColumnLayout {
      required property var modelData
      required property int index
      Layout.fillWidth: true
      spacing: 0
      Rectangle {
        Layout.fillWidth: true
        visible: root.separators && index > 0
        height: Style.spacing.hairline
        color: Theme.hairline
      }
      InfoRow {
        Layout.fillWidth: true
        label: modelData.label || ""
        value: modelData.value === undefined || modelData.value === null ? "" : String(modelData.value)
        glyph: modelData.glyph || ""
        level: modelData.level || ""
        copyable: !!modelData.copy
        labelWidth: root.labelWidth
        onCopied: function(v) { root.copied(v) }
      }
    }
  }
}
