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
// Style warnings (category "style", from `format-lint`: swift-format and SwiftLint) read as
// "Style · <rule> · file:line" with a dim glyph (amber only for a rule set to error), so they never
// look like a failed build; a click opens the file at the line, like a compiler problem.
CursorSurface {
  id: root
  property var diag: ({})
  property string projectPath: ""
  property bool showChip: true
  readonly property bool preBuild: diag.category === "config" && !!diag.check
  readonly property bool style: diag.category === "style"
  readonly property string sev: diag.severity || "error"
  readonly property bool hasFile: !!diag.file
  readonly property string category: diag.category || (hasFile ? "compiler" : "")
  readonly property var categoryLabels: ({ linker: "Linker", package: "Swift packages", signing: "Signing",
                                           device: "iPhone", toolchain: "Toolchain", config: "Project settings", style: "Style",
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
    if (style && diag.rule) parts.push(String(diag.rule))
    if (hasFile) parts.push(relFile + (diag.line ? ":" + diag.line + (diag.col ? ":" + diag.col : "") : ""))
    return parts.join(" · ")
  }
  // `where` in pieces for the location line: the lead ("Style · rule"), " · ", the folder and the file
  // name:line. Short of room, the folder goes first (it hides rather than showing a stub), then the lead
  // elides; the file name:line stays whole unless it alone is wider than the row.
  readonly property string _loc: hasFile ? relFile + (diag.line ? ":" + diag.line + (diag.col ? ":" + diag.col : "") : "") : ""
  readonly property string whereLead: hasFile ? (where.length > _loc.length + 3 ? where.slice(0, where.length - _loc.length - 3) : "") : where
  readonly property string whereDir: hasFile ? relFile.slice(0, relFile.lastIndexOf("/") + 1) : ""
  readonly property string whereFile: root._loc.slice(root.whereDir.length)

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
        color: root.style ? (root.sev === "error" ? Theme.warn : Theme.dim)
               : root.sev === "error" ? Theme.error : root.sev === "warning" ? Theme.warn : Theme.dim
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
        Item {
          id: loc
          Layout.fillWidth: true
          visible: root.where !== ""
          implicitHeight: fileText.implicitHeight
          readonly property real fileW: Math.min(fileText.implicitWidth, width)
          readonly property real sepW: sepText.visible ? sepText.implicitWidth : 0
          readonly property real rest: Math.max(0, width - fileW - sepW)
          readonly property real dirRoom: rest - leadText.implicitWidth
          // the folder: whole, middle-elided while at least 6 of its characters show (plus the "…"), else hidden
          readonly property real dirW: dirText.implicitWidth <= dirRoom ? dirText.implicitWidth
                                       : dirRoom >= locMetrics.averageCharacterWidth * 8 ? dirRoom : 0
          FontMetrics { id: locMetrics; font: fileText.font }
          AppText {
            id: leadText
            width: Math.max(0, Math.min(implicitWidth, loc.rest - loc.dirW))
            visible: text !== "" && width > 0
            text: root.whereLead; tone: "faint"; size: "small"; elide: Text.ElideRight
          }
          AppText {
            id: sepText
            // after the lead's painted text, so an elided lead leaves no gap
            x: leadText.visible ? Math.min(leadText.width, leadText.contentWidth) : 0
            visible: root.whereLead !== "" && root.hasFile && leadText.visible
            text: " · "; tone: "faint"; size: "small"
          }
          AppText {
            id: dirText
            x: sepText.x + loc.sepW
            width: loc.dirW
            visible: loc.dirW > 0
            text: root.whereDir; tone: "faint"; size: "small"; elide: Text.ElideMiddle
          }
          AppText {
            id: fileText
            x: dirText.visible ? dirText.x + Math.min(dirText.width, dirText.contentWidth) : sepText.x + loc.sepW
            width: loc.fileW
            text: root.whereFile; tone: "faint"; size: "small"; elide: Text.ElideRight
          }
        }
      }
    }
    Icon { Layout.alignment: Qt.AlignVCenter; glyph: Model.Glyph.openExternal; size: Style.font.bodySmall; color: root.hot && root.hasFile ? Theme.dim : "transparent" }
  }

  readonly property bool hot: hoverH.hovered || hasCursor
  HoverHandler { id: hoverH; cursorShape: root.hasFile ? Qt.PointingHandCursor : Qt.ArrowCursor; onHoveredChanged: root.hovered(hovered) }
  TapHandler { onTapped: if (root.hasFile) root.clicked() }
}
