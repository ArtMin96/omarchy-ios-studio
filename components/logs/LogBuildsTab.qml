import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../common"
import "../../lib/Model.js" as Model

// Logs page › Build logs tab (logs slice): build history on the left; the selected build's
// summary, problems (parsed from its saved log) and full log on the right.
Item {
  id: root
  required property var app
  readonly property var svc: app.logs
  readonly property var builds: svc.builds || []

  property int cursor: 0
  property var selected: null
  property string logText: ""
  property bool logLoading: false
  property var logError: null
  property var diagnostics: []
  property bool logTruncated: false

  function keyOf(b) { return b ? (b.logFile || (b.path + ":" + b.time)) : "" }

  function moveCursor(d) {
    if (!builds.length) return false
    cursor = Math.max(0, Math.min(builds.length - 1, cursor + d))
    open(builds[cursor])
    list.positionViewAtIndex(cursor, ListView.Contain)
    return true
  }

  function open(b) {
    if (!b) return
    if (keyOf(b) === keyOf(selected) && (logText !== "" || logLoading)) return
    selected = b
    for (var i = 0; i < builds.length; i++) if (keyOf(builds[i]) === keyOf(b)) cursor = i
    logText = ""; logError = null; logTruncated = false
    // Newer builds.json entries carry the helper's own parsed problems; older ones get re-parsed from the log.
    var stored = Array.isArray(b.diagnostics) ? b.diagnostics.filter(function(d) { return d && d.severity !== "note" }) : null
    diagnostics = stored || []
    logLoading = true
    var want = keyOf(b)
    svc.loadBuildLog(b, function(ok, d) {
      // root is null when the tab was destroyed while the log loaded (a page load started over)
      if (!root || keyOf(root.selected) !== want) return
      root.logLoading = false
      if (!ok) { root.logError = d; return }
      root.logText = String(d.text || "")
      root.logTruncated = d.truncated === true
      if (!stored) root.diagnostics = svc.parseDiagnostics(root.logText)
    })
  }

  onBuildsChanged: {
    if (!builds.length) { selected = null; return }
    var k = keyOf(selected)
    for (var i = 0; i < builds.length; i++) if (keyOf(builds[i]) === k) { cursor = i; return }
    Qt.callLater(function() { root.open(root.builds[0]) })
  }
  Component.onCompleted: if (builds.length) open(builds[0])

  function resultLevel(b) { return b.result === "ok" ? "ok" : b.result === "cancelled" ? "info" : "error" }
  function resultText(b) { return b.result === "ok" ? "Succeeded" : b.result === "cancelled" ? "Cancelled" : "Failed" }
  function countsText(b) {
    var parts = []
    if (b.errors) parts.push(Model.plural(b.errors, "error"))
    if (b.warnings) parts.push(Model.plural(b.warnings, "warning"))
    return parts.join(", ")
  }
  function logLines(text) {
    var ls = String(text || "").replace(/\x1b\[[0-9;?]*[ -\/]*[@-~]/g, "").split("\n")
    if (ls.length && ls[ls.length - 1] === "") ls.pop()
    return ls
  }

  // ---------------------------------------------------------------- empty
  EmptyState {
    anchors.centerIn: parent
    width: Math.min(parent.width, Style.space(520))
    visible: root.builds.length === 0
    glyph: Model.Glyph.hammer
    title: "No builds yet"
    text: "Every build you start from Projects is saved here with its full log and any errors, so you can look back at it later."
    actionText: "Go to Projects"; actionGlyph: Model.Glyph.hammer
    onAction: root.app.nav("projects")
  }

  RowLayout {
    anchors.fill: parent
    spacing: Style.spacing.huge
    visible: root.builds.length > 0

    // ---- history
    ListView {
      id: list
      Layout.fillWidth: false
      Layout.preferredWidth: Math.max(Style.space(280), Math.min(Style.space(360), root.width * 0.34))
      Layout.fillHeight: true
      clip: true
      spacing: Style.spacing.xxs
      model: root.builds
      boundsBehavior: Flickable.StopAtBounds
      ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
      delegate: ListRow {
        required property var modelData
        required property int index
        width: ListView.view.width
        glyph: Model.levelGlyph(root.resultLevel(modelData))
        glyphColor: Theme.levelColor(root.resultLevel(modelData))
        title: modelData.name || modelData.project || "Build"
        subtitle: [root.svc.buildKindLabel(modelData.kind, modelData), root.resultText(modelData), Model.formatDuration(modelData.duration), root.countsText(modelData)].filter(function(s) { return s !== "" }).join(" · ")
        meta: Model.relativeTime(modelData.time)
        hasCursor: root.cursor === index
        current: root.keyOf(root.selected) === root.keyOf(modelData)
        onHovered: function(h) { if (h) root.cursor = index }
        onClicked: root.open(modelData)
      }
    }

    // ---- detail
    ColumnLayout {
      Layout.fillWidth: true
      Layout.fillHeight: true
      spacing: Style.spacing.xl
      visible: !!root.selected

      RowLayout {
        Layout.fillWidth: true
        spacing: Style.spacing.xl
        ColumnLayout {
          Layout.fillWidth: true
          spacing: Style.spacing.xs
          RowLayout {
            spacing: Style.spacing.lg
            AppText { text: root.selected ? (root.selected.name || root.selected.project) : ""; size: "title"; strong: true }
            StatusPill { visible: !!root.selected; level: root.selected ? root.resultLevel(root.selected) : "info"; text: root.selected ? root.resultText(root.selected) : "" }
          }
          AppText {
            Layout.fillWidth: true
            text: root.selected ? [root.svc.buildKindLabel(root.selected.kind, root.selected), root.selected.config === "release" ? "Release" : "Debug",
                                    "took " + Model.formatDuration(root.selected.duration),
                                    new Date(root.selected.time * 1000).toLocaleString(Qt.locale(), "d MMM, HH:mm")].join(" · ") : ""
            size: "small"; tone: "dim"; elide: Text.ElideRight
          }
        }
        IconButton { glyph: Model.Glyph.copy; tooltip: "Copy the log"; enabled: root.logText !== ""
          onClicked: root.svc.copyText(root.logText, function(ok) { root.app.toast(ok ? "Copied the build log" : "Couldn't copy", ok ? "ok" : "error") }) }
        IconButton { glyph: Model.Glyph.openExternal; tooltip: "Open the log in your editor"; enabled: !!root.selected && !!root.selected.logFile && !!root.app.projects
          onClicked: root.app.projects.openBuildLog(root.selected.logFile) }
        ActionButton {
          text: "Rebuild"; glyph: Model.Glyph.hammer; kind: "primary"
          enabled: !!root.selected && !(root.app.projects && root.app.projects.building)
          tooltip: "Run this build again in Projects"
          onClicked: root.svc.rebuild(root.selected)
        }
      }

      ErrorState {
        Layout.fillWidth: true
        visible: !!root.selected && root.selected.result === "failed" && !!root.selected.errorMessage
        app: root.app
        message: root.selected && root.selected.errorMessage ? root.selected.errorMessage : ""
        banner: true
        showRetry: false
      }

      // ---- problems
      ColumnLayout {
        Layout.fillWidth: true
        visible: root.diagnostics.length > 0
        spacing: Style.spacing.xs
        SectionHeader { Layout.fillWidth: true; text: "Problems"; detail: String(root.diagnostics.length) }
        ListView {
          Layout.fillWidth: true
          Layout.preferredHeight: Math.min(contentHeight, Style.space(200))
          clip: true
          model: root.diagnostics
          boundsBehavior: Flickable.StopAtBounds
          ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
          delegate: BuildDiagnosticRow {
            required property var modelData
            width: ListView.view.width
            diag: modelData
            projectPath: root.selected ? root.selected.path : ""
            onClicked: {
              var p = root.svc.projectFor(root.selected) || { path: root.selected.path, name: root.selected.name }
              if (root.app.projects) root.app.projects.openInEditor(p, modelData.file, modelData.line, modelData.col)
            }
          }
        }
      }

      LoadingState { Layout.fillWidth: true; visible: root.logLoading; text: "Opening the build log…" }
      ErrorState {
        Layout.fillWidth: true
        visible: !!root.logError
        app: root.app
        error: root.logError
        banner: true
        showRetry: false
      }

      RowLayout {
        Layout.fillWidth: true
        visible: !root.logLoading && !root.logError && root.logText !== ""
        spacing: Style.spacing.lg
        FilterField { id: logFilter; Layout.fillWidth: true; Layout.maximumWidth: Style.space(300); placeholder: "Filter this log" }
        ButtonGroupLite {
          id: logLevel
          options: [{ value: "", label: "All" }, { value: "warn", label: "Warnings" }, { value: "error", label: "Errors" }]
          onPicked: function(v) { value = v }
        }
        Item { Layout.fillWidth: true }
        AppText { text: buildLog.shownCount === buildLog.count ? Model.plural(buildLog.count, "line") : buildLog.shownCount + " of " + buildLog.count; size: "small"; tone: "faint" }
      }

      LogView {
        id: buildLog
        Layout.fillWidth: true
        Layout.fillHeight: true
        visible: !root.logLoading && !root.logError
        showToolbar: false
        filter: logFilter.text
        levelFilter: logLevel.value
        showMeta: false
        lines: root.logLines(root.logText)
        emptyText: "This build didn't write anything."
        onCopiedAll: function(n) { root.app.toast("Copied " + n + " lines", "ok") }
        onLineCopied: function(t) { root.app.toast("Copied the line", "ok") }
      }
    }
  }
}
