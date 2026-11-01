import QtQuick
import qs.Commons
import qs.Commons as Commons

// A Nerd Font glyph. `glyph` comes from Model.Glyph.
//   Icon { glyph: Model.Glyph.phone; size: Style.font.iconLarge; color: Theme.dim }
Text {
  id: root
  property string glyph: ""
  property real size: Style.font.icon
  property bool spinning: false

  text: glyph
  textFormat: Text.PlainText
  color: Commons.Color.foreground
  font.family: Style.font.family
  font.pixelSize: size
  horizontalAlignment: Text.AlignHCenter
  verticalAlignment: Text.AlignVCenter
  transformOrigin: Item.Center

  RotationAnimation on rotation {
    running: root.spinning
    from: 0; to: 360; duration: 1000
    loops: Animation.Infinite
    onStopped: root.rotation = 0
  }
  Behavior on color { ColorAnimation { duration: 160 } }
}
