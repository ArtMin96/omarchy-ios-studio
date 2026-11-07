import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../common"
import "../../lib/Model.js" as Model

// One row of the Screen tab's Elements and Audit lists (DeviceInspectView): a glyph centred on the
// title's first line, the title (an element's label, or an audit message) with an optional dim
// value after it, and a muted line under it (role · identifier, or the element an issue is about).
// `pressable` adds the Press button; it shows on the cursor row only, its space always kept so
// the texts don't shift. `clickable` makes the whole row a link (an audit issue → its element),
// with a chevron on the cursor row. Visuals follow `hasCursor` (the view's cursor), like ListRow.
//   DeviceInspectRow { glyph: Model.Glyph.tap; title: "Sign in"; secondary: "Button · loginButton"
//                      pressable: true; onPressRequested: view.press(el) }
CursorSurface {
  id: root
  property string glyph: ""
  property color glyphColor: Theme.dim
  property string title: ""
  property bool titleMuted: false        // a placeholder title ("No label")
  property string value: ""
  property string secondary: ""
  property int titleLines: 1             // audit messages wrap to a few lines
  property bool pressable: false
  property bool pressBusy: false
  property bool pressEnabled: true
  property string pressTooltip: ""
  property bool clickable: false
  property string clickTooltip: ""

  signal hovered(bool isHovered)
  signal pressRequested()
  signal clicked()

  // Same glyph column as ListRow and BuildDiagnosticRow, so lists share a left text edge.
  readonly property real iconColumn: Style.font.iconLarge * 1.4

  FontMetrics { id: lineMetrics; font: titleText.font }

  implicitHeight: row.implicitHeight + Style.spacing.md * 2
  implicitWidth: Style.space(320)

  RowLayout {
    id: row
    anchors.fill: parent
    // Vertical insets, not centring: the glyph sits on the first text line, so the text column
    // must start at the same y.
    anchors.topMargin: Style.spacing.md
    anchors.bottomMargin: Style.spacing.md
    anchors.leftMargin: Style.spacing.rowPaddingX
    anchors.rightMargin: Style.spacing.md
    spacing: Style.spacing.xl

    Item {
      Layout.alignment: Qt.AlignTop
      Layout.preferredWidth: root.iconColumn
      Layout.preferredHeight: lineMetrics.height
      Icon {
        anchors.centerIn: parent
        glyph: root.glyph
        color: root.hasCursor && !root.titleMuted ? Commons.Color.foreground : root.glyphColor
        size: Style.font.icon
      }
    }

    ColumnLayout {
      Layout.fillWidth: true
      Layout.alignment: Qt.AlignTop
      spacing: Style.spacing.xxs
      RowLayout {
        Layout.fillWidth: true
        spacing: Style.spacing.md
        AppText {
          id: titleText
          Layout.fillWidth: true
          // The title takes its own width and the value the rest; when both are long the value
          // keeps up to 40 % (its minimum below) and the title elides first.
          Layout.maximumWidth: root.value !== "" && root.titleLines === 1 ? implicitWidth : -1
          text: root.title
          tone: root.titleMuted ? "faint" : "primary"
          wrapMode: root.titleLines > 1 ? Text.WordWrap : Text.NoWrap
          maximumLineCount: root.titleLines
          elide: Text.ElideRight
        }
        AppText {
          id: valueText
          Layout.fillWidth: true
          Layout.minimumWidth: Math.min(implicitWidth, parent.width * 0.4)
          visible: root.value !== ""
          text: root.value
          tone: "dim"
          elide: Text.ElideRight
        }
      }
      AppText {
        Layout.fillWidth: true
        visible: root.secondary !== ""
        text: root.secondary
        size: "small"; tone: "faint"
        elide: Text.ElideRight
      }
    }

    IconButton {
      Layout.alignment: Qt.AlignVCenter
      visible: root.pressable
      opacity: root.hasCursor || root.pressBusy ? (enabled ? 1 : 0.4) : 0
      glyph: Model.Glyph.tap
      tooltip: root.pressTooltip
      busy: root.pressBusy
      enabled: root.pressEnabled
      onClicked: root.pressRequested()
    }
    Icon {
      Layout.alignment: Qt.AlignVCenter
      visible: root.clickable
      glyph: Model.Glyph.chevronRight
      size: Style.font.bodySmall
      color: root.hasCursor ? Theme.dim : "transparent"
    }
  }

  HoverHandler {
    id: hoverH
    cursorShape: root.clickable ? Qt.PointingHandCursor : Qt.ArrowCursor
    onHoveredChanged: root.hovered(hovered)
  }
  TapHandler { enabled: root.clickable; onTapped: root.clicked() }
  PanelToolTip { visible: root.clickable && root.clickTooltip !== "" && hoverH.hovered; text: root.clickTooltip }
}
