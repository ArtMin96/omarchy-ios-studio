import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../Model.js" as Model

// Watch mode state on a project card's title row (issue #7): a quiet glyph + text in the same
// style as the card's last-build pill (StatusPill quiet), but it elides at narrow widths and is
// clickable (opens Problems). Fed by WatchService.label(w):
//   WatchStateLabel { info: app.watch.label(w); onClicked: … }
//   info: { level: ok|warn|error|info|busy, glyph ("" = a spinner for busy), text, tip }
Item {
  id: root

  property var info: null
  readonly property string _lv: info ? Model.level(info.level) : "info"
  readonly property color tint: Theme.levelColor(_lv)
  readonly property bool busy: _lv === "busy"

  signal clicked()

  visible: info !== null
  implicitWidth: row.implicitWidth
  implicitHeight: row.implicitHeight

  RowLayout {
    id: row
    anchors.fill: parent
    spacing: Style.spacing.sm
    Icon {
      Layout.alignment: Qt.AlignVCenter
      // One width for the spinner and the eye, so the text doesn't move when the state changes.
      Layout.preferredWidth: Math.ceil(Style.font.bodySmall * 1.25)
      glyph: root.info && root.info.glyph ? root.info.glyph : Model.levelGlyph(root._lv)
      spinning: root.busy && !(root.info && root.info.glyph)
      color: root.tint
      size: Style.font.bodySmall
    }
    AppText {
      id: label
      Layout.fillWidth: true
      Layout.alignment: Qt.AlignVCenter
      Layout.minimumWidth: 0
      text: root.info ? root.info.text : ""
      size: "small"
      tone: "inherit"
      color: root._lv === "info" ? Theme.dim : root.tint
      font.underline: hover.hovered
      elide: Text.ElideRight
    }
  }

  HoverHandler { id: hover; cursorShape: Qt.PointingHandCursor }
  TapHandler { onTapped: root.clicked() }
  PanelToolTip { visible: hover.hovered && !!root.info && root.info.tip !== "" && !Host.covers(root, Host.top); text: root.info ? root.info.tip : "" }
}
