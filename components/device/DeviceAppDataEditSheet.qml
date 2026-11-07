import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../common"
import "../../lib/Model.js" as Model

// App data › Defaults: add or edit one UserDefaults key (value + type), or delete one (after its confirm).
// Checks the value as you type, then sends appdata.defaults.set (or .delete). A change takes 10 to 20 s: the
// daemon waits for iOS to save the app's settings after a stop, writes the file, then installs the app's
// last build over it so iOS drops its cached copy (installProgress events). The sheet stays open and busy,
// with the step and the install's progress, until the reply; then the pane says how it landed.
//   DeviceAppDataEditSheet { id: editSheet; view: root.view; onSaved: root.load() }
//   editSheet.begin("", undefined, "string")        // new key
//   editSheet.begin("launchCount", 42, "int")       // edit: key, its full JSON value, its type (appdata.defaults `types`)
Sheet {
  id: root
  required property var view
  signal saved(string key, var reply)        // the set/delete reply: method, verified, warning, launchArgs, …

  property string key: ""                    // "" = new key
  property var original: undefined
  property string kind: "string"
  property bool boolValue: false
  property bool saving: false
  property var saveError: null
  property bool deleting: false              // the sheet only shows a delete's progress
  property bool refresh: true                // install the build after the write (the daemon's default)
  property string phase: ""                  // wait | write | install, while saving
  property real installPercent: -1
  property real startedAt: 0
  property int elapsed: 0                    // seconds since the change started, while saving
  Timer { interval: 1000; repeat: true; running: root.saving; onTriggered: root.elapsed = Math.round((Date.now() - root.startedAt) / 1000) }

  readonly property var kinds: [
    { value: "string", label: "String" }, { value: "int", label: "Integer" }, { value: "float", label: "Decimal number" },
    { value: "bool", label: "Boolean" }, { value: "date", label: "Date" }, { value: "dict", label: "Dictionary (JSON)" },
    { value: "array", label: "Array (JSON)" }
  ]
  readonly property bool isNew: key === ""
  readonly property string keyText: isNew ? keyField.text.trim() : key
  readonly property bool multiline: kind === "string" || kind === "dict" || kind === "array"
  readonly property string valueText: multiline ? area.text : field.text
  readonly property var parsed: _parse(kind, valueText, boolValue)

  title: deleting ? "Deleting “" + key + "”" : isNew ? "New key" : "Edit “" + key + "”"
  subtitle: view.appName + " sees the change on its next launch."
  glyph: deleting ? Model.Glyph.trash : isNew ? Model.Glyph.plus : Model.Glyph.edit
  preferredWidth: Style.space(560)
  // no silent close mid-change (Esc, the scrim and × wait); the step's own bar shows the progress
  dismissible: !saving

  function begin(k, v, type) {
    root.deleting = false
    root.phase = ""
    root.key = k
    root.original = v
    root.saveError = null
    root.saving = false
    keyField.text = ""
    var kd = type && root.kinds.some(function(o) { return o.value === type }) ? type : root._kindOf(v)
    root.kind = kd
    root.boolValue = kd === "bool" ? !!v : false
    var t = ""
    if (k !== "") {
      if (kd === "dict" || kd === "array") t = JSON.stringify(v, null, 2)
      else if (kd === "date") t = Qt.formatDateTime(new Date(v.iso), "yyyy-MM-dd HH:mm:ss")
      else if (kd !== "bool" && v !== null && v !== undefined) t = typeof v === "object" ? String(v.value) : String(v)
    }
    area.text = t
    field.text = t
    root.open()
  }

  function _kindOf(v) {
    if (Array.isArray(v)) return "array"
    if (v !== null && typeof v === "object") return v["$type"] === "date" ? "date" : "dict"
    if (typeof v === "boolean") return "bool"
    if (typeof v === "number") return Number.isInteger(v) ? "int" : "float"
    return "string"
  }

  // {ok, value, error}
  function _parse(kd, text, b) {
    var t = String(text)
    switch (kd) {
    case "string": return { ok: true, value: t }
    case "bool": return { ok: true, value: b }
    case "int":
      if (!/^\s*-?\d+\s*$/.test(t)) return { ok: false, error: "Enter a whole number, like 42 or -3." }
      return { ok: true, value: parseInt(t, 10) }
    case "float": {
      let n = Number(t.trim())
      if (t.trim() === "" || !isFinite(n)) return { ok: false, error: "Enter a number, like 0.5 or 12." }
      return { ok: true, value: n }
    }
    case "date": {
      let d = new Date(t.trim().replace(" ", "T"))
      if (t.trim() === "" || isNaN(d.getTime())) return { ok: false, error: "Enter a date like 2026-11-07 14:30 or an ISO 8601 time." }
      return { ok: true, value: d.toISOString() }
    }
    default: {
      let v
      try { v = JSON.parse(t) } catch (e) { return { ok: false, error: "That isn't valid JSON: " + String(e.message || e).replace(/^JSON\.parse: /, "") } }
      if (kd === "array" && !Array.isArray(v)) return { ok: false, error: "An array starts with [ and ends with ]." }
      if (kd === "dict" && (v === null || typeof v !== "object" || Array.isArray(v))) return { ok: false, error: "A dictionary starts with { and ends with }." }
      return { ok: true, value: v }
    }
    }
  }

  property var existing: []                  // keys already saved, to warn before replacing one
  readonly property bool replaces: isNew && existing.indexOf(keyText) >= 0
  readonly property bool canSave: !saving && keyText !== "" && parsed.ok && !view.running

  // The daemon reports no steps before the install, so the first ones follow the clock: it waits until 10 s
  // after the app stopped (iOS saves its settings 2 to 7 s after a stop), then writes.
  function _startSteps() {
    var left = root.view.waitLeft()
    root.installPercent = -1
    root.startedAt = Date.now()
    root.elapsed = 0
    root.phase = left > 0 ? "wait" : "write"
    if (left > 0) { stepTimer.interval = left; stepTimer.restart() }
  }
  Timer { id: stepTimer; onTriggered: if (root.phase === "wait") root.phase = "write" }
  Connections {
    target: root.view.svc
    function onDaemonEvent(name, data) {
      if (name !== "installProgress" || !root.saving || !data) return
      root.phase = "install"
      // over Wi-Fi the install reports no numbers (or only 0): keep the bar moving until real ones arrive
      var pct = Number(data.percent)
      if (isFinite(pct) && pct > 0) root.installPercent = pct
    }
  }
  function _done(k, ok, d) {
    root.saving = false
    root.phase = ""
    stepTimer.stop()
    if (!ok) { root.saveError = d; return }
    root.close()
    root.saved(k, d)
  }

  function save() {
    if (!root.canSave) return
    var k = root.keyText
    root.saving = true
    root.saveError = null
    root._startSteps()
    var args = { bundleId: root.view.bundleId, key: k, value: root.parsed.value, type: root.kind === "dict" || root.kind === "array" ? "json" : root.kind }
    if (!root.refresh) args.refresh = false
    root.view.svc.request("appdata.defaults.set", args, function(ok, d) { root._done(k, ok, d) }, 300000)
  }

  // after the delete's confirm: the sheet shows only its progress
  function remove(k) {
    root.key = k
    root.deleting = true
    root.saveError = null
    root.saving = true
    root.open()
    root._startSteps()
    var args = { bundleId: root.view.bundleId, key: k }
    if (!root.refresh) args.refresh = false
    root.view.svc.request("appdata.defaults.delete", args, function(ok, d) { root._done(k, ok, d) }, 300000)
  }

  // ---- form (kept while saving, but not editable; a delete shows none)
  ColumnLayout {
    Layout.fillWidth: true
    visible: !root.deleting
    enabled: !root.saving
    spacing: Style.spacing.xl
    AppText { text: "Key"; size: "small"; tone: "dim"; visible: root.isNew; Layout.bottomMargin: Style.spacing.labelGap - Style.spacing.xl }
    TextField {
      id: keyField
      Layout.fillWidth: true
      visible: root.isNew
      placeholderText: "e.g. onboardingDone"
      onAccepted: if (!root.multiline) field.forceActiveFocus()
    }
    AppText {
      Layout.fillWidth: true
      visible: root.replaces
      text: "“" + root.keyText + "” already exists. Saving replaces its value."
      size: "small"; tone: "warn"; wrapMode: Text.Wrap
    }

    AppText { text: "Type"; size: "small"; tone: "dim"; Layout.bottomMargin: Style.spacing.labelGap - Style.spacing.xl }
    Dropdown {
      showLabel: false
      Layout.preferredWidth: Style.space(240)
      value: root.kind
      options: root.kinds
      onChanged: function(v) {
        // carry the text over where it still makes sense
        if ((v === "dict" || v === "array") !== (root.kind === "dict" || root.kind === "array")) { area.text = ""; field.text = "" }
        else if (root.multiline !== (v === "string" || v === "dict" || v === "array")) { if (root.multiline) field.text = area.text; else area.text = field.text }
        root.kind = v
      }
    }

    AppText { text: "Value"; size: "small"; tone: "dim"; Layout.bottomMargin: Style.spacing.labelGap - Style.spacing.xl }
    RowLayout {
      Layout.fillWidth: true
      visible: root.kind === "bool"
      spacing: Style.spacing.md
      ToggleSwitch {
        Layout.leftMargin: cursorRing ? -cursorPad : 0
        checked: root.boolValue
        onToggled: root.boolValue = !checked
      }
      AppText { text: root.boolValue ? "true" : "false"; tone: "dim" }
    }
    RowLayout {
      Layout.fillWidth: true
      visible: root.kind === "int" || root.kind === "float" || root.kind === "date"
      spacing: Style.spacing.lg
      TextField {
        id: field
        Layout.fillWidth: true
        placeholderText: root.kind === "date" ? "2026-11-07 14:30" : root.kind === "int" ? "42" : "0.5"
        onAccepted: root.save()
      }
      ActionButton {
        visible: root.kind === "date"
        text: "Now"; kind: "ghost"; compact: true
        onClicked: field.text = Qt.formatDateTime(new Date(), "yyyy-MM-dd HH:mm:ss")
      }
    }
    Rectangle {
      Layout.fillWidth: true
      visible: root.multiline
      implicitHeight: Math.min(Math.max(area.implicitHeight, areaMetrics.height * 4 + Style.spacing.lg * 2), Style.space(260))
      radius: Style.cornerRadius
      color: Style.controlFill(area.activeFocus, areaHover.hovered, Commons.Color.foreground, Commons.Color.accent)
      border.width: Style.normalBorderWidth
      border.color: area.activeFocus ? Commons.Color.accent : Theme.hairline
      FontMetrics { id: areaMetrics; font: area.font }
      HoverHandler { id: areaHover; cursorShape: Qt.IBeamCursor }
      ScrollView {
        id: areaScroll
        anchors.fill: parent
        anchors.margins: Style.normalBorderWidth
        // shown whenever the text is taller than the box, so a long value says there is more
        ScrollBar.vertical.policy: area.implicitHeight > areaScroll.height ? ScrollBar.AlwaysOn : ScrollBar.AlwaysOff
        TextArea {
          id: area
          padding: Style.spacing.lg
          leftPadding: Style.spacing.controlPaddingX
          wrapMode: root.kind === "string" ? TextEdit.Wrap : TextEdit.WrapAnywhere
          rightPadding: Style.spacing.controlPaddingX + (areaScroll.ScrollBar.vertical.visible ? Style.spacing.lg : 0)
          font.family: Style.font.family
          font.pixelSize: Style.font.body
          color: Commons.Color.foreground
          selectionColor: Style.selectionFill
          selectedTextColor: Commons.Color.foreground
          placeholderText: root.kind === "string" ? "Text" : root.kind === "array" ? "[1, 2, 3]" : "{\"key\": \"value\"}"
          placeholderTextColor: Theme.faint
          background: null
        }
      }
    }
    AppText {
      Layout.fillWidth: true
      visible: !root.parsed.ok && root.valueText.trim() !== ""
      text: root.parsed.error || ""
      size: "small"; tone: "error"; wrapMode: Text.Wrap
    }
    AppText {
      Layout.fillWidth: true
      visible: root.kind === "date" && root.parsed.ok
      text: root.parsed.ok && root.kind === "date" ? "Your local time. Saved as " + root.parsed.value + " (UTC)." : ""
      size: "small"; tone: "faint"; wrapMode: Text.Wrap
    }
  }

  // ---- while saving: the step, then the install's progress; edges on the sheet's content edges
  ProgressTrack {
    Layout.fillWidth: true
    visible: root.saving
    indeterminate: root.phase !== "install" || root.installPercent <= 0
    value: Math.max(0, root.installPercent) / 100
    label: root.phase === "wait" ? "Waiting for iOS to save " + root.view.appName + "'s settings…"
         : root.phase === "write" ? (root.deleting ? "Deleting the key…" : "Writing…")
         : "Reinstalling " + root.view.appName + "…"
    // a percent once the install reports one, else the seconds so far
    trailing: root.phase === "install" && root.installPercent > 0 ? Math.round(root.installPercent) + "%" : Model.formatDuration(root.elapsed)
  }
  AppText {
    Layout.fillWidth: true
    // two sentences on their own lines, so neither wraps mid-phrase in the sheet's width
    text: (root.deleting ? "Deleting" : "Saving") + " reinstalls the same build, so iOS drops its cached settings.\nThe app's data stays."
    size: "small"; tone: "faint"; wrapMode: Text.Wrap
  }

  DeviceAppDataRunning {
    Layout.fillWidth: true
    visible: root.view.running && !root.deleting
    view: root.view
    text: "Stop it before you save, or it writes its own copy back over your change."
  }
  ErrorState {
    Layout.fillWidth: true
    visible: !!root.saveError
    banner: true
    app: root.view.app
    error: root.saveError
    showRetry: false
  }

  footer: [
    // a change can't be stopped halfway (the file is written, the install runs): Close waits for it
    ActionButton {
      text: root.deleting && !root.saving ? "Close" : "Cancel"; kind: "ghost"
      // fully disabled (no hover wash) so it reads as off; the bar above says what it waits for
      enabled: !root.saving
      onClicked: root.close()
    },
    ActionButton {
      visible: !root.deleting
      text: root.isNew ? "Add key" : "Save"; glyph: Model.Glyph.check; kind: "primary"
      busy: root.saving
      disabled: !root.canSave
      tooltip: root.saving ? "" : root.view.running ? "Stop " + root.view.appName + " first" : root.keyText === "" ? "Enter a key name" : !root.parsed.ok ? "Fix the value first" : ""
      onClicked: root.save()
    }
  ]
  onShown: { if (root.isNew) keyField.forceActiveFocus(); else if (root.multiline) area.forceActiveFocus(); else if (root.kind !== "bool") field.forceActiveFocus() }
}
