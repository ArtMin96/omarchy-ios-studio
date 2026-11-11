import QtQuick
import QtTest
import "../../lib/Model.js" as Model

// lib/Model.js editorDebug*: the state machine behind services/EditorDebugService.qml (the `debug editor`
// stream's events → the entry a project card shows) and the bar's text.
TestCase {
  name: "EditorDebug"

  function feed(e, events) {
    for (var i = 0; i < events.length; i++) e = Model.editorDebugEvent(e, events[i])
    return e
  }
  function ready(extra) {
    return Object.assign({ event: "ready", bundleId: "XTL-A1.com.example.crashlab", appName: "CrashLab", port: 52917,
                           pid: 400, launched: false, editors: ["vscode"],
                           configName: "Attach to CrashLab on iPhone (iOS Studio)" }, extra || {})
  }

  function test_startingShowsTheRunningStep() {
    var e = Model.editorDebugEntry("/p/CrashLab", "CrashLab")
    compare(e.state, "starting"); verify(e.owned)
    e = feed(e, [{ event: "step", id: "lldb", title: "Checking the debugger", status: "running" },
                 { event: "step", id: "lldb", title: "Checking the debugger", status: "done" },
                 { event: "step", id: "forward", title: "Starting the debugger on your iPhone", status: "running" }])
    compare(e.step, "Starting the debugger on your iPhone")
    var l = Model.editorDebugLabel(e)
    compare(l.level, "busy"); compare(l.text, "Starting the debugger on your iPhone…"); verify(l.live)
  }

  function test_readyAttachedDetached() {
    var e = feed(Model.editorDebugEntry("/p/CrashLab", "CrashLab"), [ready()])
    compare(e.state, "ready"); compare(e.port, 52917); compare(e.editors, ["vscode"]); compare(e.step, "")
    var l = Model.editorDebugLabel(e)
    compare(l.text, "Waiting for VS Code · press F5 on “Attach to CrashLab on iPhone (iOS Studio)”")
    compare(l.detail, "")
    e = feed(e, [{ event: "editor", state: "attached", connections: 1 }])
    compare(e.state, "attached"); verify(e.attached)
    compare(Model.editorDebugLabel(e).text, "VS Code is attached · the app pauses at breakpoints")
    compare(Model.editorDebugLabel(e).level, "ok")
    e = feed(e, [{ event: "editor", state: "detached", connections: 0 }])
    compare(e.state, "detached"); verify(Model.editorDebugLabel(e).live)
    verify(Model.editorDebugLabel(e).text.indexOf("to attach again") > 0)
    e = feed(e, [{ event: "forward", port: 60001, host: "127.0.0.1" }])
    compare(e.port, 60001); compare(e.state, "detached")
  }

  function test_launchedPausedSaysItWaits() {
    var e = feed(Model.editorDebugEntry("/p/CrashLab", "CrashLab"),
                 [{ event: "notice", id: "waiting", level: "info", message: "CrashLab is started and waits…" }, ready({ launched: true })])
    compare(e.notices.length, 0)          // "waiting" is what the ready line already says
    compare(Model.editorDebugLabel(e).detail, "CrashLab started paused and waits for the debugger.")
  }

  function test_editorWordingFollowsTheConfiguredEditors() {
    var zed = feed(Model.editorDebugEntry("/p/A", "A"), [ready({ editors: ["zed"], configName: "Attach to A on iPhone (iOS Studio)" })])
    compare(Model.editorDebugLabel(zed).text, "Waiting for Zed · start “Attach to A on iPhone (iOS Studio)” from the debug panel")
    var both = feed(Model.editorDebugEntry("/p/A", "A"), [ready({ editors: ["vscode", "zed"] }), { event: "editor", state: "attached" }])
    compare(Model.editorDebugLabel(both).text, "Your editor is attached · the app pauses at breakpoints")
  }

  function test_noticesKeepOnePerIdAndACap() {
    var e = Model.editorDebugEntry("/p/A", "A")
    e = feed(e, [{ event: "notice", id: "no-symbols", level: "info", message: "one" },
                 { event: "notice", id: "no-symbols", level: "info", message: "two" },
                 { event: "notice", id: "no-local-build", level: "warn", message: "build" }])
    compare(e.notices.length, 2); compare(e.notices[0].message, "two"); compare(e.notices[1].level, "warn")
    for (var i = 0; i < 10; i++) e = Model.editorDebugEvent(e, { event: "notice", id: "k" + i, message: "m" })
    compare(e.notices.length, 6)
  }

  function test_doneEndsCalmly() {
    var e = feed(Model.editorDebugEntry("/p/A", "A"), [ready({ launched: true, appName: "A" }),
      { event: "done", ok: true, data: { ended: "cancelled", attached: false, killed: true } }])
    compare(e.state, "ended"); compare(e.ended, "cancelled"); verify(e.killed)
    var l = Model.editorDebugLabel(e)
    compare(l.text, "Stopped debugging in the editor"); verify(!l.live)
    compare(l.detail, "A was closed, since no editor attached.")
    var x = feed(Model.editorDebugEntry("/p/A", "A"), [ready({ appName: "A" }), { event: "done", ok: true, data: { ended: "app-exited" } }])
    compare(Model.editorDebugLabel(x).text, "A exited, so debugging in the editor ended")
  }

  function test_errorKeepsItsFix() {
    var err = { code: "not-installed", message: "A isn't installed on your iPhone. Run it from iOS Studio first.",
                fix: { label: "Open Projects", page: "projects" } }
    var e = feed(Model.editorDebugEntry("/p/A", "A"), [{ event: "done", ok: false, error: err }])
    compare(e.state, "error"); compare(e.error.fix.label, "Open Projects")
    var l = Model.editorDebugLabel(e)
    compare(l.level, "error"); compare(l.text, err.message); verify(!l.live)
    var c = feed(Model.editorDebugEntry("/p/A", "A"), [{ event: "done", ok: false, error: { code: "cancelled", message: "x" } }])
    compare(c.state, "ended")
  }

  function test_stoppingIgnoresEditorChanges() {
    var e = feed(Model.editorDebugEntry("/p/A", "A"), [ready()])
    e = Object.assign({}, e, { state: "stopping" })
    compare(Model.editorDebugEvent(e, { event: "editor", state: "attached" }).state, "stopping")
    compare(Model.editorDebugLabel(e).text, "Stopping…")
    compare(Model.editorDebugEvent(e, { event: "done", ok: true, data: { ended: "cancelled" } }).state, "ended")
  }

  function test_unknownEventsAndOldEntryUnchanged() {
    var e = Model.editorDebugEntry("/p/A", "A")
    var n = Model.editorDebugEvent(e, { event: "log", text: "x" })
    compare(n.state, "starting")
    var r = Model.editorDebugEvent(e, ready())
    compare(e.state, "starting"); compare(r.state, "ready")
    compare(Model.editorDebugEvent(null, ready()), null)
    compare(Model.editorDebugLabel(null), null)
  }

  function test_fromStatus() {
    var e = Model.editorDebugFromStatus({ bundleId: "com.example.a", installedBundleId: "XTL-A1.com.example.a", appName: "A",
                                          project: "/p/A", port: 5000, pid: 9, launched: false, editor: "attached", helperPid: 4242 })
    compare(e.path, "/p/A"); compare(e.state, "attached"); verify(!e.owned); compare(e.helperPid, 4242)
    compare(e.bundleId, "XTL-A1.com.example.a"); verify(Model.editorDebugLabel(e).live)
    compare(Model.editorDebugFromStatus({ project: "/p/B", appName: "B", editor: "waiting" }).state, "ready")
  }
}
