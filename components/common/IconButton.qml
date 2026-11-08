import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../../core"

// Square glyph-only button with a tooltip (toolbar / row actions).
//   IconButton { glyph: Model.Glyph.trash; tooltip: "Uninstall"; danger: true; onClicked: … }
// disabled: true dims it and swallows clicks but keeps hover, so `tooltip` can say why (like ActionButton).
Button {
  id: root
  property string glyph: ""
  property string tooltip: ""
  property bool danger: false
  property bool busy: false
  property bool disabled: false

  iconText: busy ? String.fromCodePoint(0xF04E6) : glyph
  iconSpinning: busy
  tooltipText: Host.covers(root, Host.top) ? "" : tooltip   // none under a confirm or sheet
  foreground: danger && hot ? Commons.Color.urgent : Theme.dim
  iconSize: Style.font.icon
  horizontalPadding: Style.spacing.md
  verticalPadding: Style.spacing.md
  implicitWidth: implicitHeight
  Layout.minimumWidth: root.implicitWidth      // never squeezed by a filling neighbour in a RowLayout
  opacity: enabled && !disabled ? 1 : 0.4

  // disabled: presses stop here; hover still reaches the button for its tooltip.
  MouseArea {
    anchors.fill: parent
    visible: root.disabled
    acceptedButtons: Qt.LeftButton | Qt.RightButton
    cursorShape: Qt.ArrowCursor
  }
}
