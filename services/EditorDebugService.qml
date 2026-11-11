import QtQuick
import Quickshell
import qs.Commons
import "../core"
import "../lib/Model.js" as Model

// Debug in editor (issue #14; helper `debug editor`, docs/features/editor-debugging.md): a bridge per
// project that holds the phone's debugserver open for VS Code / Zed's lldb-dap (F5 in the editor).
//
// Public surface (app.editorDebug):
//   forProject(p) → the entry for a project, or null:
//     { path, name, state: starting|ready|attached|detached|stopping|ended|error, step, owned, bundleId, port,
//       pid, launched, configName, editors[], notices[{id, level, message}], error, ended, attached, killed, at }
//   label(entry) → { level, text, detail, tip, live } (Model.editorDebugLabel)
//   isLive(p) · start(p) · stop(p) · toggle(p) · dismiss(p) (drops an ended/error entry) · checkRunning()
// At most one bridge per project. The state machine lives in lib/Model.js (editorDebugEvent), tested in
// tests/qml/tst_editor_debug.qml. On start-up it asks `debug editor-status` once, so a bridge still running
// (another window, or one from before a restart) shows on its card; such a bridge is re-checked every 15 s
// while the window is focused, and Stop ends it by its helper's pid (the bridge stops cleanly on SIGTERM).
// Our own streams end with {"cancel": true} on Stop and when the app quits.
Item {
  id: root
  required property var app

  property var entries: ({})           // project path → entry
  property int liveCount: 0

  readonly property int _endedShownMs: 20000

  function _path(p) { return p && p.path ? String(p.path) : String(p || "") }
  function forProject(p) { var path = _path(p); return path && root.entries[path] ? root.entries[path] : null }
  function label(e) { return Model.editorDebugLabel(e) }
  function isLive(p) { var e = forProject(p); return !!e && Model.editorDebugLabel(e).live }

  function _set(path, e) {
    var copy = Object.assign({}, root.entries)
    if (e) copy[path] = e
    else delete copy[path]
    root.entries = copy
    var n = 0
    for (var k in copy) if (Model.editorDebugLabel(copy[k]).live) n++
    root.liveCount = n
    if (e && (e.state === "ended")) endTimer.restart()
  }

  // ------------------------------------------------------------ actions
  function start(p) {
    var path = _path(p)
    if (!path || root.isLive(path)) return
    if (root._streams[path]) return
    var s = streamComp.createObject(root, { path: path })
    if (!s) { app.toast("Couldn't start iOS Studio's helper.", "error"); return }
    root._streams[path] = s
    _set(path, Model.editorDebugEntry(path, p && p.name ? p.name : path.split("/").pop()))
    s.start(["debug", "editor", "--project", path])
  }

  function stop(p) {
    var path = _path(p)
    var e = root.entries[path]
    if (!e || !Model.editorDebugLabel(e).live || e.state === "stopping") return
    _set(path, Object.assign({}, e, { state: "stopping" }))
    var s = root._streams[path]
    if (s) {
      if (!s.write({ cancel: true })) s.stop()
      s.killLater()
      return
    }
    // A bridge another process runs: its helper ends it cleanly on SIGTERM.
    if (e.helperPid > 0) Util.execArgv(["kill", "-TERM", String(e.helperPid)])
    soon.restart()
  }

  function toggle(p) { if (isLive(p)) stop(p); else start(p) }

  function dismiss(p) {
    var path = _path(p)
    var e = root.entries[path]
    if (e && !Model.editorDebugLabel(e).live) _set(path, null)
  }

  // Bridges running now (`debug editor-status`): adds the ones we don't follow, ends the ones that are gone.
  function checkRunning() {
    app.cli.call(["debug", "editor-status"], "", function(d) {
      var seen = {}
      var list = (d && d.bridges) || []
      for (var i = 0; i < list.length; i++) {
        var b = list[i]
        if (!b || !b.project) continue
        var path = String(b.project)
        seen[path] = true
        if (root._streams[path]) continue
        var pr = app.projects ? app.projects.projectByPath(path) : null
        var old = root.entries[path]
        var e = Model.editorDebugFromStatus(b, pr ? pr.name : "")
        if (old && old.state === "stopping") e.state = "stopping"
        if (!old || JSON.stringify(old) !== JSON.stringify(Object.assign({}, e, { at: old.at }))) _set(path, e)
      }
      for (var k in root.entries) {
        var o = root.entries[k]
        if (!o.owned && !seen[k] && Model.editorDebugLabel(o).live)
          _set(k, Object.assign({}, o, { state: "ended", ended: "cancelled", at: Date.now() }))
      }
    }, null, { timeout: 15000 })
  }

  // ------------------------------------------------------------ our streams
  property var _streams: ({})          // path → Stream (not a binding: changed in place)

  Component {
    id: streamComp
    Stream {
      id: s
      property string path: ""
      program: [root.app.helperPath]
      onEvent: function(ev) {
        var e = root.entries[s.path]
        if (!e) return
        var n = Model.editorDebugEvent(e, ev)
        if (n !== e) root._set(s.path, n)
      }
      onExited: function(code) {
        var e = root.entries[s.path]
        // Exited without a done event (crashed, or killed after Stop): end it here.
        if (e && Model.editorDebugLabel(e).live) {
          root._set(s.path, e.state === "stopping"
            ? Object.assign({}, e, { state: "ended", ended: "cancelled", at: Date.now() })
            : Model.editorDebugEvent(e, { event: "done", ok: false, error: s.lastDone && s.lastDone.error ? s.lastDone.error
                : { code: "exited", message: "Debug in editor stopped unexpectedly." } }))
        }
        delete root._streams[s.path]
        Qt.callLater(function() { s.destroy() })
      }
      // After {"cancel": true}, the helper releases the forward and ends; this only catches a hung one.
      function killLater() { kill.restart() }
      Timer { id: kill; interval: 20000; onTriggered: s.stop() }
    }
  }

  // An ended bridge shows for a while, then leaves the card (an error stays until dismissed or retried).
  Timer {
    id: endTimer
    interval: 2000
    repeat: true
    onTriggered: {
      var left = false, now = Date.now()
      for (var k in root.entries) {
        var e = root.entries[k]
        if (e.state !== "ended") continue
        if (now - e.at >= root._endedShownMs) root._set(k, null)
        else left = true
      }
      if (!left) endTimer.stop()
    }
  }

  // Bridges we don't follow ourselves change only on disk: ask again now and then, only while one shows
  // and the window is focused.
  readonly property bool _hasForeign: {
    for (var k in root.entries) { var e = root.entries[k]; if (!e.owned && Model.editorDebugLabel(e).live) return true }
    return false
  }
  Timer { id: soon; interval: 1500; onTriggered: root.checkRunning() }
  Timer {
    id: recheck
    interval: 15000
    repeat: true
    running: root._hasForeign && root.app.windowFocused
    onTriggered: root.checkRunning()
  }

  Component.onCompleted: checkRunning()
  Component.onDestruction: {
    for (var k in root._streams) { var s = root._streams[k]; if (s && s.running) s.write({ cancel: true }) }
  }
}
