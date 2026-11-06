import QtQuick
import qs.Commons
import qs.Commons as Commons
import qs.Ui

// Square glyph-only button with a tooltip (toolbar / row actions).
//   IconButton { glyph: Model.Glyph.trash; tooltip: "Uninstall"; danger: true; onClicked: … }
Button {
  id: root
  property string glyph: ""
  property string tooltip: ""
  property bool danger: false
  property bool busy: false

  iconText: busy ? String.fromCodePoint(0xF04E6) : glyph
  iconSpinning: busy
  tooltipText: Host.covers(root, Host.top) ? "" : tooltip   // none under a confirm or sheet
  foreground: danger && hot ? Commons.Color.urgent : Theme.dim
  iconSize: Style.font.icon
  horizontalPadding: Style.spacing.md
  verticalPadding: Style.spacing.md
  implicitWidth: implicitHeight
  opacity: enabled ? 1 : 0.4
}
