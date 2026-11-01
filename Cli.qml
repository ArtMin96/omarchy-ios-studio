import QtQuick
import Quickshell
import Quickshell.Io

// One-shot helper calls. Each call spawns its own Process, so calls never
// queue behind each other and a slow `account ds` listing can't block a fast
// `device info`.
//
//   Cli { id: cli }
//   cli.call(["toolchain", "status"], "", function(data) { ... }, function(err) { ... })
//   cli.call(["account", "signin"], JSON.stringify({ password: pw }), onOk, onErr)   // secrets via stdin
//
// The helper prints exactly one JSON object: {"ok": true, "data": …} or
// {"ok": false, "error": {code, message, fix?, detail?}}. onFail always gets
// an error object of that shape, even when the process crashed or printed
// garbage, so callers can hand it straight to <ErrorState error: …>.
//
// opts (optional 5th arg): { timeout: ms (default 120000), program: [argv prefix] }
Item {
  id: root

  // argv prefix. Defaults to bin/iosstudio-helper next to this file.
  property var program: [decodeURIComponent(String(Qt.resolvedUrl("bin/iosstudio-helper")).replace(/^file:\/\//, ""))]
  readonly property int busy: _inflight
  property int _inflight: 0

  // Also emitted for every call, for callers that prefer signals.
  signal done(var data, var args)
  signal failed(var error, var args)

  function call(args, stdinText, onDone, onFail, opts) {
    var o = opts || {}
    var p = procComp.createObject(root, {
      args: args || [],
      payload: stdinText ? String(stdinText) : "",
      onDoneCb: onDone || null,
      onFailCb: onFail || null,
      timeoutMs: o.timeout || 120000
    })
    if (!p) {
      var err = { code: "internal", message: "Couldn't start iOS Studio's helper." }
      if (onFail) onFail(err)
      root.failed(err, args)
      return false
    }
    p.command = (o.program || root.program).concat(args || [])
    p.stdinEnabled = p.payload !== ""
    root._inflight++
    p.running = true
    p.guardTimer.start()
    return true
  }

  // Parses the helper's stdout. Tolerates stray lines before the payload.
  function parse(raw) {
    var text = String(raw || "").trim()
    if (text === "") return null
    try { return JSON.parse(text) } catch (e) {}
    var lines = text.split("\n")
    for (var i = lines.length - 1; i >= 0; i--) {
      var l = lines[i].trim()
      if (l.charAt(0) !== "{") continue
      try { return JSON.parse(l) } catch (e2) {}
    }
    return null
  }

  function _tail(text) {
    var lines = String(text || "").trim().split("\n")
    for (var i = lines.length - 1; i >= 0; i--) {
      var l = lines[i].trim()
      if (l) return l.length > 300 ? l.substring(0, 297) + "…" : l
    }
    return ""
  }

  Component {
    id: procComp

    Process {
      id: p

      property var args: []
      property string payload: ""
      property var onDoneCb: null
      property var onFailCb: null
      property int timeoutMs: 120000
      property string out: ""
      property bool gotOut: false
      property bool gotExit: false
      property int code: -1
      property bool settled: false
      property bool timedOut: false

      stdout: StdioCollector {
        waitForEnd: true
        onStreamFinished: { p.out = String(text || ""); p.gotOut = true; p.finish() }
      }
      stderr: StdioCollector { id: errOut; waitForEnd: true }

      onStarted: {
        if (p.payload !== "") {
          p.write(p.payload)
          p.payload = ""
          p.stdinEnabled = false
        }
      }

      onExited: function(exitCode) {
        p.code = exitCode
        p.gotExit = true
        p.finish()
        if (!p.settled) p.settleTimer.start()
      }

      function finish() {
        if (p.settled) return
        if (!p.timedOut && (!p.gotOut || !p.gotExit)) return
        p.settled = true
        p.guardTimer.stop()
        p.settleTimer.stop()
        root._inflight = Math.max(0, root._inflight - 1)

        var parsed = root.parse(p.out)
        if (parsed && parsed.ok === true) {
          var data = parsed.data === undefined ? {} : parsed.data
          if (p.onDoneCb) { try { p.onDoneCb(data) } catch (e) { console.warn("Cli onDone threw:", e, JSON.stringify(p.args)) } }
          root.done(data, p.args)
        } else {
          var err
          if (parsed && parsed.error) err = (typeof parsed.error === "object") ? parsed.error : { code: "failed", message: String(parsed.error) }
          else if (p.timedOut) err = { code: "timeout", message: "That took too long and was stopped." }
          else err = { code: "bad-output", message: "iOS Studio's helper stopped unexpectedly.", detail: root._tail(errOut.text) || ("exit code " + p.code) }
          if (p.onFailCb) { try { p.onFailCb(err) } catch (e2) { console.warn("Cli onFail threw:", e2, JSON.stringify(p.args)) } }
          root.failed(err, p.args)
        }
        Qt.callLater(function() { p.destroy() })
      }

      // Exit without the stdout stream ending (exec failure): settle anyway.
      property Timer settleTimer: Timer {
        interval: 1500
        onTriggered: { p.gotOut = true; p.finish() }
      }
      property Timer guardTimer: Timer {
        interval: p.timeoutMs
        onTriggered: {
          if (p.settled) return
          p.timedOut = true
          p.running = false
          p.finish()
        }
      }
    }
  }
}
