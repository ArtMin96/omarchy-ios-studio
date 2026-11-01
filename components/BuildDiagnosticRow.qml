import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../Model.js" as Model

// One compiler diagnostic in the build drawer's Problems tab (projects slice).
// Click opens the file at that line in the editor.
CursorSurface {
  id: root
  property var diag: ({})
  property string projectPath: ""
  readonly property string sev: diag.severity || "error"
  readonly property string relFile: {
    var f = String(diag.file || "")
    if (projectPath && f.indexOf(projectPath + "/") === 0) return f.substring(projectPath.length + 1)
    return f.split("/").slice(-2).join("/")
  }

  signal clicked()
  signal hovered(bool h)

  implicitHeight: row.implicitHeight + Style.spacing.lg * 2
  implicitWidth: Style.space(320)

  RowLayout {
    id: row
    anchors.fill: parent
    anchors.leftMargin: Style.spacing.rowPaddingX
    anchors.rightMargin: Style.spacing.md
    spacing: Style.spacing.xl

    Icon {
      Layout.alignment: Qt.AlignTop
      glyph: root.sev === "error" ? Model.Glyph.error : root.sev === "warning" ? Model.Glyph.warn : Model.Glyph.info
      color: root.sev === "error" ? Theme.error : root.sev === "warning" ? Theme.warn : Theme.dim
      size: Style.font.icon
    }
    ColumnLayout {
      Layout.fillWidth: true
      spacing: Style.spacing.xs
      AppText { Layout.fillWidth: true; text: root.diag.message || ""; wrapMode: Text.WordWrap; maximumLineCount: 4; elide: Text.ElideRight }
      AppText {
        Layout.fillWidth: true
        text: root.relFile + ":" + root.diag.line + (root.diag.col ? ":" + root.diag.col : "")
        tone: "faint"; size: "small"; elide: Text.ElideMiddle
      }
    }
    Icon { Layout.alignment: Qt.AlignVCenter; glyph: Model.Glyph.openExternal; size: Style.font.bodySmall; color: root.hot ? Theme.dim : "transparent" }
  }

  readonly property bool hot: hoverH.hovered || hasCursor
  HoverHandler { id: hoverH; cursorShape: Qt.PointingHandCursor; onHoveredChanged: root.hovered(hovered) }
  TapHandler { onTapped: root.clicked() }
}
