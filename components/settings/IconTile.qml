import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import "../common"

// The fixed square glyph box at the start of a Settings row (editors, AI tools, the iOS Studio server).
// Every tile is the same size and has a visible edge, so a neutral tile reads as big as a tinted one.
//   IconTile { glyph: Model.Glyph.fileCode; level: ready ? "ok" : "" }
//   level: "" neutral, or ok | warn | error | info (tinted fill, edge and glyph)
//   glyphColor: overrides the glyph's colour (e.g. Theme.dim for something not installed)
Rectangle {
  id: root

  property string glyph: ""
  property string level: ""
  property color glyphColor: level !== "" ? Theme.levelColor(level) : Commons.Color.foreground
  property alias spinning: icon.spinning

  readonly property real side: Style.space(32)

  Layout.alignment: Qt.AlignTop
  Layout.preferredWidth: side
  Layout.preferredHeight: side
  Layout.minimumWidth: side
  Layout.maximumWidth: side
  implicitWidth: side
  implicitHeight: side
  radius: Style.cornerRadius
  color: level !== "" ? Theme.levelFill(level) : Theme.surfaceRaised
  border.width: Style.normalBorderWidth
  border.color: level !== "" ? Util.alpha(Theme.levelColor(level), 0.35) : Util.alpha(Commons.Color.foreground, 0.18)

  Icon {
    id: icon
    anchors.centerIn: parent
    glyph: root.glyph
    size: Style.font.icon
    color: root.glyphColor
  }
}
