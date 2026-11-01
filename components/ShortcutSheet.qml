import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../Model.js" as Model

// The `?` overlay. Content is Model.SHORTCUTS (+ the page list).
Item {
  id: root
  property bool opened: false
  signal dismissed()
  function dismiss() { root.dismissed() }

  onOpenedChanged: opened ? Host.push(root) : Host.pop(root)
  z: 150
  visible: opacity > 0.001
  opacity: opened ? 1 : 0
  enabled: opened
  Behavior on opacity { NumberAnimation { duration: 120 } }

  Rectangle { anchors.fill: parent; color: Theme.scrim; MouseArea { anchors.fill: parent; onClicked: root.dismissed() } }

  BorderSurface {
    anchors.centerIn: parent
    width: Math.min(parent.width - Style.spacing.huge * 4, Style.space(620))
    height: col.implicitHeight + Style.spacing.panelPadding * 2
    radius: Style.cornerRadius
    color: Commons.Color.popups.background
    borderSpec: Border.surfaceSpec("popups", "border", Commons.Color.accent, Style.normalBorderWidth)
    MouseArea { anchors.fill: parent; onClicked: root.dismissed() }

    ColumnLayout {
      id: col
      anchors.fill: parent
      anchors.margins: Style.spacing.panelPadding
      spacing: Style.spacing.xxl

      RowLayout {
        spacing: Style.spacing.lg
        Icon { glyph: Model.Glyph.keyboard; color: Commons.Color.accent; size: Style.font.heading }
        AppText { text: "Keyboard shortcuts"; size: "heading"; strong: true; Layout.fillWidth: true }
        AppText { text: "? or Esc to close"; size: "small"; tone: "faint" }
      }

      RowLayout {
        Layout.fillWidth: true
        spacing: Style.spacing.huge * 2
        ColumnLayout {
          Layout.alignment: Qt.AlignTop
          Layout.fillWidth: true
          spacing: Style.spacing.xl
          Repeater {
            model: Model.shortcutGroups()
            delegate: ColumnLayout {
              required property var modelData
              Layout.fillWidth: true
              spacing: Style.spacing.md
              SectionHeader { Layout.fillWidth: true; text: modelData.title }
              Repeater {
                model: modelData.entries
                delegate: RowLayout {
                  required property var modelData
                  spacing: Style.spacing.xl
                  AppText { text: modelData.keys; tone: "accent"; Layout.preferredWidth: Style.space(84); Layout.alignment: Qt.AlignTop }
                  AppText { text: modelData.text; tone: "dim"; Layout.fillWidth: true; wrapMode: Text.WordWrap }
                }
              }
            }
          }
        }
        ColumnLayout {
          Layout.alignment: Qt.AlignTop
          Layout.preferredWidth: Style.space(170)
          spacing: Style.spacing.md
          SectionHeader { Layout.fillWidth: true; text: "Pages" }
          Repeater {
            model: Model.PAGES
            delegate: RowLayout {
              required property var modelData
              required property int index
              spacing: Style.spacing.xl
              AppText { text: String(index + 1); tone: "accent"; Layout.preferredWidth: Style.space(16) }
              Icon { glyph: modelData.glyph; color: Theme.dim; size: Style.font.bodySmall }
              AppText { text: modelData.label; tone: "dim" }
            }
          }
        }
      }
    }
  }
}
