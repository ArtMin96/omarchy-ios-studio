import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../common"
import "../../lib/Model.js" as Model

// Toolchain › Updates, at the top of the Toolchain page: the update channel (Tested = the versions this iOS Studio
// release was tested with, Latest = the newest upstream releases, untested), when it last checked + Check now,
// one row per tool with an update, notices, and progress while updating. When everything is current it is one
// quiet line and the channel choice. Data and actions: app.toolchain (updates, checkUpdates, update, …).
//   UpdatesSection { Layout.fillWidth: true; app: page.app }
ColumnLayout {
  id: root
  required property var app
  readonly property var svc: app.toolchain
  readonly property var st: svc.updates                       // `updates status`, null until read
  readonly property var rows: Model.updateRows(st)
  readonly property int updatable: rows.filter(function(c) { return c.state === "update" }).length
  readonly property var blocked: Model.updateBlocked(st)
  readonly property var notices: Model.updateNotices(st)
  readonly property var interrupted: Model.updateInterrupted(st)
  readonly property var failure: Model.updateFailureNote(svc.lastFailure)
  readonly property bool locked: svc.applying || svc.checking
  // the status line and channel stack below this width, like ListRow's narrow layout
  readonly property bool narrow: width > 0 && width < Style.space(560)
  property real now: Date.now()
  readonly property var line: Model.updatesLine(st, svc.checking, now)

  spacing: Style.spacing.md

  // "checked 3 h ago" stays true while the page is on screen (a fresh check reads "just now" on its own)
  Timer { interval: 60000; repeat: true; running: root.visible && root.app.windowFocused; onTriggered: root.now = Date.now() }
  onVisibleChanged: if (visible) root.now = Date.now()

  function openFix(fix) {
    if (!fix) return
    if (fix.url) Qt.openUrlExternally(fix.url)
    else if (fix.page) root.app.nav(fix.page, fix)
  }

  SectionHeader {
    Layout.fillWidth: true
    text: "Updates"
    detail: root.rows.length ? String(root.rows.length) : ""
    trailing: [
      ActionButton {
        visible: root.updatable > 1
        compact: true
        text: "Update all"
        glyph: Model.Glyph.download
        busy: root.svc.applying && root.svc.applyTarget === "all"
        enabled: !root.locked || busy
        onClicked: root.svc.updateAll()
      }
    ]
  }

  Skeleton {
    Layout.fillWidth: true
    visible: !root.st && root.svc.updatesResource.status === "loading"
    lines: 1
    rowHeight: Style.space(20)
  }
  ErrorState {
    Layout.fillWidth: true
    visible: !root.st && root.svc.updatesResource.status === "error"
    banner: true
    app: root.app
    error: root.svc.updatesResource.error
    onRetry: root.svc.updatesResource.load(true)
  }

  // one quiet line (+ Check now) beside the channel choice; stacked when narrow
  GridLayout {
    Layout.fillWidth: true
    visible: !!root.st
    columns: root.narrow ? 1 : 2
    columnSpacing: Style.spacing.xl
    rowSpacing: Style.spacing.md

    // the line keeps its natural width and Check now follows it; it wraps (at word boundaries) only when the
    // row can't fit both
    RowLayout {
      id: statusRow
      Layout.fillWidth: true
      spacing: Style.spacing.lg
      RowLayout {
        // the unwrapped width, measured apart from the Text (a wrapping Text's implicitWidth follows its width)
        readonly property real natural: lineGlyph.Layout.preferredWidth + spacing + Math.ceil(lineNatural.advanceWidth) + 1
        Layout.fillWidth: true
        Layout.preferredWidth: natural
        Layout.maximumWidth: natural
        Layout.alignment: Qt.AlignVCenter
        spacing: Style.spacing.md
        TextMetrics { id: lineNatural; font: lineText.font; text: root.line.text }
        // the glyph centred on the text's first line
        FontMetrics { id: lineMetrics; font: lineText.font }
        Item {
          id: lineGlyph
          Layout.preferredWidth: Style.font.bodySmall * 1.3
          Layout.preferredHeight: lineMetrics.height
          Layout.alignment: Qt.AlignTop
          Icon {
            anchors.centerIn: parent
            glyph: root.line.level === "warn" ? Model.Glyph.warn : root.line.level === "ok" ? Model.Glyph.checkCircle : Model.Glyph.clock
            size: Style.font.bodySmall
            color: root.line.level === "warn" ? Theme.warn : Theme.dim
          }
        }
        AppText {
          id: lineText
          Layout.fillWidth: true
          text: root.line.text
          size: "small"
          tone: "dim"
          wrapMode: Text.WordWrap
          HoverHandler { id: lineHover }
          PanelToolTip { visible: lineHover.hovered && root.line.tip !== ""; text: root.line.tip }
        }
      }
      ActionButton {
        id: checkNow
        Layout.alignment: Qt.AlignVCenter
        compact: true
        kind: "ghost"
        text: "Check now"
        glyph: Model.Glyph.refresh
        busy: root.svc.checking
        enabled: !root.svc.applying
        onClicked: root.svc.checkUpdates(true)
      }
      Item { Layout.fillWidth: true }
    }

    ButtonGroupLite {
      id: channelChoice
      Layout.alignment: root.narrow ? Qt.AlignLeft : (Qt.AlignRight | Qt.AlignVCenter)
      options: [{ value: "tested", label: "Tested" }, { value: "latest", label: "Latest (untested)" }]
      value: root.svc.channel
      enabled: !root.svc.applying && root.svc.pendingChannel === ""
      opacity: enabled ? 1 : 0.45
      onPicked: function(v) { root.svc.setChannel(v) }
      HoverHandler { id: channelHover }
      PanelToolTip {
        visible: channelHover.hovered
        text: "Tested: the versions this iOS Studio release was tested with.\nLatest: the newest releases, not tested with iOS Studio yet."
      }
    }
  }

  // one row per tool with an update
  Repeater {
    model: root.rows
    delegate: ListRow {
      id: urow
      required property var modelData
      readonly property bool back: Model.updateIsBack(root.st, modelData)
      readonly property bool running: root.svc.applying && root.svc.applyStep === modelData.id
      Layout.fillWidth: true
      glyph: Model.toolchainGlyph(modelData.id)
      glyphColor: Theme.dim
      title: modelData.name
      subtitle: Model.updateVersions(modelData, root.st)
      // pill, notes and button share one centre line, a normal control gap apart (as the Installed rows)
      trailing: [
        Row {
          anchors.verticalCenter: parent ? parent.verticalCenter : undefined
          spacing: Style.spacing.md
          StatusPill {
            anchors.verticalCenter: parent.verticalCenter
            visible: !!urow.modelData.untested
            level: "warn"
            text: "untested"
            quiet: true
          }
          IconButton {
            anchors.verticalCenter: parent.verticalCenter
            visible: !!urow.modelData.notesUrl
            glyph: Model.Glyph.openExternal
            tooltip: "Release notes"
            onClicked: Qt.openUrlExternally(urow.modelData.notesUrl)
          }
          ActionButton {
            anchors.verticalCenter: parent.verticalCenter
            compact: true
            text: urow.back ? "Use tested" : "Update"
            glyph: urow.back ? Model.Glyph.reinstall : Model.Glyph.download
            busy: urow.running
            enabled: !root.locked || urow.running
            onClicked: root.svc.update(urow.modelData.id)
          }
        }
      ]
    }
  }

  // while updating (also a switch back to Tested, which may have no rows); on the header's edges like the status
  // line. Cancel is centred on the bar and its label together, as DeviceTransferBar's.
  RowLayout {
    Layout.fillWidth: true
    visible: root.svc.applying
    spacing: Style.spacing.xl
    ProgressTrack {
      id: applyTrack
      Layout.fillWidth: true
      Layout.alignment: Qt.AlignVCenter
      value: root.svc.applyFraction >= 0 ? root.svc.applyFraction : 0
      indeterminate: root.svc.applyFraction < 0
      label: root.svc.applyDetail
    }
    ActionButton {
      Layout.alignment: Qt.AlignVCenter
      compact: true
      kind: "ghost"
      text: "Cancel"
      glyph: Model.Glyph.close
      onClicked: root.svc.cancelUpdate()
    }
  }

  // an update whose process died mid-way: its previous version is set aside until it's put back
  RowLayout {
    Layout.fillWidth: true
    visible: !!root.interrupted
    spacing: Style.spacing.lg
    QuietNote {
      Layout.fillWidth: true
      level: "warn"
      glyph: Model.Glyph.warn
      title: root.interrupted ? root.interrupted.title : ""
      text: root.interrupted ? root.interrupted.text : ""
    }
    ActionButton {
      Layout.rightMargin: Style.spacing.md
      compact: true
      text: "Put back"
      glyph: Model.Glyph.reinstall
      busy: root.svc.applying && root.svc.applyTarget === "restore"
      enabled: !root.locked || busy
      onClicked: root.svc.putBack()
    }
  }

  // the last update failed its check and was put back: stays until a good check or update, or Dismiss
  RowLayout {
    Layout.fillWidth: true
    visible: !!root.failure && !root.svc.applying
    spacing: Style.spacing.lg
    QuietNote {
      Layout.fillWidth: true
      level: "warn"
      glyph: Model.Glyph.warn
      title: root.failure ? root.failure.title : ""
      text: root.failure ? root.failure.text : ""
    }
    IconButton {
      Layout.rightMargin: Style.spacing.md
      glyph: Model.Glyph.close
      tooltip: "Dismiss"
      onClicked: root.svc.dismissFailure()
    }
  }

  // newer versions that can't be installed yet
  Repeater {
    model: root.blocked
    delegate: QuietNote {
      required property var modelData
      Layout.fillWidth: true
      glyph: Model.toolchainGlyph(modelData.id)
      title: modelData.name + " " + Model.updateVersion(modelData.latest) + " can't be installed yet"
      text: modelData.blockedReason || ""
    }
  }

  // notices (SDK, Xcode, Swift, upstream xtool): information only, with a fix where there is one
  Repeater {
    model: root.notices
    delegate: RowLayout {
      id: nrow
      required property var modelData
      Layout.fillWidth: true
      spacing: Style.spacing.lg
      QuietNote {
        Layout.fillWidth: true
        glyph: nrow.modelData.level === "warn" ? Model.Glyph.warn : Model.Glyph.info
        level: nrow.modelData.level === "warn" ? "warn" : ""
        title: nrow.modelData.message
      }
      // right edge with the rows' buttons (ListRow's right padding)
      ActionButton {
        Layout.rightMargin: Style.spacing.md
        visible: !!nrow.modelData.fix
        compact: true
        text: nrow.modelData.fix ? nrow.modelData.fix.label || "Open" : ""
        onClicked: root.openFix(nrow.modelData.fix)
      }
    }
  }
}
