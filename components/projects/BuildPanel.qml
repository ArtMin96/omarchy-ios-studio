import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../common"
import "../../lib/Model.js" as Model

// Build drawer (projects slice): live step timeline, progress, problems and raw log, result
// banner. Problems lists errors first; within a severity, pre-build project settings issues
// (chip "Pre-build check") come before xtool's own output. Slides in from the right edge of the
// Projects page; not modal, so the list stays usable. A run with a run preset names it in the
// subtitle, the launch step and the result banner. A compile-on-save build (watch mode, build.watch)
// shows up here when it finishes, like a finished Build: its problems, and its saved log in Log.
// Style warnings (`format-lint`, StyleWarningsSection) follow the build's problems under their own
// "Style" header; they never change the build's result, its banner or the tab's colour, and Hide keeps
// them out (remembered). After "Lint project" on another project's card (svc.lintFocus), the drawer
// shows only that project's style warnings until a build starts or "Show build" is pressed.
//   BuildPanel { app: page.app; svc: page.app.projects; opened: page.panelOpen; onCloseRequested: … }
Item {
  id: root

  property var app: null
  property var svc: null
  property bool opened: false
  property string tab: "log"
  readonly property var b: svc ? svc.build : ({})
  readonly property var result: b.result || null
  // Diagnostics stay out of `build` (ProjectService.diagnostics: a ListModel, errors first).
  readonly property int diagCount: svc ? svc.diagnostics.count : 0
  readonly property int errorCount: svc ? svc.errorCount : 0
  readonly property int warningCount: svc ? svc.warningCount : 0
  // Warnings from the pre-build project settings check (category "config"; they never fail a build).
  readonly property int configCount: svc ? svc.configCount || 0 : 0
  function warningsText() {
    if (!root.warningCount) return ""
    var t = Model.plural(root.warningCount, "warning")
    if (root.configCount > 0) t += root.configCount >= root.warningCount ? " in project settings" : " (" + root.configCount + " in project settings)"
    return t
  }
  // Only reassigned when a step changes, not on every progress tick, so the step rows stay put.
  property var steps: []
  onBChanged: {
    var st = b.steps || []
    if (st !== steps && (st.length > 0 || steps.length > 0)) steps = st
  }
  readonly property alias log: logView
  // Style warnings: of the project lintFocus names (lint view: nothing about the build shows), else the build's.
  readonly property bool lintView: !!(svc && svc.lintFocus)
  readonly property string stylePath: lintView ? svc.lintFocus : (b.path || "")
  readonly property var styleProject: svc && stylePath ? svc.projectByPath(stylePath) : null
  readonly property var styleEntry: svc && stylePath ? svc.lintFor(stylePath) : null
  readonly property var styleResult: styleEntry && styleEntry.result ? styleEntry.result : null
  readonly property int styleTotal: styleResult ? (styleResult.total !== undefined ? styleResult.total : (styleResult.diagnostics || []).length) : 0
  readonly property bool styleShown: app ? app.setting("showStyleWarnings", true) !== false : true
  readonly property int styleCount: styleShown && styleEntry && styleEntry.state !== "error" ? styleTotal : 0
  onStylePathChanged: if (svc && stylePath) svc.lintLastSoon(stylePath)

  signal closeRequested()
  signal formatRequested(string path)     // "Format project…" in the Style section

  visible: slide.x < root.width
  clip: true

  function kindLabel(k) {
    if (root.b.watch) return "Compiled on save"
    return { run: "Run on iPhone", ipa: "Build .ipa", renew: "Re-sign & reinstall", install: "Reinstall", compile: "Build" }[k] || "Build"
  }

  // The project's name goes on the subtitle line (lintSubtitle), so a long name never elides the count.
  function lintHeadline() {
    var e = root.styleEntry
    if (!e || (e.state === "loading" && !e.result)) return "Checking the style"
    if (e.state === "error") return "Couldn't check the style"
    var n = root.styleTotal
    return n ? Model.plural(n, "style warning") : "No style warnings"
  }
  function lintSubtitle(withPath) {
    var name = root.styleProject ? root.styleProject.name : String(root.stylePath).split("/").pop()
    var where = withPath ? (root.styleProject ? root.styleProject.displayPath || "" : root.stylePath) : ""
    return [name, where].filter(function(x) { return !!x }).join(" · ")
  }

  function headline() {
    if (root.lintView) return root.lintHeadline()
    if (!b.kind) return "No builds yet"
    if (b.active) return { run: "Building ", ipa: "Building ", renew: "Renewing ", install: "Reinstalling ", compile: "Building " }[b.kind] + b.name
    if (!result) return b.name
    if (result.ok) {
      if (b.kind === "run") return result.data && result.data.launchError ? b.name + " is installed" : b.name + " is running"
      if (b.kind === "renew") return b.name + " is good for another week"
      if (b.kind === "ipa") return b.name + ".ipa is ready"
      if (b.kind === "install") return b.name + " was reinstalled"
      return b.name + (b.watch ? " compiles" : " built")
    }
    if (result.error && result.error.code === "cancelled") return "Build cancelled"
    if (result.error && result.error.code === "compile-errors") return Model.plural(root.errorCount || 1, "error") + " in " + b.name
    return "Couldn't finish " + b.name
  }

  function stepGlyph(state) {
    return { done: Model.Glyph.checkCircle, failed: Model.Glyph.error, running: Model.Glyph.sync, skipped: Model.Glyph.minus }[state] || Model.Glyph.circle
  }
  function stepColor(state) {
    return { done: Theme.success, failed: Theme.error, running: Commons.Color.accent }[state] || Theme.faint
  }

  // When a build starts, show its log; when it fails with compile errors, show the problems.
  Connections {
    target: root.svc
    function onBuildStarted() { logView.clear(); root.tab = "log" }
    function onBuildLog(text, level) { logView.append(text, level || undefined) }
    function onBuildFinished(ok) { if (!ok && root.errorCount > 0) root.tab = "problems" }
    function onWatchBuildShown(ok) {
      logView.clear()
      if (!ok && root.errorCount > 0) root.tab = "problems"
      var f = root.b.logFile
      if (!f || !root.app) return
      root.app.cli.call(["build", "log", "--file", f], "", function(d) {
        if (root.b.logFile !== f) return
        var lines = String(d.text || "").split("\n")
        if (lines.length && lines[lines.length - 1] === "") lines.pop()
        logView.appendMany(lines.slice(-4000))
      }, function(e) {})
    }
  }
  Component.onCompleted: {
    if (svc && svc.logBuffer.length) logView.appendMany(svc.logBuffer.map(function(l) { return l.text }))
  }

  Rectangle {
    id: slide
    width: root.width
    height: root.height
    x: root.opened ? 0 : root.width
    Behavior on x { NumberAnimation { duration: 220; easing.type: Easing.OutCubic } }
    color: Commons.Color.background

    Rectangle { anchors.left: parent.left; anchors.top: parent.top; anchors.bottom: parent.bottom; width: Style.normalBorderWidth; color: Theme.hairline }
    // soft edge shadow into the page
    Rectangle {
      anchors.right: parent.left
      anchors.top: parent.top
      anchors.bottom: parent.bottom
      width: Style.space(18)
      gradient: Gradient {
        orientation: Gradient.Horizontal
        GradientStop { position: 0.0; color: "transparent" }
        GradientStop { position: 1.0; color: Util.alpha(Commons.Color.background, 0.55) }
      }
    }
    // Takes hover too, so a card tooltip behind the panel can't show through, and the wheel a list inside
    // has no use for (past the end of Problems or Log), so the Projects page behind doesn't scroll.
    MouseArea { anchors.fill: parent; acceptedButtons: Qt.NoButton; hoverEnabled: true; onWheel: function(wheel) { wheel.accepted = true } }

    ColumnLayout {
      anchors.fill: parent
      anchors.margins: Style.spacing.huge
      spacing: Style.spacing.xxl

      // ---------------------------------------------------------- header
      RowLayout {
        Layout.fillWidth: true
        spacing: Style.spacing.xxl
        Item {
          Layout.preferredWidth: Style.font.display
          Layout.preferredHeight: Style.font.display
          Layout.alignment: Qt.AlignTop
          Spinner { anchors.fill: parent; visible: root.lintView ? !!(root.styleEntry && root.styleEntry.state === "loading") : !!root.b.active; running: visible }
          Icon {
            anchors.centerIn: parent
            visible: root.lintView && !(root.styleEntry && root.styleEntry.state === "loading")
            size: Style.font.display
            glyph: Model.Glyph.lint
            color: root.styleEntry && root.styleEntry.state === "error" ? Theme.error : (root.styleResult && root.styleTotal === 0 ? Theme.success : Theme.dim)
          }
          Icon {
            anchors.centerIn: parent
            visible: !root.lintView && !root.b.active
            size: Style.font.display
            glyph: !root.result ? Model.Glyph.hammer : root.result.ok ? Model.Glyph.checkCircle : (root.result.error && root.result.error.code === "cancelled" ? Model.Glyph.stop : Model.Glyph.error)
            color: !root.result ? Theme.dim : root.result.ok ? Theme.success : (root.result.error && root.result.error.code === "cancelled" ? Theme.dim : Theme.error)
          }
        }
        ColumnLayout {
          Layout.fillWidth: true
          spacing: Style.spacing.xs
          AppText { Layout.fillWidth: true; text: root.headline(); size: "heading"; strong: true; elide: Text.ElideRight }
          AppText {
            id: subtitle
            Layout.fillWidth: true
            Layout.preferredWidth: 0   // its width comes from the row, never from the text it picks below
            tone: "dim"; size: "small"; elide: Text.ElideRight
            // lint view: the path is left out when the whole line doesn't fit (no "~/D…" stub)
            TextMetrics { id: lintFull; font: subtitle.font; text: root.lintView ? root.lintSubtitle(true) : "" }
            text: {
              if (root.lintView) return root.lintSubtitle(lintFull.advanceWidth <= subtitle.width)
              if (!root.b.kind) return ""
              var parts = [root.kindLabel(root.b.kind) + (root.b.kind === "run" && root.b.preset ? " (" + root.b.preset + ")" : "")]
              // watch mode: the next compile on save already runs (its result replaces this one)
              var w = root.b.watch && root.app && root.app.watch ? root.app.watch.forProject(root.b.path) : null
              if (w && (w.state === "compiling" || w.state === "changed")) parts[0] += " · compiling again…"
              if (root.result && root.result.duration) parts.push(Model.formatDuration(root.result.duration))
              else if (root.b.active && root.b.eta) parts.push("about " + Model.formatDuration(root.b.eta) + " left")
              if (root.b.bundleId) parts.push(root.b.bundleId)
              return parts.join(" · ")
            }
          }
        }
        ActionButton {
          Layout.alignment: Qt.AlignTop
          Layout.topMargin: Math.max(0, (closeBtn.implicitHeight - implicitHeight) / 2)   // centred on the close button
          visible: root.lintView && !!root.b.kind
          text: "Show build"
          kind: "ghost"
          compact: true
          tooltip: "Back to " + (root.b.name || "the last build") + (root.b.active ? " (building now)" : "")
          onClicked: { root.svc.lintFocus = ""; root.tab = root.b.active ? "log" : root.tab }
        }
        IconButton { id: closeBtn; Layout.alignment: Qt.AlignTop; glyph: Model.Glyph.close; tooltip: root.lintView ? "Hide (Esc)" : "Hide (Esc). The build keeps running."; onClicked: root.closeRequested() }
      }

      // ---------------------------------------------------------- progress / result
      ProgressTrack {
        Layout.fillWidth: true
        visible: !!root.b.active && !root.lintView
        value: root.b.fraction || 0
        indeterminate: !(root.b.fraction > 0)
        label: root.b.detail || ""
        trailing: root.b.fraction > 0 ? Math.round(root.b.fraction * 100) + "%" : ""
      }

      // success banner
      Rectangle {
        Layout.fillWidth: true
        visible: !!(root.result && root.result.ok) && !root.lintView
        radius: Style.cornerRadius
        color: Theme.levelFill(root.result && root.result.data && root.result.data.launchError ? "warn" : "ok")
        border.width: Style.normalBorderWidth
        border.color: Util.alpha(root.result && root.result.data && root.result.data.launchError ? Theme.warn : Theme.success, 0.45)
        implicitHeight: okRow.implicitHeight + Style.spacing.xl * 2
        RowLayout {
          id: okRow
          anchors.fill: parent
          anchors.margins: Style.spacing.xl
          spacing: Style.spacing.xl
          AppText {
            Layout.fillWidth: true
            wrapMode: Text.Wrap
            text: {
              var r = root.result
              if (!r || !r.ok) return ""
              var d = r.data || {}
              if (d.launchError) return "Installed, but it didn't open: " + Model.errorMessage(d.launchError)
              if (root.b.kind === "run") return "Installed and opened on your iPhone" + (root.b.preset ? " with the run preset “" + root.b.preset + "”" : "") + ". "
                                                + (root.warningCount ? root.warningsText() + " to look at." : "")
              if (root.b.kind === "renew") return "Re-signed and reinstalled. It keeps opening for another 7 days."
              if (root.b.kind === "ipa") return "Saved to " + String(d.ipa || "").replace(app ? app.home : "", "~")
              if (root.b.kind === "compile") return "Your code compiles for iPhone." + (root.warningCount ? " " + root.warningsText() + "." : "")
                                                    + (root.b.watch ? " It compiles again when you save." : "")
              return "Done."
            }
          }
          ActionButton { text: "Run again"; glyph: Model.Glyph.play; compact: true; visible: root.b.kind === "run"; enabled: !root.svc.building; onClicked: root.svc.runAgain() }
        }
      }

      ErrorState {
        Layout.fillWidth: true
        visible: !!(root.result && !root.result.ok && root.result.error && root.result.error.code !== "cancelled") && !root.lintView
        app: root.app
        banner: true
        error: root.result ? root.result.error : null
        retryText: "Try again"
        onRetry: root.svc.runAgain()
        onFixed: function(fix) {
          if (fix && fix.action === "open-diagnostic" && root.diagCount) {
            var all = root.svc.diagnosticList()
            var located = all.filter(function(x) { return !!x.file })
            var d = located.filter(function(x) { return x.severity === "error" })[0] || located[0] || all[0]
            root.svc.openInEditor({ path: root.b.path }, d.file, d.line, d.col)
          } else if (fix && fix.action === "log") {
            root.tab = "log"
          } else if (fix && fix.action === "retry") {
            root.svc.runAgain()
          }
        }
      }

      // ---------------------------------------------------------- steps
      ColumnLayout {
        Layout.fillWidth: true
        visible: root.steps.length > 0 && !root.lintView
        spacing: Style.spacing.sm
        Repeater {
          model: root.steps
          delegate: RowLayout {
            required property var modelData
            Layout.fillWidth: true
            spacing: Style.spacing.xl
            Icon {
              glyph: root.stepGlyph(modelData.state)
              color: root.stepColor(modelData.state)
              spinning: modelData.state === "running"
              size: Style.font.icon
            }
            AppText {
              text: modelData.title
              tone: modelData.state === "pending" || modelData.state === "skipped" ? "faint" : "primary"
              strong: modelData.state === "running"
            }
            AppText {
              Layout.fillWidth: true
              text: modelData.state === "running" ? (root.b.detail || modelData.detail || "") : ""
              tone: "dim"; size: "small"; elide: Text.ElideRight
            }
            AppText {
              visible: modelData.duration !== undefined && modelData.state === "done"
              text: modelData.duration !== undefined ? (modelData.duration < 1 ? "<1 s" : Math.round(modelData.duration) + " s") : ""
              tone: "faint"; size: "small"
            }
          }
        }
      }

      // ---------------------------------------------------------- problems / log
      PageTabs {
        Layout.fillWidth: true
        visible: !!root.b.kind && !root.lintView
        current: root.tab
        // Style warnings add to the count but not to the colour: they don't make a build look broken.
        tabs: [
          { key: "problems", label: "Problems", glyph: Model.Glyph.warn, count: (root.diagCount + root.styleCount) || undefined, level: root.errorCount ? "error" : (root.warningCount ? "warn" : "") },
          { key: "log", label: "Log", glyph: Model.Glyph.console }
        ]
        onSelected: function(k) { root.tab = k }
      }

      Item {
        Layout.fillWidth: true
        Layout.fillHeight: true
        visible: !!root.b.kind || root.lintView

        LogView {
          id: logView
          anchors.fill: parent
          visible: root.tab === "log" && !root.lintView
          showMeta: false
          emptyText: "The build's output appears here."
          onCopiedAll: function(n) { if (root.app) root.app.toast("Copied " + n + " lines", "ok") }
          toolbarExtra: [
            IconButton {
              glyph: Model.Glyph.openExternal
              tooltip: "Open the full log in your editor"
              visible: !!root.b.logFile
              onClicked: root.svc.openBuildLog(root.b.logFile)
            }
          ]
        }

        ColumnLayout {
          anchors.fill: parent
          visible: root.tab === "problems" || root.lintView
          spacing: 0

          EmptyState {
            Layout.fillWidth: true
            Layout.topMargin: Style.spacing.huge
            visible: root.diagCount === 0 && !root.styleEntry && !root.lintView
            compact: true
            glyph: root.b.active ? Model.Glyph.hammer : Model.Glyph.checkCircle
            level: root.b.active ? "" : "ok"
            title: root.b.active ? "No problems so far" : "No problems"
            text: root.b.active ? "Errors and warnings show up here as they happen." : "The build didn't report any errors or warnings."
          }

          ListView {
            id: diagList
            Layout.fillWidth: true
            Layout.fillHeight: true
            visible: root.diagCount > 0 || !!root.styleEntry || root.lintView
            clip: true
            spacing: Style.spacing.xs
            boundsBehavior: Flickable.StopAtBounds
            ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
            model: root.svc && !root.lintView ? root.svc.diagnostics : null
            // No build problems, but style warnings below: say so in one quiet line.
            header: QuietNote {
              width: diagList.width
              height: visible ? implicitHeight : 0
              visible: root.diagCount === 0 && !root.lintView && !!root.b.kind
              glyph: root.b.active ? Model.Glyph.hammer : Model.Glyph.checkCircle
              level: root.b.active ? "" : "ok"
              title: root.b.active ? "No build problems so far" : "No build problems"
              text: root.b.active ? "" : "The build didn't report any errors or warnings."
            }
            footer: Item {
              width: diagList.width
              implicitHeight: styleSection.implicitHeight
              height: styleSection.visible ? implicitHeight : 0
              StyleWarningsSection {
                id: styleSection
                width: parent.width
                app: root.app
                svc: root.svc
                path: root.stylePath
                onFormatRequested: root.formatRequested(root.stylePath)
                onRowHovered: diagList.currentIndex = -1     // one highlighted row at a time
              }
            }
            delegate: BuildDiagnosticRow {
              required property int n
              required property int index
              width: diagList.width
              diag: root.svc.diagnostic(n) || ({})
              projectPath: root.b.path || ""
              hasCursor: diagList.currentIndex === index
              onHovered: function(h) { if (h) diagList.currentIndex = index }
              onClicked: root.svc.openInEditor({ path: root.b.path }, diag.file, diag.line, diag.col)
            }
          }
        }
      }

      EmptyState {
        Layout.fillWidth: true
        Layout.fillHeight: true
        visible: !root.b.kind && !root.lintView
        glyph: Model.Glyph.hammer
        title: "No builds yet"
        text: "Press Run on a project. Its progress, problems and log show up here."
      }

      // ---------------------------------------------------------- footer
      RowLayout {
        Layout.fillWidth: true
        visible: !!root.b.active && !root.lintView
        spacing: Style.spacing.md
        AppText { Layout.fillWidth: true; text: "You can hide this panel; the build keeps going."; tone: "faint"; size: "small"; elide: Text.ElideRight }
        ActionButton { text: "Cancel build"; glyph: Model.Glyph.stop; kind: "danger"; onClicked: root.svc.cancel() }
      }
    }
  }
}
