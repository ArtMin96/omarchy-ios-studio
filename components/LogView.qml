import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import Quickshell.Io
import qs.Ui
import "../Model.js" as Model

// Fast, bounded log viewer (monospace). Two ways to feed it:
//   1. Declarative: `lines: ["…", "…"]` (small logs, e.g. a StepRow expander).
//      Items may be strings or {text, level, meta}.
//   2. Imperative (streams): `log.append(text, level, meta)`, `log.appendMany(arr)`, `log.clear()`.
//      Appends are batched every ~80 ms; the buffer keeps the last `maxLines`.
//
//   LogView {
//     id: log; Layout.fillWidth: true; Layout.fillHeight: true
//     filter: search.text          // or use the built-in toolbar (showToolbar: true)
//     levelFilter: "warn"          // "" = all, "info" | "warn" | "error" = at least that level
//   }
//   Connections { target: stream; function onEvent(ev) { if (ev.event === "log") log.append(ev.text) } }
//
// Levels: error | warn | info | debug (guessed from the text when omitted).
// `follow` sticks to the bottom; scrolling up turns it off, and the toolbar's
// Follow button (or End) turns it back on. `paused` freezes the view while
// lines keep buffering. Double-click a line to copy it.
Rectangle {
  id: root

  property var lines: []
  property int maxLines: 5000
  property bool follow: true
  property bool paused: false
  property bool wrap: true
  property bool showToolbar: true
  property bool showMeta: true
  property string filter: ""
  property string levelFilter: ""
  property string emptyText: "Nothing logged yet."
  readonly property int count: _all.length
  readonly property int shownCount: model.count
  property alias toolbarExtra: extraSlot.data

  signal lineCopied(string text)
  signal copiedAll(int count)

  property var _all: []
  property var _pending: []

  function guessLevel(t) {
    var s = String(t)
    if (/\b(error|fatal|failed|exception|crash(ed)?)\b|error:/i.test(s)) return "error"
    if (/\b(warn(ing)?|deprecated)\b|warning:/i.test(s)) return "warn"
    if (/\b(debug|trace|verbose)\b/i.test(s)) return "debug"
    return "info"
  }

  function _norm(item) {
    if (item === null || item === undefined) return null
    if (typeof item === "object") {
      var t = String(item.text === undefined ? "" : item.text)
      return { text: t, level: item.level || guessLevel(t), meta: item.meta ? String(item.meta) : "" }
    }
    var s = String(item)
    return { text: s, level: guessLevel(s), meta: "" }
  }

  function append(text, level, meta) {
    var n = _norm({ text: text, level: level, meta: meta })
    root._pending.push(n)
    if (!flush.running) flush.start()
  }

  function appendMany(arr) {
    for (var i = 0; i < arr.length; i++) { var n = _norm(arr[i]); if (n) root._pending.push(n) }
    if (!flush.running) flush.start()
  }

  function clear() {
    root._all = []
    root._pending = []
    model.clear()
  }

  function text() {
    var out = []
    for (var i = 0; i < root._all.length; i++) out.push(root._all[i].text)
    return out.join("\n")
  }

  function copyAll() {
    var visible = []
    for (var i = 0; i < model.count; i++) visible.push(model.get(i).text)
    _copy(visible.join("\n"))
    root.copiedAll(visible.length)
  }

  // wl-copy reads stdin: an argv argument is capped at 128 KB, a long log isn't.
  function _copy(text) {
    var p = copyComp.createObject(root, { payload: String(text) })
    if (!p) return
    p.stdinEnabled = true
    p.running = true
  }
  Component {
    id: copyComp
    Process {
      id: cp
      property string payload: ""
      command: ["wl-copy"]
      onStarted: { cp.write(cp.payload); cp.payload = ""; cp.stdinEnabled = false }
      onExited: cp.destroy()
    }
  }

  function scrollToEnd() { root.follow = true; list.positionViewAtEnd() }

  readonly property var _rank: ({ debug: 0, info: 1, warn: 2, error: 3 })
  function _passes(n) {
    if (root.levelFilter !== "" && (root._rank[n.level] || 1) < (root._rank[root.levelFilter] || 0)) return false
    if (root.filter !== "" && !Model.matches(n.text + " " + n.meta, root.filter)) return false
    return true
  }

  function _rebuild() {
    model.clear()
    var out = []
    for (var i = 0; i < root._all.length; i++) if (_passes(root._all[i])) out.push(root._all[i])
    if (out.length) model.append(out)
    if (root.follow) Qt.callLater(list.positionViewAtEnd)
  }

  function _flush() {
    if (root.paused || root._pending.length === 0) return
    var batch = root._pending
    root._pending = []
    var all = root._all.concat(batch)
    var drop = all.length - root.maxLines
    if (drop > 0) all = all.slice(drop)
    root._all = all
    // trim the display model from the front, then append what passes
    if (drop > 0) {
      var extra = model.count + batch.length - root.maxLines
      if (extra > 0) model.remove(0, Math.min(extra, model.count))
    }
    var add = []
    for (var i = 0; i < batch.length; i++) if (_passes(batch[i])) add.push(batch[i])
    if (add.length) model.append(add)
    if (root.follow) list.positionViewAtEnd()
  }

  onLinesChanged: { root._all = []; root._pending = []; model.clear(); appendMany(root.lines || []); _flushNow() }
  function _flushNow() { flush.stop(); _flush() }
  onFilterChanged: _rebuild()
  onLevelFilterChanged: _rebuild()
  onPausedChanged: if (!paused) _flushNow()

  color: Util.alpha(Commons.Color.background, Theme.isDark ? 0.5 : 0.3)
  radius: Style.cornerRadius
  border.width: Style.normalBorderWidth
  border.color: Theme.hairline
  clip: true

  Timer { id: flush; interval: 80; onTriggered: root._flush() }
  ListModel { id: model }

  ColumnLayout {
    anchors.fill: parent
    anchors.margins: Style.spacing.hairline
    spacing: 0

    // Toolbar: one row when there's room; below ~640 px the filter gets its own
    // row and the line count hides, so nothing spills out of a narrow window.
    GridLayout {
      id: toolbar
      readonly property bool narrow: root.width < Style.space(640)
      visible: root.showToolbar
      Layout.fillWidth: true
      Layout.margins: Style.spacing.md
      columns: narrow ? 2 : 3
      columnSpacing: Style.spacing.md
      rowSpacing: Style.spacing.md
      FilterField {
        id: search
        Layout.columnSpan: toolbar.narrow ? 2 : 1
        Layout.fillWidth: toolbar.narrow
        Layout.preferredWidth: toolbar.narrow ? -1 : Style.space(220)
        placeholder: "Filter log"
        onTextChanged: root.filter = text
      }
      RowLayout {
        spacing: Style.spacing.md
        ButtonGroupLite {
          options: [{ value: "", label: "All" }, { value: "warn", label: "Warnings" }, { value: "error", label: "Errors" }]
          value: root.levelFilter
          onPicked: function(v) { root.levelFilter = v }
        }
        Row { id: extraSlot; spacing: Style.spacing.xs }
      }
      RowLayout {
        Layout.fillWidth: true
        spacing: Style.spacing.md
        Item { Layout.fillWidth: true }
        AppText { visible: !toolbar.narrow; text: root.shownCount === root.count ? root.count + " lines" : root.shownCount + " of " + root.count; size: "small"; tone: "faint" }
        IconButton { glyph: root.paused ? Model.Glyph.play : Model.Glyph.pause; tooltip: root.paused ? "Resume" : "Pause"; onClicked: root.paused = !root.paused }
        IconButton { glyph: Model.Glyph.chevronDown; tooltip: root.follow ? "Following new lines" : "Follow new lines"; foreground: root.follow ? Commons.Color.accent : Theme.dim; onClicked: root.scrollToEnd() }
        IconButton { glyph: Model.Glyph.copy; tooltip: "Copy visible lines"; onClicked: root.copyAll() }
        IconButton { glyph: Model.Glyph.trash; tooltip: "Clear"; onClicked: root.clear() }
      }
    }
    Rectangle { visible: root.showToolbar; Layout.fillWidth: true; height: Style.spacing.hairline; color: Theme.hairline }

    ListView {
      id: list
      Layout.fillWidth: true
      Layout.fillHeight: true
      clip: true
      model: model
      boundsBehavior: Flickable.StopAtBounds
      reuseItems: true
      topMargin: Style.spacing.md
      bottomMargin: Style.spacing.md
      ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
      onMovementStarted: root.follow = false
      onAtYEndChanged: if (atYEnd && moving) root.follow = true

      delegate: Item {
        id: line
        required property string text
        required property string level
        required property string meta
        required property int index
        width: list.width
        height: lineText.implicitHeight + Style.spacing.xxs
        Rectangle {
          anchors.fill: parent
          color: lineHover.hovered ? Style.hoverFill : (line.level === "error" ? Util.alpha(Commons.Color.urgent, 0.06) : "transparent")
        }
        Rectangle {
          visible: line.level === "error" || line.level === "warn"
          width: Style.space(2); height: parent.height
          color: line.level === "error" ? Commons.Color.urgent : Theme.warn
        }
        Text {
          id: metaText
          visible: root.showMeta && line.meta !== ""
          x: Style.spacing.lg
          width: visible ? Style.space(110) : 0
          text: line.meta
          textFormat: Text.PlainText
          elide: Text.ElideRight
          color: Theme.faint
          font.family: Style.font.family
          font.pixelSize: Style.font.bodySmall
        }
        Text {
          id: lineText
          x: Style.spacing.lg + (metaText.visible ? metaText.width + Style.spacing.lg : 0)
          width: parent.width - x - Style.spacing.lg
          text: line.text
          textFormat: Text.PlainText
          wrapMode: root.wrap ? Text.WrapAnywhere : Text.NoWrap
          elide: root.wrap ? Text.ElideNone : Text.ElideRight
          color: line.level === "error" ? Commons.Color.urgent : (line.level === "warn" ? Theme.warn : (line.level === "debug" ? Theme.faint : Commons.Color.foreground))
          font.family: Style.font.family
          font.pixelSize: Style.font.bodySmall
        }
        HoverHandler { id: lineHover }
        TapHandler {
          onDoubleTapped: { root._copy(line.text); root.lineCopied(line.text) }
        }
      }
    }
  }

  AppText {
    anchors.centerIn: parent
    visible: model.count === 0
    text: root.count > 0 ? "No lines match the filter." : root.emptyText
    tone: "faint"
  }
}
