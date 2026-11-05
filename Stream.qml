import QtQuick
import Quickshell
import Quickshell.Io

// Long-running helper process that speaks JSON lines (install steps, builds,
// login flows, the device daemon).
//
//   Stream {
//     id: build
//     onEvent: function(ev) { if (ev.event === "progress") bar.value = ev.fraction }
//     onFinished: function(ok, data, error) { ... }        // from {"event":"done"}
//   }
//   build.start(["build", "run", "--project", path])
//   build.write({ prompt: "p1", value: code })              // answer a prompt
//   build.stop()
//
// Non-JSON stdout lines and every stderr line arrive as {event: "log", text, stream}.
// Set `program` to another argv prefix (e.g. bin/iosstudio-deviced) for other
// line-protocol processes. `autoRestart` restarts with 1 s → 30 s backoff when
// the process exits on its own (never after stop()).
Item {
  id: root
  // Non-visual: hidden so a Layout it sits in doesn't give it a slot and a gap.
  visible: false

  property var program: [decodeURIComponent(String(Qt.resolvedUrl("bin/iosstudio-helper")).replace(/^file:\/\//, ""))]
  property var args: []
  property bool autoRestart: false
  property int maxLineLength: 512 * 1024
  // Written to stdin before terminating on stop(), e.g. {cmd: "quit"}.
  property var quitMessage: null
  property var environment: ({})

  readonly property bool running: proc.running
  property int exitCode: -1
  property var lastDone: null       // the last {"event":"done"} object
  property int restarts: 0

  signal event(var obj)
  signal finished(bool ok, var data, var error)
  signal exited(int code)

  property bool _stopping: false
  property int _backoff: 1000

  function start(argv) {
    if (argv !== undefined) root.args = argv
    root._stopping = false
    root.lastDone = null
    root.exitCode = -1
    if (proc.running) { proc.running = false; Qt.callLater(root._launch); return }
    root._launch()
  }

  function _launch() {
    proc.command = root.program.concat(root.args || [])
    proc.environment = root.environment
    proc.running = true
  }

  function stop() {
    root._stopping = true
    backoff.stop()
    if (!proc.running) return
    if (root.quitMessage) {
      root.write(root.quitMessage)
      killTimer.restart()
    } else {
      proc.running = false
    }
  }

  function restart() { root.stop(); Qt.callLater(function() { root.start() }) }

  // Write one JSON line (objects are serialised; strings are sent as-is).
  function write(obj) {
    if (!proc.running) return false
    var line = typeof obj === "string" ? obj : JSON.stringify(obj)
    proc.write(line + "\n")
    return true
  }

  function _line(raw, stream) {
    var line = String(raw)
    if (line.length > root.maxLineLength) line = line.substring(0, root.maxLineLength)
    if (line.trim() === "") return
    var obj = null
    if (stream === "stdout" && line.charAt(0) === "{") {
      try { obj = JSON.parse(line) } catch (e) { obj = null }
    }
    if (!obj || typeof obj !== "object") obj = { event: "log", text: line, stream: stream }
    root.event(obj)
    if (obj.event === "done") {
      root.lastDone = obj
      root.finished(obj.ok === true, obj.data || {}, obj.error || null)
    }
  }

  Process {
    id: proc
    stdinEnabled: true
    stdout: SplitParser { onRead: function(line) { root._line(line, "stdout") } }
    stderr: SplitParser { onRead: function(line) { root._line(line, "stderr") } }

    onRunningChanged: if (running) { stableTimer.restart() }

    onExited: function(code) {
      root.exitCode = code
      killTimer.stop()
      stableTimer.stop()
      root.exited(code)
      if (!root.lastDone && !root._stopping && !root.autoRestart && code !== 0)
        root.finished(false, {}, { code: "exited", message: "The process stopped unexpectedly (exit code " + code + ")." })
      if (root.autoRestart && !root._stopping) {
        backoff.interval = root._backoff
        root._backoff = Math.min(30000, root._backoff * 2)
        backoff.restart()
      }
    }
  }

  // Reset the backoff once a run has survived for a while.
  Timer { id: stableTimer; interval: 20000; onTriggered: root._backoff = 1000 }
  Timer { id: backoff; onTriggered: { root.restarts++; root._launch() } }
  Timer { id: killTimer; interval: 800; onTriggered: proc.running = false }

  Component.onDestruction: {
    root._stopping = true
    if (proc.running) {
      if (root.quitMessage) root.write(root.quitMessage)
      proc.running = false
    }
  }
}
