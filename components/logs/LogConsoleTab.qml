import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../common"
import "../../lib/Model.js" as Model

// Logs page › Console tab: what an app launched with the console on prints (Swift print(),
// stdout and stderr). The daemon keeps each launch's lines (console.* in deviced-protocol.md);
// this tab shows the newest launch, catches up from the daemon's buffer when it opens or falls
// behind, then appends the live `console` events. Run on a project, or Launch here, starts one.
Item {
  id: root
  required property var app
  readonly property var svc: app.logs
  readonly property var dev: app.device
  readonly property alias view: logView
  readonly property alias search: searchField

  readonly property bool ready: svc.deviceApi && !svc.deviceNeedsSetup && svc.deviceConnected

  // ---- sessions (newest first) and the one on screen
  property var sessions: []
  property string sessionId: ""
  property bool followLatest: true         // switch to each new launch (off once the user picks one)
  property int lastSeq: -1                 // last line number shown
  property int clearedAt: 0                // Clear: lines before this aren't shown again
  property bool loading: false
  property bool loaded: false
  property bool launching: false
  property string streamFilter: ""         // "" = everything, "warn" = stderr only (stderr lines are warn/error)
  property string launchBid: ""
  readonly property int lineChars: 600  // longer lines are shortened on screen (the daemon keeps 8 KB)

  readonly property var current: {
    for (var i = 0; i < sessions.length; i++) if (sessions[i].sessionId === sessionId) return sessions[i]
    return null
  }
  readonly property bool split: !!current && current.streams === "split"

  // Your apps on the phone (developer-signed), for Launch.
  readonly property var ownApps: {
    var apps = svc.deviceApi && dev.apps ? dev.apps : []
    var out = []
    for (var i = 0; i < apps.length; i++) if (apps[i].developer) out.push({ value: apps[i].bundleId, label: apps[i].name || apps[i].bundleId })
    return out
  }
  readonly property string effectiveBid: {
    if (launchBid !== "" && ownApps.some(function(o) { return o.value === launchBid })) return launchBid
    if (current && ownApps.some(function(o) { return o.value === current.bundleId })) return current.bundleId
    return ownApps.length ? ownApps[0].value : ""
  }

  readonly property var sessionOptions: {
    var out = []
    for (var i = 0; i < sessions.length; i++) {
      var s = sessions[i]
      out.push({ value: s.sessionId, label: s.name + " · " + Qt.formatTime(new Date(s.started * 1000), "hh:mm:ss") + (s.running ? " · live" : "") })
    }
    return out
  }

  // ---- data
  function _upsert(s) {
    var list = root.sessions.filter(function(x) { return x.sessionId !== s.sessionId })
    list.push(s)
    list.sort(function(a, b) { return b.started - a.started })
    root.sessions = list
  }

  function reload() {
    if (!root.ready) return
    dev.request("console.list", {}, function(ok, d) {
      root.loaded = true
      if (!ok) return
      root.sessions = d.sessions || []
      if (root.sessions.length && (root.followLatest || !root.current)) root.show(root.sessions[0].sessionId, true)
    })
  }

  function show(sid, auto) {
    if (!auto) root.followLatest = sid === (root.sessions.length ? root.sessions[0].sessionId : "")
    if (sid === root.sessionId && root.lastSeq >= 0) return
    root.sessionId = sid
    root.lastSeq = -1
    root.clearedAt = 0
    logView.clear()
    var s = root.current
    root.catchUp(s ? Math.max(0, s.lines - logView.maxLines) : 0)
  }

  // Read from `from` (the daemon's buffer) until caught up; live events resume after that.
  function catchUp(from) {
    if (root.loading || root.sessionId === "") return
    root.loading = true
    var sid = root.sessionId
    dev.request("console.read", { sessionId: sid, cursor: from, limit: 2000 }, function(ok, d) {
      root.loading = false
      if (sid !== root.sessionId) return
      if (!ok) { if (d && d.code !== "not-found") root.app.toast(Model.errorMessage(d), "warn"); return }
      root._appendLines(d.cursor, d.entries || [])
      if (d.session) root._upsert(d.session)
      if (d.hasMore) root.catchUp(d.nextCursor)
    })
  }

  function _appendLines(first, lines) {
    var out = []
    for (var i = 0; i < lines.length; i++) {
      var seq = first + i
      if (seq <= root.lastSeq || seq < root.clearedAt) continue
      var e = lines[i]
      var t = String(e.text === undefined ? "" : e.text)
      var more = []
      if (t.length > root.lineChars) { more.push((t.length - root.lineChars) + " more characters"); t = t.slice(0, root.lineChars) }
      if (e.cut) more.push(e.cut + " bytes not kept")
      if (more.length) t += "  …(" + more.join(", ") + ")"
      var lv = e.stream === "stderr" ? (logView.guessLevel(t) === "error" ? "error" : "warn")
             : e.stream === "meta" ? "debug" : "info"
      out.push({ text: t, level: lv, meta: Qt.formatTime(new Date(e.ts * 1000), "hh:mm:ss") + (e.stream === "stderr" ? " err" : "") })
    }
    if (lines.length) root.lastSeq = Math.max(root.lastSeq, first + lines.length - 1)
    if (out.length) logView.appendMany(out)
  }

  function launch() {
    var bid = root.effectiveBid
    if (bid === "" || root.launching) return
    root.launching = true
    root.followLatest = true
    dev.request("app.launch", { bundleId: bid, console: true }, function(ok, d) {
      root.launching = false
      if (!ok) { root.app.toast(Model.errorMessage(d), "error"); return }
      var c = d && d.console ? d.console : null
      if (c && c.sessionId) { root._upsert(c); root.show(c.sessionId, true) }
      else root.app.toast(c && c.error ? Model.errorMessage(c.error) : "Opened, but without the console", "warn", { timeout: 6000 })
    })
  }

  // ---- actions (also used by the page's keys)
  function copySelected() {
    var n = logView.shownCount
    if (n === 0) { root.app.toast("Nothing to copy yet"); return }
    logView.copyAll()
  }
  function exportVisible() {
    if (logView.shownCount === 0) { root.app.toast("Nothing to export yet"); return }
    var lines = logView.text()
    var path = svc.downloadsDir + "/app-console-" + svc.stamp() + ".txt"
    svc.writeFile(path, lines + "\n", function(ok) {
      root.app.toast(ok ? "Saved the output to " + svc.prettyPath(path) : "Couldn't save the output", ok ? "ok" : "error", { timeout: 6000 })
    })
  }
  function togglePause() { logView.paused = !logView.paused; if (!logView.paused) logView.scrollToEnd() }
  function clearView() { root.clearedAt = root.lastSeq + 1; logView.clear() }

  Connections {
    target: root.svc.deviceApi ? root.dev : null
    ignoreUnknownSignals: true
    function onDaemonEvent(name, data) {
      if (!data) return
      if (name === "consoleState") {
        var isNew = !root.sessions.some(function(x) { return x.sessionId === data.sessionId })
        root._upsert(data)
        if (isNew && data.running && root.followLatest) root.show(data.sessionId, true)
        else if (root.sessionId === "") root.show(data.sessionId, true)
      } else if (name === "console") {
        if (data.sessionId !== root.sessionId || root.loading) return
        if (data.first > root.lastSeq + 1) root.catchUp(Math.max(root.lastSeq + 1, root.clearedAt))  // fell behind
        else root._appendLines(data.first, data.lines || [])
      } else if (name === "ready") {
        root.sessions = []; root.sessionId = ""; root.lastSeq = -1; logView.clear(); root.reload()
      }
    }
  }
  onReadyChanged: if (ready) reload()
  Component.onCompleted: reload()

  ColumnLayout {
    anchors.fill: parent
    spacing: Style.spacing.xl
    visible: root.ready

    // ---- toolbar (two rows in a narrow window)
    GridLayout {
      id: bar
      readonly property bool narrow: root.width < Style.space(820)
      Layout.fillWidth: true
      columns: narrow ? 3 : 6
      columnSpacing: Style.spacing.lg
      rowSpacing: Style.spacing.md

      ActionButton {
        text: "Launch"
        glyph: Model.Glyph.play
        kind: "primary"
        busy: root.launching
        disabled: root.effectiveBid === ""
        tooltip: root.effectiveBid === "" ? "Install one of your apps first (Projects › Run)"
               : "Start the app with its output shown here (restarts it if it's running)"
        onClicked: root.launch()
      }
      Dropdown {
        showLabel: false
        visible: root.ownApps.length > 0
        Layout.preferredWidth: Style.space(190)
        value: root.effectiveBid
        options: root.ownApps
        onChanged: function(v) { root.launchBid = v }
      }
      Dropdown {
        showLabel: false
        visible: root.sessions.length > 1
        Layout.preferredWidth: Style.space(220)
        value: root.sessionId
        options: root.sessionOptions
        onChanged: function(v) { root.show(v, false) }
      }
      FilterField {
        id: searchField
        Layout.fillWidth: true
        Layout.minimumWidth: Style.space(140)
        placeholder: "Filter the output"
      }
      ButtonGroupLite {
        visible: root.split
        options: [{ value: "", label: "All" }, { value: "warn", label: "stderr" }]
        value: root.streamFilter
        onPicked: function(v) { root.streamFilter = v }
      }
      RowLayout {
        spacing: Style.spacing.lg
        IconButton {
          glyph: logView.paused ? Model.Glyph.play : Model.Glyph.pause
          tooltip: logView.paused ? "Resume (Space)" : "Pause the view (Space)"
          foreground: logView.paused ? Commons.Color.accent : Theme.dim
          onClicked: root.togglePause()
        }
        IconButton { glyph: Model.Glyph.copy; tooltip: "Copy visible lines (c)"; onClicked: root.copySelected() }
        IconButton { glyph: Model.Glyph.export; tooltip: "Export the output to Downloads"; onClicked: root.exportVisible() }
        IconButton { glyph: Model.Glyph.broom; tooltip: "Clear"; onClicked: root.clearView() }
      }
    }

    LogView {
      id: logView
      Layout.fillWidth: true
      Layout.fillHeight: true
      showToolbar: false
      filter: searchField.text.trim()
      levelFilter: root.split ? root.streamFilter : ""
      emptyText: root.loading ? "Loading…"
               : root.current ? (root.current.running ? "Nothing printed yet. Lines appear as the app writes them." : "The app didn't print anything.")
               : "Press Launch, or Run a project: what your app prints shows up here."
    }

    // ---- status line
    RowLayout {
      Layout.fillWidth: true
      spacing: Style.spacing.xl
      StatusPill {
        quiet: true
        readonly property var s: root.current
        level: !s ? "info" : s.running ? (logView.paused ? "info" : "busy") : (s.status === "lost" ? "warn" : "info")
        glyph: s && s.running && !logView.paused ? Model.Glyph.dot : ""
        text: !s ? "No app launched with the console yet"
            : s.running ? (logView.paused ? "Paused · new lines are waiting" : "Live from " + s.name + (s.pid ? " (pid " + s.pid + ")" : ""))
            : s.name + ": " + (s.reason || s.status)
      }
      AppText {
        visible: !bar.narrow && logView.count > 0
        text: logView.shownCount === logView.count ? Model.plural(logView.count, "line") : logView.shownCount + " of " + logView.count + " lines"
        size: "small"; tone: "faint"
      }
      AppText {
        visible: !!root.current && root.current.dropped > 0
        text: root.current ? root.current.dropped + " lines dropped: the app printed more than 2,000 lines a second." : ""
        size: "small"; tone: "faint"; elide: Text.ElideRight
        Layout.fillWidth: true
      }
      Item { Layout.fillWidth: true; visible: !root.current || root.current.dropped === 0 }
      AppText {
        visible: !!root.current && !root.split
        text: "stdout and stderr arrive together on the iPhone"
        size: "small"; tone: "faint"
      }
    }
  }

  // ---- no phone / no tools
  EmptyState {
    anchors.centerIn: parent
    width: Math.min(parent.width, Style.space(520))
    visible: !root.ready
    glyph: root.svc.deviceNeedsSetup ? Model.Glyph.toolbox : Model.Glyph.phone
    title: root.svc.deviceNeedsSetup ? "iPhone tools aren't installed yet"
         : !root.svc.deviceApi ? "Starting the iPhone service…"
         : "Connect your iPhone to see your app's output"
    text: root.svc.deviceNeedsSetup ? "iOS Studio needs a small helper to talk to your iPhone. Setup installs it for you."
        : !root.svc.deviceApi ? "This takes a second."
        : "What your app prints (print(), stdout and stderr) shows up here when iOS Studio launches it."
    actionText: root.svc.deviceNeedsSetup ? "Open Setup" : (root.svc.deviceApi ? "Go to iPhone" : "")
    actionGlyph: root.svc.deviceNeedsSetup ? Model.Glyph.rocket : Model.Glyph.phone
    onAction: root.svc.deviceNeedsSetup ? root.app.openSetupStep("pymobiledevice3") : root.app.nav("device")
  }
}
