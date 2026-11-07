import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell.Io
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../common"
import "../../lib/Model.js" as Model

// Logs page › Crashes tab (logs slice): crash reports on the iPhone, newest first, and a viewer
// with a parsed summary card followed by the full report.
Item {
  id: root
  required property var app
  readonly property var svc: app.logs

  property string scope: ""               // "mine" | "all"; "" = pick automatically
  property int cursor: 0
  property string selectedName: ""
  readonly property bool ownAny: svc.crashes.some(function(c) { return svc.isOwn(c) })
  readonly property string effScope: scope !== "" ? scope : (ownAny ? "mine" : "all")

  readonly property var shown: {
    var out = []
    for (var i = 0; i < svc.crashes.length; i++) {
      var c = svc.crashes[i]
      if (effScope === "mine" && !svc.isOwn(c)) continue
      out.push(c)
    }
    return out
  }
  readonly property var selected: {
    for (var i = 0; i < shown.length; i++) if (shown[i].name === selectedName) return shown[i]
    return null
  }

  function moveCursor(d) {
    if (!shown.length) return false
    cursor = Math.max(0, Math.min(shown.length - 1, cursor + d))
    open(shown[cursor])
    list.positionViewAtIndex(cursor, ListView.Contain)
    return true
  }
  function copyReport() { report.copy() }
  function open(c) {
    if (!c) return
    selectedName = c.name
    for (var i = 0; i < shown.length; i++) if (shown[i].name === c.name) cursor = i
    report.load(c)
  }
  onShownChanged: if (!selected && shown.length && selectedName === "") Qt.callLater(function() { root.open(root.shown[0]) })

  function titleOf(c) {
    if (c.kind === "jetsam") return "Out of memory"
    return c.process || c.file
  }
  function glyphOf(c) {
    if (c.kind === "jetsam" || c.kind === "resource") return Model.Glyph.memory
    if (c.kind === "hang") return Model.Glyph.timer
    return Model.Glyph.crash
  }
  function when(c) {
    var d = new Date(c.ts * 1000)
    var age = Date.now() / 1000 - c.ts
    if (age < 6 * 3600) return Model.relativeTime(c.ts)
    if (age < 6 * 86400) return Model.relativeTime(c.ts) + ", " + d.toLocaleString(Qt.locale(), "HH:mm")
    return d.toLocaleString(Qt.locale(), "d MMM, HH:mm")
  }

  // ---------------------------------------------------------------- states
  LoadingState {
    anchors.centerIn: parent
    width: Math.min(parent.width, Style.space(420))
    visible: svc.crashStatus === "loading" && svc.crashes.length === 0
    text: "Reading crash reports from " + svc.deviceName + "…"
    detail: "This can take a few seconds the first time."
  }
  ErrorState {
    anchors.centerIn: parent
    width: Math.min(parent.width, Style.space(520))
    visible: svc.crashStatus === "error" && svc.crashes.length === 0
    app: root.app
    error: svc.crashError
    title: "Couldn't read crash reports"
    onRetry: svc.refreshCrashes()
  }
  EmptyState {
    anchors.centerIn: parent
    width: Math.min(parent.width, Style.space(520))
    visible: !svc.deviceConnected && svc.crashes.length === 0
    glyph: Model.Glyph.phone
    title: "Connect your iPhone to see crash reports"
    text: "When one of your apps crashes, iPhone keeps a report. iOS Studio reads it and shows what went wrong."
    actionText: "Go to iPhone"; actionGlyph: Model.Glyph.phone
    onAction: root.app.nav("device")
  }
  EmptyState {
    anchors.centerIn: parent
    width: Math.min(parent.width, Style.space(520))
    visible: svc.crashStatus === "ready" && root.shown.length === 0
    glyph: Model.Glyph.checkCircle
    level: "ok"
    title: root.effScope === "mine" && svc.crashes.length ? "None of your apps crashed" : "No crash reports"
    text: root.effScope === "mine" && svc.crashes.length
      ? "Your iPhone has " + Model.plural(svc.crashes.length, "report") + " from other apps and the system."
      : "Nothing has crashed on " + svc.deviceName + " lately. Reports show up here on their own."
    actionText: root.effScope === "mine" && svc.crashes.length ? "Show all reports" : "Check again"
    actionGlyph: root.effScope === "mine" && svc.crashes.length ? Model.Glyph.eye : Model.Glyph.refresh
    actionKind: "secondary"
    onAction: root.effScope === "mine" && svc.crashes.length ? root.scope = "all" : svc.refreshCrashes()
  }

  // ---------------------------------------------------------------- two panes
  RowLayout {
    anchors.fill: parent
    spacing: Style.spacing.huge
    visible: root.shown.length > 0

    ColumnLayout {
      Layout.fillWidth: false
      Layout.preferredWidth: Math.max(Style.space(280), Math.min(Style.space(380), root.width * 0.36))
      Layout.fillHeight: true
      spacing: Style.spacing.lg

      RowLayout {
        Layout.fillWidth: true
        spacing: Style.spacing.md
        ButtonGroupLite {
          options: [{ value: "mine", label: "My apps" }, { value: "all", label: "All reports" }]
          value: root.effScope
          onPicked: function(v) { root.scope = v }
        }
        Item { Layout.fillWidth: true }
        AppText { text: String(root.shown.length); size: "small"; tone: "faint" }
        IconButton {
          glyph: Model.Glyph.refresh; tooltip: "Check for new reports"
          busy: svc.crashResource.busy
          enabled: svc.deviceConnected
          onClicked: svc.refreshCrashes()
        }
      }

      ListView {
        id: list
        Layout.fillWidth: true
        Layout.fillHeight: true
        clip: true
        spacing: Style.spacing.xxs
        model: root.shown
        boundsBehavior: Flickable.StopAtBounds
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
        delegate: ListRow {
          required property var modelData
          required property int index
          width: ListView.view.width
          glyph: root.glyphOf(modelData)
          glyphColor: svc.isOwn(modelData) ? Theme.error : Theme.dim
          title: root.titleOf(modelData)
          subtitle: svc.exceptionText(modelData) + " · " + root.when(modelData)
          hasCursor: root.cursor === index
          current: root.selectedName === modelData.name
          onHovered: function(h) { if (h) root.cursor = index }
          onClicked: root.open(modelData)
          trailing: [
            StatusPill { visible: svc.isNew(modelData); level: "warn"; text: "New"; showGlyph: false }
          ]
        }
      }
    }

    CrashReportView {
      id: report
      Layout.fillWidth: true
      Layout.fillHeight: true
      app: root.app
      item: root.selected
    }
  }
}
