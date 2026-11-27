import QtQuick
import QtTest
import "../../lib/Model.js" as Model

// lib/Model.js editorDebugEvent keeps a notice's fix (no-symbols: Download symbols), so the project card's Debug in
// editor strip (components/projects/EditorDebugBar.qml) shows it as a button; Model.debugNoticeFix picks it per row.
TestCase {
  name: "EditorDebugNoticeFix"

  readonly property var symbolsFix: ({ label: "Download symbols", page: "device", tab: "debug", action: "symbols" })

  function notice(id, fix) {
    var ev = { event: "notice", id: id, level: "info", message: "m " + id }
    if (fix !== undefined) ev.fix = fix
    return ev
  }

  function test_noticeKeepsItsFix() {
    var e = Model.editorDebugEvent(Model.editorDebugEntry("/p/CrashLab", "CrashLab"), notice("no-symbols", symbolsFix))
    compare(e.notices.length, 1)
    compare(e.notices[0].fix, symbolsFix)
    compare(e.notices[0].fix.action, "symbols")
    compare(Model.debugNoticeFix(e.notices, 0).label, "Download symbols")
  }

  function test_noFixOrAnUnusableOneIsNull() {
    var e = Model.editorDebugEntry("/p/A", "A")
    e = Model.editorDebugEvent(e, notice("no-local-build"))
    e = Model.editorDebugEvent(e, notice("bad-1", { page: "device" }))       // no label
    e = Model.editorDebugEvent(e, notice("bad-2", { label: "Do it" }))       // nowhere to go
    e = Model.editorDebugEvent(e, notice("bad-3", "Download symbols"))       // not an object
    compare(e.notices.length, 4)
    for (var i = 0; i < 4; i++) {
      compare(e.notices[i].fix, null)
      compare(Model.debugNoticeFix(e.notices, i), null)
    }
    var u = Model.editorDebugEvent(e, notice("docs", { label: "Docs", url: "https://example.com" }))
    compare(u.notices[4].fix.url, "https://example.com")
  }

  function test_aRepeatedNoticeReplacesItsFix() {
    var e = Model.editorDebugEntry("/p/A", "A")
    e = Model.editorDebugEvent(e, notice("no-symbols", symbolsFix))
    e = Model.editorDebugEvent(e, notice("no-symbols"))
    compare(e.notices.length, 1)
    compare(e.notices[0].fix, null)
  }

  function test_theSameFixShowsOnce() {
    var e = Model.editorDebugEntry("/p/A", "A")
    e = Model.editorDebugEvent(e, notice("no-symbols", symbolsFix))
    e = Model.editorDebugEvent(e, notice("other", symbolsFix))
    compare(Model.debugNoticeFix(e.notices, 0).page, "device")
    compare(Model.debugNoticeFix(e.notices, 1), null)
  }

  function test_theFixSurvivesReadyAndEditorEvents() {
    var e = Model.editorDebugEvent(Model.editorDebugEntry("/p/A", "A"), notice("no-symbols", symbolsFix))
    e = Model.editorDebugEvent(e, { event: "ready", port: 1, pid: 2, editors: ["vscode"] })
    e = Model.editorDebugEvent(e, { event: "editor", state: "attached" })
    compare(e.notices[0].fix.label, "Download symbols")
  }
}
