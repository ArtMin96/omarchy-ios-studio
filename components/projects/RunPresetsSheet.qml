import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../common"
import "../../core"
import "../../lib/Model.js" as Model

// "Run presets" sheet for one project (projects slice): saved launch arguments, environment variables,
// app language, region and a UI test mode switch that Run can launch the app with. Two views in one
// sheet: the list (use for Run, edit, delete, new) and the editor for one preset, with a live preview
// of what the app gets. Stored per project by the helper (`projects presets`, helper/iosstudio/features/run_presets.py);
// the rules below mirror the helper's, which still checks everything on save.
//   RunPresetsSheet { id: presetsSheet; app: page.app; svc: page.svc }
//   presetsSheet.openFor(project)            // the list
//   presetsSheet.openFor(project, "German")  // straight into that preset's editor
Sheet {
  id: root

  property var app: null
  property var svc: null
  property var project: ({})
  property bool working: false
  property var saveError: null           // saving failed (the editor stays open)
  // The helper's answer: { path, presets, selected }; saving, deleting and choosing a preset answer with it too.
  Resource {
    id: presetsRes
    fetch: (done, fail) => root.svc.loadPresets(root.project, done, fail)
    onSucceeded: if (root._openWith !== "") { var n = root._openWith; root._openWith = ""; root.edit(n) }
  }
  readonly property var loaded: presetsRes.data
  readonly property bool loading: presetsRes.busy
  readonly property var error: root.saveError || presetsRes.error
  property var editing: null             // null = the list; { renameFrom: "" (new) | old name }
  property string _openWith: ""
  property int cursor: -1

  readonly property var presets: loaded ? (loaded.presets || []) : []
  readonly property string selected: loaded ? (loaded.selected || "") : ""

  // ---------------------------------------------------------------- editor state
  property var argRows: []               // [string]
  property var envRows: []               // [{ key, value }]
  property bool uiTestMode: false
  property string languageChoice: ""     // "" = the iPhone's, a code from Model.PRESET_LANGUAGES, or "other"
  property bool nameTouched: false
  property bool saveTried: false

  readonly property string nameText: nameField.text.trim()
  readonly property string languageCode: languageChoice === "other" ? Model.presetTag(otherLanguageField.text, "-") : languageChoice
  readonly property string regionCode: Model.presetTag(regionField.text, "_")
  readonly property var languageInfo: {
    for (var i = 0; i < Model.PRESET_LANGUAGES.length; i++) if (Model.PRESET_LANGUAGES[i].value === languageCode) return Model.PRESET_LANGUAGES[i]
    return null
  }
  readonly property string suggestedRegion: languageInfo ? languageInfo.region : ""

  readonly property string nameProblem: {
    var n = nameText
    if (n === "") return "Give the preset a name."
    if (n.length > Model.PRESET_MAX_NAME) return "Keep the name to " + Model.PRESET_MAX_NAME + " characters (it has " + n.length + ")."
    if (/[\x00-\x1f]/.test(n)) return "The name can't contain line breaks."
    var old = editing ? String(editing.renameFrom || "").toLowerCase() : ""
    for (var i = 0; i < presets.length; i++) {
      var other = String(presets[i].name).toLowerCase()
      if (other === n.toLowerCase() && other !== old) return "There's already a preset named “" + presets[i].name + "”. Pick another name."
    }
    return ""
  }
  readonly property string languageProblem: languageChoice === "other" && languageCode !== "" && !Model.PRESET_LANGUAGE_RE.test(languageCode)
                                            ? "“" + otherLanguageField.text.trim() + "” isn't a language code. Use one like de, fr, pt-BR or zh-Hans." : ""
  readonly property string regionProblem: regionCode !== "" && !Model.PRESET_REGION_RE.test(regionCode)
                                          ? "“" + regionField.text.trim() + "” isn't a region. Use a locale like de_DE, en_GB or fr_CA." : ""
  readonly property string envProblem: {
    var bad = [], seen = {}, twice = [], unnamed = false
    for (var i = 0; i < envRows.length; i++) {
      var k = envRows[i].key.trim()
      if (k === "") { if (envRows[i].value !== "") unnamed = true; continue }
      if (!Model.ENV_NAME_RE.test(k)) bad.push(k)
      else if (seen[k]) { if (twice.indexOf(k) < 0) twice.push(k) }
      seen[k] = true
    }
    if (bad.length) return "Names use letters, digits and _, and don't start with a digit: " + bad.join(", ")
    if (twice.length) return "Set once each: " + twice.join(", ")
    if (unnamed) return "A variable has a value but no name."
    return ""
  }
  // The first problem, for the Save button's tooltip.
  readonly property string problem: nameProblem || languageProblem || regionProblem || envProblem
  readonly property bool canSave: editing !== null && !working && problem === ""

  readonly property var draft: ({
    name: nameText,
    args: argRows.filter(function(a) { return a !== "" }),
    env: (function() {
      var e = {}
      for (var i = 0; i < envRows.length; i++) { var k = envRows[i].key.trim(); if (k !== "") e[k] = envRows[i].value }
      return e
    })(),
    language: languageCode !== "" ? languageCode : null,
    region: regionCode !== "" ? regionCode : null,
    uiTestMode: uiTestMode
  })
  // Preview chips: own arguments one by one, generated ones as their pair, tagged with where they come from.
  readonly property var previewArgs: {
    var out = [], d = draft
    for (var i = 0; i < d.args.length; i++) out.push({ text: d.args[i], from: "" })
    if (d.language) out.push({ text: "-AppleLanguages (" + d.language + ")", from: "Language" })
    if (d.region) out.push({ text: "-AppleLocale " + d.region, from: "Region" })
    if (d.uiTestMode) out.push({ text: "-UITestMode YES", from: "UI test mode" })
    return out
  }
  readonly property var previewEnv: Object.keys(draft.env).map(function(k) { return k + "=" + draft.env[k] })

  title: editing === null ? "Run presets" + (project && project.name ? " · " + project.name : "") : (editing.renameFrom ? "Edit “" + editing.renameFrom + "”" : "New run preset")
  subtitle: editing === null
            ? "Launch arguments, environment and language for Run."
            : "Run launches the app with these. Your iPhone's own settings don't change."
  glyph: Model.Glyph.tune
  preferredWidth: Style.space(680)
  busy: working || loading

  function openFor(p, presetName) {
    var again = root.opened && root.project && p && root.project.path === p.path
    root.project = p || {}
    root.editing = null
    root._openWith = presetName || ""
    if (!again) presetsRes.reset()       // never show another project's presets while loading
    if (root.opened) root.load()         // shown() only fires on the first open
    else root.open()
  }

  function load() { root.saveError = null; presetsRes.load(true) }

  function presetNamed(name) {
    for (var i = 0; i < root.presets.length; i++) if (root.presets[i].name === name) return root.presets[i]
    return null
  }

  // name "" starts a new preset.
  function edit(name) {
    var p = name ? root.presetNamed(name) : null
    if (name && !p) return
    p = p || {}
    root.saveError = null
    nameField.text = p.name || ""
    root.argRows = (p.args || []).slice()
    var rows = []
    var env = p.env || {}
    for (var k in env) rows.push({ key: k, value: String(env[k]) })
    root.envRows = rows
    root.uiTestMode = p.uiTestMode === true
    var lang = p.language || ""
    var known = lang === "" || Model.PRESET_LANGUAGES.some(function(l) { return l.value === lang })
    root.languageChoice = known ? lang : "other"
    otherLanguageField.text = known ? "" : lang
    regionField.text = p.region || ""
    root.nameTouched = false
    root.saveTried = false
    root.editing = { renameFrom: p.name || "" }
    if (!p.name) Qt.callLater(function() { nameField.forceActiveFocus() })
  }

  function back() {
    root.editing = null
    root.saveError = null
  }

  function save() {
    root.saveTried = true
    if (!root.canSave) return
    root.working = true
    root.saveError = null
    svc.savePreset(root.project, root.draft, root.editing.renameFrom || "", function(d) {
      root.working = false
      presetsRes.set(d)
      root.editing = null
    }, function(e) {
      root.working = false
      root.saveError = e
    })
  }

  function remove(name) {
    svc.deletePreset(root.project, name, function(d) { presetsRes.set(d) })
  }

  function use(name) {
    presetsRes.set(Object.assign({}, root.loaded, { selected: name || null }))
    svc.selectPreset(root.project, name, function(d) { presetsRes.set(d) }, function() { root.load() })
  }

  function addArg() { root.argRows = root.argRows.concat([""]) ; Qt.callLater(function() { var it = argRepeater.itemAt(root.argRows.length - 1); if (it) it.focusField() }) }
  function removeArg(i) { var rows = root.argRows.slice(); rows.splice(i, 1); root.argRows = rows }
  function addEnv() { root.envRows = root.envRows.concat([{ key: "", value: "" }]) }
  function removeEnv(i) { var rows = root.envRows.slice(); rows.splice(i, 1); root.envRows = rows }

  onShown: load()
  onClosed: { root.editing = null; root.cursor = -1 }

  LoadingState { Layout.fillWidth: true; visible: root.loading && root.loaded === null; text: "Reading run presets…" }

  ErrorState {
    Layout.fillWidth: true
    visible: root.error !== null
    app: root.app
    banner: true
    error: root.error
    showRetry: root.loaded === null
    onRetry: root.load()
  }

  // ================================================================ list
  EmptyState {
    Layout.fillWidth: true
    visible: root.editing === null && root.loaded !== null && root.presets.length === 0
    compact: true
    glyph: Model.Glyph.tune
    title: "No presets yet"
    text: "Save launch arguments, environment and language to run the app in different setups."
    actionText: "New preset"
    actionGlyph: Model.Glyph.plus
    onAction: root.edit("")
  }

  ColumnLayout {
    Layout.fillWidth: true
    visible: root.editing === null && root.presets.length > 0
    spacing: Style.spacing.sm

    RowLayout {
      Layout.fillWidth: true
      spacing: Style.spacing.md
      AppText { Layout.fillWidth: true; text: Model.plural(root.presets.length, "preset"); strong: true; elide: Text.ElideRight }
      ActionButton { text: "New preset"; glyph: Model.Glyph.plus; kind: "ghost"; compact: true; onClicked: root.edit("") }
    }

    // "Default" is always there: Run without a preset.
    ListRow {
      Layout.fillWidth: true
      glyph: Model.Glyph.play
      title: "Default (no preset)"
      subtitle: "Run opens the app with no extra arguments, in the iPhone's language."
      current: root.selected === ""
      hasCursor: root.cursor === 0
      onHovered: function(h) { if (h) root.cursor = 0 }
      onClicked: if (root.selected !== "") root.use("")
      trailing: [ UseSlot { inUse: root.selected === ""; onUse: root.use("") } ]
    }

    Repeater {
      model: root.presets
      delegate: ListRow {
        id: presetRow
        required property var modelData
        required property int index
        readonly property bool isSelected: modelData.name === root.selected
        Layout.fillWidth: true
        glyph: modelData.language ? Model.Glyph.translate : Model.Glyph.tune
        title: modelData.name
        subtitle: Model.presetSummary(modelData)
        current: isSelected
        hasCursor: root.cursor === index + 1
        onHovered: function(h) { if (h) root.cursor = index + 1 }
        onClicked: root.edit(modelData.name)
        trailing: [
          IconButton { glyph: Model.Glyph.edit; tooltip: "Edit"; anchors.verticalCenter: parent ? parent.verticalCenter : undefined; onClicked: root.edit(presetRow.modelData.name) },
          IconButton { glyph: Model.Glyph.trash; tooltip: "Delete this preset"; danger: true; anchors.verticalCenter: parent ? parent.verticalCenter : undefined; onClicked: root.remove(presetRow.modelData.name) },
          UseSlot { inUse: presetRow.isSelected; onUse: root.use(presetRow.modelData.name) }
        ]
      }
    }
  }

  // ================================================================ editor
  // ---------------------------------------------------------------- name
  ColumnLayout {
    Layout.fillWidth: true
    visible: root.editing !== null
    spacing: Style.spacing.sm
    AppText { text: "Name"; strong: true }
    TextField {
      id: nameField
      Layout.fillWidth: true
      placeholderTextColor: Theme.faint     // muted: an example, not a value
      placeholderText: "e.g. German or Debug flags"
      onTextEdited: root.nameTouched = true
      onAccepted: root.save()
    }
    AppText {
      Layout.fillWidth: true
      visible: text !== ""
      text: (root.nameTouched || root.saveTried) ? root.nameProblem : ""
      size: "small"; tone: "warn"; wrapMode: Text.Wrap
    }
  }

  // ---------------------------------------------------------------- language + region
  GridLayout {
    Layout.fillWidth: true
    visible: root.editing !== null
    columns: root.width >= Style.space(620) ? 2 : 1
    columnSpacing: Style.spacing.xl
    rowSpacing: Style.spacing.xl

    ColumnLayout {
      Layout.fillWidth: true
      Layout.preferredWidth: 1
      Layout.alignment: Qt.AlignTop
      spacing: Style.spacing.sm
      AppText { text: "Language"; strong: true }
      Dropdown {
        Layout.fillWidth: true
        showLabel: false
        value: root.languageChoice
        options: [{ value: "", label: "Same as the iPhone" }]
                 .concat(Model.PRESET_LANGUAGES.map(function(l) { return { value: l.value, label: l.label + " (" + l.value + ")" } }))
                 .concat([{ value: "other", label: "Other language code…" }])
        onChanged: function(v) {
          root.languageChoice = v
          if (v === "other") Qt.callLater(function() { otherLanguageField.forceActiveFocus() })
        }
      }
      TextField {
        id: otherLanguageField
        Layout.fillWidth: true
        visible: root.languageChoice === "other"
        placeholderTextColor: Theme.faint
        placeholderText: "e.g. hy, sv or zh-Hant"
      }
      AppText {
        Layout.fillWidth: true
        text: root.languageProblem !== "" ? root.languageProblem : "The language the app's text shows in, for this launch only."
        size: "small"; tone: root.languageProblem !== "" ? "warn" : "faint"; wrapMode: Text.Wrap
      }
    }

    ColumnLayout {
      Layout.fillWidth: true
      Layout.preferredWidth: 1
      Layout.alignment: Qt.AlignTop
      spacing: Style.spacing.sm
      AppText { text: "Region"; strong: true }
      RowLayout {
        Layout.fillWidth: true
        spacing: Style.spacing.md
        TextField {
          id: regionField
          Layout.fillWidth: true
          placeholderTextColor: Theme.faint
          placeholderText: root.suggestedRegion !== "" ? "e.g. " + root.suggestedRegion : "e.g. de_DE"
        }
        ActionButton {
          visible: root.suggestedRegion !== "" && regionField.text.trim() === ""
          text: "Use " + root.suggestedRegion
          kind: "ghost"; compact: true
          tooltip: "Formats dates, numbers and currency the way " + Model.languageLabel(root.languageCode) + " speakers in that region expect"
          onClicked: regionField.text = root.suggestedRegion
        }
      }
      AppText {
        Layout.fillWidth: true
        text: root.regionProblem !== "" ? root.regionProblem : "Formats dates, numbers and currency. Empty keeps the iPhone's region."
        size: "small"; tone: root.regionProblem !== "" ? "warn" : "faint"; wrapMode: Text.Wrap
      }
    }
  }

  // ---------------------------------------------------------------- launch arguments
  ColumnLayout {
    Layout.fillWidth: true
    visible: root.editing !== null
    spacing: Style.spacing.sm
    RowLayout {
      Layout.fillWidth: true
      AppText { text: "Launch arguments"; strong: true; Layout.fillWidth: true; elide: Text.ElideRight }
      ActionButton { text: "Add argument"; glyph: Model.Glyph.plus; kind: "ghost"; compact: true; onClicked: root.addArg() }
    }
    AppText {
      Layout.fillWidth: true
      text: root.argRows.length === 0
            ? "None. One argument per row, passed as-is: the app reads them from ProcessInfo.arguments, and -Key value pairs from UserDefaults."
            : "One argument per row, passed as-is (no shell quoting needed)."
      size: "small"; tone: "faint"; wrapMode: Text.Wrap
    }
    Repeater {
      id: argRepeater
      model: root.argRows.length
      delegate: RowLayout {
        id: argRow
        required property int index
        function focusField() { argField.forceActiveFocus() }
        Layout.fillWidth: true
        spacing: Style.spacing.md
        TextField {
          id: argField
          Layout.fillWidth: true
          placeholderTextColor: Theme.faint
          placeholderText: argRow.index === 0 ? "e.g. -ShowDebugMenu" : "argument"
          text: root.argRows[argRow.index] || ""
          onTextEdited: { root.argRows[argRow.index] = text; root.argRows = root.argRows.slice() }
        }
        IconButton { glyph: Model.Glyph.trash; tooltip: "Remove this argument"; onClicked: root.removeArg(argRow.index) }
      }
    }
  }

  // ---------------------------------------------------------------- environment
  ColumnLayout {
    Layout.fillWidth: true
    visible: root.editing !== null
    spacing: Style.spacing.sm
    RowLayout {
      Layout.fillWidth: true
      AppText { text: "Environment variables"; strong: true; Layout.fillWidth: true; elide: Text.ElideRight }
      ActionButton { text: "Add variable"; glyph: Model.Glyph.plus; kind: "ghost"; compact: true; onClicked: root.addEnv() }
    }
    AppText {
      Layout.fillWidth: true
      visible: root.envRows.length === 0
      text: "None. The app reads them with ProcessInfo.processInfo.environment."
      size: "small"; tone: "faint"; wrapMode: Text.Wrap
    }
    Repeater {
      model: root.envRows.length
      delegate: RowLayout {
        id: envRow
        required property int index
        readonly property var row: root.envRows[index] || { key: "", value: "" }
        Layout.fillWidth: true
        spacing: Style.spacing.md
        TextField {
          Layout.preferredWidth: Style.space(200)
          Layout.minimumWidth: Style.space(100)
          placeholderTextColor: Theme.faint
          placeholderText: "NAME"
          text: envRow.row.key
          onTextEdited: { root.envRows[envRow.index].key = text; root.envRows = root.envRows.slice() }
        }
        AppText { text: "="; tone: "faint" }
        TextField {
          Layout.fillWidth: true
          placeholderTextColor: Theme.faint
          placeholderText: "value"
          text: envRow.row.value
          onTextEdited: { root.envRows[envRow.index].value = text; root.envRows = root.envRows.slice() }
        }
        IconButton { glyph: Model.Glyph.trash; tooltip: "Remove this variable"; onClicked: root.removeEnv(envRow.index) }
      }
    }
    AppText {
      Layout.fillWidth: true
      visible: root.envProblem !== ""
      text: root.envProblem
      size: "small"; tone: "warn"; wrapMode: Text.Wrap
    }
  }

  // ---------------------------------------------------------------- UI test mode
  ColumnLayout {
    Layout.fillWidth: true
    visible: root.editing !== null
    spacing: 0
    SettingRow {
      title: "UI test mode"
      description: "Adds -UITestMode YES. iOS does nothing with it; your app can read it (UserDefaults.standard.bool(forKey: \"UITestMode\")) to skip onboarding or use test data."
      ToggleSwitch {
        checked: root.uiTestMode
        onToggled: root.uiTestMode = !checked
      }
    }
  }

  // ---------------------------------------------------------------- preview
  BorderSurface {
    Layout.fillWidth: true
    visible: root.editing !== null
    radius: Style.cornerRadius
    color: Theme.surface
    borderSpec: Border.flat(Theme.hairline, Style.normalBorderWidth)
    padding: Style.spacing.xl
    implicitHeight: previewCol.implicitHeight + contentTopInset + contentBottomInset

    ColumnLayout {
      id: previewCol
      anchors.fill: parent
      anchors.topMargin: parent.contentTopInset; anchors.bottomMargin: parent.contentBottomInset
      anchors.leftMargin: parent.contentLeftInset; anchors.rightMargin: parent.contentRightInset
      spacing: Style.spacing.md

      AppText { Layout.fillWidth: true; text: "What Run passes to the app"; strong: true; elide: Text.ElideRight }
      AppText {
        Layout.fillWidth: true
        visible: root.previewArgs.length === 0 && root.previewEnv.length === 0
        text: "Nothing extra yet: a plain launch, like Default."
        tone: "dim"; size: "small"; wrapMode: Text.Wrap
      }

      GridLayout {
        Layout.fillWidth: true
        visible: root.previewArgs.length > 0 || root.previewEnv.length > 0
        columns: 2
        columnSpacing: Style.spacing.xl
        rowSpacing: Style.spacing.md

        AppText {
          Layout.alignment: Qt.AlignTop
          Layout.topMargin: Style.spacing.xxs      // the chips' vertical padding: same first line
          visible: root.previewArgs.length > 0
          text: "Arguments"; tone: "dim"; size: "small"
        }
        Flow {
          Layout.fillWidth: true
          visible: root.previewArgs.length > 0
          spacing: Style.spacing.sm
          Repeater {
            model: root.previewArgs
            delegate: PresetChip { required property var modelData; text: modelData.text; generated: modelData.from !== ""; tip: modelData.from !== "" ? "From " + modelData.from : "" }
          }
        }

        AppText {
          Layout.alignment: Qt.AlignTop
          Layout.topMargin: Style.spacing.xxs      // the chips' vertical padding: same first line
          visible: root.previewEnv.length > 0
          text: "Environment"; tone: "dim"; size: "small"
        }
        Flow {
          Layout.fillWidth: true
          visible: root.previewEnv.length > 0
          spacing: Style.spacing.sm
          Repeater {
            model: root.previewEnv
            delegate: PresetChip { required property var modelData; text: modelData }
          }
        }
      }
    }
  }

  // "Used by Run" or a "Use for Run" button, in a slot as wide as the wider of the two, so the
  // row actions before it stay in one column from row to row.
  component UseSlot: Item {
    id: slot
    property bool inUse: false
    signal use()
    anchors.verticalCenter: parent ? parent.verticalCenter : undefined
    // The pill's text ends where the button's label does (a compact button's padding plus its border).
    readonly property real labelInset: Style.spacing.lg + Style.normalBorderWidth
    implicitWidth: Math.max(usePill.implicitWidth + labelInset, useButton.implicitWidth)
    implicitHeight: Math.max(usePill.implicitHeight, useButton.implicitHeight)
    StatusPill { id: usePill; anchors.right: parent.right; anchors.rightMargin: slot.labelInset; anchors.verticalCenter: parent.verticalCenter; opacity: slot.inUse ? 1 : 0; level: "ok"; text: "Used by Run"; quiet: true }
    ActionButton { id: useButton; anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter; visible: !slot.inUse; text: "Use for Run"; kind: "ghost"; compact: true; onClicked: slot.use() }
  }

  // One argument or variable in the preview. Generated ones (language, region, UI test mode) are tinted.
  component PresetChip: Rectangle {
    id: chip
    property alias text: chipText.text
    property bool generated: false
    property string tip: ""
    implicitWidth: Math.min(chipText.implicitWidth + Style.spacing.md * 2, parent ? parent.width : chipText.implicitWidth + Style.spacing.md * 2)
    implicitHeight: chipText.implicitHeight + Style.spacing.xxs * 2
    radius: Style.cornerRadius
    color: generated ? Util.alpha(Commons.Color.accent, 0.14) : Util.alpha(Commons.Color.foreground, 0.06)
    border.width: Style.normalBorderWidth
    border.color: generated ? Util.alpha(Commons.Color.accent, 0.35) : Theme.hairline
    AppText {
      id: chipText
      anchors.fill: parent
      anchors.leftMargin: Style.spacing.md
      anchors.rightMargin: Style.spacing.md
      verticalAlignment: Text.AlignVCenter
      size: "small"; mono: true
      tone: chip.generated ? "accent" : "primary"
      elide: Text.ElideMiddle
    }
    HoverHandler { id: chipHover }
    PanelToolTip { visible: chipHover.hovered && chip.tip !== ""; text: chip.tip }
  }

  footer: [
    ActionButton { visible: root.editing === null; text: "Close"; kind: "ghost"; onClicked: root.close() },
    ActionButton { visible: root.editing !== null; text: "Back"; glyph: Model.Glyph.chevronLeft; kind: "ghost"; enabled: !root.working; onClicked: root.back() },
    ActionButton {
      visible: root.editing !== null
      text: root.editing && root.editing.renameFrom ? "Save changes" : "Save preset"
      glyph: Model.Glyph.save
      kind: "primary"
      disabled: !root.canSave && !root.working
      busy: root.working
      tooltip: root.problem !== "" ? root.problem : ""
      onClicked: root.save()
    }
  ]
}
