import QtQuick
import qs.Commons
import qs.Commons as Commons
import qs.Ui

// The app's button. Wraps qs.Ui Button with a clear hierarchy:
//   kind: "primary"   accent-filled call to action (one per view, ideally)
//         "secondary" bordered (default)
//         "ghost"     borderless, for toolbars and inline actions
//         "danger"    urgent-tinted, for destructive actions (still confirm!)
//   busy: shows a spinning sync glyph and ignores clicks
//   enabled: false dims and ignores clicks
//
//   ActionButton { text: "Install everything"; glyph: Model.Glyph.download; kind: "primary"; onClicked: … }
Item {
  id: root

  property string text: ""
  property string glyph: ""
  property string kind: "secondary"
  property bool busy: false
  property bool hasCursor: false
  property string tooltip: ""
  property bool compact: false
  readonly property bool interactive: root.enabled && !root.busy

  signal clicked()
  signal hovered(bool isHovered)

  readonly property color _tint: kind === "danger" ? Commons.Color.urgent : Commons.Color.accent
  readonly property bool _primary: kind === "primary"

  implicitWidth: btn.implicitWidth
  implicitHeight: btn.implicitHeight
  opacity: root.enabled ? 1 : 0.45

  Behavior on opacity { NumberAnimation { duration: 120 } }

  // Accent wash under the primary button; the Ui Button paints its own
  // hover/press state on top so the kit's interaction feel is unchanged.
  Rectangle {
    anchors.fill: parent
    visible: root._primary || root.kind === "danger"
    radius: Style.cornerRadius
    color: Util.alpha(root._tint, root._primary ? (btn.hot ? 0.30 : 0.22) : (btn.hot ? 0.16 : 0.08))
    border.width: Style.normalBorderWidth
    border.color: Util.alpha(root._tint, root._primary ? 0.85 : 0.5)
    Behavior on color { ColorAnimation { duration: 120 } }
  }

  Button {
    id: btn
    anchors.fill: parent
    text: root.text
    iconText: root.busy ? String.fromCodePoint(0xF04E6) : root.glyph
    iconSpinning: root.busy
    tooltipText: root.tooltip
    hasCursor: root.hasCursor
    bordered: root.kind === "secondary"
    foreground: root._primary || root.kind === "danger" ? (root.kind === "danger" ? Commons.Color.urgent : Commons.Color.foreground) : Commons.Color.foreground
    fontSize: Style.font.body
    iconSize: Style.font.icon
    verticalPadding: root.compact ? Style.spacing.xs : Style.spacing.controlPaddingY
    horizontalPadding: root.compact ? Style.spacing.lg : Style.spacing.controlPaddingX + Style.spacing.xs
    background: "transparent"
    onClicked: if (root.interactive) root.clicked()
    onHovered: function(h) { root.hovered(h) }
  }
}
