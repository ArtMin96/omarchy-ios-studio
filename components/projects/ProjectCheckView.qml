import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../common"
import "../../lib/Model.js" as Model

// Project settings check on a ProjectCard (projects slice): config mistakes xtool only reports late
// or never (misspelled xtool.yml keys, missing usage descriptions, icon, entitlements, bundle id,
// version), from `projects check` (helper/project_check.py) via ProjectService.check/checkFor.
//   ProjectCheckView { project: root.project; svc: root.svc; showClean: root.expanded }
// A one-line summary ("2 settings warnings"), expanding to the issues (BuildDiagnosticRow; a click
// opens the file at its line) with "Check again". Loading, error and "No problems found" states.
// Warnings only: nothing here blocks a build.
ColumnLayout {
  id: root

  property var project: ({})
  property var svc: null
  // Show the row when there's nothing to fix (or the first check is still running). The card
  // passes its "More" state, so a clean project doesn't add a line to every card.
  property bool showClean: false
  property bool open: false

  readonly property var entry: svc && project && project.path ? svc.checkFor(project.path) : null
  readonly property string checkState: entry ? entry.state : "none"
  readonly property var result: entry && entry.result ? entry.result : null
  readonly property var issues: result && result.issues ? result.issues : []
  readonly property int total: result ? (result.total !== undefined ? result.total : issues.length) : 0
  readonly property bool loading: checkState === "loading"
  readonly property bool failed: checkState === "error"
  readonly property int warnings: result && result.counts ? (result.counts.warning || 0) + (result.counts.error || 0) : issues.length
  readonly property string level: total === 0 ? "ok" : (warnings > 0 ? "warn" : "info")
  readonly property bool busyBuilding: !!(svc && svc.building && svc.build.path === project.path)

  onTotalChanged: if (total === 0) open = false
  visible: failed || total > 0 || (showClean && checkState !== "none")
  spacing: Style.spacing.sm

  function headline() {
    if (failed) return "Couldn't check this project's settings"
    if (loading && !result) return "Checking project settings…"
    if (total === 0) return "Project settings: no problems found"
    if (warnings === 0) return Model.plural(total, "settings note")
    return Model.plural(warnings, "settings warning") + (total > warnings ? " · " + Model.plural(total - warnings, "note") : "")
  }

  function detail() {
    if (failed) return Model.errorMessage(entry.error)
    if (loading && !result) return ""
    if (total === 0) return "xtool.yml, Info.plist, icon, entitlements and bundle ID look right."
    return root.open ? "Fix these before building; they don't stop a build." : "Found before building. Click to see them."
  }

  Rectangle { Layout.fillWidth: true; implicitHeight: Style.normalBorderWidth; color: Theme.hairline }

  // ---- summary row
  CursorSurface {
    id: head
    Layout.fillWidth: true
    implicitHeight: headRow.implicitHeight + Style.spacing.lg * 2
    readonly property bool canOpen: root.total > 0 && !root.failed
    hasCursor: canOpen && headHover.hovered

    // Same insets, glyph column and gap as the BuildDiagnosticRow issues below it, so the glyphs
    // share a centre line and the texts a left edge. The glyph is centred across the two lines,
    // as in ListRow.
    RowLayout {
      id: headRow
      anchors.fill: parent
      anchors.leftMargin: Style.spacing.rowPaddingX
      anchors.rightMargin: Style.spacing.md
      spacing: Style.spacing.xl

      Item {
        Layout.alignment: Qt.AlignVCenter
        Layout.preferredWidth: Style.font.iconLarge * 1.4
        Layout.preferredHeight: Style.font.iconLarge
        Spinner { anchors.centerIn: parent; visible: root.loading && !root.result; running: visible; size: Style.font.icon }
        Icon {
          anchors.centerIn: parent
          visible: !(root.loading && !root.result)
          glyph: root.failed ? Model.Glyph.error : root.total === 0 ? Model.Glyph.shieldCheck : Model.Glyph.warn
          color: root.failed ? Theme.error : Theme.levelColor(root.level)
          size: Style.font.iconLarge
        }
      }
      ColumnLayout {
        Layout.fillWidth: true
        spacing: Style.spacing.xxs
        AppText { Layout.fillWidth: true; text: root.headline(); strong: root.total > 0 || root.failed; elide: Text.ElideRight }
        AppText { Layout.fillWidth: true; visible: text !== ""; text: root.detail(); tone: root.failed ? "error" : "dim"; size: "small"; wrapMode: Text.WordWrap; maximumLineCount: 2; elide: Text.ElideRight }
      }
      ActionButton {
        Layout.alignment: Qt.AlignVCenter
        text: root.failed ? "Try again" : "Check again"
        glyph: Model.Glyph.refresh
        kind: "ghost"
        compact: true
        busy: root.loading
        disabled: root.busyBuilding
        tooltip: root.loading ? "Checking…" : root.busyBuilding ? "The build is checking this project already" : "Check xtool.yml, Info.plist, icon and entitlements again"
        onClicked: if (root.svc) root.svc.check(root.project)
      }
      // Same size and right inset as the rows' open-in-editor glyph, so the two line up.
      Icon {
        visible: head.canOpen
        glyph: root.open ? Model.Glyph.chevronUp : Model.Glyph.chevronDown
        color: Theme.dim
        size: Style.font.bodySmall
        Layout.alignment: Qt.AlignVCenter
      }
    }
    HoverHandler { id: headHover; cursorShape: head.canOpen ? Qt.PointingHandCursor : Qt.ArrowCursor }
    TapHandler { onTapped: if (head.canOpen) root.open = !root.open }
  }

  // ---- issues
  Repeater {
    model: root.open && !root.failed ? root.issues : []
    delegate: BuildDiagnosticRow {
      required property var modelData
      Layout.fillWidth: true
      diag: modelData
      projectPath: root.project.path || ""
      showChip: false
      onClicked: if (root.svc) root.svc.openInEditor(root.project, modelData.file, modelData.line, modelData.col)
    }
  }
  AppText {
    Layout.fillWidth: true
    Layout.leftMargin: Style.spacing.rowPaddingX + Style.font.iconLarge * 1.4 + Style.spacing.xl   // the rows' text edge
    visible: root.open && !root.failed && !!root.result && root.result.truncated === true
    text: "Showing " + root.issues.length + " of " + root.total + ". Fix these and check again to see the rest."
    tone: "faint"
    size: "small"
    wrapMode: Text.WordWrap
  }
}
