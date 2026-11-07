import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../common"
import "../../lib/Model.js" as Model

// Screen › Compare, beside the phone (DeviceScreenView picks the pane). A baseline is a saved
// screenshot of one screen of a project ("home", "settings"); Compare takes a new screenshot and
// diffs it against the baseline with that name (helper group `screenshots`, see
// docs/features/screenshot-compare.md). The result shows baseline | current | diff side by side,
// with the changed regions outlined, the diff %, the verdict and the threshold. The status bar
// (clock, battery) is masked by the helper and shaded here. The current screenshot can replace
// the baseline; a baseline can be deleted (asks first).
// The project is the one last picked here, else the last one built, else the first in the list;
// the choice is kept in the `compareProject` setting.
//   ScreenshotCompareView { app: root.app }
ColumnLayout {
  id: root
  required property var app
  readonly property var svc: app.device
  readonly property var projects: app.projects ? app.projects.items || [] : []

  spacing: Style.spacing.huge

  // ---------------------------------------------------------------- project
  property string projectPath: ""
  readonly property var project: {
    for (var i = 0; i < projects.length; i++) if (projects[i].path === projectPath) return projects[i]
    return null
  }
  readonly property string projectName: project ? (project.name || project.path.slice(project.path.lastIndexOf("/") + 1)) : ""
  function pickDefaultProject() {
    if (root.project || projects.length === 0) return
    var want = [root.app.setting("compareProject", ""), root.app.projects.build ? root.app.projects.build.path || "" : ""]
    for (var w = 0; w < want.length; w++)
      for (var i = 0; i < projects.length; i++)
        if (want[w] !== "" && projects[i].path === want[w]) { root.projectPath = want[w]; return }
    root.projectPath = projects[0].path
  }
  function chooseProject(path) {
    if (path === root.projectPath) return
    root.projectPath = path
    root.app.setSetting("compareProject", path)
  }
  onProjectsChanged: pickDefaultProject()
  Component.onCompleted: pickDefaultProject()
  // The list is read when the pane is shown (and again after every save or delete), never in the background.
  onProjectPathChanged: { root.result = null; root.actionError = null; root.baselines = []; root.listStatus = "idle"; if (root.visible) root.loadBaselines() }
  onVisibleChanged: if (root.visible && root.listStatus === "idle") root.loadBaselines()

  // ---------------------------------------------------------------- baselines
  property string listStatus: "idle"      // idle | loading | ready | error
  property var baselines: []
  property var listError: null
  property int _listGen: 0
  property int revision: 0                // bumps when a baseline file is rewritten, so images reload

  function baselineNamed(n) {
    // the helper matches names ignoring case
    var k = String(n || "").toLowerCase()
    for (var i = 0; i < baselines.length; i++) if (String(baselines[i].name).toLowerCase() === k) return baselines[i]
    return null
  }
  function loadBaselines() {
    if (root.projectPath === "") { root.baselines = []; root.listStatus = "idle"; return }
    var gen = ++root._listGen
    root.listStatus = "loading"
    root.listError = null
    root.app.cli.call(["screenshots", "list", "--project", root.projectPath], "", function(d) {
      if (gen !== root._listGen) return
      root.baselines = d.baselines || []
      root.listStatus = "ready"
    }, function(e) {
      if (gen !== root._listGen) return
      root.listError = e
      root.listStatus = "error"
    }, { timeout: 30000 })
  }

  // ---------------------------------------------------------------- name
  readonly property string name: nameField.text.trim()
  function setName(n) { nameField.text = n }
  readonly property var named: baselineNamed(name)

  // ---------------------------------------------------------------- actions
  property string busy: ""                // "" | compare-shot | comparing | save-shot | saving | deleting
  property var result: null               // last `screenshots compare` answer
  property var actionError: null
  property string actionErrorTitle: ""

  // Why Compare / Save can't run now ("" = they can).
  readonly property string blocked: !svc.connected ? "Connect your iPhone first"
                                  : (projectPath === "" ? "Pick a project first"
                                  : (name === "" ? "Type a screen name first"
                                  : (busy !== "" ? "Wait for the current step to finish" : "")))

  function fail(title, err) { root.busy = ""; root.actionErrorTitle = title; root.actionError = err }

  function compare() {
    if (root.blocked !== "") return
    var project = root.projectPath, name = root.name
    root.actionError = null
    root.busy = "compare-shot"
    root.svc.screenshot(function(ok, d) {
      if (!ok) { root.busy = ""; return }       // DeviceService already said why (toast)
      root.busy = "comparing"
      root.app.cli.call(["screenshots", "compare", "--project", project, "--name", name, "--shot", d.path], "", function(r) {
        root.busy = ""
        if (project !== root.projectPath) return  // another project was picked meanwhile
        root.revision++
        root.result = r
      }, function(e) { root.fail("Couldn't compare the screenshots", e) }, { timeout: 60000 })
    })
  }

  // Save a new screenshot as the baseline (asks first when it would replace one).
  function saveNew() {
    if (root.blocked !== "") return
    var go = function() {
      root.actionError = null
      root.busy = "save-shot"
      root.svc.screenshot(function(ok, d) {
        if (!ok) { root.busy = ""; return }     // DeviceService already said why (toast)
        root.save(root.name, d.path)
      })
    }
    if (root.named) confirmReplace(root.name, go)
    else go()
  }

  // The compared screenshot becomes the baseline (no new screenshot).
  function accept() {
    var r = root.result
    if (!r || !r.current_path || root.busy !== "") return
    var go = function() { root.save(r.name, r.current_path, true) }
    if (r.verdict !== "no-baseline" && root.baselineNamed(r.name)) confirmReplace(r.name, go)
    else go()
  }

  function confirmReplace(n, go) {
    root.app.confirm({
      title: "Replace the baseline “" + n + "”?",
      message: "The saved screenshot of " + root.projectName + " is overwritten. Later comparisons use the new one.",
      confirmText: "Replace", destructive: true, glyph: Model.Glyph.imagePlus,
      onConfirm: go
    })
  }

  function save(n, shot, accepted) {
    root.busy = "saving"
    root.actionError = null
    root.app.cli.call(["screenshots", "save", "--project", root.projectPath, "--name", n, "--shot", shot], "", function(d) {
      root.busy = ""
      root.revision++
      if (accepted && root.result && root.result.name === n) {
        // the current screenshot is the baseline now: the comparison is moot
        root.result = null
      }
      nameField.text = d.name || n
      root.app.toast((d.replaced ? "Replaced" : "Saved") + " the baseline “" + (d.name || n) + "” of " + root.projectName, "ok")
      root.loadBaselines()
    }, function(e) { root.fail("Couldn't save the baseline", e) }, { timeout: 30000 })
  }

  function remove(n) {
    if (root.busy !== "") return
    root.app.confirm({
      title: "Delete the baseline “" + n + "”?",
      message: "Its saved screenshot of " + root.projectName + " is deleted. Comparing “" + n + "” then needs a new baseline.",
      confirmText: "Delete", destructive: true, glyph: Model.Glyph.trash,
      onConfirm: function() {
        root.busy = "deleting"
        root.actionError = null
        root.app.cli.call(["screenshots", "delete", "--project", root.projectPath, "--name", n, "--confirm"], "", function(d) {
          root.busy = ""
          if (root.result && root.result.name === n) root.result = null
          root.app.toast("Deleted the baseline “" + n + "”", "ok")
          root.loadBaselines()
        }, function(e) { root.fail("Couldn't delete the baseline", e) })
      }
    })
  }

  // ---------------------------------------------------------------- result text
  function pct(v) {
    var n = Number(v || 0)
    if (n === 0) return "0 %"
    if (n < 0.01) return "< 0.01 %"
    return (n < 10 ? n.toFixed(2) : n.toFixed(1)).replace(/\.?0+$/, "") + " %"
  }
  function dims(w, h) { return w > 0 && h > 0 ? w + " × " + h : "" }
  readonly property var verdictInfo: ({
    "same": { label: "Same", level: "ok" },
    "changed": { label: "Changed", level: "warn" },
    "size-mismatch": { label: "Size mismatch", level: "warn" },
    "no-baseline": { label: "No baseline", level: "info" }
  })
  readonly property string verdict: result ? String(result.verdict || "") : ""
  readonly property var resultBaseline: result ? baselineNamed(result.name) : null
  readonly property string resultText: {
    var r = root.result
    if (!r) return ""
    var t = root.pct(r.threshold !== undefined ? r.threshold : 0.5)
    if (r.verdict === "same") return root.pct(r.diff_percent) + " of the pixels differ, within the " + t + " threshold."
    if (r.verdict === "changed") return root.pct(r.diff_percent) + " of the pixels differ, above the " + t + " threshold."
    if (r.verdict === "size-mismatch") {
      var bw = r.baseline_width || (root.resultBaseline ? root.resultBaseline.width : 0)
      var bh = r.baseline_height || (root.resultBaseline ? root.resultBaseline.height : 0)
      return "The baseline is " + (root.dims(bw, bh) || "another size")
             + ", this screenshot " + (root.dims(r.width, r.height) || "a different size") + ". Turn the iPhone the same way, or save this one as the new baseline."
    }
    if (r.verdict === "no-baseline") return "Nothing is saved as “" + r.name + "” yet. Save this screenshot as the baseline, then compare against it."
    return ""
  }
  readonly property var regions: result && result.regions ? result.regions : []
  readonly property var resultNotes: {
    var r = root.result
    if (!r) return ""
    var p = []
    var total = Math.max(r.regions_total || 0, root.regions.length)
    if (total) p.push(total + (total === 1 ? " changed area" : " changed areas")
                      + (total > root.regions.length ? ", the " + root.regions.length + " biggest outlined" : " outlined"))
    if (r.masked_top > 0) p.push("status bar ignored (top " + r.masked_top + " px)")
    var s = p.join(" · ")
    return s ? s.charAt(0).toUpperCase() + s.slice(1) : ""
  }

  // ================================================================ no projects
  EmptyState {
    Layout.fillWidth: true
    visible: root.projects.length === 0
    compact: true
    glyph: Model.Glyph.hammer
    title: "No projects yet"
    text: "Baselines belong to a project. Add or create an app on the Projects page first."
    actionText: "Open Projects"; actionGlyph: Model.Glyph.arrowRight
    onAction: root.app.nav("projects")
  }

  // ================================================================ project + name + actions
  ColumnLayout {
    Layout.fillWidth: true
    visible: root.projects.length > 0
    spacing: Style.spacing.lg

    AppText {
      Layout.fillWidth: true
      wrapMode: Text.WordWrap
      tone: "dim"
      Layout.bottomMargin: Style.spacing.md
      text: "Save a screenshot of a screen as its baseline, then compare the iPhone's screen against it after a change. The status bar is ignored."
    }

    GridLayout {
      id: fields
      Layout.fillWidth: true
      columns: width < Style.space(440) ? 1 : 2
      columnSpacing: Style.spacing.xl
      rowSpacing: Style.spacing.lg

      ColumnLayout {
        Layout.fillWidth: true
        Layout.preferredWidth: 1
        Layout.alignment: Qt.AlignTop
        spacing: Style.spacing.labelGap
        AppText { text: "Project"; size: "caption"; tone: "dim" }
        Dropdown {
          Layout.fillWidth: true
          showLabel: false
          enabled: root.busy === ""             // the running step belongs to this project
          rowHeight: nameField.implicitHeight
          value: root.projectPath
          options: root.projects.map(function(p) { return { value: p.path, label: p.name || p.path } })
          onChanged: function(v) { root.chooseProject(v) }
        }
      }
      ColumnLayout {
        Layout.fillWidth: true
        Layout.preferredWidth: 1
        Layout.alignment: Qt.AlignTop
        spacing: Style.spacing.labelGap
        AppText { text: "Screen name"; size: "caption"; tone: "dim" }
        TextField {
          id: nameField
          Layout.fillWidth: true
          placeholderText: "home, settings, …"
          onAccepted: root.compare()
        }
      }
    }

    Flow {
      Layout.fillWidth: true
      spacing: Style.spacing.lg
      ActionButton {
        id: compareButton
        text: root.busy === "compare-shot" ? "Taking screenshot…" : (root.busy === "comparing" ? "Comparing…" : "Compare")
        glyph: Model.Glyph.compare
        kind: "primary"
        busy: root.busy === "compare-shot" || root.busy === "comparing"
        disabled: root.blocked !== "" && !busy
        tooltip: root.blocked !== "" ? root.blocked
               : (root.named ? "Take a screenshot and compare it with “" + root.name + "”" : "No baseline named “" + root.name + "” yet: the screenshot is shown, then you can save it")
        onClicked: root.compare()
      }
      ActionButton {
        text: root.busy === "save-shot" ? "Taking screenshot…" : (root.busy === "saving" ? "Saving…" : (root.named ? "Replace baseline" : "Save as baseline"))
        glyph: Model.Glyph.imagePlus
        busy: root.busy === "save-shot" || (root.busy === "saving" && !root.result)
        disabled: root.blocked !== "" && !busy
        tooltip: root.blocked !== "" ? root.blocked : "Take a screenshot and save it as “" + root.name + "”"
        onClicked: root.saveNew()
      }
      // the reason, spelled out (tooltips need a hover)
      AppText {
        visible: root.blocked !== "" && root.busy === ""
        height: compareButton.height
        verticalAlignment: Text.AlignVCenter
        text: root.blocked + "."
        size: "small"; tone: "faint"
      }
    }

    ErrorState {
      Layout.fillWidth: true
      visible: !!root.actionError
      banner: true
      app: root.app
      title: root.actionErrorTitle
      error: root.actionError
      showRetry: false
    }
  }

  // ================================================================ result
  ColumnLayout {
    Layout.fillWidth: true
    visible: !!root.result
    spacing: Style.spacing.lg

    SectionHeader {
      Layout.fillWidth: true
      text: "Result"
      detail: root.result ? "“" + root.result.name + "”" : ""
      trailing: [ IconButton { glyph: Model.Glyph.close; tooltip: "Close the result"; onClicked: root.result = null } ]
    }

    RowLayout {
      Layout.fillWidth: true
      spacing: Style.spacing.lg
      StatusPill {
        id: verdictPill
        Layout.alignment: Qt.AlignTop
        readonly property var vi: root.verdictInfo[root.verdict] || { label: root.verdict, level: "info" }
        level: vi.level
        text: vi.label
      }
      // the first line centred on the pill
      FontMetrics { id: resultMetrics; font: resultLine.font }
      ColumnLayout {
        Layout.fillWidth: true
        Layout.alignment: Qt.AlignTop
        Layout.topMargin: Math.max(0, (verdictPill.implicitHeight - resultMetrics.height) / 2)
        spacing: Style.spacing.xxs
        AppText { id: resultLine; Layout.fillWidth: true; text: root.resultText; wrapMode: Text.WordWrap }
        AppText { Layout.fillWidth: true; visible: root.resultNotes !== ""; text: root.resultNotes; size: "small"; tone: "faint"; wrapMode: Text.WordWrap }
      }
    }

    // baseline | current | diff, the same size; tall screenshots keep their shape
    RowLayout {
      id: panes
      Layout.fillWidth: true
      spacing: Style.spacing.lg
      readonly property real paneW: (width - spacing * 2) / 3
      readonly property real aspect: root.result && root.result.width > 0 && root.result.height > 0 ? root.result.width / root.result.height : 1206 / 2622
      readonly property real cellH: Math.min(Style.space(520), Math.max(Style.space(160), paneW / aspect))

      ScreenshotDiffPane {
        Layout.fillWidth: true
        Layout.preferredWidth: 1
        title: "Baseline"
        detail: root.resultBaseline && root.result.verdict !== "no-baseline" ? root.dims(root.resultBaseline.width, root.resultBaseline.height) : ""
        path: root.result && root.result.verdict !== "no-baseline" ? root.result.baseline_path || "" : ""
        revision: root.revision
        imageSize: root.resultBaseline ? { w: root.resultBaseline.width, h: root.resultBaseline.height } : null
        maskedTop: root.result ? root.result.masked_top || 0 : 0
        placeholder: "No baseline saved yet"
        cellHeight: panes.cellH
      }
      ScreenshotDiffPane {
        Layout.fillWidth: true
        Layout.preferredWidth: 1
        title: "Current"
        detail: root.result ? root.dims(root.result.width, root.result.height) : ""
        path: root.result ? root.result.current_path || "" : ""
        revision: root.revision
        imageSize: root.result && root.result.width > 0 ? { w: root.result.width, h: root.result.height } : null
        regions: root.verdict === "changed" || root.verdict === "same" ? root.regions : []
        maskedTop: root.result ? root.result.masked_top || 0 : 0
        cellHeight: panes.cellH
      }
      ScreenshotDiffPane {
        Layout.fillWidth: true
        Layout.preferredWidth: 1
        title: "Diff"
        detail: root.result && root.result.diff_path ? root.pct(root.result.diff_percent) : ""
        path: root.result ? root.result.diff_path || "" : ""
        revision: root.revision
        imageSize: root.result && root.result.width > 0 ? { w: root.result.width, h: root.result.height } : null
        // the diff image has its own outlines and its ignored areas striped
        placeholderGlyph: Model.Glyph.compare
        placeholder: root.verdict === "size-mismatch" ? "No diff: the sizes differ" : "No diff without a baseline"
        cellHeight: panes.cellH
      }
    }

    Flow {
      Layout.fillWidth: true
      // With only the borderless Delete button left (Same), its glyph lines up with the panes' edge.
      Layout.leftMargin: root.verdict === "same" ? -(Style.spacing.controlPaddingX + Style.spacing.xs) : 0
      spacing: Style.spacing.lg
      ActionButton {
        visible: root.verdict !== "same"
        text: root.verdict === "no-baseline" ? "Save as baseline" : "Accept as new baseline"
        glyph: Model.Glyph.check
        busy: root.busy === "saving"
        disabled: root.busy !== "" && !busy
        tooltip: root.busy !== "" ? "Wait for the current step to finish" : "Use the current screenshot as the baseline for “" + (root.result ? root.result.name : "") + "”"
        onClicked: root.accept()
      }
      ActionButton {
        visible: root.verdict !== "no-baseline"
        text: "Delete baseline"
        glyph: Model.Glyph.trash
        kind: "ghost"
        busy: root.busy === "deleting"
        disabled: root.busy !== "" && !busy
        tooltip: root.busy !== "" ? "Wait for the current step to finish" : "Delete the saved screenshot “" + (root.result ? root.result.name : "") + "”"
        onClicked: root.remove(root.result.name)
      }
    }
  }

  // ================================================================ baselines of the project
  ColumnLayout {
    Layout.fillWidth: true
    visible: root.projectPath !== ""
    spacing: Style.spacing.md

    SectionHeader {
      Layout.fillWidth: true
      text: "Baselines"
      detail: root.listStatus === "ready" && root.baselines.length ? String(root.baselines.length) : ""
      trailing: [ IconButton { glyph: Model.Glyph.refresh; tooltip: "Read the list again"; busy: root.listStatus === "loading"; onClicked: root.loadBaselines() } ]
    }

    Skeleton {
      Layout.fillWidth: true
      visible: root.listStatus === "loading" && root.baselines.length === 0
      lines: 2
      rowHeight: Style.space(44)
      gap: Style.spacing.md
    }

    ErrorState {
      Layout.fillWidth: true
      visible: root.listStatus === "error"
      banner: true
      app: root.app
      title: "Couldn't list the baselines"
      error: root.listError
      onRetry: root.loadBaselines()
    }

    QuietNote {
      Layout.fillWidth: true
      visible: root.listStatus === "ready" && root.baselines.length === 0
      glyph: Model.Glyph.image
      title: "No baselines yet for " + root.projectName + ", save one first"
      text: "Open the screen on your iPhone, type a name for it and press Save as baseline."
    }

    Repeater {
      model: root.baselines
      delegate: ListRow {
        id: brow
        required property var modelData
        required property int index
        Layout.fillWidth: true
        glyph: Model.Glyph.image
        title: modelData.name
        subtitle: [root.dims(modelData.width, modelData.height), modelData.created ? "saved " + Model.relativeTime(modelData.created) : ""]
                  .filter(function(s) { return s !== "" }).join(" · ")
        current: !!root.named && root.named.name === modelData.name
        hasCursor: rowHover
        property bool rowHover: false
        onHovered: function(h) { rowHover = h }
        onClicked: nameField.text = modelData.name
        trailing: [
          IconButton {
            glyph: Model.Glyph.trash
            tooltip: "Delete this baseline"
            danger: true
            enabled: root.busy === ""
            onClicked: root.remove(brow.modelData.name)
          }
        ]
      }
    }
  }
}
