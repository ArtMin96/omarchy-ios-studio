pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../common"
import "../../core"
import "../../lib/Model.js" as Model

// "Run tests" sheet (projects slice): a project's unit tests run on this computer with `swift test` (app.tests,
// TestService; helper `test run` / `test info`). Opened from the card's More row ("Run tests") or `T`, which also
// start a run; the run keeps going when the sheet closes (its outcome toasts then).
//   header    the project, and the summary: "12 passed · 2 failed · 1 skipped · 4.2 s" (or the last run, from
//             `test info`, before one ran in this session)
//   filter    a regular expression for --filter (Enter runs); "Run failed only" after a failing run
//   running   the steps (Checking the test targets → Building the tests → Running the tests) with the number of
//             finished tests; failures show as they come
//   hint      one quiet line over passed / failed / didn't-compile results ("Click a failure to open it at its line.")
//             with "Show in Problems"; the header's summary already counts them, so there's no headline to repeat it
//   results   failures first (name, suite, library when both XCTest and Swift Testing are used, the message, file:line
//             that opens the editor), then skipped (with the reason), then the passed ones in one row that expands;
//             scrollable. "Run failed only" filters by their ids (Model.testFailedFilter)
//   states    no test target and tested code that imports UIKit/SwiftUI (with the imports' file:line) both offer
//             "Check again" and disable Run until the project changes; didn't compile
//             (the compile problems; they're in Problems too), filter matched nothing, cancelled, no toolchain
//             (the helper's fix opens Settings), other errors with Try again
// Failing tests and compile errors also go to the build panel's Problems when the run ends ("Show in Problems").
//   ProjectTestsSheet { id: testsSheet; app: page.app; svc: page.svc; onShowProblems: function(p) { … } }
//   testsSheet.openFor(project, true)       // true: start a run (unless one runs already)
Sheet {
  id: root

  property var app: null
  property var svc: null                  // app.projects
  readonly property var tests: app ? app.tests : null
  property var project: ({})
  property bool passedOpen: false

  readonly property string path: project && project.path ? String(project.path) : ""
  readonly property var run: tests && path ? tests.forProject(path) : null
  readonly property int rev: run ? run.rev : 0
  readonly property var res: { var r = root.rev; return tests && path ? tests.results(path) : null }
  readonly property bool infoMine: !!tests && tests.infoPath === path
  readonly property var info: infoMine ? tests.info.data : null
  readonly property string infoStatus: infoMine ? tests.info.status : "idle"
  readonly property string view: Model.testSheetState(run, infoStatus, info)
  readonly property bool running: view === "running"
  readonly property var counts: run ? run.counts : ({})
  readonly property var rows: { var r = root.rev; return Model.testRows(root.res, root.passedOpen) }
  readonly property bool bothLibraries: !!res && !!res.libraries.xctest && !!res.libraries["swift-testing"]
  readonly property var compileProblems: {
    var r = root.rev
    return root.res ? root.res.diagnostics.filter(function(d) { return d && d.category !== "test" }).slice(0, 20) : []
  }
  readonly property var blockers: {
    if (root.info && root.info.host_blockers && root.info.host_blockers.length) return root.info.host_blockers
    // a run said so before `test info` answered: its detail has the imports as "file:line: import Module"
    var d = root.run && root.run.error && root.run.error.code === "host_unsupported" ? String(root.run.error.detail || "") : ""
    return d.split("\n").map(function(l) {
      var m = /^(.*):(\d+): import (\w+)/.exec(l)
      return m ? { file: m[1], line: Number(m[2]), module: m[3] } : null
    }).filter(function(x) { return x !== null })
  }
  readonly property string failedFilter: root.view === "failed" && root.res ? Model.testFailedFilter(root.res.failed) : ""

  // Run can't help until the project changes: the state's own "Check again" looks after that.
  readonly property string runBlocker: root.view === "no_tests" ? "Add a test target first, then check again"
                                       : root.view === "host_unsupported" ? "Move the UIKit / SwiftUI code out of the tested targets first, then check again"
                                       : ""

  title: "Unit tests" + (project && project.name ? " · " + project.name : "")
  subtitle: root.summaryLine()
  glyph: Model.Glyph.testTube
  preferredWidth: Style.space(760)

  // The header glyph's box and ink (as ProjectFormatSheet): blocks start where its ink does, the summary's glyph
  // column is as wide as its box, so the summary text starts on the title's left edge.
  Icon { id: headGlyph; visible: false; glyph: root.glyph; size: Style.font.display }
  TextMetrics { id: headInk; font.family: Style.font.family; font.pixelSize: Style.font.display * 100; text: root.glyph }
  readonly property real inkInset: Math.max(0, (headGlyph.implicitWidth - headInk.advanceWidth / 100) / 2 + headInk.tightBoundingRect.x / 100)
  readonly property real inkCentre: inkInset + headInk.tightBoundingRect.width / 200
  // The results list takes what the window leaves (between space(160) and space(420)).
  readonly property real listMaxHeight: Math.min(Style.space(420), Math.max(Style.space(160), root.height * 0.42))

  function openFor(p, startRun) {
    var path = p && p.path ? String(p.path) : ""
    if (path !== root.path) { root.passedOpen = false; filterField.text = "" }
    root.project = p || {}
    if (root.tests) {
      root.tests.viewing = path
      root.tests.loadInfo(path)
    }
    var r = root.tests ? root.tests.forProject(path) : null
    if (r && r.filter && filterField.text === "") filterField.text = r.filter
    root.open()
    if (startRun === true && !(r && r.state === "running")) root.start("")
  }

  function start(filter) {
    if (!root.tests || root.running || root.runBlocker !== "") return
    var f = filter === undefined ? filterField.text.trim() : String(filter)
    filterField.text = f
    root.passedOpen = false
    root.tests.start(root.project, f)
  }
  function runFailedOnly() { if (root.failedFilter !== "") root.start(root.failedFilter) }
  function cancel() { if (root.tests) root.tests.cancel(root.project) }
  function openAt(file, line) { if (root.svc && file) root.svc.openInEditor(root.project, file, line) }
  function useFix(fix) {
    if (!fix) return
    if (fix.action === "open") { if (root.svc) root.svc.openInEditor(root.project); return }
    if (fix.url) { Qt.openUrlExternally(fix.url); return }
    if (root.app && fix.page) { root.close(); root.app.nav(fix.page, fix) }
  }
  // Look again after the project changed: a run's "no test target" or "imports SwiftUI" gives way to `test info`.
  function checkAgain() {
    if (!root.tests) return
    var code = root.run && root.run.state === "done" && root.run.error ? root.run.error.code : ""
    if (code === "no_tests" || code === "host_unsupported") root.tests.forget(root.path)
    root.tests.loadInfo(root.path)
  }

  onClosed: { if (root.tests && root.tests.viewing === root.path) root.tests.viewing = "" }

  signal showProblems(var project)

  function summaryLine() {
    var r = root.run
    if (root.view === "running") {
      var p = Model.testProgressText(r.counts)
      return (r.cancelling ? "Cancelling…" : r.step ? r.step.title : "Starting…") + (p ? " · " + p : "")
    }
    if (["passed", "failed", "no_match"].indexOf(root.view) >= 0 && r.result) return Model.testSummary(r.counts, r.result.duration)
    if (root.view === "cancelled") return r.counts.finished > 0 ? Model.testSummary(r.counts) : "Nothing ran"
    if (root.view === "build_failed") return Model.plural(root.compileProblems.length || 1, "compile error")
    var last = root.info ? root.info.last_run : null
    if (last && last.counts) return "Last run " + Model.relativeTime(last.at) + ": " + Model.testSummary(last.counts, last.duration)
    if (root.view === "no_tests") return "No test target yet"
    return root.view === "idle" ? "Not run yet" : root.targetsLine()
  }
  // "Test targets: AppCoreTests, AppCoreSwiftTests" from `test info`.
  function targetsLine() {
    var t = root.info && root.info.test_targets ? root.info.test_targets : []
    if (!t.length) return ""
    return (t.length === 1 ? "Test target: " : "Test targets: ") + t.map(function(x) { return x.name }).join(", ")
  }

  // A headline with a glyph for the states the header's summary doesn't explain. Passed, failed and didn't
  // compile (with its errors listed) have none: the summary already counts them, so they get hintLine() instead.
  function headline() {
    switch (root.view) {
    case "build_failed": return root.compileProblems.length ? "" : "The tests didn't compile"
    case "cancelled": return "Test run cancelled"
    case "idle": return root.targetsLine()
    }
    return ""
  }
  function headDetail() {
    var r = root.run
    switch (root.view) {
    case "idle":
      return root.info && root.info.tested_targets && root.info.tested_targets.length
             ? "They test " + root.info.tested_targets.join(", ") + ". The first run builds them, which takes longest." : ""
    case "build_failed":
      return Model.errorMessage(r ? r.error : null)
    case "cancelled":
      return root.counts.finished > 0 ? "These finished before it stopped." : "It stopped before any test ran."
    }
    return ""
  }
  // One quiet line above the results: what the list is and what a click does (and "Show in Problems" beside it).
  function hintLine() {
    var filtered = !!root.run && !!root.run.filter
    switch (root.view) {
    case "passed": return filtered ? "Only the tests matching the filter ran." : ""
    case "failed": return (filtered ? "Only the tests matching the filter ran. " : "") + "Click a failure to open it at its line."
    case "build_failed":
      return root.compileProblems.length ? "The tests didn't compile. Click an error to open it at its line." : ""
    }
    return ""
  }

  // ---------------------------------------------------------------- filter
  RowLayout {
    Layout.fillWidth: true
    Layout.leftMargin: root.inkInset
    visible: root.view !== "no_tests" && root.view !== "host_unsupported" && root.view !== "toolchain"
    spacing: Style.spacing.md
    TextField {
      id: filterField
      Layout.fillWidth: true
      enabled: !root.running
      placeholderTextColor: Theme.faint
      placeholderText: "Filter tests (a regular expression)"
      onAccepted: root.start()
    }
    ActionButton {
      visible: filterField.text !== ""
      text: "Clear"
      kind: "ghost"
      compact: true
      enabled: !root.running
      onClicked: filterField.text = ""
    }
  }

  // ---------------------------------------------------------------- loading (test info, before any run)
  LoadingState {
    Layout.fillWidth: true
    Layout.preferredHeight: Style.space(160)
    visible: root.view === "loading"
    text: "Looking for the test targets…"
    detail: "Reading Package.swift."
  }

  // ---------------------------------------------------------------- running: the steps
  ColumnLayout {
    Layout.fillWidth: true
    visible: root.running
    spacing: Style.spacing.sm
    Repeater {
      model: root.running ? root.shownSteps : []
      delegate: RowLayout {
        id: stepRow
        required property var modelData
        Layout.fillWidth: true
        spacing: Style.spacing.xxl
        FontMetrics { id: stepMetrics; font: stepTitle.font }
        // centred under the header glyph's ink
        Item {
          Layout.alignment: Qt.AlignTop
          Layout.preferredWidth: headGlyph.implicitWidth
          Layout.preferredHeight: stepMetrics.height
          Icon {
            x: root.inkCentre - width / 2
            anchors.verticalCenter: parent.verticalCenter
            glyph: { var s = stepRow.modelData.state; return s === "done" ? Model.Glyph.checkCircle : s === "failed" ? Model.Glyph.error : s === "running" ? Model.Glyph.sync : Model.Glyph.circle }
            color: { var s = stepRow.modelData.state; return s === "done" ? Theme.success : s === "failed" ? Theme.error : s === "running" ? Commons.Color.accent : Theme.faint }
            spinning: stepRow.modelData.state === "running"
            size: Style.font.icon
          }
        }
        AppText {
          id: stepTitle
          text: stepRow.modelData.title
          tone: stepRow.modelData.state === "pending" ? "faint" : "primary"
          strong: stepRow.modelData.state === "running"
        }
        AppText {
          Layout.fillWidth: true
          text: stepRow.modelData.id === "run" && stepRow.modelData.state === "running" ? Model.testProgressText(root.counts) : ""
          tone: "dim"; size: "small"; elide: Text.ElideRight
        }
      }
    }
  }

  // ---------------------------------------------------------------- outcome: glyph on the headline's line, detail under it
  RowLayout {
    Layout.fillWidth: true
    visible: root.headline() !== ""
    spacing: Style.spacing.xxl
    FontMetrics { id: headMetrics; font: headText.font }
    Item {
      Layout.alignment: Qt.AlignTop
      Layout.preferredWidth: headGlyph.implicitWidth
      Layout.preferredHeight: headMetrics.height
      Icon {
        x: root.inkCentre - width / 2
        anchors.verticalCenter: parent.verticalCenter
        size: Style.font.iconLarge
        glyph: root.view === "passed" ? Model.Glyph.checkCircle : root.view === "cancelled" ? Model.Glyph.stop
               : root.view === "idle" ? Model.Glyph.testTube : Model.Glyph.error
        color: root.view === "passed" ? Theme.success : root.view === "cancelled" || root.view === "idle" ? Theme.dim : Theme.error
      }
    }
    ColumnLayout {
      Layout.fillWidth: true
      spacing: Style.spacing.xxs
      AppText { id: headText; Layout.fillWidth: true; text: root.headline(); strong: root.view !== "idle"; elide: Text.ElideRight }
      AppText { Layout.fillWidth: true; visible: text !== ""; text: root.headDetail(); tone: "dim"; size: "small"; wrapMode: Text.Wrap }
    }
  }

  // ---------------------------------------------------------------- hint: what a click does, "Show in Problems"
  RowLayout {
    Layout.fillWidth: true
    Layout.leftMargin: root.inkInset
    visible: hintText.text !== ""
    spacing: Style.spacing.lg
    AppText {
      id: hintText
      Layout.fillWidth: true
      Layout.alignment: Qt.AlignVCenter
      text: root.hintLine()
      tone: "dim"; size: "small"; wrapMode: Text.Wrap
    }
    ActionButton {
      Layout.alignment: Qt.AlignVCenter
      visible: (root.view === "failed" || root.view === "build_failed") && !!root.res && root.res.diagnostics.length > 0
      text: "Show in Problems"
      glyph: Model.Glyph.warn
      kind: "ghost"
      compact: true
      tooltip: "Open the build panel's Problems, where a click opens each one in your editor"
      onClicked: { root.close(); root.showProblems(root.project) }
    }
  }

  // a test crashed: the ones after it didn't run
  RowLayout {
    Layout.fillWidth: true
    Layout.leftMargin: root.inkInset + Style.spacing.rowPaddingX     // the result rows' glyph column and gap
    visible: !root.running && !!root.run && !!root.run.result && !!root.run.result.crashed
    spacing: Style.spacing.xl
    FontMetrics { id: crashMetrics; font: crashText.font }
    Item {
      Layout.alignment: Qt.AlignTop
      Layout.preferredWidth: Style.font.iconLarge * 1.4
      Layout.preferredHeight: crashMetrics.height
      Icon { anchors.centerIn: parent; glyph: Model.Glyph.warn; color: Theme.warn; size: Style.font.icon }
    }
    AppText {
      id: crashText
      Layout.fillWidth: true
      text: root.run && root.run.result && root.run.result.crashed
            ? String(root.run.result.crashed).split("/").pop() + " crashed the test process, so the tests after it didn't run."
            : ""
      tone: "warn"; size: "small"; wrapMode: Text.Wrap
    }
  }

  // ---------------------------------------------------------------- the results
  Rectangle {
    Layout.fillWidth: true
    Layout.leftMargin: root.inkInset
    visible: root.rows.length > 0 && ["running", "passed", "failed", "cancelled"].indexOf(root.view) >= 0
    implicitHeight: Math.min(list.contentHeight, root.listMaxHeight) + Style.normalBorderWidth * 2
    // past the cap the list scrolls: a scrollbar that stays, and a gutter for it (only then, so the rows don't
    // rewrap whenever the bar comes and goes)
    readonly property bool scrolls: list.contentHeight > root.listMaxHeight
    radius: Style.cornerRadius
    color: "transparent"
    border.width: Style.normalBorderWidth
    border.color: Theme.hairline
    clip: true
    ListView {
      id: list
      anchors.fill: parent
      anchors.margins: Style.normalBorderWidth
      clip: true
      interactive: contentHeight > height
      boundsBehavior: Flickable.StopAtBounds
      ScrollBar.vertical: ScrollBar { policy: list.parent.scrolls ? ScrollBar.AlwaysOn : ScrollBar.AlwaysOff }
      property int hoverIndex: -1          // one highlighted row (the pointer's), never a default first one
      onModelChanged: hoverIndex = -1
      model: root.rows
      reuseItems: true
      delegate: TestResultRow {
        required property var modelData
        required property int index
        width: list.width - (list.parent.scrolls ? Style.spacing.lg : 0)
        kind: modelData.kind
        test: modelData.test || ({})
        count: modelData.kind === "passedGroup" ? (root.counts.passed || modelData.count) : 0
        open: root.passedOpen
        projectPath: root.path
        showLibrary: root.bothLibraries
        hasCursor: list.hoverIndex === index
        onHovered: function(h) { if (h) list.hoverIndex = index; else if (list.hoverIndex === index) list.hoverIndex = -1 }
        onClicked: {
          if (kind === "passedGroup") {
            var y = list.contentY
            root.passedOpen = !root.passedOpen
            Qt.callLater(function() { list.contentY = Math.max(0, Math.min(y, list.contentHeight - list.height)) })
          } else root.openAt(test.file, test.line)
        }
      }
    }
  }
  AppText {
    Layout.fillWidth: true
    Layout.leftMargin: root.inkInset + Style.spacing.rowPaddingX
    visible: root.passedOpen && !!root.res && root.res.passedTotal > root.res.passed.length
    text: "Showing " + (root.res ? root.res.passed.length : 0) + " of " + (root.res ? root.res.passedTotal : 0) + " passed tests."
    tone: "faint"; size: "small"
  }

  // ---------------------------------------------------------------- didn't compile: the compile problems
  ColumnLayout {
    Layout.fillWidth: true
    Layout.leftMargin: root.inkInset
    visible: root.view === "build_failed" && root.compileProblems.length > 0
    spacing: Style.spacing.xs
    Repeater {
      model: root.view === "build_failed" ? root.compileProblems : []
      delegate: BuildDiagnosticRow {
        required property var modelData
        Layout.fillWidth: true
        diag: modelData
        projectPath: root.path
        app: root.app
        showChip: false
        onClicked: root.openAt(modelData.file, modelData.line)
      }
    }
  }

  // ---------------------------------------------------------------- no test target
  EmptyState {
    Layout.fillWidth: true
    Layout.topMargin: Style.spacing.lg
    Layout.bottomMargin: Style.spacing.lg
    visible: root.view === "no_tests"
    compact: true
    glyph: Model.Glyph.testTube
    title: "No unit tests yet"
    text: (root.project.name || "This project") + " has no test target. Add one in a Tests/ folder that depends on a library "
          + "target with your app's logic. New projects made with the “App with unit tests” template already have one."
    readonly property var fix: root.run && root.run.error ? root.run.error.fix || null : null
    actionText: fix ? fix.label : "Open in editor"
    actionGlyph: Model.Glyph.code
    actionKind: "secondary"
    onAction: fix ? root.useFix(fix) : root.useFix({ action: "open" })
    secondaryText: "Check again"
    secondaryGlyph: Model.Glyph.refresh
    onSecondary: root.checkAgain()
  }

  // ---------------------------------------------------------------- tested code imports UIKit / SwiftUI
  Rectangle {
    id: hostBox
    Layout.fillWidth: true
    Layout.leftMargin: root.inkInset
    visible: root.view === "host_unsupported"
    radius: Style.cornerRadius
    color: Theme.levelFill("warn")
    border.width: Style.normalBorderWidth
    border.color: Util.alpha(Theme.warn, 0.35)
    implicitHeight: hostCol.implicitHeight + Style.spacing.lg * 2
    // the rows' insets, glyph column and gap (BuildDiagnosticRow), so its glyph and text line up with the imports below
    ColumnLayout {
      id: hostCol
      anchors.fill: parent
      anchors.topMargin: Style.spacing.lg
      anchors.bottomMargin: Style.spacing.lg
      anchors.leftMargin: Style.spacing.rowPaddingX
      anchors.rightMargin: Style.spacing.md
      spacing: Style.spacing.xs
      RowLayout {
        Layout.fillWidth: true
        spacing: Style.spacing.xl
        FontMetrics { id: hostMetrics; font: hostTitle.font }
        Item {
          Layout.alignment: Qt.AlignTop
          Layout.preferredWidth: Style.font.iconLarge * 1.4
          Layout.preferredHeight: hostMetrics.height
          Icon { anchors.centerIn: parent; glyph: Model.Glyph.warn; color: Theme.warn; size: Style.font.icon }
        }
        ColumnLayout {
          Layout.fillWidth: true
          spacing: Style.spacing.xxs
          AppText { id: hostTitle; Layout.fillWidth: true; text: "This code can't run on the computer"; strong: true; wrapMode: Text.Wrap }
          AppText {
            Layout.fillWidth: true
            text: "The code these tests use imports UIKit or SwiftUI, which only exist on the iPhone. Only Foundation logic in "
                  + "a separate library target can be tested here: move it there, out of the files below, and check again."
            tone: "dim"; size: "small"; wrapMode: Text.Wrap
          }
        }
        ActionButton {
          Layout.alignment: Qt.AlignTop
          text: "Check again"
          glyph: Model.Glyph.refresh
          kind: "ghost"
          compact: true
          busy: root.infoMine && root.tests.info.busy
          onClicked: root.checkAgain()
        }
      }
    }
  }
  ColumnLayout {
    Layout.fillWidth: true
    Layout.leftMargin: root.inkInset
    visible: root.view === "host_unsupported" && root.blockers.length > 0
    spacing: Style.spacing.xs
    Repeater {
      model: root.view === "host_unsupported" ? root.blockers.slice(0, 20) : []
      delegate: BuildDiagnosticRow {
        required property var modelData
        Layout.fillWidth: true
        diag: ({ severity: "warning", category: "compiler", message: "import " + (modelData.module || "SwiftUI"),
                 file: modelData.file, line: modelData.line })
        projectPath: root.path
        showChip: false
        onClicked: root.openAt(modelData.file, modelData.line)
      }
    }
  }

  // ---------------------------------------------------------------- the filter matched nothing
  EmptyState {
    Layout.fillWidth: true
    Layout.topMargin: Style.spacing.lg
    Layout.bottomMargin: Style.spacing.lg
    visible: root.view === "no_match"
    compact: true
    glyph: Model.Glyph.filter
    title: "No test matches the filter"
    text: "The filter is a regular expression matched against Target.Suite/test, e.g. StreakCalculatorTests or testAdd."
    actionText: "Run all tests"
    actionGlyph: Model.Glyph.play
    actionKind: "secondary"
    onAction: root.start("")
  }

  // ---------------------------------------------------------------- toolchain, info and other errors
  ErrorState {
    Layout.fillWidth: true
    visible: root.view === "toolchain" || root.view === "error" || root.view === "info_error"
    compact: true
    title: root.view === "toolchain" ? "Swift isn't ready for tests" : root.view === "info_error" ? "Couldn't read the test targets" : "The tests didn't run"
    error: root.view === "info_error" ? (root.tests ? root.tests.info.error : null) : (root.run ? root.run.error : null)
    showRetry: root.view !== "toolchain"
    onRetry: root.view === "info_error" ? root.checkAgain() : root.start()
    // "Open in editor" ({page: "projects", action: "open"}) is this sheet's to do: navigating to Projects with it
    // would do nothing. Any other fix opens a page (Settings, or Projects' {action: "test", path}, which opens
    // this sheet again), so the sheet closes first.
    readonly property bool localFix: !!fix && fix.action === "open" && !fix.url
    app: localFix ? null : root.app
    onFixed: function(f) { if (localFix) root.useFix(f); else root.close() }
  }

  // ---------------------------------------------------------------- footnote
  RowLayout {
    Layout.fillWidth: true
    Layout.leftMargin: root.inkInset
    Layout.topMargin: Style.spacing.xs
    spacing: Style.spacing.lg
    FontMetrics { id: noteMetrics; font: noteText.font }
    Item {
      Layout.alignment: Qt.AlignTop
      Layout.preferredWidth: noteIcon.implicitWidth
      Layout.preferredHeight: noteMetrics.height
      Icon { id: noteIcon; anchors.centerIn: parent; glyph: Model.Glyph.info; size: Style.font.bodySmall; color: Theme.faint }
    }
    AppText {
      id: noteText
      Layout.fillWidth: true
      text: "Tests run on this computer with swift test. UI code is tested on the iPhone."
      tone: "faint"; size: "small"; wrapMode: Text.Wrap
    }
  }

  // The steps so far, then the ones still to come (pending), so the run's shape shows from the start. Only
  // rebuilt when a step changes (not on every count), so the running step's spinner doesn't restart.
  readonly property string _stepsKey: run ? run.steps.map(function(s) { return s.id + ":" + s.state }).join(",") : ""
  property var shownSteps: []
  on_StepsKeyChanged: root.shownSteps = root.displaySteps()
  function displaySteps() {
    var have = root.run ? root.run.steps : []
    var out = have.slice()
    var defaults = [{ id: "prepare", title: "Checking the test targets" }, { id: "build", title: "Building the tests" },
                    { id: "run", title: "Running the tests" }]
    for (var i = 0; i < defaults.length; i++) {
      var known = false
      for (var j = 0; j < have.length; j++) if (have[j].id === defaults[i].id) known = true
      if (!known) out.push({ id: defaults[i].id, title: defaults[i].title, state: "pending" })
    }
    return out
  }

  footer: [
    ActionButton {
      text: "Close"
      kind: "ghost"
      tooltip: root.running ? "The tests keep running; their result shows when they finish." : ""
      onClicked: root.close()
    },
    ActionButton {
      visible: root.failedFilter !== ""
      text: "Run failed only"
      glyph: Model.Glyph.refresh
      tooltip: "Run only the " + Model.plural(root.res ? root.res.failed.length : 0, "test") + " that failed"
      onClicked: root.runFailedOnly()
    },
    ActionButton {
      visible: !root.running
      text: root.run && root.run.state === "done" && root.runBlocker === "" ? "Run again" : "Run tests"
      glyph: Model.Glyph.play
      kind: "primary"
      disabled: root.runBlocker !== ""
      tooltip: root.runBlocker !== "" ? root.runBlocker
               : filterField.text.trim() !== "" ? "Run the tests matching the filter (Enter)" : "Build and run every test target with swift test"
      onClicked: root.start()
    },
    ActionButton {
      visible: root.running
      text: "Cancel"
      glyph: Model.Glyph.stop
      kind: "danger"
      busy: !!root.run && root.run.cancelling
      tooltip: "Stop swift test"
      onClicked: root.cancel()
    }
  ]
}
