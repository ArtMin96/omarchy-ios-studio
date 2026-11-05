import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../Model.js" as Model

// One label → value line. Long values elide; `copyable` adds a copy button
// (wl-copy) that confirms through `copied(value)`.
//   InfoRow { label: "UDID"; value: dev.udid; copyable: true; onCopied: app.toast("Copied UDID") }
//   InfoRow { label: "Developer Mode"; value: "Off"; level: "warn" }   // tinted value + glyph
Item {
  id: root
  property string label: ""
  property string value: ""
  property string glyph: ""
  property string level: ""          // "", ok, warn, error, info
  property bool copyable: false
  property real labelWidth: Style.space(140)
  property alias trailing: trail.data

  signal copied(string value)

  // Room for the copy button on every row, so rows with and without one are the same height.
  implicitHeight: Math.max(valueText.implicitHeight, copyBtn.implicitHeight) + Style.spacing.sm * 2
  implicitWidth: labelWidth + valueText.implicitWidth

  RowLayout {
    anchors.fill: parent
    spacing: Style.spacing.lg

    RowLayout {
      // fixed width: a long value must not squeeze the label column, or values stop lining up
      Layout.preferredWidth: root.labelWidth
      Layout.minimumWidth: root.labelWidth
      Layout.maximumWidth: root.labelWidth
      spacing: Style.spacing.md
      Icon { visible: root.glyph !== ""; glyph: root.glyph; color: Theme.faint; size: Style.font.bodySmall }
      AppText { Layout.fillWidth: true; text: root.label; tone: "dim"; elide: Text.ElideRight }
    }
    Icon {
      visible: root.level !== "" && root.level !== "info"
      glyph: Model.levelGlyph(root.level)
      color: Theme.levelColor(root.level)
      size: Style.font.bodySmall
    }
    AppText {
      id: valueText
      Layout.fillWidth: true
      text: root.value === "" ? "—" : root.value
      tone: root.level !== "" && root.level !== "info" ? root.level : (root.value === "" ? "faint" : "primary")
      elide: Text.ElideMiddle
    }
    Row { id: trail; spacing: Style.spacing.xs }
    IconButton {
      id: copyBtn
      visible: root.copyable && root.value !== ""
      glyph: Model.Glyph.copy
      tooltip: "Copy"
      verticalPadding: Style.spacing.xs
      horizontalPadding: Style.spacing.xs
      onClicked: { Util.execArgv(["wl-copy", "--", root.value]); root.copied(root.value) }
    }
  }
}
