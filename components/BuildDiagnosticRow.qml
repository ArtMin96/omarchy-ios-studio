import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../Model.js" as Model

// One build diagnostic in the build drawer's Problems tab and Logs › Builds (projects slice).
// Compiler problems have a file:line, and a click opens it in the editor. Linker, package, signing,
// device, toolchain and config problems may have no location: they show their category and a hint.
CursorSurface {
  id: root
  property var diag: ({})
  property string projectPath: ""
  readonly property string sev: diag.severity || "error"
  readonly property bool hasFile: !!diag.file
  readonly property string category: diag.category || (hasFile ? "compiler" : "")
  readonly property var categoryLabels: ({ linker: "Linker", package: "Swift packages", signing: "Signing",
                                           device: "iPhone", toolchain: "Toolchain", config: "Project settings",
                                           other: "Build" })
  readonly property string relFile: {
    var f = String(diag.file || "")
    if (projectPath && f.indexOf(projectPath + "/") === 0) return f.substring(projectPath.length + 1)
    return f.split("/").slice(-2).join("/")
  }
  readonly property string where: {
    var parts = []
    if (category && category !== "compiler") parts.push(categoryLabels[category] || category)
    if (hasFile) parts.push(relFile + (diag.line ? ":" + diag.line + (diag.col ? ":" + diag.col : "") : ""))
    return parts.join(" · ")
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
        visible: text !== ""
        text: (root.diag.details || []).slice(0, 3).join("\n")
        tone: "faint"; size: "small"; wrapMode: Text.WordWrap; maximumLineCount: 3; elide: Text.ElideRight
      }
      AppText {
        Layout.fillWidth: true
        visible: !!root.diag.hint
        text: root.diag.hint || ""
        tone: "dim"; size: "small"; wrapMode: Text.WordWrap; maximumLineCount: 3; elide: Text.ElideRight
      }
      AppText {
        Layout.fillWidth: true
        visible: root.where !== ""
        text: root.where
        tone: "faint"; size: "small"; elide: Text.ElideMiddle
      }
    }
    Icon { Layout.alignment: Qt.AlignVCenter; glyph: Model.Glyph.openExternal; size: Style.font.bodySmall; color: root.hot && root.hasFile ? Theme.dim : "transparent" }
  }

  readonly property bool hot: hoverH.hovered || hasCursor
  HoverHandler { id: hoverH; cursorShape: root.hasFile ? Qt.PointingHandCursor : Qt.ArrowCursor; onHoveredChanged: root.hovered(hovered) }
  TapHandler { onTapped: if (root.hasFile) root.clicked() }
}
