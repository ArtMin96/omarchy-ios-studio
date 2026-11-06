import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../Model.js" as Model

// One build diagnostic in the build drawer's Problems tab and Logs › Builds (projects slice).
// Compiler problems have a file:line, and a click opens it in the editor. Linker, package, signing,
// device, toolchain and config problems may have no location: they show their category and a hint.
// Issues from the pre-build project check (category "config" with a `check` id, see
// helper/project_check.py) carry a "Pre-build check" chip, so they read apart from xtool's output.
// Also used by ProjectCheckView, where every row is such an issue (showChip: false there).
CursorSurface {
  id: root
  property var diag: ({})
  property string projectPath: ""
  property bool showChip: true
  readonly property bool preBuild: diag.category === "config" && !!diag.check
  readonly property string sev: diag.severity || "error"
  readonly property bool hasFile: !!diag.file
  readonly property string category: diag.category || (hasFile ? "compiler" : "")
  readonly property var categoryLabels: ({ linker: "Linker", package: "Swift packages", signing: "Signing",
                                           device: "iPhone", toolchain: "Toolchain", config: "Project settings",
                                           other: "Build" })
  readonly property string relFile: {
    var f = String(diag.file || "")
    if (projectPath && f.indexOf(projectPath + "/") === 0) return f.substring(projectPath.length + 1)
    if (f !== "" && f.charAt(0) !== "/") return f      // already relative to the project (project check)
    return f.split("/").slice(-2).join("/")
  }
  readonly property string where: {
    var parts = []
    if (category && category !== "compiler" && !preBuild) parts.push(categoryLabels[category] || category)
    if (hasFile) parts.push(relFile + (diag.line ? ":" + diag.line + (diag.col ? ":" + diag.col : "") : ""))
    return parts.join(" · ")
  }

  // Glyph column: the same width as ProjectCheckView's summary glyph, so the texts share a left edge.
  readonly property real iconColumn: Style.font.iconLarge * 1.4
  // Narrower rows (the build drawer) put the chip at the start of the location line instead of
  // squeezing the message; same threshold as ListRow's narrow layout.
  readonly property bool chipBeside: preBuild && showChip && width >= Style.space(560)
  readonly property bool chipBelow: preBuild && showChip && !chipBeside

  signal clicked()
  signal hovered(bool h)

  FontMetrics { id: msgMetrics; font: msg.font }

  implicitHeight: row.implicitHeight + Style.spacing.lg * 2
  implicitWidth: Style.space(320)

  RowLayout {
    id: row
    anchors.fill: parent
    // Vertical insets here, not by centring: the glyph is top-aligned to the first line, so the
    // text column must start at the same y.
    anchors.topMargin: Style.spacing.lg
    anchors.bottomMargin: Style.spacing.lg
    anchors.leftMargin: Style.spacing.rowPaddingX
    anchors.rightMargin: Style.spacing.md
    spacing: Style.spacing.xl

    // The glyph is centred on the message's first line (its line box), not the row's top edge.
    Item {
      Layout.alignment: Qt.AlignTop
      Layout.preferredWidth: root.iconColumn
      Layout.preferredHeight: msgMetrics.height
      Icon {
        anchors.centerIn: parent
        glyph: root.sev === "error" ? Model.Glyph.error : root.sev === "warning" ? Model.Glyph.warn : Model.Glyph.info
        color: root.sev === "error" ? Theme.error : root.sev === "warning" ? Theme.warn : Theme.dim
        size: Style.font.icon
      }
    }
    ColumnLayout {
      Layout.fillWidth: true
      Layout.alignment: Qt.AlignTop
      spacing: Style.spacing.xs
      RowLayout {
        Layout.fillWidth: true
        spacing: Style.spacing.md
        AppText { id: msg; Layout.fillWidth: true; text: root.diag.message || ""; wrapMode: Text.WordWrap; maximumLineCount: 4; elide: Text.ElideRight }
        Badge {
          id: chip
          // Centred on the first line, like the glyph.
          Layout.alignment: Qt.AlignTop
          Layout.topMargin: Math.max(0, (msgMetrics.height - implicitHeight) / 2)
          visible: root.chipBeside
          level: "info"
          text: "Pre-build check"
        }
      }
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
      RowLayout {
        Layout.fillWidth: true
        visible: root.where !== "" || root.chipBelow
        spacing: Style.spacing.md
        Badge { visible: root.chipBelow; level: "info"; text: "Pre-build check" }
        AppText { Layout.fillWidth: true; visible: root.where !== ""; text: root.where; tone: "faint"; size: "small"; elide: Text.ElideMiddle }
      }
    }
    Icon { Layout.alignment: Qt.AlignVCenter; glyph: Model.Glyph.openExternal; size: Style.font.bodySmall; color: root.hot && root.hasFile ? Theme.dim : "transparent" }
  }

  readonly property bool hot: hoverH.hovered || hasCursor
  HoverHandler { id: hoverH; cursorShape: root.hasFile ? Qt.PointingHandCursor : Qt.ArrowCursor; onHoveredChanged: root.hovered(hovered) }
  TapHandler { onTapped: if (root.hasFile) root.clicked() }
}
