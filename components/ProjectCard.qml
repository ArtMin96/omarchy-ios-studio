import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../Model.js" as Model

// One app project on the Projects page (projects slice).
//   ProjectCard { project: modelData; svc: app.projects; hasCursor: …; onRun: … }
// Run is the card's primary action; the rest live behind "More", which expands in place.
BorderSurface {
  id: root

  property var project: ({})
  property var svc: null
  property bool hasCursor: false
  property bool expanded: false
  readonly property bool isBuilding: !!(svc && svc.building && svc.build.path === project.path)
  readonly property bool otherBuilding: !!(svc && svc.building && !isBuilding)
  readonly property var last: project.lastBuild || null
  readonly property var signing: project.signing || null
  readonly property bool hot: hasCursor || hover.hovered
  readonly property bool narrow: width < Style.space(640)

  signal run()
  signal buildIpa()
  signal checkBuild()
  signal renew()
  signal openEditor()
  signal openFolder()
  signal removeFromList()
  signal toggleAutoRenew()
  signal showBuild()
  signal hovered(bool h)

  radius: Style.cornerRadius
  color: hot ? Theme.surfaceRaised : Theme.surface
  borderSpec: Border.flat(isBuilding || hasCursor ? Util.alpha(Commons.Color.accent, hasCursor ? 0.75 : 0.55)
                                       : (hover.hovered ? Util.alpha(Commons.Color.foreground, 0.22) : Theme.hairline), Style.normalBorderWidth)
  padding: Style.spacing.panelPadding
  implicitHeight: col.implicitHeight + contentTopInset + contentBottomInset
  Behavior on color { ColorAnimation { duration: 140 } }

  HoverHandler { id: hover; onHoveredChanged: root.hovered(hovered) }

  function lastBuildPill() {
    if (!last) return null
    var kind = { run: "Ran", renew: "Re-signed", ipa: "Built .ipa", install: "Reinstalled", compile: "Built" }[last.kind] || "Built"
    if (last.result === "ok") return { level: "ok", text: kind + " " + Model.relativeTime(last.time) }
    if (last.result === "cancelled") return { level: "info", text: "Cancelled " + Model.relativeTime(last.time) }
    var e = last.errors > 0 ? " · " + Model.plural(last.errors, "error") : ""
    return { level: "error", text: "Failed " + Model.relativeTime(last.time) + e }
  }

  function signingPill() {
    if (!signing || signing.daysLeft === null || signing.daysLeft === undefined) return null
    var d = signing.daysLeft
    if (d <= 0) return { level: "error", text: "Signing expired" }
    if (d < 1) return { level: "warn", text: "Expires in " + Math.max(1, Math.round(d * 24)) + " h" }
    if (d < 2) return { level: "warn", text: "Expires tomorrow" }
    return { level: "ok", text: Math.floor(d) + " days left" }
  }

  ColumnLayout {
    id: col
    anchors.fill: parent
    anchors.topMargin: root.contentTopInset
    anchors.bottomMargin: root.contentBottomInset
    anchors.leftMargin: root.contentLeftInset
    anchors.rightMargin: root.contentRightInset
    spacing: Style.spacing.lg

    GridLayout {
      Layout.fillWidth: true
      columns: root.narrow ? 2 : 3
      columnSpacing: Style.spacing.xxl
      rowSpacing: Style.spacing.lg

      // App icon, or a monogram tile
      Rectangle {
        Layout.preferredWidth: Style.space(48)
        Layout.preferredHeight: Style.space(48)
        Layout.alignment: Qt.AlignTop
        radius: Style.cornerRadius + Style.space(4)
        color: Util.alpha(Commons.Color.accent, 0.14)
        border.width: Style.normalBorderWidth
        border.color: Util.alpha(Commons.Color.accent, 0.28)
        clip: true
        Image {
          id: iconImg
          anchors.fill: parent
          visible: status === Image.Ready
          source: root.project.icon ? "file://" + root.project.icon : ""
          sourceSize.width: Style.space(96)
          sourceSize.height: Style.space(96)
          fillMode: Image.PreserveAspectCrop
          asynchronous: true
          smooth: true
        }
        AppText {
          anchors.centerIn: parent
          visible: !iconImg.visible
          text: String(root.project.name || "?").replace(/[^A-Za-z0-9]/g, "").substring(0, 2).toUpperCase() || "?"
          size: "title"
          strong: true
          tone: "accent"
        }
      }

      ColumnLayout {
        Layout.fillWidth: true
        spacing: Style.spacing.xs

        RowLayout {
          Layout.fillWidth: true
          spacing: Style.spacing.lg
          AppText {
            text: root.project.name || ""
            size: "title"
            strong: true
            elide: Text.ElideRight
            Layout.fillWidth: true
            Layout.maximumWidth: implicitWidth + 1
          }
          StatusPill {
            readonly property var pill: root.isBuilding ? { level: "busy", text: "Building…" } : root.lastBuildPill()
            visible: pill !== null
            level: pill ? pill.level : "info"
            text: pill ? pill.text : ""
            quiet: true
          }
          Item { Layout.fillWidth: true }
        }
        AppText {
          Layout.fillWidth: true
          text: root.project.bundleId || "No bundle ID in xtool.yml"
          tone: root.project.bundleId ? "dim" : "warn"
          size: "small"
          elide: Text.ElideRight
        }
        RowLayout {
          Layout.fillWidth: true
          spacing: Style.spacing.lg
          Icon { glyph: Model.Glyph.folder; size: Style.font.bodySmall; color: Theme.faint }
          // The branch follows the path (the path only takes what it needs), so it sits in the same
          // place whether or not the card shows a signing badge on the right.
          AppText { text: root.project.displayPath || ""; tone: "faint"; size: "small"; elide: Text.ElideMiddle; Layout.fillWidth: true; Layout.maximumWidth: implicitWidth + 1; Layout.minimumWidth: Style.space(60) }
          Icon { visible: !!root.project.branch; glyph: Model.Glyph.link; size: Style.font.bodySmall; color: Theme.faint; Layout.leftMargin: Style.spacing.md }
          AppText { visible: !!root.project.branch; text: root.project.branch || ""; tone: "faint"; size: "small"; elide: Text.ElideRight; Layout.maximumWidth: Style.space(160) }
          Item { Layout.fillWidth: true }
        }
      }

      RowLayout {
        Layout.columnSpan: root.narrow ? 2 : 1
        Layout.alignment: Qt.AlignRight | Qt.AlignVCenter
        spacing: Style.spacing.xl
      StatusPill {
        readonly property var pill: root.signingPill()
        visible: pill !== null && !root.isBuilding
        level: pill ? pill.level : "info"
        text: pill ? pill.text : ""
        glyph: Model.Glyph.signature
        Layout.alignment: Qt.AlignVCenter
      }

      IconButton {
        Layout.alignment: Qt.AlignVCenter
        glyph: root.expanded ? Model.Glyph.chevronUp : Model.Glyph.dots
        tooltip: root.expanded ? "Fewer actions" : "More actions"
        onClicked: root.expanded = !root.expanded
      }

      ActionButton {
        Layout.alignment: Qt.AlignVCenter
        visible: !root.isBuilding
        text: "Run"
        glyph: Model.Glyph.play
        kind: "primary"
        disabled: root.otherBuilding || root.project.generated
        tooltip: root.project.generated ? "Built by its own scripts" : (root.svc && !root.svc.canRun ? "Connect your iPhone first" : "Build, install and open on your iPhone (Enter)")
        onClicked: root.run()
      }
      ActionButton {
        Layout.alignment: Qt.AlignVCenter
        visible: root.isBuilding
        text: "View build"
        glyph: Model.Glyph.chevronRight
        onClicked: root.showBuild()
      }
      }
    }

    // Live progress while this project builds
    ProgressTrack {
      Layout.fillWidth: true
      visible: root.isBuilding
      value: root.svc ? root.svc.build.fraction || 0 : 0
      indeterminate: root.svc ? !(root.svc.build.fraction > 0) : true
      label: root.svc ? root.svc.build.detail || "" : ""
      trailing: root.svc && root.svc.build.eta ? "about " + Model.formatDuration(root.svc.build.eta) + " left" : ""
    }

    AppText {
      Layout.fillWidth: true
      visible: !!root.project.note
      text: root.project.note || ""
      tone: "warn"
      size: "small"
      wrapMode: Text.WordWrap
    }

    // More actions, expanded in place
    Rectangle { Layout.fillWidth: true; visible: root.expanded; height: Style.normalBorderWidth; color: Theme.hairline }
    Flow {
      Layout.fillWidth: true
      visible: root.expanded
      spacing: Style.spacing.md
      ActionButton { text: "Check build"; glyph: Model.Glyph.hammer; kind: "ghost"; compact: true; enabled: !root.svc.building && !root.project.generated; tooltip: "Compile for iPhone without signing or installing (Shift+B)"; onClicked: root.checkBuild() }
      ActionButton { text: "Build .ipa"; glyph: Model.Glyph.package; kind: "ghost"; compact: true; enabled: !root.svc.building && !root.project.generated; tooltip: "Build and sign an installable .ipa file"; onClicked: root.buildIpa() }
      ActionButton { text: "Re-sign & reinstall"; glyph: Model.Glyph.sync; kind: "ghost"; compact: true; enabled: !root.svc.building && !root.project.generated; tooltip: "Free Apple IDs sign apps for 7 days. This renews it for another week."; onClicked: root.renew() }
      ActionButton { text: root.project.autoRenew ? "Auto-renew: on" : "Auto-renew: off"; glyph: Model.Glyph.timer; kind: "ghost"; compact: true; tooltip: "Re-sign this app automatically before it expires"; onClicked: root.toggleAutoRenew() }
      ActionButton { text: "Open in editor"; glyph: Model.Glyph.code; kind: "ghost"; compact: true; onClicked: root.openEditor() }
      ActionButton { text: "Open folder"; glyph: Model.Glyph.folderOpen; kind: "ghost"; compact: true; onClicked: root.openFolder() }
      ActionButton { text: "Remove from list"; glyph: Model.Glyph.trash; kind: "ghost"; compact: true; onClicked: root.removeFromList() }
    }
  }
}
