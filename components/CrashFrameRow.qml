import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../Model.js" as Model

// One frame of the crashed thread in CrashReportView (logs slice). An app frame that crash symbolication
// resolved shows its function and file:line, and a click opens that line in the editor (like
// BuildDiagnosticRow). Other frames are the report's own '<n> <image> <symbol>' line.
CursorSurface {
  id: root
  property string text: ""
  property var info: null          // { image, function, file, path, line, column } or null
  property bool mine: false        // a frame in the app's own image
  readonly property bool hasFile: !!info && !!info.file
  readonly property string where: hasFile ? info.file + (info.line ? ":" + info.line + (info.column ? ":" + info.column : "") : "") : ""

  signal clicked()

  implicitHeight: col.implicitHeight + (hasFile ? Style.spacing.xs * 2 : 0)
  implicitWidth: Style.space(320)

  RowLayout {
    anchors.fill: parent
    spacing: Style.spacing.md
    ColumnLayout {
      id: col
      Layout.fillWidth: true
      Layout.alignment: Qt.AlignVCenter
      spacing: 0
      Text {
        Layout.fillWidth: true
        // the summary line ends with " (file:line)"; that goes on its own line below
        text: root.hasFile && root.text.endsWith(" (" + root.where + ")") ? root.text.slice(0, -(root.where.length + 3)) : root.text
        elide: Text.ElideRight
        color: root.mine ? Commons.Color.foreground : Theme.dim
        font.bold: root.mine
        font.family: Style.font.family
        font.pixelSize: Style.font.bodySmall
      }
      Text {
        Layout.fillWidth: true
        visible: root.hasFile
        text: root.where
        elide: Text.ElideMiddle
        color: root.hot ? Theme.accent : Theme.faint
        font.underline: root.hot
        font.family: Style.font.family
        font.pixelSize: Style.font.caption
      }
    }
    Icon { visible: root.hasFile; Layout.alignment: Qt.AlignVCenter; glyph: Model.Glyph.openExternal; size: Style.font.bodySmall; color: root.hot ? Theme.dim : "transparent" }
  }

  readonly property bool hot: hasFile && (hoverH.hovered || hasCursor)
  HoverHandler { id: hoverH; cursorShape: root.hasFile ? Qt.PointingHandCursor : Qt.ArrowCursor }
  TapHandler { onTapped: if (root.hasFile) root.clicked() }
}
