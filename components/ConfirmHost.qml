import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../Model.js" as Model

// The app's confirm dialog. shell.qml hosts one; pages call
//   app.confirm({ title, message, confirmText, cancelText, destructive, glyph, onConfirm, onCancel })
// Cancel is the default (Enter on open cancels). ←/→/Tab switch, Esc cancels.
Item {
  id: root
  property bool opened: false
  property string title: ""
  property string message: ""
  property string confirmText: "Confirm"
  property string cancelText: "Cancel"
  property string glyph: ""
  property bool destructive: false
  property int selectedIndex: 0
  property var _onConfirm: null
  property var _onCancel: null

  function show(opts) {
    var o = opts || {}
    root.title = o.title || "Are you sure?"
    root.message = o.message || ""
    root.confirmText = o.confirmText || (o.destructive ? "Remove" : "Confirm")
    root.cancelText = o.cancelText || "Cancel"
    root.destructive = !!o.destructive
    root.glyph = o.glyph || (o.destructive ? Model.Glyph.warn : Model.Glyph.help)
    root._onConfirm = o.onConfirm || null
    root._onCancel = o.onCancel || null
    root.selectedIndex = 0
    root.opened = true
  }

  function _finish(ok) {
    var cb = ok ? root._onConfirm : root._onCancel
    root._onConfirm = null
    root._onCancel = null
    root.opened = false
    if (cb) Qt.callLater(cb)
  }
  function dismiss() { _finish(false) }

  function handleKey(event) {
    if (!root.opened) return false
    if (event.key === Qt.Key_Escape) { _finish(false); return true }
    if (event.key === Qt.Key_Left || event.key === Qt.Key_Right || event.key === Qt.Key_Tab || event.key === Qt.Key_Backtab
        || event.text === "h" || event.text === "l") { root.selectedIndex = 1 - root.selectedIndex; return true }
    if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter || event.key === Qt.Key_Space) { _finish(root.selectedIndex === 1); return true }
    if (event.text === "y") { _finish(true); return true }
    if (event.text === "n") { _finish(false); return true }
    return true   // swallow everything else while open
  }

  onOpenedChanged: opened ? Host.push(root) : Host.pop(root)

  z: 200
  visible: opacity > 0.001
  opacity: opened ? 1 : 0
  enabled: opened
  Behavior on opacity { NumberAnimation { duration: 120 } }

  Rectangle {
    anchors.fill: parent
    color: Theme.scrim
    MouseArea { anchors.fill: parent; onClicked: root._finish(false) }
  }

  BorderSurface {
    id: card
    anchors.centerIn: parent
    width: Math.min(parent.width - Style.spacing.huge * 4, Style.space(420))
    implicitHeight: col.implicitHeight + Style.spacing.panelPadding * 2
    height: implicitHeight
    radius: Style.cornerRadius
    color: Commons.Color.popups.background
    borderSpec: Border.flat(root.destructive ? Util.alpha(Commons.Color.urgent, 0.7) : Commons.Color.popups.border, Style.normalBorderWidth)
    MouseArea { anchors.fill: parent }
    transform: Scale { origin.x: card.width / 2; origin.y: card.height / 2; xScale: root.opened ? 1 : 0.97; yScale: xScale; Behavior on xScale { NumberAnimation { duration: 140; easing.type: Easing.OutCubic } } }

    ColumnLayout {
      id: col
      anchors.fill: parent
      anchors.margins: Style.spacing.panelPadding
      spacing: Style.spacing.xl

      RowLayout {
        spacing: Style.spacing.xxl
        Layout.fillWidth: true
        Icon { glyph: root.glyph; size: Style.font.display; color: root.destructive ? Commons.Color.urgent : Commons.Color.accent; Layout.alignment: Qt.AlignTop }
        ColumnLayout {
          Layout.fillWidth: true
          spacing: Style.spacing.md
          AppText { Layout.fillWidth: true; text: root.title; size: "heading"; strong: true; wrapMode: Text.WordWrap }
          AppText { Layout.fillWidth: true; visible: root.message !== ""; text: root.message; tone: "dim"; wrapMode: Text.WordWrap; lineHeight: 1.25 }
        }
      }
      RowLayout {
        Layout.fillWidth: true
        Layout.topMargin: Style.spacing.md
        Item { Layout.fillWidth: true }
        ActionButton {
          text: root.cancelText; kind: "secondary"
          hasCursor: root.selectedIndex === 0
          onHovered: function(h) { if (h) root.selectedIndex = 0 }
          onClicked: root._finish(false)
        }
        ActionButton {
          text: root.confirmText; kind: root.destructive ? "danger" : "primary"
          hasCursor: root.selectedIndex === 1
          onHovered: function(h) { if (h) root.selectedIndex = 1 }
          onClicked: root._finish(true)
        }
      }
    }
  }
}
