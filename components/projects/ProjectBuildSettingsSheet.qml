import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../common"
import "../../lib/Model.js" as Model

// "Build settings" sheet for one project (projects slice): the prebuild command, extra environment
// variables, the open-file limit and whether a generated xtool.yml may be built. Stored per project by
// the helper (`projects build-settings`, see helper/build_settings.py).
//   ProjectBuildSettingsSheet { id: bsSheet; app: page.app; svc: page.svc }
//   bsSheet.openFor(project)
Sheet {
  id: root

  property var app: null
  property var svc: null
  property var project: ({})
  property bool loading: false
  property bool working: false
  property var error: null
  property var loaded: null              // the helper's answer: { settings, suggested, openFilesError }
  property var envRows: []               // [{ key, value }], edited in place
  property bool allowGenerated: false

  readonly property var suggested: loaded && loaded.suggested ? loaded.suggested : null
  readonly property var badKeys: envRows.filter(function(r) { return r.key.trim() !== "" && !/^[A-Za-z_][A-Za-z0-9_]*$/.test(r.key.trim()) })
  readonly property string openFilesText: filesField.text.trim()
  readonly property bool openFilesValid: openFilesText === "" || (/^\d+$/.test(openFilesText) && Number(openFilesText) >= 256 && Number(openFilesText) <= 16777216)
  readonly property bool canSave: !loading && !working && loaded !== null && badKeys.length === 0 && openFilesValid

  title: "Build settings"
  subtitle: project && project.name ? "How iOS Studio builds " + project.name + "." : ""
  glyph: Model.Glyph.wrench
  preferredWidth: Style.space(640)
  busy: working || loading

  function openFor(p) {
    root.project = p || {}
    root.open()
  }

  function fill(s) {
    s = s || {}
    prebuildField.text = s.prebuild || ""
    var rows = []
    var env = s.env || {}
    for (var k in env) rows.push({ key: k, value: String(env[k]) })
    root.envRows = rows
    filesField.text = s.openFiles ? String(s.openFiles) : ""
    root.allowGenerated = s.allowGenerated === true
  }

  function load() {
    root.loading = true
    root.error = null
    root.loaded = null
    svc.loadBuildSettings(root.project, function(d) {
      root.loading = false
      root.loaded = d
      root.fill(d.settings)
    }, function(e) {
      root.loading = false
      root.error = e
    })
  }

  // Suggestions only fill the form; nothing is saved until Save.
  function useSuggested() {
    if (!suggested) return
    var s = suggested.settings || {}
    if (s.prebuild) prebuildField.text = s.prebuild
    if (s.env) {
      var rows = root.envRows.slice()
      for (var k in s.env) {
        var found = false
        for (var i = 0; i < rows.length; i++) if (rows[i].key.trim() === k) { rows[i] = { key: k, value: String(s.env[k]) }; found = true }
        if (!found) rows.push({ key: k, value: String(s.env[k]) })
      }
      root.envRows = rows
    }
    if (s.openFiles) filesField.text = String(s.openFiles)
    if (s.allowGenerated) root.allowGenerated = true
  }

  function addRow() { root.envRows = root.envRows.concat([{ key: "", value: "" }]) }
  function removeRow(i) { var rows = root.envRows.slice(); rows.splice(i, 1); root.envRows = rows }

  function save() {
    if (!root.canSave) return
    var env = {}
    for (var i = 0; i < root.envRows.length; i++) {
      var k = root.envRows[i].key.trim()
      if (k !== "") env[k] = root.envRows[i].value
    }
    root.working = true
    root.error = null
    svc.saveBuildSettings(root.project, {
      prebuild: prebuildField.text.trim(),
      env: env,
      openFiles: root.openFilesText === "" ? 0 : Number(root.openFilesText),
      allowGenerated: root.allowGenerated
    }, function() {
      root.working = false
      root.close()
    }, function(e) {
      root.working = false
      root.error = e
    })
  }

  onShown: load()

  LoadingState { Layout.fillWidth: true; visible: root.loading && root.loaded === null; text: "Reading build settings…" }

  ErrorState {
    Layout.fillWidth: true
    visible: root.error !== null
    app: root.app
    banner: true
    error: root.error
    showRetry: root.loaded === null
    onRetry: root.load()
  }

  // ---------------------------------------------------------------- suggestions from the project's scripts
  BorderSurface {
    Layout.fillWidth: true
    visible: root.suggested !== null && !root.loading
    radius: Style.cornerRadius
    color: Theme.levelFill("info")
    borderSpec: Border.flat(Util.alpha(Commons.Color.accent, 0.35), Style.normalBorderWidth)
    padding: Style.spacing.xl
    implicitHeight: sugRow.implicitHeight + contentTopInset + contentBottomInset
    RowLayout {
      id: sugRow
      anchors.fill: parent
      anchors.topMargin: parent.contentTopInset; anchors.bottomMargin: parent.contentBottomInset
      anchors.leftMargin: parent.contentLeftInset; anchors.rightMargin: parent.contentRightInset
      spacing: Style.spacing.xl
      Icon { glyph: Model.Glyph.sparkles; color: Commons.Color.accent; size: Style.font.iconLarge; Layout.alignment: Qt.AlignTop }
      ColumnLayout {
        Layout.fillWidth: true
        spacing: Style.spacing.xxs
        AppText { Layout.fillWidth: true; strong: true; wrapMode: Text.WordWrap; text: "This project's own scripts set things up for its build" }
        AppText {
          Layout.fillWidth: true; tone: "dim"; size: "small"; wrapMode: Text.WordWrap
          text: root.suggested ? (root.suggested.reasons || []).join(" · ") : ""
        }
      }
      ActionButton { text: "Use these"; compact: true; Layout.alignment: Qt.AlignVCenter; onClicked: root.useSuggested() }
    }
  }

  // ---------------------------------------------------------------- prebuild
  ColumnLayout {
    Layout.fillWidth: true
    visible: root.loaded !== null
    spacing: Style.spacing.sm
    AppText { text: "Prebuild command"; strong: true }
    TextField {
      id: prebuildField
      Layout.fillWidth: true
      placeholderText: "./generate.sh"
      onAccepted: root.save()
    }
    AppText {
      Layout.fillWidth: true
      text: "Runs in the project folder before every build, re-signing included, as its first step; its output goes to the build log. It runs without a shell: quotes work, but not |, && or $VARIABLES. Leave it empty for none."
      size: "small"; tone: "faint"; wrapMode: Text.WordWrap
    }
  }

  // ---------------------------------------------------------------- environment
  ColumnLayout {
    Layout.fillWidth: true
    visible: root.loaded !== null
    spacing: Style.spacing.sm
    RowLayout {
      Layout.fillWidth: true
      AppText { text: "Environment variables"; strong: true; Layout.fillWidth: true }
      ActionButton { text: "Add variable"; glyph: Model.Glyph.plus; kind: "ghost"; compact: true; onClicked: root.addRow() }
    }
    AppText {
      Layout.fillWidth: true
      visible: root.envRows.length === 0
      text: "None. The prebuild command and xtool get your normal environment."
      size: "small"; tone: "faint"; wrapMode: Text.WordWrap
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
          placeholderText: "NAME"
          text: envRow.row.key
          onTextEdited: { root.envRows[envRow.index].key = text; root.envRows = root.envRows.slice() }
        }
        AppText { text: "="; tone: "faint" }
        TextField {
          Layout.fillWidth: true
          placeholderText: "value"
          text: envRow.row.value
          onTextEdited: { root.envRows[envRow.index].value = text; root.envRows = root.envRows.slice() }
        }
        IconButton { glyph: Model.Glyph.trash; tooltip: "Remove this variable"; onClicked: root.removeRow(envRow.index) }
      }
    }
    AppText {
      Layout.fillWidth: true
      visible: root.badKeys.length > 0
      text: "Names use letters, digits and _, and don't start with a digit: " + root.badKeys.map(function(r) { return r.key }).join(", ")
      size: "small"; tone: "warn"; wrapMode: Text.WordWrap
    }
  }

  // ---------------------------------------------------------------- open files + generated
  ColumnLayout {
    Layout.fillWidth: true
    visible: root.loaded !== null
    spacing: 0

    SettingRow {
      title: "Open-file limit"
      description: "Raised for the build only. Empty uses 65536 (or your system's maximum, if lower)."
      note: !root.openFilesValid ? "Use a number from 256 to 16777216." : (root.loaded && root.loaded.openFilesError ? root.loaded.openFilesError : "")
      noteLevel: "warn"
      TextField {
        id: filesField
        width: Style.space(140)
        placeholderText: "65536"
        validator: IntValidator { bottom: 0; top: 16777216 }
      }
    }

    SettingRow {
      title: "Build generated project"
      description: root.project.generated
                   ? "This project's xtool.yml is generated by its own scripts. Allow building it here; set the script that regenerates it as the prebuild command, so every build uses a fresh one."
                   : "Only matters for a project whose xtool.yml is generated by its own scripts (this one's isn't)."
      note: root.allowGenerated && root.project.generated && prebuildField.text.trim() === "" ? "Without a prebuild command, builds use xtool.yml as it is now." : ""
      noteLevel: "warn"
      ToggleSwitch {
        checked: root.allowGenerated
        onToggled: root.allowGenerated = !checked
      }
    }
  }

  footer: [
    ActionButton { text: "Cancel"; kind: "ghost"; enabled: !root.working; onClicked: root.close() },
    ActionButton { text: "Save"; glyph: Model.Glyph.save; kind: "primary"; enabled: root.canSave; busy: root.working; onClicked: root.save() }
  ]
}
