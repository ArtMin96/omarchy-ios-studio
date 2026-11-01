import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../Model.js" as Model

// Bottom-right stack of transient messages. shell.qml hosts one; pages call
// app.toast("Copied UDID") or app.toast("Build failed", "error").
// Levels: info (default) | ok | warn | error. Errors stay longer.
Item {
  id: root
  property int maxToasts: 4
  property int _seq: 0

  function show(text, level, opts) {
    var lv = Model.level(level || "info")
    if (lv === "busy") lv = "info"
    var o = opts || {}
    // collapse an identical toast that's already showing
    for (var i = 0; i < toasts.count; i++) if (toasts.get(i).text === String(text)) { toasts.remove(i); break }
    root._seq++
    toasts.append({ uid: root._seq, text: String(text), level: lv, ttl: o.timeout || (lv === "error" ? 7000 : (lv === "warn" ? 5000 : 3200)) })
    while (toasts.count > root.maxToasts) toasts.remove(0)
  }

  function _remove(uid) {
    for (var i = 0; i < toasts.count; i++) if (toasts.get(i).uid === uid) { toasts.remove(i); return }
  }

  ListModel { id: toasts }

  Column {
    anchors.right: parent.right
    anchors.bottom: parent.bottom
    anchors.margins: Style.spacing.huge
    spacing: Style.spacing.md

    move: Transition { NumberAnimation { properties: "y"; duration: 160; easing.type: Easing.OutCubic } }
    add: Transition {
      NumberAnimation { property: "opacity"; from: 0; to: 1; duration: 160 }
      NumberAnimation { property: "x"; from: Style.space(24); to: 0; duration: 180; easing.type: Easing.OutCubic }
    }

    Repeater {
      model: toasts
      delegate: BorderSurface {
        id: toast
        required property int uid
        required property string text
        required property string level
        required property int ttl
        readonly property color tint: level === "info" ? Commons.Color.accent : Theme.levelColor(level)
        width: Style.space(340)
        implicitHeight: row.implicitHeight + Style.spacing.xl * 2
        radius: Style.cornerRadius
        color: Commons.Color.notifications.background
        borderSpec: Border.flat(Util.alpha(tint, 0.6), Style.normalBorderWidth)

        Rectangle { anchors.left: parent.left; anchors.top: parent.top; anchors.bottom: parent.bottom; width: Style.space(3); color: toast.tint }
        RowLayout {
          id: row
          anchors.fill: parent
          anchors.leftMargin: Style.spacing.xxl + Style.space(3)
          anchors.rightMargin: Style.spacing.xxl
          spacing: Style.spacing.lg
          Icon { glyph: Model.levelGlyph(toast.level); color: toast.tint; size: Style.font.icon }
          AppText { Layout.fillWidth: true; text: toast.text; wrapMode: Text.WordWrap }
        }
        HoverHandler { id: th }
        TapHandler { onTapped: root._remove(toast.uid) }
        Timer { interval: toast.ttl; running: !th.hovered; onTriggered: root._remove(toast.uid) }
      }
    }
  }
}
