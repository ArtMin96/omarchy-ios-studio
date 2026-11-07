import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import "../common"
import "../../lib/Model.js" as Model

// App data: "the app is running" strip with a Stop app button, shown where a change needs the app
// stopped (Defaults edits, Reset). `view` is the DeviceAppDataView.
//   DeviceAppDataRunning { Layout.fillWidth: true; view: root.view; text: "Stop it to edit its defaults." }
Rectangle {
  id: root
  required property var view
  property string text: ""

  implicitHeight: row.implicitHeight + Style.spacing.lg * 2
  radius: Style.cornerRadius
  // a quiet surface with the warn glyph: a warn-tinted fill turns olive over green themes
  color: Theme.surface
  border.width: Style.normalBorderWidth
  border.color: Theme.hairline

  RowLayout {
    id: row
    anchors.fill: parent
    anchors.margins: Style.spacing.lg
    anchors.leftMargin: Style.spacing.xxl
    spacing: Style.spacing.lg
    // the glyph centred on the title's first line
    FontMetrics { id: fm; font: title.font }
    Item {
      Layout.alignment: Qt.AlignTop
      Layout.preferredWidth: Style.font.icon
      Layout.preferredHeight: fm.height
      Icon { anchors.centerIn: parent; glyph: Model.Glyph.warn; size: Style.font.icon; color: Theme.warn }
    }
    ColumnLayout {
      Layout.fillWidth: true
      Layout.alignment: Qt.AlignVCenter
      spacing: Style.spacing.xxs
      AppText { id: title; Layout.fillWidth: true; text: root.view.appName + " is running"; strong: true; wrapMode: Text.Wrap }
      AppText { Layout.fillWidth: true; visible: root.text !== ""; text: root.text; size: "small"; tone: "dim"; wrapMode: Text.Wrap }
    }
    ActionButton {
      Layout.alignment: Qt.AlignVCenter
      text: "Stop app"; glyph: Model.Glyph.stop; compact: true
      busy: root.view.stopping
      onClicked: root.view.stopApp()
    }
  }
}
