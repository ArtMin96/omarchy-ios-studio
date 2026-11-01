import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../Model.js" as Model

// Live device log list (logs slice). Columns: time · level · process[pid] · message.
// Feed it SyslogEntry batches with `push(batch)`; appends are batched (80 ms) and the buffer
// keeps the last `maxLines`. Client-side filters: `minLevel`, `processes`, `query` (with
// match highlighting). `paused` freezes the view while entries keep buffering.
//
//   LogSyslogView { id: view; query: search.text; minLevel: svc.levelFilter; processes: svc.processList }
//   Connections { target: svc; function onEntries(b) { view.push(b) } }
Rectangle {
  id: root

  property int maxLines: 5000
  property bool follow: true
  property bool paused: false
  property string query: ""
  property string minLevel: "debug"
  property var processes: []
  property string emptyTitle: "Waiting for log lines…"
  property string emptyText: ""

  readonly property int count: _all.length
  readonly property int shownCount: model.count
  readonly property int pendingCount: _pendingCount
  readonly property int selectedRow: _rowOf(selectedSeq)
  property int selectedSeq: -1

  signal copyRequested()

  property var _all: []
  property var _pending: []
  property int _pendingCount: 0
  property int _seq: 0

  readonly property var _rank: ({ debug: 0, info: 1, "default": 2, error: 3, fault: 4 })

  // ---------------------------------------------------------------- feeding
  function _pad(n, w) { var s = String(n); while (s.length < w) s = "0" + s; return s }
  function _time(ts) {
    var d = new Date(ts * 1000)
    return _pad(d.getHours(), 2) + ":" + _pad(d.getMinutes(), 2) + ":" + _pad(d.getSeconds(), 2) + "." + _pad(d.getMilliseconds(), 3)
  }

  function _norm(e) {
    var full = String(e.message === undefined || e.message === null ? "" : e.message)
    var nl = full.indexOf("\n")
    var first = nl >= 0 ? full.substring(0, nl) : full
    if (first.length > 400) first = first.substring(0, 400) + "…"
    root._seq++
    return {
      seq: root._seq, t: _time(Number(e.ts) || Date.now() / 1000), lv: String(e.level || "default"),
      proc: String(e.process || "?"), pid: Number(e.pid) || 0, msg: first, full: full, multi: nl >= 0,
      sub: String(e.subsystem || ""), cat: String(e.category || ""), img: String(e.image || "")
    }
  }

  function push(batch) {
    if (!batch || !batch.length) return
    for (var i = 0; i < batch.length; i++) root._pending.push(_norm(batch[i]))
    if (root._pending.length > root.maxLines) root._pending = root._pending.slice(root._pending.length - root.maxLines)
    root._pendingCount = root._pending.length
    if (!flushTimer.running && !root.paused) flushTimer.start()
  }

  function clear() {
    root._all = []; root._pending = []; root._pendingCount = 0
    root.selectedSeq = -1
    model.clear()
  }

  function _passes(n) {
    if ((root._rank[n.lv] === undefined ? 2 : root._rank[n.lv]) < (root._rank[root.minLevel] || 0)) return false
    if (root.processes && root.processes.length && root.processes.indexOf(n.proc) < 0) return false
    if (root.query !== "") {
      var q = root.query.toLowerCase()
      if (n.full.toLowerCase().indexOf(q) < 0 && n.proc.toLowerCase().indexOf(q) < 0 && n.sub.toLowerCase().indexOf(q) < 0) return false
    }
    return true
  }

  function _flush() {
    if (root.paused || root._pending.length === 0) return
    var batch = root._pending
    root._pending = []; root._pendingCount = 0
    var all = root._all.concat(batch)
    var drop = all.length - root.maxLines
    if (drop > 0) {
      var cut = all[drop - 1].seq
      all = all.slice(drop)
      // drop rows that left the buffer from the front of the display model
      var n = 0
      while (n < model.count && model.get(n).seq <= cut) n++
      if (n > 0) model.remove(0, n)
    }
    root._all = all
    var add = []
    for (var i = 0; i < batch.length; i++) if (_passes(batch[i])) add.push(batch[i])
    if (add.length) model.append(add)
    if (root.follow) list.positionViewAtEnd()
  }

  function _rebuild() {
    model.clear()
    var out = []
    for (var i = 0; i < root._all.length; i++) if (_passes(root._all[i])) out.push(root._all[i])
    if (out.length) model.append(out)
    if (root.follow) Qt.callLater(list.positionViewAtEnd)
    else if (root.selectedRow >= 0) Qt.callLater(function() { list.positionViewAtIndex(root.selectedRow, ListView.Contain) })
  }

  onQueryChanged: rebuildTimer.restart()
  onMinLevelChanged: _rebuild()
  onProcessesChanged: _rebuild()
  onPausedChanged: if (!paused) { flushTimer.stop(); _flush() }

  Timer { id: flushTimer; interval: 80; onTriggered: root._flush() }
  Timer { id: rebuildTimer; interval: 120; onTriggered: root._rebuild() }
  ListModel { id: model }

  // ---------------------------------------------------------------- selection / copy
  function _rowOf(seq) {
    if (seq < 0 || model.count === 0) return -1
    // seq is monotonic → binary search
    var lo = 0, hi = model.count - 1
    while (lo <= hi) {
      var mid = (lo + hi) >> 1, s = model.get(mid).seq
      if (s === seq) return mid
      if (s < seq) lo = mid + 1; else hi = mid - 1
    }
    return -1
  }

  function select(row) {
    if (row < 0 || row >= model.count) return
    root.selectedSeq = model.get(row).seq
    root.follow = false
    list.positionViewAtIndex(row, ListView.Contain)
  }
  function selectNext(d) {
    if (model.count === 0) return
    var r = root.selectedRow
    if (r < 0) {
      // start from the bottom-most line on screen (where the eye is while tailing)
      var bottom = list.indexAt(Style.spacing.lg, list.contentY + list.height - list.bottomMargin - 2)
      r = bottom < 0 ? model.count - 1 : bottom
    }
    else r = Math.max(0, Math.min(model.count - 1, r + d))
    select(r)
  }
  function deselect() { root.selectedSeq = -1 }

  function formatLine(o) {
    return o.t + " " + o.lv + " " + o.proc + "[" + o.pid + "]: " + o.full
  }
  function selectedText() {
    var r = root.selectedRow
    return r < 0 ? "" : formatLine(model.get(r))
  }
  function visibleText() {
    var out = []
    for (var i = 0; i < model.count; i++) out.push(formatLine(model.get(i)))
    return out.join("\n")
  }

  function jumpToLatest() { root.follow = true; root.selectedSeq = -1; list.positionViewAtEnd() }

  // ---------------------------------------------------------------- highlight
  function _esc(s) { return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;") }
  function highlight(s) {
    var q = root.query
    if (q === "") return _esc(s)
    var low = s.toLowerCase(), ql = q.toLowerCase(), out = "", i = 0
    while (true) {
      var j = low.indexOf(ql, i)
      if (j < 0) { out += _esc(s.substring(i)); break }
      out += _esc(s.substring(i, j)) + "<b><font color=\"" + root._hlColor + "\">" + _esc(s.substring(j, j + q.length)) + "</font></b>"
      i = j + q.length
    }
    return out
  }
  readonly property string _hlColor: String(Commons.Color.accent)

  function levelColor(lv) {
    switch (lv) {
    case "fault": return Theme.error
    case "error": return Theme.warn
    case "debug": return Theme.faint
    case "info": return Theme.dim
    default: return Commons.Color.foreground
    }
  }

  // ---------------------------------------------------------------- look
  color: Util.alpha(Commons.Color.background, Theme.isDark ? 0.5 : 0.3)
  radius: Style.cornerRadius
  border.width: Style.normalBorderWidth
  border.color: Theme.hairline
  clip: true

  TextMetrics { id: tmTime; font.family: Style.font.family; font.pixelSize: Style.font.bodySmall; text: "00:00:00.000" }
  TextMetrics { id: tmLevel; font.family: Style.font.family; font.pixelSize: Style.font.bodySmall; text: "default" }
  TextMetrics { id: tmProc; font.family: Style.font.family; font.pixelSize: Style.font.bodySmall; text: "SpringBoard[12345]" }
  readonly property real _gap: Style.spacing.xl
  readonly property real _xLevel: Style.spacing.lg + tmTime.advanceWidth + _gap
  readonly property real _xProc: _xLevel + tmLevel.advanceWidth + _gap
  readonly property real _xMsg: _xProc + tmProc.advanceWidth + _gap

  ListView {
    id: list
    anchors.fill: parent
    anchors.margins: Style.spacing.hairline
    clip: true
    model: model
    boundsBehavior: Flickable.StopAtBounds
    reuseItems: true
    topMargin: Style.spacing.sm
    bottomMargin: Style.spacing.sm
    ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
    onMovementStarted: root.follow = false
    onAtYEndChanged: if (atYEnd && moving && root.selectedSeq < 0) root.follow = true

    delegate: Item {
      id: row
      required property int seq
      required property string t
      required property string lv
      required property string proc
      required property int pid
      required property string msg
      required property string full
      required property bool multi
      required property string sub
      required property string cat
      required property string img
      required property int index
      readonly property bool selected: root.selectedSeq === seq
      readonly property bool loud: lv === "fault" || lv === "error"
      readonly property color tone: root.levelColor(lv)

      width: ListView.view.width
      height: col.implicitHeight + Style.spacing.xs * 2

      Rectangle {
        anchors.fill: parent
        color: row.selected ? Style.selectedAccentFill
             : hover.hovered ? Style.hoverFill
             : row.lv === "fault" ? Util.alpha(Theme.error, 0.08)
             : row.lv === "error" ? Util.alpha(Theme.warn, 0.05) : "transparent"
      }

      Text {
        x: Style.spacing.lg; y: Style.spacing.xs
        text: row.t
        color: Theme.faint
        font.family: Style.font.family; font.pixelSize: Style.font.bodySmall
        font.features: { "tnum": 1 }
      }
      Text {
        x: root._xLevel; y: Style.spacing.xs
        text: row.lv
        color: row.loud ? row.tone : Theme.faint
        font.family: Style.font.family; font.pixelSize: Style.font.bodySmall
        font.bold: row.loud
      }
      Text {
        x: root._xProc; y: Style.spacing.xs
        width: tmProc.advanceWidth
        text: row.proc + "[" + row.pid + "]"
        elide: Text.ElideMiddle
        color: row.selected ? Commons.Color.foreground : Theme.dim
        font.family: Style.font.family; font.pixelSize: Style.font.bodySmall
      }
      Column {
        id: col
        x: root._xMsg; y: Style.spacing.xs
        width: row.width - x - Style.spacing.xl
        spacing: Style.spacing.xs
        Text {
          width: parent.width
          text: root.query === "" ? (row.selected ? row.full : row.msg) : root.highlight(row.selected ? row.full : row.msg)
          textFormat: root.query === "" ? Text.PlainText : Text.StyledText
          wrapMode: row.selected ? Text.WrapAnywhere : Text.NoWrap
          maximumLineCount: row.selected ? 60 : 1
          elide: row.selected || root.query !== "" ? Text.ElideNone : Text.ElideRight
          clip: !row.selected
          color: row.lv === "info" ? Commons.Color.foreground : row.tone
          font.family: Style.font.family; font.pixelSize: Style.font.bodySmall
          font.bold: row.lv === "fault"
        }
        Text {
          visible: row.selected && (row.sub !== "" || row.img !== "" || row.multi)
          width: parent.width
          text: [row.sub !== "" ? row.sub + (row.cat !== "" ? " · " + row.cat : "") : "", row.img !== "" ? "from " + row.img : ""].filter(function(s) { return s !== "" }).join("   ")
          elide: Text.ElideRight
          color: Theme.faint
          font.family: Style.font.family; font.pixelSize: Style.font.caption
        }
        Text {
          visible: !row.selected && row.multi
          text: "+ more lines, click to expand"
          color: Theme.faint
          font.family: Style.font.family; font.pixelSize: Style.font.caption
        }
      }

      HoverHandler { id: hover; cursorShape: Qt.PointingHandCursor }
      TapHandler {
        onTapped: { if (row.selected) root.deselect(); else { root.selectedSeq = row.seq; root.follow = false } }
        onDoubleTapped: { root.selectedSeq = row.seq; root.copyRequested() }
      }
    }
  }

  // jump-to-latest pill
  Rectangle {
    visible: !root.follow && model.count > 0
    anchors.horizontalCenter: parent.horizontalCenter
    anchors.bottom: parent.bottom
    anchors.bottomMargin: Style.spacing.xl
    width: jumpRow.implicitWidth + Style.spacing.xxl * 2
    height: jumpRow.implicitHeight + Style.spacing.md * 2
    radius: height / 2
    color: jumpHover.hovered ? Qt.lighter(Commons.Color.accent, 1.1) : Commons.Color.accent
    Row {
      id: jumpRow
      anchors.centerIn: parent
      spacing: Style.spacing.md
      Icon { glyph: Model.Glyph.chevronDown; size: Style.font.iconSmall; color: Commons.Color.background; anchors.verticalCenter: parent.verticalCenter }
      AppText { text: root.paused && root.pendingCount ? "Resume · " + root.pendingCount + " new" : "Jump to latest"; size: "small"; strong: true; tone: "inherit"; color: Commons.Color.background; anchors.verticalCenter: parent.verticalCenter }
    }
    HoverHandler { id: jumpHover; cursorShape: Qt.PointingHandCursor }
    TapHandler { onTapped: { root.paused = false; root.jumpToLatest() } }
  }

  ColumnLayout {
    anchors.centerIn: parent
    width: Math.min(parent.width - Style.spacing.huge * 2, Style.space(420))
    visible: model.count === 0
    spacing: Style.spacing.md
    AppText {
      Layout.fillWidth: true
      horizontalAlignment: Text.AlignHCenter
      text: root.count > 0 ? "No lines match" : root.emptyTitle
      size: "subtitle"; tone: "dim"
    }
    AppText {
      Layout.fillWidth: true
      horizontalAlignment: Text.AlignHCenter
      wrapMode: Text.WordWrap
      visible: text !== ""
      text: root.count > 0 ? root.count + " lines are hidden by the search or level filter." : root.emptyText
      size: "small"; tone: "faint"
    }
  }
}
