import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import qs.components
import "../Model.js" as Model

// Settings › Code editor: which editor "Open in editor" uses, and Swift completion and inline errors
// (SourceKit-LSP) set up once per computer for VS Code, Zed and Neovim (helper/iosstudio/features/editor_setup.py,
// `editor status` / `editor setup --editor X [--confirm]`, through app.projects).
//   EditorSection { Layout.fillWidth: true; app: page.app }
// Set up first shows the dry run under the row (Create / Update / Keep / Back up) and only Apply
// changes anything. A plan that touches your own config (Neovim's lazyvim.json) says so above Apply.
// Editors that aren't installed collapse into one quiet line.
Card {
  id: root
  required property var app
  readonly property var svc: app.projects

  title: "Code editor"
  subtitle: "Open projects in your editor, with Swift completion and errors as you type."
  glyph: Model.Glyph.code
  contentSpacing: 0
  actions: [
    IconButton { glyph: Model.Glyph.help; tooltip: "How it works, and setting up an editor by hand"; onClicked: root.openDocs("") },
    IconButton { glyph: Model.Glyph.refresh; tooltip: "Check again"; busy: !!root.svc && root.svc.editorInfoLoading; onClicked: root.svc.loadEditors() }
  ]

  readonly property var info: svc ? svc.editorInfo : null
  readonly property var editorList: info && info.editors ? info.editors : []
  readonly property var installed: editorList.filter(function(e) { return e.installed })
  readonly property var missing: editorList.filter(function(e) { return !e.installed })
  readonly property string lspPath: {
    var s = info ? info.sourcekit_lsp : null
    if (!s) return ""
    return typeof s === "string" ? s : (s.path || "")
  }
  property bool customOpen: false     // "Other command…" picked; the field shows
  property var plans: ({})              // editor id -> { state: "loading"|"ready"|"error"|"running"|"done", result, error }

  Component.onCompleted: if (svc && !svc.editorInfo && !svc.editorInfoLoading) svc.loadEditors()

  function _setPlan(id, fields) {
    var all = Object.assign({}, root.plans)
    if (fields === null) delete all[id]
    else all[id] = Object.assign({}, all[id] || {}, fields)
    root.plans = all
  }
  function plan(ed) {
    root._setPlan(ed.id, { state: "loading", error: null, result: null })
    svc.editorSetup(ed.id, false, function(d) { root._setPlan(ed.id, { state: "ready", result: d }) },
                    function(e) { root._setPlan(ed.id, { state: "error", error: e }) })
  }
  function cancel(ed) { root._setPlan(ed.id, null) }
  function apply(ed) {
    root._setPlan(ed.id, { state: "running" })
    svc.editorSetup(ed.id, true, function(d) {
      root._setPlan(ed.id, { state: "done", result: d })
      root.choose(ed.id, true)     // setup remembers it as your editor; "Open in editor" follows unless it's a custom command
      root.app.toast(ed.name + " is set up for Swift", "ok")
    }, function(e) { root._setPlan(ed.id, { state: "error", error: e }) })
  }

  // The editor docs (setup by hand, per editor), in the browser or the default viewer.
  function openDocs(anchor) { Util.execArgv(["xdg-open", root.app.repoPath + "/docs/features/editor-setup.md"]) }

  // ---------------------------------------------------------------- wording
  function shortPath(p) {
    var s = String(p || "")
    var home = root.app.home
    return home && s.indexOf(home + "/") === 0 ? "~" + s.substring(home.length) : s
  }
  function tidy(text) { var h = root.app.home; return h ? String(text || "").split(h + "/").join("~/") : String(text || "") }
  // A change to the editor's own config: scope "user", except iOS Studio's settings.json (also "user"),
  // which only remembers your choice of editor.
  function editorConfig(a) {
    var p = String(a.path || "")
    return a.scope === "user" && p.indexOf(root.app.configDir + "/") !== 0 && !/\/ios-studio\/settings\.json$/.test(p)
  }
  function changes(d) { return ((d && d.actions) || []).filter(function(a) { return a.kind !== "skip" }) }
  readonly property var _kindLabel: ({ write: "Create", merge: "Update", skip: "Keep", run: "Run", backup: "Back up" })
  readonly property var _kindDone: ({ write: "Created", merge: "Updated", skip: "Kept", run: "Ran", backup: "Backed up" })
  function kindLabel(kind, done) { return (done ? root._kindDone : root._kindLabel)[kind] || kind }
  function stateLevel(s) { return { "ready": "ok", "partial": "warn" }[s] || "info" }
  function stateText(s) { return { "ready": "Ready", "partial": "One step left", "needs-setup": "Not set up", "not-installed": "Not installed" }[s] || s }
  // Your editor: settings.editorSetup (what the helper calls `preferred`) and the command "Open in editor"
  // runs (settings.editorCommand), kept in step. keepCustom leaves a hand-written command alone.
  function choose(id, keepCustom) {
    var cmd = ""
    for (var i = 0; i < root.editorList.length; i++) if (root.editorList[i].id === id) cmd = root.commandOf(root.editorList[i])
    var cur = root.app.settings.editorCommand || ""
    if (keepCustom && cur !== "" && preferred.value === "custom") cmd = cur
    root.app.setSetting("editorSetup", id || null)
    root.app.setSetting("editorCommand", cmd)
  }
  // The command "Open in editor" runs for an editor (its binary's name).
  function commandOf(e) {
    var b = String(e.command || e.binary || "")
    if (b === "" || b.indexOf("flatpak:") === 0) return { vscode: "code", zed: "zeditor", neovim: "nvim" }[e.id] || ""
    return b.split("/").pop()
  }

  // ---------------------------------------------------------------- open in
  SettingRow {
    separator: false
    title: "Your editor"
    description: "Opens your projects and the file and line of an error. With VS Code, projects also get its project settings."
    Dropdown {
      id: preferred
      showLabel: false
      width: Style.space(240)
      readonly property string cmd: root.app.settings.editorCommand || ""
      value: {
        if (cmd !== "") {
          for (var i = 0; i < root.installed.length; i++) if (root.commandOf(root.installed[i]) === cmd) return root.installed[i].id
          return "custom"
        }
        var pref = root.app.settings.editorSetup || (root.info ? root.info.preferred : "") || ""
        return root.installed.some(function(e) { return e.id === pref }) ? pref : ""
      }
      options: {
        var o = [{ value: "", label: "Omarchy default" }]
        for (var i = 0; i < root.installed.length; i++) o.push({ value: root.installed[i].id, label: root.installed[i].name })
        o.push({ value: "custom", label: "Other command…" })
        return o
      }
      onChanged: function(v) {
        root.customOpen = v === "custom"
        if (v === "custom") { Qt.callLater(function() { customField.forceActiveFocus() }); return }
        root.choose(v, false)
        var name = ""
        for (var i = 0; i < root.installed.length; i++) if (root.installed[i].id === v) name = root.installed[i].name
        root.app.toast(v === "" ? "Projects open in Omarchy's default editor" : "Projects open in " + name, "ok")
      }
    }
  }
  RowLayout {
    Layout.fillWidth: true
    Layout.bottomMargin: Style.spacing.xl
    visible: root.customOpen || preferred.value === "custom"
    Item { Layout.fillWidth: true }
    TextField {
      id: customField
      Layout.preferredWidth: Style.space(240)
      text: root.app.settings.editorCommand || ""
      placeholderText: "e.g. hx or code --new-window"
      onEditingFinished: if (text.trim() !== (root.app.settings.editorCommand || "")) { root.app.setSetting("editorCommand", text.trim()); root.app.toast("Editor saved", "ok") }
    }
  }

  // ---------------------------------------------------------------- Swift completion
  SectionHeader {
    Layout.fillWidth: true
    Layout.topMargin: Style.spacing.lg
    Layout.bottomMargin: Style.spacing.sm
    text: "Swift completion"
    detail: root.lspPath !== "" ? "SourceKit-LSP · " + root.shortPath(root.lspPath) : ""
  }

  ErrorState {
    Layout.fillWidth: true
    visible: !!root.svc.editorInfoError && !root.info
    banner: true
    app: root.app
    error: root.svc.editorInfoError
    onRetry: root.svc.loadEditors()
  }
  Skeleton {
    Layout.fillWidth: true
    visible: !root.info && !root.svc.editorInfoError
    lines: 2
    rowHeight: Style.space(44)
    gap: Style.spacing.sm
  }
  AppText {
    Layout.fillWidth: true
    Layout.bottomMargin: Style.spacing.md
    visible: !!root.info && root.lspPath === ""
    text: "SourceKit-LSP wasn't found. It comes with the Swift toolchain: install that on the Setup page first."
    size: "small"; tone: "warn"; wrapMode: Text.WordWrap
  }

  // Width probe for the rows' button slot (hidden, so the layout skips it).
  ActionButton { id: probeSetup; visible: false; compact: true; text: "Set up…"; glyph: Model.Glyph.editorSetup }

  Repeater {
    model: root.installed
    delegate: EditorSetupRow {
      required property var modelData
      required property int index
      Layout.fillWidth: true
      section: root
      editor: modelData
      plan: root.plans[modelData.id] || null
      first: index === 0
      slotWidth: probeSetup.implicitWidth
      onSetupRequested: root.plan(modelData)
      onApplyRequested: root.apply(modelData)
      onCancelRequested: root.cancel(modelData)
    }
  }

  // Not installed: one quiet line, the glyph and text lined up with the rows above.
  RowLayout {
    Layout.fillWidth: true
    Layout.topMargin: Style.spacing.lg
    visible: !!root.info && root.missing.length > 0
    spacing: Style.spacing.xxl
    Item {
      Layout.alignment: Qt.AlignTop
      Layout.preferredWidth: Style.space(32)
      Layout.preferredHeight: missingText.implicitHeight
      Icon { anchors.centerIn: parent; glyph: Model.Glyph.fileCode; size: Style.font.icon; color: Theme.faint }
    }
    AppText {
      id: missingText
      Layout.fillWidth: true
      text: root.installed.length === 0
            ? "No supported editor found. Install VS Code, Zed or Neovim, then press Check again."
            : "Not installed: " + root.missing.map(function(e) { return e.name }).join(", ")
      size: "small"; tone: "faint"; wrapMode: Text.WordWrap
    }
  }
}
