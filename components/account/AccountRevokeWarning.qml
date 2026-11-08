import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../common"
import "../../lib/Model.js" as Model

// Explains the "one certificate per computer" hazard of using the same Apple ID on
// two computers. Used on AccountPage; the projects slice can show it next to xtool's
// revoke prompt.   AccountRevokeWarning { Layout.fillWidth: true; compact: true }
BorderSurface {
  id: root
  property bool compact: false
  property bool expanded: !compact

  radius: Style.cornerRadius
  color: Theme.levelFill("warn")
  borderSpec: Border.flat(Util.alpha(Theme.warn, 0.35), Style.normalBorderWidth)
  implicitHeight: col.implicitHeight + Style.spacing.xl * 2

  ColumnLayout {
    id: col
    anchors.fill: parent
    anchors.margins: Style.spacing.xl
    spacing: Style.spacing.md

    RowLayout {
      Layout.fillWidth: true
      spacing: Style.spacing.lg
      Icon { glyph: Model.Glyph.warn; color: Theme.warn; size: Style.font.icon }
      AppText { Layout.fillWidth: true; text: "Using this Apple ID on another computer?"; strong: true; wrapMode: Text.Wrap }
      AppText {
        visible: root.compact
        text: root.expanded ? "Less" : "What happens"
        size: "small"; tone: "accent"
        MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor; onClicked: root.expanded = !root.expanded }
      }
    }
    AppText {
      Layout.fillWidth: true
      visible: root.expanded
      text: "Each computer needs its own signing certificate, and the secret part of it never leaves the computer that made it. "
          + "When you build on a new computer, iOS Studio asks before replacing the old certificate."
      tone: "dim"; wrapMode: Text.Wrap; lineHeight: 1.15
    }
    AppText {
      Layout.fillWidth: true
      visible: root.expanded
      text: "If you replace it, apps you installed from the other computer stop opening until you install them again from here. "
          + "If you're not sure, choose “Don't revoke”: nothing changes."
      tone: "dim"; wrapMode: Text.Wrap; lineHeight: 1.15
    }
  }
}
