import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../common"
import "../../lib/Model.js" as Model

// App data › Defaults: the app's UserDefaults (Library/Preferences/<bundle id>.plist) as a key list.
// Dictionaries and arrays expand to pretty JSON; data and dates show as size / local time. Edit and
// delete only while the app is stopped (it would write its own copy back over yours); the app reads
// a change on its next launch. Daemon: appdata.defaults, appdata.defaults.set, appdata.defaults.delete.
ColumnLayout {
  id: root
  required property var view                 // DeviceAppDataView
  readonly property var app: view.app
  readonly property var svc: view.svc
  property bool active: false

  readonly property string bundleId: view.bundleId
  property string status: "unknown"           // unknown | loading | ready | error
  property var reply: null                    // appdata.defaults reply (not `data`: that is the default property)
  property var error: null
  property var expanded: ({})                 // key -> true
  property string loadedFor: ""
  property bool waiting: false                // this read is waiting for iOS to save the settings after a stop
  property var reverted: []                   // [{key, expected, actual}]: keys iOS put back after a change
  property var result: null                   // a change that may not reach the app: {key, title, text, launchArgs}

  readonly property var keys: reply && reply.values ? Object.keys(reply.values).sort(function(a, b) { return a.toLowerCase() < b.toLowerCase() ? -1 : a.toLowerCase() > b.toLowerCase() ? 1 : 0 }) : []
  readonly property var shown: keys.filter(function(k) { return Model.matches(k, filter.text) || Model.matches(root.preview(k), filter.text) })
  readonly property bool empty: status === "ready" && !!reply && (!reply.exists || keys.length === 0)
  // the chevron slot is kept only when a shown row can open, so plain keys start on the row's padding
  readonly property bool anyExpandable: shown.some(function(k) { return root.expandable(k) })
  readonly property real chevronSlot: anyExpandable ? Style.font.icon + Style.spacing.xl : 0
  readonly property bool locked: view.running
  readonly property string lockReason: view.appName + " is running. Stop it first, or it writes its own copy back over your change."
  readonly property bool narrow: width > 0 && width < Style.space(560)
  readonly property real keyWidth: {
    var w = 0
    for (var i = 0; i < keys.length; i++) w = Math.max(w, keyMetrics.advanceWidth(keys[i]))
    return Math.ceil(Math.min(Math.max(w, Style.space(120)), root.width * 0.38))
  }

  spacing: Style.spacing.lg

  onActiveChanged: if (active && loadedFor !== bundleId) load()
  onBundleIdChanged: { result = null; reverted = []; status = "unknown"; reply = null; expanded = ({}); loadedFor = ""; if (active) load() }

  FontMetrics { id: keyMetrics; font.family: Style.font.family; font.pixelSize: Style.font.body }

  function load() {
    if (!root.bundleId) return
    var bid = root.bundleId
    root.status = "loading"
    root.error = null
    root.waiting = root.view.waitLeft() > 0
    root.svc.request("appdata.defaults", { bundleId: bid }, function(ok, d) {
      if (bid !== root.bundleId) return
      root.loadedFor = bid
      root.waiting = false
      if (ok) { root.reply = root.view.norm(d); root.reverted = root.reply.reverted || []; root.status = "ready" }
      else if (d && d.code === "no-defaults") { root.reply = { exists: false, values: {}, keys: 0 }; root.status = "ready" }
      else { root.error = d; root.status = "error" }
    }, 120000)
  }

  // How a set or delete landed (the reply's method, verified, warning, refreshError, launchArgs).
  function changed(k, d, deleted) {
    var name = root.view.appName
    root.result = null
    if (d.method === "none") root.app.toast("“" + k + "” was already gone", "info")
    else if (d.method === "reinstall" && d.verified !== false) {
      // two short lines rather than one that wraps mid-phrase
      root.app.toast((deleted ? "Deleted “" : "Saved “") + k + "”.\n" + (d.stopped ? "Open " + name + " again to see it" : name + " sees it on next launch"), "ok")
    } else if (d.method === "reinstall") {
      root.result = { key: k, title: "iOS put the old value of “" + k + "” back",
                      text: "The change was written " + Model.plural(d.attempts || 1, "time") + " and overwritten. Try again in a few seconds." }
    } else {
      root.result = { key: k, title: (deleted ? "“" + k + "” is deleted in the file" : "“" + k + "” is saved in the file") + ", but " + name + " may still see the old value",
                      text: d.refreshError ? d.refreshError.message : (d.warning || ""),
                      launchArgs: d.launchArgs || null }
    }
    root.load()
  }
  function copyLaunchArgs(args) {
    var text = args.map(function(a) { return /[\s"']/.test(a) ? "'" + String(a).replace(/'/g, "'\\''") + "'" : a }).join(" ")
    Util.execArgv(["wl-copy", "--", text])
    root.app.toast("Copied the launch arguments", "ok")
  }

  // ---- values (as helper/app_data.py's view() sends them: plain JSON, or {"$type": …} where JSON lacks the
  // type or the value was clipped for display)
  function tag(v) { return v !== null && typeof v === "object" && !Array.isArray(v) ? v["$type"] || "" : "" }
  function kindOf(k) {
    var t = root.reply && root.reply.types ? root.reply.types[k] : ""
    if (t) return t
    var v = root.reply ? root.reply.values[k] : null
    if (v === null || v === undefined) return "null"
    if (Array.isArray(v)) return "array"
    if (typeof v === "object") return tag(v) || "dict"
    if (typeof v === "boolean") return "bool"
    if (typeof v === "number") return Number.isInteger(v) ? "int" : "float"
    return "string"
  }
  function clipped(v) { return tag(v) !== "" && !!v.truncated }
  function formatDate(iso) {
    var d = new Date(iso)
    if (isNaN(d.getTime())) return String(iso)
    return Qt.formatDateTime(d, "yyyy-MM-dd HH:mm:ss") + "  ·  " + Model.relativeTime(iso)
  }
  // the first bytes of base64 text as hex ("89 50 4e 47 …")
  function hexPrefix(b64) {
    var abc = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    var t = String(b64 || "").replace(/[^A-Za-z0-9+\/]/g, "").substring(0, 12)
    var bits = 0, n = 0, out = []
    for (var i = 0; i < t.length && out.length < 8; i++) {
      bits = (bits << 6) | abc.indexOf(t[i]); n += 6
      if (n >= 8) { n -= 8; out.push(("0" + ((bits >> n) & 0xff).toString(16)).slice(-2)) }
    }
    return out.join(" ")
  }
  // a display copy without the tags: what the pretty JSON shows
  function plain(v) {
    if (Array.isArray(v)) return v.map(root.plain)
    if (v === null || typeof v !== "object") return v
    switch (tag(v)) {
    case "": { var o = {}; for (var k in v) o[k] = root.plain(v[k]); return o }
    case "dict": return v.truncated ? root.plain(v.preview || {}) : root.plain(v.value || {})
    case "array": return root.plain(v.preview || [])
    case "string": return String(v.preview || "") + " …"
    case "date": return v.iso
    case "data": return "<" + Model.formatBytes(v.size) + " of data>"
    case "float": case "uid": return v.value
    default: return v
    }
  }
  function count(v, kind) {
    if (kind === "array") return Array.isArray(v) ? v.length : (v.count || 0)
    if (tag(v) === "dict") return v.truncated ? (v.count || 0) : Object.keys(v.value || {}).length
    return Object.keys(v || {}).length
  }
  function preview(k) {
    var v = root.reply ? root.reply.values[k] : null
    var kind = root.kindOf(k)
    switch (kind) {
    case "string":
      if (tag(v) === "string") return String(v.preview).replace(/\n/g, " ⏎ ") + " …"
      return v === "" ? "(empty)" : String(v).replace(/\n/g, " ⏎ ")
    case "bool": return v ? "true" : "false"
    case "int": case "float": case "uid": return tag(v) ? String(v.value) : String(v)
    case "date": return formatDate(v.iso)
    case "data": { var h = hexPrefix(v.base64); return Model.formatBytes(v.size) + (h ? "  ·  " + h + (v.size > 8 ? " …" : "") : "") }
    case "array": return Model.plural(count(v, kind), "item") + "  ·  " + JSON.stringify(root.plain(v))
    case "dict": return Model.plural(count(v, kind), "key") + "  ·  " + JSON.stringify(root.plain(v))
    default: return JSON.stringify(v)
    }
  }
  function expandable(k) { var kd = kindOf(k); return (kd === "dict" || kd === "array") && count(root.reply.values[k], kd) > 0 }
  function toggle(k) { var e = Object.assign({}, root.expanded); if (e[k]) delete e[k]; else e[k] = true; root.expanded = e }

  // full: true for the values as they are (nothing clipped), then `then(values)`
  function withFull(then) {
    if (root.reply && !root.reply.truncated) { then(root.reply.values); return }
    root.svc.request("appdata.defaults", { bundleId: root.bundleId, full: true }, function(ok, d) {
      if (ok) then(root.view.norm(d.values))
      else root.app.toast(Model.errorMessage(d), "error")
    }, 60000)
  }
  function copyJson() {
    if (!root.reply) return
    root.withFull(function(values) {
      Util.execArgv(["wl-copy", "--", JSON.stringify(values, null, 2)])
      root.app.toast("Copied " + Model.plural(Object.keys(values).length, "key") + " as JSON", "ok")
    })
  }
  function edit(k) {
    var v = root.reply.values[k]
    if (!root.hasClip(v)) { editSheet.begin(k, v, root.kindOf(k)); return }
    root.withFull(function(values) { editSheet.begin(k, values[k], root.kindOf(k)) })
  }
  function hasClip(v) {
    if (Array.isArray(v)) return v.some(root.hasClip)
    if (v === null || typeof v !== "object") return false
    if (v.truncated) return true
    for (var k in v) if (root.hasClip(v[k])) return true
    return false
  }

  function remove(k) {
    root.app.confirm({
      title: "Delete “" + k + "”?",
      message: "This removes the key from " + root.view.appName + "'s defaults on your iPhone. The app no longer finds it on its next launch. You can't undo this.",
      confirmText: "Delete key", cancelText: "Cancel", destructive: true, glyph: Model.Glyph.trash,
      // the delete runs in the edit sheet, which shows its 10 to 20 s of steps
      onConfirm: function() { editSheet.remove(k) }
    })
  }

  // ---- toolbar
  RowLayout {
    Layout.fillWidth: true
    visible: root.status === "ready" && !root.empty
    spacing: Style.spacing.lg
    FilterField { id: filter; Layout.fillWidth: true; Layout.maximumWidth: Style.space(360); placeholder: "Filter keys and values" }
    Item { Layout.fillWidth: true }
    ActionButton {
      text: root.narrow ? "" : "New key"; glyph: Model.Glyph.plus; kind: "ghost"
      disabled: root.locked
      tooltip: root.locked ? root.lockReason : (root.narrow ? "New key" : "")
      onClicked: editSheet.begin("", undefined, "string")
    }
    ActionButton {
      text: root.narrow ? "" : "Copy as JSON"; glyph: Model.Glyph.json; kind: "ghost"
      tooltip: root.narrow ? "Copy as JSON" : ""
      onClicked: root.copyJson()
    }
  }

  DeviceAppDataRunning {
    Layout.fillWidth: true
    visible: root.locked && root.status === "ready"
    view: root.view
    text: "Stop it to edit or delete keys. A running app writes its own copy back over your change."
  }

  // A change that may not reach the app, with what to do about it
  Notice {
    Layout.fillWidth: true
    visible: !!root.result
    title: root.result ? root.result.title : ""
    text: root.result ? root.result.text : ""
    actionText: root.result && root.result.launchArgs ? "Copy launch arguments" : ""
    actionTip: root.result && root.result.launchArgs ? "For one launch: " + root.result.launchArgs.join(" ") : ""
    onAction: root.copyLaunchArgs(root.result.launchArgs)
    onDismissed: root.result = null
  }
  // Keys iOS put back after an earlier change (the read lists each once)
  Notice {
    Layout.fillWidth: true
    visible: root.status === "ready" && root.reverted.length > 0
    title: "iOS put back the old value of " + root.reverted.map(function(r) { return "“" + r.key + "”" }).join(", ")
    text: ""
    detail: root.reverted.map(function(r) { return r.key + ": expected " + JSON.stringify(r.expected) + ", found " + JSON.stringify(r.actual) }).join("\n")
    onDismissed: root.reverted = []
  }

  // ---- states
  // A read right after a stop waits until iOS has saved the settings (up to 10 s)
  LoadingState {
    Layout.fillWidth: true
    visible: root.status === "loading" && root.waiting && !root.reply
    text: "Waiting for iOS to save the app's settings…"
    detail: "It saves them a few seconds after the app stops."
  }
  RowLayout {
    Layout.fillWidth: true
    visible: root.status === "loading" && root.waiting && !!root.reply
    spacing: Style.spacing.lg
    Spinner { size: Style.font.body; running: parent.visible; color: Commons.Color.accent }
    AppText { Layout.fillWidth: true; text: "Waiting for iOS to save the app's settings…"; size: "small"; tone: "dim"; elide: Text.ElideRight }
  }
  Skeleton { Layout.fillWidth: true; visible: root.status === "loading" && !root.reply && !root.waiting; lines: 6; rowHeight: Style.space(36); gap: Style.spacing.md }
  ErrorState {
    Layout.fillWidth: true
    visible: root.status === "error"
    app: root.app
    error: root.error
    title: root.error && root.error.code === "not-developer" ? "Only your own builds expose their defaults" : "Couldn't read the defaults"
    onRetry: root.load()
  }
  EmptyState {
    Layout.fillWidth: true
    visible: root.empty
    compact: true
    glyph: Model.Glyph.database
    title: "No defaults saved yet"
    text: "Run " + root.view.appName + " once, or add a key yourself."
    actionText: "New key"; actionGlyph: Model.Glyph.plus; actionKind: "secondary"
    onAction: if (!root.locked) editSheet.begin("", undefined, "string")
  }
  QuietNote {
    Layout.fillWidth: true
    visible: root.status === "ready" && !root.empty && root.shown.length === 0
    glyph: Model.Glyph.search
    title: "No key matches “" + filter.text.trim() + "”"
    text: "The filter looks at key names and values."
  }

  // ---- the keys
  ColumnLayout {
    Layout.fillWidth: true
    visible: root.status !== "error" && !root.empty && root.reply !== null
    opacity: root.status === "loading" ? 0.5 : 1
    spacing: Style.spacing.xxs
    Repeater {
      model: root.shown
      delegate: DefaultsRow { Layout.fillWidth: true }
    }
  }

  AppText {
    id: footLine
    Layout.fillWidth: true
    visible: root.status === "ready" && !root.empty
    readonly property string shortText: {
      if (!root.reply) return ""
      var p = [Model.plural(root.keys.length, "key")]
      if (root.reply.keys > root.keys.length) p[0] = "First " + root.keys.length + " of " + root.reply.keys + " keys"
      else if (root.reply.truncated) p[0] += ", long values shortened"
      if (root.reply.size) p.push(Model.formatBytes(root.reply.size))
      return p.join("  ·  ")
    }
    readonly property string fullText: root.reply && root.reply.path ? shortText + "  ·  " + root.reply.path : shortText
    // the file's path is the first thing to go when the line is short
    text: footMetrics.advanceWidth <= width ? fullText : shortText
    TextMetrics { id: footMetrics; font: footLine.font; text: footLine.fullText }
    size: "caption"; tone: "faint"; elide: Text.ElideRight
    horizontalAlignment: Text.AlignRight
  }

  // ---- a quiet strip with a warn glyph on its title's first line, an optional action and a close button
  component Notice: Rectangle {
    id: notice
    property string title: ""
    property string text: ""
    property string detail: ""                // hover text
    property string actionText: ""
    property string actionTip: ""
    signal action()
    signal dismissed()
    // narrow strips put the action under the text, so the text keeps its width
    readonly property bool stacked: width > 0 && width < Style.space(640)
    implicitHeight: noticeRow.implicitHeight + Style.spacing.lg * 2
    radius: Style.cornerRadius
    color: Theme.surface
    border.width: Style.normalBorderWidth
    border.color: Theme.hairline
    RowLayout {
      id: noticeRow
      anchors.fill: parent
      anchors.margins: Style.spacing.lg
      anchors.leftMargin: Style.spacing.xxl
      spacing: Style.spacing.lg
      // the glyph sits on the title's line, whatever the strip's height
      ColumnLayout {
        Layout.fillWidth: true
        Layout.alignment: Qt.AlignVCenter
        spacing: Style.spacing.xxs
        RowLayout {
          Layout.fillWidth: true
          spacing: Style.spacing.lg
          FontMetrics { id: noticeFm; font: noticeTitle.font }
          Item {
            // centred on the title's first line when the title wraps
            Layout.alignment: Qt.AlignTop
            Layout.preferredWidth: Style.font.icon
            Layout.preferredHeight: noticeFm.height
            Icon { anchors.centerIn: parent; glyph: Model.Glyph.warn; size: Style.font.icon; color: Theme.warn }
          }
          AppText {
            id: noticeTitle
            Layout.fillWidth: true
            text: notice.title; strong: true; wrapMode: Text.WordWrap
            HoverHandler { id: noticeHover }
            PanelToolTip { visible: noticeHover.hovered && (noticeTitle.truncated || notice.detail !== ""); text: notice.detail !== "" ? notice.detail : notice.title }
          }
        }
        AppText {
          Layout.fillWidth: true
          Layout.leftMargin: Style.font.icon + Style.spacing.lg
          visible: notice.text !== ""; text: notice.text; size: "small"; tone: "dim"; wrapMode: Text.WordWrap
        }
        ActionButton {
          Layout.leftMargin: Style.font.icon + Style.spacing.lg
          Layout.topMargin: Style.spacing.md
          visible: notice.actionText !== "" && notice.stacked
          text: notice.actionText; glyph: Model.Glyph.copy; compact: true
          tooltip: notice.actionTip
          onClicked: notice.action()
        }
      }
      ActionButton {
        Layout.alignment: Qt.AlignVCenter
        visible: notice.actionText !== "" && !notice.stacked
        text: notice.actionText; glyph: Model.Glyph.copy; compact: true
        tooltip: notice.actionTip
        onClicked: notice.action()
      }
      IconButton { Layout.alignment: notice.stacked ? Qt.AlignTop : Qt.AlignVCenter; glyph: Model.Glyph.close; tooltip: "Dismiss"; onClicked: notice.dismissed() }
    }
  }

  // ---- one key
  component TypeChip: Rectangle {
    property string kind: ""
    implicitWidth: Math.max(chipMetrics.advanceWidth("string"), chipText.implicitWidth) + Style.spacing.md * 2
    implicitHeight: chipText.implicitHeight + Style.spacing.xxs * 2
    radius: Style.cornerRadius
    color: "transparent"
    border.width: Style.normalBorderWidth
    border.color: Theme.hairline
    FontMetrics { id: chipMetrics; font: chipText.font }
    AppText { id: chipText; anchors.centerIn: parent; text: parent.kind; size: "caption"; tone: "faint" }
  }

  component DefaultsRow: CursorSurface {
    id: row
    required property var modelData
    readonly property string key: modelData
    readonly property var value: root.reply ? root.reply.values[key] : null
    readonly property string kind: root.kindOf(key)
    readonly property bool canExpand: root.expandable(key)
    readonly property bool open: canExpand && !!root.expanded[key]
    readonly property bool editable: kind !== "data" && kind !== "uid"

    hasCursor: rowHover.hovered
    implicitHeight: Math.max(Style.space(44), col.implicitHeight + Style.spacing.md * 2)

    HoverHandler { id: rowHover; cursorShape: row.canExpand ? Qt.PointingHandCursor : Qt.ArrowCursor }

    ColumnLayout {
      id: col
      anchors.left: parent.left; anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      anchors.leftMargin: Style.spacing.rowPaddingX
      anchors.rightMargin: Style.spacing.md
      spacing: Style.spacing.md

      RowLayout {
        Layout.fillWidth: true
        spacing: Style.spacing.xl
        TapHandler { enabled: row.canExpand; onTapped: root.toggle(row.key) }
        Item {
          visible: root.anyExpandable
          Layout.preferredWidth: Style.font.icon
          Layout.preferredHeight: Style.font.icon
          Icon {
            anchors.centerIn: parent
            visible: row.canExpand
            glyph: row.open ? Model.Glyph.chevronDown : Model.Glyph.chevronRight
            size: Style.font.bodySmall
            color: Theme.faint
          }
        }
        ColumnLayout {
          Layout.fillWidth: true
          spacing: Style.spacing.xxs
          RowLayout {
            Layout.fillWidth: true
            spacing: Style.spacing.xl
            AppText {
              Layout.preferredWidth: row.width > 0 && root.narrow ? -1 : root.keyWidth
              Layout.fillWidth: root.narrow
              text: row.key
              elide: Text.ElideRight
            }
            TypeChip { kind: row.kind }
            AppText {
              id: valueText
              Layout.fillWidth: true
              visible: !root.narrow
              text: root.preview(row.key)
              tone: row.value === "" ? "faint" : "dim"
              elide: Text.ElideRight
              HoverHandler { id: valueHover }
              PanelToolTip { visible: valueHover.hovered && valueText.truncated && !row.open; text: valueText.text.length > 600 ? valueText.text.substring(0, 600) + " …" : valueText.text }
            }
          }
          AppText {
            Layout.fillWidth: true
            visible: root.narrow
            text: root.preview(row.key)
            size: "small"
            tone: "dim"
            elide: Text.ElideRight
          }
        }
        Row {
          spacing: Style.spacing.xxs
          IconButton {
            glyph: Model.Glyph.edit
            tooltip: root.locked ? root.lockReason : !row.editable ? "Data values can't be edited here; delete or replace the key" : "Edit"
            disabled: root.locked || !row.editable
            onClicked: root.edit(row.key)
          }
          IconButton {
            glyph: Model.Glyph.trash; danger: true
            tooltip: root.locked ? root.lockReason : "Delete"
            disabled: root.locked
            onClicked: root.remove(row.key)
          }
        }
      }

      Rectangle {
        Layout.fillWidth: true
        Layout.leftMargin: root.chevronSlot
        // right edge on the drawn edge of the row's buttons (inside their padding), like the toolbar's above
        Layout.rightMargin: Style.spacing.md + Style.spacing.xxs
        Layout.bottomMargin: Style.spacing.sm
        visible: row.open
        implicitHeight: json.implicitHeight + Style.spacing.lg * 2
        radius: Style.cornerRadius
        color: Theme.surface
        border.width: Style.normalBorderWidth
        border.color: Theme.hairline
        TextEdit {
          id: json
          anchors.fill: parent
          anchors.margins: Style.spacing.lg
          anchors.leftMargin: Style.spacing.xl
          text: row.open ? JSON.stringify(root.plain(row.value), null, 2) : ""
          readOnly: true
          selectByMouse: true
          wrapMode: TextEdit.WrapAnywhere
          textFormat: TextEdit.PlainText
          font.family: Style.font.family
          font.pixelSize: Style.font.bodySmall
          color: Theme.dim
          selectionColor: Style.selectionFill
          selectedTextColor: Commons.Color.foreground
        }
      }
    }
  }

  DeviceAppDataEditSheet {
    id: editSheet
    view: root.view
    existing: root.keys
    onSaved: function(k, d) { root.changed(k, d, editSheet.deleting) }
  }
}
