import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons

// Friendly "nothing here yet" with a way forward.
//   EmptyState {
//     glyph: Model.Glyph.folder
//     title: "No projects yet"
//     text: "Create a new app from a template, or add a folder you already have."
//     actionText: "New app"; actionGlyph: Model.Glyph.plus; onAction: …
//     secondaryText: "Add folder"; onSecondary: …
//   }
Item {
  id: root
  property string glyph: ""
  property string title: ""
  property string text: ""
  property string actionText: ""
  property string actionGlyph: ""
  property string actionKind: "primary"
  property string secondaryText: ""
  property string secondaryGlyph: ""
  property bool compact: false
  property string level: ""            // tint the glyph (ok/warn/error) — empty = quiet

  signal action()
  signal secondary()

  implicitHeight: col.implicitHeight + (compact ? Style.spacing.xxl : Style.spacing.huge * 2) * 2
  implicitWidth: col.implicitWidth

  ColumnLayout {
    id: col
    anchors.centerIn: parent
    width: Math.min(parent.width - Style.spacing.huge * 2, Style.space(440))
    spacing: Style.spacing.lg

    Rectangle {
      visible: root.glyph !== ""
      Layout.alignment: Qt.AlignHCenter
      Layout.bottomMargin: Style.spacing.sm
      implicitWidth: (root.compact ? Style.font.display : Style.font.displayLarge) * 2
      implicitHeight: implicitWidth
      radius: Style.cornerRadius > 0 ? width / 2 : 0
      color: root.level !== "" ? Util.alpha(Theme.levelColor(root.level), 0.12) : Theme.surfaceRaised
      border.width: Style.normalBorderWidth
      border.color: root.level !== "" ? Util.alpha(Theme.levelColor(root.level), 0.3) : Theme.hairline
      Icon {
        anchors.centerIn: parent
        glyph: root.glyph
        size: root.compact ? Style.font.display : Style.font.displayLarge
        color: root.level !== "" ? Theme.levelColor(root.level) : Theme.dim
      }
    }
    AppText {
      Layout.fillWidth: true
      horizontalAlignment: Text.AlignHCenter
      text: root.title
      size: root.compact ? "title" : "heading"
      strong: true
      wrapMode: Text.WordWrap
    }
    AppText {
      Layout.fillWidth: true
      visible: root.text !== ""
      horizontalAlignment: Text.AlignHCenter
      text: root.text
      tone: "dim"
      wrapMode: Text.WordWrap
      lineHeight: 1.25
    }
    Row {
      Layout.alignment: Qt.AlignHCenter
      Layout.topMargin: Style.spacing.md
      visible: root.actionText !== "" || root.secondaryText !== ""
      spacing: Style.spacing.lg
      ActionButton { visible: root.actionText !== ""; text: root.actionText; glyph: root.actionGlyph; kind: root.actionKind; onClicked: root.action() }
      ActionButton { visible: root.secondaryText !== ""; text: root.secondaryText; glyph: root.secondaryGlyph; kind: "secondary"; onClicked: root.secondary() }
    }
  }
}
