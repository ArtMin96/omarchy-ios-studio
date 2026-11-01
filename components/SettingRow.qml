import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons

// Preference row: title + description on the left, the control on the right.
//   SettingRow {
//     title: "Notifications"; description: "Tell me when a build finishes while I'm in another window."
//     ToggleSwitch { checked: app.settings.notifications; onToggled: app.setSetting("notifications", !checked) }
//   }
// `note` adds a coloured line under the description (level: ok/warn/error/info).
Item {
  id: root
  property string title: ""
  property string description: ""
  property string glyph: ""
  property string note: ""
  property string noteLevel: "info"
  property bool separator: true
  default property alias control: slot.data

  Layout.fillWidth: true
  implicitHeight: row.implicitHeight + Style.spacing.xl * 2
  implicitWidth: Style.space(520)

  Rectangle {
    visible: root.separator
    anchors.top: parent.top
    width: parent.width
    height: Style.spacing.hairline
    color: Theme.hairline
  }

  RowLayout {
    id: row
    anchors.fill: parent
    anchors.topMargin: Style.spacing.xl
    anchors.bottomMargin: Style.spacing.xl
    spacing: Style.spacing.huge

    Icon { visible: root.glyph !== ""; glyph: root.glyph; color: Theme.dim; Layout.alignment: Qt.AlignTop; Layout.topMargin: Style.spacing.xxs }
    ColumnLayout {
      Layout.fillWidth: true
      spacing: Style.spacing.xs
      AppText { Layout.fillWidth: true; text: root.title; size: "subtitle"; wrapMode: Text.WordWrap }
      AppText { Layout.fillWidth: true; visible: root.description !== ""; text: root.description; size: "small"; tone: "dim"; wrapMode: Text.WordWrap; lineHeight: 1.2 }
      AppText { Layout.fillWidth: true; visible: root.note !== ""; text: root.note; size: "small"; tone: root.noteLevel === "info" ? "accent" : root.noteLevel; wrapMode: Text.WordWrap }
    }
    Row { id: slot; spacing: Style.spacing.md; Layout.alignment: Qt.AlignVCenter }
  }
}
