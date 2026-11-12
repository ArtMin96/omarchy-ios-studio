import QtQuick
import qs.Commons
import qs.Commons as Commons

// A Nerd Font glyph. `glyph` comes from Model.Glyph.
//   Icon { glyph: Model.Glyph.phone; size: Style.font.iconLarge; color: Theme.dim }
// spinning turns it only while it shows: `visible` is false when any parent is hidden (a page that stays loaded,
// a hidden pill), and nothing turns while the window is hidden, so a hidden spinner never keeps the window redrawing.
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
    running: root.spinning && root.visible && root.Window.visibility !== Window.Hidden
    from: 0; to: 360; duration: 1000
    loops: Animation.Infinite
    onStopped: root.rotation = 0
  }
  Behavior on color { ColorAnimation { duration: 160 } }
}
