import QtQuick
import qs.Commons
import qs.Commons as Commons
import "../../lib/Model.js" as Model

// Compact level indicator: tinted pill with glyph + text.
//   StatusPill { level: "warn"; text: "expires in 2 days" }
//   level: ok | warn | error | info | busy   (anything else → info; "ready"/"failed"… are normalised)
//   glyph: override the level glyph ("" keeps the default, set `showGlyph: false` to hide)
//   quiet: true → no fill, just coloured glyph + text (for dense rows)
Rectangle {
  id: root

  property string level: "info"
  property string text: ""
  property string glyph: ""
  property bool showGlyph: true
  property bool quiet: false

  readonly property string _lv: Model.level(level)
  readonly property color tint: Theme.levelColor(_lv)

  implicitWidth: row.implicitWidth + (quiet ? 0 : Style.spacing.lg * 2)
  implicitHeight: row.implicitHeight + (quiet ? 0 : Style.spacing.xs * 2)
  radius: Style.cornerRadius > 0 ? height / 2 : 0
  color: quiet ? "transparent" : Util.alpha(tint, _lv === "info" ? 0.10 : 0.15)
  border.width: quiet ? 0 : Style.normalBorderWidth
  border.color: quiet ? "transparent" : Util.alpha(tint, 0.35)

  Behavior on color { ColorAnimation { duration: 160 } }

  Row {
    id: row
    anchors.centerIn: parent
    spacing: Style.spacing.sm

    Icon {
      visible: root.showGlyph
      glyph: root.glyph !== "" ? root.glyph : Model.levelGlyph(root._lv)
      color: root.tint
      size: Style.font.bodySmall
      spinning: root._lv === "busy" && root.glyph === ""
      anchors.verticalCenter: parent.verticalCenter
    }
    AppText {
      visible: root.text !== ""
      text: root.text
      size: "small"
      tone: "inherit"
      color: root._lv === "info" ? Theme.dim : root.tint
      anchors.verticalCenter: parent.verticalCenter
    }
  }
}
