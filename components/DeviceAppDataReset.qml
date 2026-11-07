import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../Model.js" as Model

// App data › Reset: put the app back to its first launch. Wipe data deletes the container's contents
// (the app stays installed); Clean reinstall removes the app and installs your last build of it again.
// Both ask the daemon for a dry run first and show exactly what goes, then run with confirm: true.
// Daemon: appdata.reset {bundleId, mode: wipe|reinstall, confirm}.
ColumnLayout {
  id: root
  required property var view                 // DeviceAppDataView
  readonly property var app: view.app
  readonly property var svc: view.svc

  property string mode: "wipe"                // wipe | reinstall: what the sheet is about
  property string phase: "idle"               // idle | checking | ready | running | error
  property var plan: null                     // the dry run's reply
  property var error: null
  readonly property string appName: view.appName
  property real buttonWidth: 0
  signal done()                               // a reset ran: the other panes' data is stale

  spacing: Style.spacing.lg

  function begin(m) {
    root.svc.refreshApps()
    root.mode = m
    root.plan = null
    root.error = null
    root.phase = "checking"
    sheet.open()
    root._dryRun()
  }
  function _dryRun() {
    var bid = root.view.bundleId, m = root.mode
    root.phase = "checking"
    root.error = null
    root.svc.request("appdata.reset", { bundleId: bid, mode: m, confirm: false }, function(ok, d) {
      if (!sheet.opened || m !== root.mode) return
      if (ok) { root.plan = root.view.norm(d); root.phase = "ready" }
      else { root.error = d; root.phase = "error" }
    }, 120000)
  }
  // while it runs: deleting (stopping the app first, waiting for iOS to save its settings), then the install
  // (a wipe installs the same build again so iOS drops its cached settings; a reinstall installs it fresh)
  property string step: ""                    // delete | install
  property real installPercent: -1
  property real startedAt: 0
  property int elapsed: 0
  Timer { interval: 1000; repeat: true; running: root.phase === "running"; onTriggered: root.elapsed = Math.round((Date.now() - root.startedAt) / 1000) }
  Connections {
    target: root.svc
    function onDaemonEvent(name, data) {
      if (name !== "installProgress" || root.phase !== "running" || !data) return
      root.step = "install"
      // over Wi-Fi the install reports no numbers (or only 0): keep the bar moving until real ones arrive
      var pct = Number(data.percent)
      if (isFinite(pct) && pct > 0) root.installPercent = pct
    }
  }

  function run() {
    var bid = root.view.bundleId, m = root.mode, name = root.appName
    root.phase = "running"
    root.step = "delete"
    root.installPercent = -1
    root.startedAt = Date.now()
    root.elapsed = 0
    // a running app is stopped first (stopApp), so it can't write new files while they're deleted
    root.svc.request("appdata.reset", { bundleId: bid, mode: m, confirm: true, stopApp: true }, function(ok, d) {
      if (!ok) { root.error = d; root.phase = "error"; return }
      root.phase = "idle"
      sheet.close()
      var what = d.removed !== undefined ? Model.plural(d.removed, "item") + " removed" : ""
      var failed = d.failed && d.failed.length ? d.failed.length : 0
      if (failed) root.app.toast(Model.plural(failed, "item") + " couldn't be removed", "warn")
      else if (m === "reinstall") root.app.toast("Reinstalled " + name + (what ? ": " + what : ""), "ok")
      // a wipe without the install (no build, or it failed): iOS may hand the app its old settings
      else if (d.refreshed === false || d.warning || d.refreshError)
        root.app.toast("Wiped " + name + (what ? ": " + what : "") + ".\niOS may still hand it its old settings", "warn")
      else root.app.toast("Wiped " + name + (what ? ": " + what : ""), "ok")
      root.done()
      root.svc.refreshApps()
    }, 15 * 60000)
  }

  // ---- the two actions
  component ResetCard: Rectangle {
    id: card
    property string glyph: ""
    property string title: ""
    property string text: ""
    property string buttonText: ""
    signal go()
    Layout.fillWidth: true
    Layout.alignment: Qt.AlignTop
    implicitHeight: cardCol.implicitHeight + Style.spacing.xxl * 2
    radius: Style.cornerRadius
    color: Theme.surface
    border.width: Style.normalBorderWidth
    border.color: Theme.hairline
    // narrow cards put the button under the text instead of squeezing it
    readonly property bool roomy: width > Style.space(560)
    GridLayout {
      id: cardCol
      anchors.left: parent.left; anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      anchors.leftMargin: Style.spacing.xxl; anchors.rightMargin: Style.spacing.xxl
      columns: card.roomy ? 3 : 2
      columnSpacing: Style.spacing.xxl
      rowSpacing: Style.spacing.lg
      FontMetrics { id: cardFm; font: cardTitle.font }
      Item {
        Layout.row: 0; Layout.column: 0
        Layout.alignment: Qt.AlignTop
        Layout.preferredWidth: Style.font.iconLarge * 1.4
        Layout.preferredHeight: cardFm.height
        Icon { anchors.centerIn: parent; glyph: card.glyph; size: Style.font.iconLarge; color: Theme.dim }
      }
      ColumnLayout {
        Layout.row: 0; Layout.column: 1
        Layout.fillWidth: true
        spacing: Style.spacing.xs
        AppText { id: cardTitle; Layout.fillWidth: true; text: card.title; strong: true; wrapMode: Text.WordWrap }
        AppText { Layout.fillWidth: true; text: card.text; size: "small"; tone: "dim"; wrapMode: Text.WordWrap }
      }
      ActionButton {
        id: cardButton
        // both cards' buttons as wide as the wider one, so their texts end at the same edge
        Layout.alignment: card.roomy ? Qt.AlignVCenter : Qt.AlignLeft
        Layout.column: card.roomy ? 2 : 1
        Layout.row: card.roomy ? 0 : 1
        Layout.preferredWidth: Math.max(implicitWidth, root.buttonWidth)
        text: card.buttonText; glyph: card.glyph
        onClicked: card.go()
        onImplicitWidthChanged: root.buttonWidth = Math.max(root.buttonWidth, implicitWidth)
        Component.onCompleted: root.buttonWidth = Math.max(root.buttonWidth, implicitWidth)
      }
    }
  }

  ResetCard {
    glyph: Model.Glyph.eraser
    title: "Wipe data"
    text: "Deletes everything " + root.appName + " saved: its files, defaults and databases. The app stays installed."
    buttonText: "Wipe data…"
    onGo: root.begin("wipe")
  }
  ResetCard {
    glyph: Model.Glyph.reinstall
    title: "Clean reinstall"
    text: "Removes " + root.appName + " with all its data, then installs your last build of it again."
    buttonText: "Clean reinstall…"
    onGo: root.begin("reinstall")
  }
  AppText {
    Layout.fillWidth: true
    text: "Both show what would be removed first. Nothing is deleted until you confirm."
    size: "caption"; tone: "faint"; wrapMode: Text.WordWrap
    horizontalAlignment: Text.AlignRight
  }

  // ---- dry run → confirm
  Item {
    visible: false
    Sheet {
      id: sheet
      title: root.mode === "reinstall" ? "Clean reinstall of " + root.appName + "?" : "Wipe " + root.appName + "'s data?"
      subtitle: root.phase === "checking" ? "Looking at what's on the iPhone…" : "Nothing is deleted until you confirm."
      glyph: root.mode === "reinstall" ? Model.Glyph.reinstall : Model.Glyph.eraser
      preferredWidth: Style.space(600)
      // no silent close while it runs; its own bar shows the progress
      dismissible: root.phase !== "running"

      LoadingState {
        Layout.fillWidth: true
        visible: root.phase === "checking"
        text: "Listing the app's files…"
      }
      // while it runs: the step, then the install's progress, on the sheet's content edges
      ProgressTrack {
        Layout.fillWidth: true
        visible: root.phase === "running"
        indeterminate: root.step !== "install" || root.installPercent <= 0
        value: Math.max(0, root.installPercent) / 100
        label: root.step === "install"
               ? (root.mode === "reinstall" ? "Installing " + root.appName + " again…" : "Reinstalling " + root.appName + " so iOS forgets its old settings…")
               : "Deleting " + (root.plan ? Model.plural(root.plan.count, "item") : "the app's data") + "…"
        // a percent once the install reports one, else the seconds so far
        trailing: root.step === "install" && root.installPercent > 0 ? Math.round(root.installPercent) + "%" : Model.formatDuration(root.elapsed)
      }

      AppText {
        Layout.fillWidth: true
        // the dry run's own answer counts too: an app started outside this window (an agent) isn't in the list yet
        visible: (root.view.running || (!!root.plan && !!root.plan.running)) && (root.phase === "ready" || root.phase === "running")
        text: root.appName + " is running, so it's stopped first."
        size: "small"; tone: "warn"; wrapMode: Text.WordWrap
      }

      ErrorState {
        Layout.fillWidth: true
        visible: root.phase === "error" && !(root.error && root.error.code === "app-running")
        banner: true
        app: root.app
        error: root.error
        title: root.error && root.error.code === "no-build" ? "No build to reinstall" : "Couldn't reset the app"
        showRetry: false
        onFixed: sheet.close()
      }
      DeviceAppDataRunning {
        Layout.fillWidth: true
        visible: root.phase === "error" && !!root.error && root.error.code === "app-running"
        view: root.view
        text: "Stop it, then check again."
      }

      // what goes
      ColumnLayout {
        Layout.fillWidth: true
        visible: !!root.plan && (root.phase === "ready" || root.phase === "running")
        spacing: Style.spacing.lg
        AppText {
          Layout.fillWidth: true
          wrapMode: Text.WordWrap
          text: {
            var p = root.plan
            if (!p) return ""
            var what = Model.plural(p.count, "item") + " (" + (p.files !== undefined ? Model.plural(p.files, "file") + ", " : "") + Model.formatBytes(p.bytes) + ")"
            if (root.mode === "reinstall") return p.count === 0 ? "Removes the app, which has no saved data." : "Removes the app and its " + what + ":"
            return p.count === 0 ? "The app's container is already empty." : "Deletes " + what + ":"
          }
        }
        Rectangle {
          Layout.fillWidth: true
          visible: !!root.plan && root.plan.items && root.plan.items.length > 0
          implicitHeight: Math.min(items.implicitHeight, Style.space(240)) + Style.normalBorderWidth * 2
          radius: Style.cornerRadius
          color: "transparent"
          border.width: Style.normalBorderWidth
          border.color: Theme.hairline
          clip: true
          Flickable {
            id: itemsFlick
            anchors.fill: parent
            anchors.margins: Style.normalBorderWidth
            contentHeight: items.implicitHeight
            boundsBehavior: Flickable.StopAtBounds
            clip: true
            ScrollBar.vertical: ScrollBar { policy: itemsFlick.contentHeight > itemsFlick.height ? ScrollBar.AlwaysOn : ScrollBar.AlwaysOff }
            Column {
              id: items
              width: itemsFlick.width
              Repeater {
                model: root.plan && root.plan.items ? root.plan.items : []
                delegate: Rectangle {
                  required property var modelData
                  required property int index
                  width: items.width
                  implicitHeight: itemRow.implicitHeight + Style.spacing.sm * 2
                  color: index % 2 === 1 ? Theme.surface : "transparent"
                  RowLayout {
                    id: itemRow
                    anchors.left: parent.left; anchors.right: parent.right
                    anchors.verticalCenter: parent.verticalCenter
                    anchors.leftMargin: Style.spacing.lg; anchors.rightMargin: Style.spacing.xxl
                    spacing: Style.spacing.lg
                    // a fixed slot: file and folder glyphs differ in width, the paths start on one edge
                    Item {
                      Layout.preferredWidth: Style.font.bodySmall * 1.4
                      Layout.preferredHeight: Style.font.bodySmall
                      Icon { anchors.centerIn: parent; glyph: modelData.kind === "dir" ? Model.Glyph.folder : Model.Glyph.file; size: Style.font.bodySmall; color: modelData.kind === "dir" ? Commons.Color.accent : Theme.faint }
                    }
                    AppText { Layout.fillWidth: true; text: modelData.path; size: "small"; elide: Text.ElideMiddle }
                    AppText {
                      text: {
                        var p = []
                        if (modelData.kind === "dir") p.push(modelData.files ? Model.plural(modelData.files, "file") : "empty")
                        if (modelData.size) p.push(Model.formatBytes(modelData.size))
                        return p.join("  ·  ")
                      }
                      size: "small"; tone: "faint"
                    }
                  }
                }
              }
            }
          }
        }
        AppText {
          Layout.fillWidth: true
          visible: !!root.plan
          text: !root.plan ? "" : root.mode === "reinstall" ? "Then installs " + (root.plan.project ? "the last build of " + root.plan.project : "your last build") + " again."
                : "Then installs the same build over it, so iOS drops its cached settings."
          wrapMode: Text.WordWrap
        }
        AppText {
          Layout.fillWidth: true
          visible: !!root.plan && !!root.plan.truncated
          text: root.plan ? "The first " + (root.plan.items || []).length + " are listed; all " + root.plan.count + " go." : ""
          size: "small"; tone: "faint"; wrapMode: Text.WordWrap
        }
      }

      footer: [
        ActionButton { text: "Cancel"; kind: "ghost"; enabled: root.phase !== "running"; onClicked: sheet.close() },
        ActionButton {
          visible: root.phase === "error"
          text: "Check again"; glyph: Model.Glyph.refresh
          onClicked: root._dryRun()
        },
        ActionButton {
          visible: root.phase !== "error"
          text: root.mode === "reinstall" ? "Reinstall" : root.plan && root.plan.count ? "Delete " + Model.plural(root.plan.count, "item") : "Wipe data"
          glyph: root.mode === "reinstall" ? Model.Glyph.reinstall : Model.Glyph.trash
          kind: "danger"
          busy: root.phase === "running"
          disabled: root.phase !== "ready" && root.phase !== "running" || (root.mode === "wipe" && !!root.plan && root.plan.count === 0)
          tooltip: root.phase === "checking" ? "Checking what's on the iPhone…"
                   : root.mode === "wipe" && root.plan && root.plan.count === 0 ? "There's nothing to delete" : ""
          onClicked: root.run()
        }
      ]
    }
  }
}
