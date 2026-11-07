import QtQuick
import qs.Commons
import qs.Commons as Commons
import "../../lib/Model.js" as Model

// Tiny status marker for sidebars and tabs: a dot, or a short label/count.
//   Badge { level: "error" }            // dot
//   Badge { level: "warn"; text: "3" }  // count chip
Rectangle {
  id: root
  property string level: "info"
  property string text: ""
  readonly property color tint: Theme.levelColor(Model.level(level))

  readonly property int dot: Style.space(6)
  implicitWidth: text === "" ? dot : Math.max(label.implicitWidth + Style.spacing.md * 2, implicitHeight)
  implicitHeight: text === "" ? dot : label.implicitHeight + Style.spacing.xxs * 2
  radius: Style.cornerRadius > 0 || text === "" ? height / 2 : 0
  color: text === "" ? tint : Util.alpha(tint, 0.18)

  AppText {
    id: label
    anchors.centerIn: parent
    visible: root.text !== ""
    text: root.text
    size: "caption"
    strong: true
    tone: "inherit"
    color: root.tint
  }
}
