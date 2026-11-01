import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import "../Model.js" as Model

// Numbered instruction list for pairing / Developer Mode walkthroughs.
//   DeviceSteps { steps: [{ text: "Plug it in", done: true }, { text: "Tap Trust", active: true }] }
ColumnLayout {
  id: root
  property var steps: []
  spacing: Style.spacing.lg

  Repeater {
    model: root.steps
    delegate: RowLayout {
      required property var modelData
      required property int index
      Layout.fillWidth: true
      spacing: Style.spacing.xl

      Rectangle {
        Layout.alignment: Qt.AlignTop
        implicitWidth: Style.font.body * 1.9
        implicitHeight: implicitWidth
        radius: width / 2
        color: modelData.done ? Theme.levelFill("ok") : (modelData.active ? Util.alpha(Commons.Color.accent, 0.16) : "transparent")
        border.width: Style.normalBorderWidth
        border.color: modelData.done ? Theme.success : (modelData.active ? Commons.Color.accent : Theme.hairline)
        AppText {
          anchors.centerIn: parent
          visible: !modelData.done
          text: String(index + 1)
          size: "small"; strong: true
          tone: modelData.active ? "accent" : "dim"
        }
        Icon { anchors.centerIn: parent; visible: !!modelData.done; glyph: Model.Glyph.check; color: Theme.success; size: Style.font.body }
      }
      AppText {
        Layout.fillWidth: true
        Layout.alignment: Qt.AlignVCenter
        text: modelData.text
        wrapMode: Text.WordWrap
        tone: modelData.done ? "dim" : "primary"
      }
    }
  }
}
