import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../common"
import "../../lib/Model.js" as Model

// Logs page › Device log tab (logs slice). Toolbar (start/stop, app, level, search, pause,
// copy, export, clear), a status line, and the live list. Empty states when there's no phone.
Item {
  id: root
  required property var app
  readonly property var svc: app.logs
  readonly property alias view: logView
  readonly property alias search: searchField

  readonly property bool ready: svc.deviceApi && !svc.deviceNeedsSetup && svc.deviceConnected

  // ---- actions (also used by the page's keys)
  function copySelected() {
    var t = logView.selectedText()
    if (t === "") { copyVisible(); return }
    svc.copyText(t, function(ok) { root.app.toast(ok ? "Copied the line" : "Couldn't copy", ok ? "ok" : "error") })
  }
  function copyVisible() {
    var n = logView.shownCount
    if (n === 0) { root.app.toast("Nothing to copy yet"); return }
    svc.copyText(logView.visibleText(), function(ok) { root.app.toast(ok ? "Copied " + n + " lines" : "Couldn't copy", ok ? "ok" : "error") })
  }
  function exportVisible() {
    var n = logView.shownCount
    if (n === 0) { root.app.toast("Nothing to export yet"); return }
    var path = svc.downloadsDir + "/iphone-log-" + svc.stamp() + ".txt"
    svc.writeFile(path, logView.visibleText() + "\n", function(ok) {
      root.app.toast(ok ? "Saved " + n + " lines to " + svc.prettyPath(path) : "Couldn't save the log", ok ? "ok" : "error", { timeout: 6000 })
    })
  }
  function togglePause() { logView.paused = !logView.paused; if (!logView.paused) logView.jumpToLatest() }

  Connections {
    target: root.svc
    function onEntries(batch) { logView.push(batch) }
    function onCleared() { logView.clear() }
  }

  readonly property var processOptions: {
    var opts = [{ value: "", label: "All processes" }]
    var own = svc.ownApps
    if (own.length > 1) opts.push({ value: "__mine", label: "All my apps" })
    for (var i = 0; i < own.length; i++) opts.push({ value: own[i].process, label: own[i].name })
    var cur = svc.effectiveProcess
    if (cur !== "" && cur !== "__mine" && !opts.some(function(o) { return o.value === cur })) opts.push({ value: cur, label: cur })
    return opts
  }

  readonly property var levelOptions: [
    { value: "debug", label: "Everything (debug)" },
    { value: "info", label: "Info and up" },
    { value: "default", label: "Default and up" },
    { value: "error", label: "Errors and faults" },
    { value: "fault", label: "Faults only" }
  ]

  ColumnLayout {
    anchors.fill: parent
    spacing: Style.spacing.xl
    visible: root.ready

    // ---- toolbar (two rows in a narrow window)
    GridLayout {
      id: bar
      readonly property bool narrow: root.width < Style.space(820)
      Layout.fillWidth: true
      columns: narrow ? 3 : 5
      columnSpacing: Style.spacing.lg
      rowSpacing: Style.spacing.md

      ActionButton {
        text: root.svc.streaming || root.svc.starting ? "Stop" : "Start"
        glyph: root.svc.streaming || root.svc.starting ? Model.Glyph.stop : Model.Glyph.play
        kind: root.svc.streaming || root.svc.starting ? "secondary" : "primary"
        busy: root.svc.starting || root.svc.stopping
        tooltip: root.svc.streaming ? "Stop reading the iPhone's log" : "Read the iPhone's log live"
        onClicked: root.svc.streaming || root.svc.starting ? root.svc.stop() : root.svc.start()
      }
      Dropdown {
        showLabel: false
        Layout.preferredWidth: Style.space(190)
        value: root.svc.effectiveProcess
        options: root.processOptions
        onChanged: function(v) { root.svc.setProcess(v) }
      }
      Dropdown {
        showLabel: false
        Layout.preferredWidth: Style.space(170)
        Layout.fillWidth: bar.narrow
        value: root.svc.levelFilter
        options: root.levelOptions
        onChanged: function(v) { root.svc.setLevel(v) }
      }
      FilterField {
        id: searchField
        Layout.fillWidth: true
        Layout.columnSpan: bar.narrow ? 2 : 1
        Layout.minimumWidth: Style.space(140)
        placeholder: "Search the log"
      }
      RowLayout {
      spacing: Style.spacing.lg
      IconButton {
        glyph: logView.paused ? Model.Glyph.play : Model.Glyph.pause
        tooltip: logView.paused ? "Resume (Space)" : "Pause the view (Space)"
        foreground: logView.paused ? Commons.Color.accent : Theme.dim
        onClicked: root.togglePause()
      }
      IconButton { glyph: Model.Glyph.copy; tooltip: logView.selectedSeq >= 0 ? "Copy the selected line (c)" : "Copy visible lines (c)"; onClicked: root.copySelected() }
      IconButton { glyph: Model.Glyph.export; tooltip: "Export visible lines to Downloads"; onClicked: root.exportVisible() }
      IconButton { glyph: Model.Glyph.broom; tooltip: "Clear"; onClicked: root.svc.clear() }
      }
    }

    // ---- live list
    LogSyslogView {
      id: logView
      Layout.fillWidth: true
      Layout.fillHeight: true
      query: searchField.text.trim()
      minLevel: root.svc.levelFilter
      processes: root.svc.processList
      emptyTitle: root.svc.streaming ? "Waiting for log lines…" : (root.svc.syslogError ? "The log stopped" : "The log is stopped")
      emptyText: root.svc.streaming
        ? (root.svc.processList.length ? "Nothing from " + root.svc.processList.join(", ") + " yet. Open the app on your iPhone, or pick All processes." : "Lines appear here as your iPhone writes them.")
        : (root.svc.syslogError ? Model.errorMessage(root.svc.syslogError) : "Press Start to read " + root.svc.deviceName + "'s log live.")
      onCopyRequested: root.copySelected()
    }

    // ---- status line
    RowLayout {
      Layout.fillWidth: true
      spacing: Style.spacing.xl
      StatusPill {
        quiet: true
        level: root.svc.reconnecting ? "warn" : root.svc.streaming ? (logView.paused ? "info" : "busy") : (root.svc.syslogError ? "error" : "info")
        glyph: root.svc.streaming && !logView.paused && !root.svc.reconnecting ? Model.Glyph.dot : ""
        text: root.svc.reconnecting ? "Reconnecting to " + root.svc.deviceName + "…"
            : root.svc.streaming ? (logView.paused ? "Paused · " + logView.pendingCount + " new lines waiting" : "Live from " + root.svc.deviceName)
            : root.svc.starting ? "Starting…" : "Stopped"
      }
      AppText {
        visible: !bar.narrow
        text: logView.shownCount === logView.count ? Model.plural(logView.count, "line") : logView.shownCount + " of " + logView.count + " lines"
        size: "small"; tone: "faint"
      }
      AppText {
        visible: root.svc.dropped > 0
        text: root.svc.dropped + " skipped, the phone is too chatty. Pick an app or a higher level to see every line."
        size: "small"; tone: "faint"; elide: Text.ElideRight
        Layout.fillWidth: true
      }
      Item { Layout.fillWidth: true; visible: root.svc.dropped === 0 }
      AppText {
        text: bar.narrow ? "Keep streaming" : "Keep streaming when I leave"
        size: "small"; tone: "dim"
        TapHandler { onTapped: root.svc.setKeepStreaming(!root.svc.keepStreaming) }
      }
      ToggleSwitch {
        checked: root.svc.keepStreaming
        onToggled: root.svc.setKeepStreaming(!root.svc.keepStreaming)
      }
    }
  }

  // ---- no phone / no tools
  EmptyState {
    anchors.centerIn: parent
    width: Math.min(parent.width, Style.space(520))
    visible: !root.ready
    glyph: root.svc.deviceNeedsSetup ? Model.Glyph.toolbox : Model.Glyph.phone
    title: root.svc.deviceNeedsSetup ? "iPhone tools aren't installed yet"
         : !root.svc.deviceApi ? "Starting the iPhone service…"
         : "Connect your iPhone to see its log"
    text: root.svc.deviceNeedsSetup ? "iOS Studio needs a small helper to talk to your iPhone. Setup installs it for you."
        : !root.svc.deviceApi ? "This takes a second."
        : (root.app.device.connection && root.app.device.connection.message ? root.app.device.connection.message + ". " : "")
          + "Once your iPhone is connected, its live log shows up here, filtered to your apps."
    actionText: root.svc.deviceNeedsSetup ? "Open Setup" : (root.svc.deviceApi ? "Go to iPhone" : "")
    actionGlyph: root.svc.deviceNeedsSetup ? Model.Glyph.rocket : Model.Glyph.phone
    onAction: root.svc.deviceNeedsSetup ? root.app.openSetupStep("pymobiledevice3") : root.app.nav("device")
  }
}
