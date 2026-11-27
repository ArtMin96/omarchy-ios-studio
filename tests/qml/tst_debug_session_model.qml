import QtQuick
import QtTest
import "../../lib/Model.js" as Model

// The iPhone › Debug session panel's helpers (components/device/DebugSessionPanel.qml): a variable's value text
// (unread ones), which notice shows a fix button, and late locals of an earlier stop. Run by
// tests/qml/test_qml_core.py.
TestCase {
  name: "DebugSessionModel"

  readonly property var symbolsFix: ({ label: "Download debug symbols", page: "device", tab: "debug" })

  function test_valueText() {
    compare(Model.debugValueText({ name: "n", type: "Int", value: "3", childCount: 0 }), "= 3")
    compare(Model.debugValueText({ name: "s", type: "String", value: "", childCount: 0 }), "= ")
    compare(Model.debugValueText({ name: "x", type: "Foo", error: "unavailable", childCount: 0 }), "unavailable")
    compare(Model.debugValueText({ name: "x", type: "Foo", childCount: 0 }), "")
    compare(Model.debugValueText(null), "")
  }

  function test_unreadReadsNotReadYet() {
    // the helper's shape for a value past the stop's read budget: name and type only
    compare(Model.debugValueText({ name: "days", type: "[Day]", childCount: 0, unread: true }), "not read yet")
    compare(Model.debugValueText({ name: "d", type: "Day", value: "stale", unread: true }), "not read yet")
  }

  function test_noticeFixOncePerLabel() {
    var notices = [
      { id: "no-symbols", level: "info", message: "a", fix: symbolsFix },
      { id: "lldb-plain", level: "warn", message: "b" },
      { id: "other", level: "info", message: "c", fix: symbolsFix },
      { id: "stale-modules", level: "warn", message: "d", fix: { label: "Run the app", page: "projects", action: "run", path: "/p" } }
    ]
    compare(Model.debugNoticeFix(notices, 0).label, "Download debug symbols")
    compare(Model.debugNoticeFix(notices, 1), null)
    compare(Model.debugNoticeFix(notices, 2), null)        // the same fix is on the first notice already
    compare(Model.debugNoticeFix(notices, 3).page, "projects")
  }

  function test_noticeFixNeedsLabelAndTarget() {
    compare(Model.debugNoticeFix([{ fix: { page: "device" } }], 0), null)
    compare(Model.debugNoticeFix([{ fix: { label: "Do it" } }], 0), null)
    compare(Model.debugNoticeFix([{ fix: { label: "Docs", url: "https://example.com" } }], 0).url, "https://example.com")
    compare(Model.debugNoticeFix([], 0), null)
    compare(Model.debugNoticeFix(null, 2), null)
  }

  function test_variablesForStop() {
    var stop = { stopId: 4 }
    verify(Model.debugVariablesForStop(stop, { stopId: 4, variables: [] }))
    verify(!Model.debugVariablesForStop(stop, { stopId: 3, variables: [] }))    // late locals of the last stop
    verify(Model.debugVariablesForStop(stop, { requestId: "r1", variables: [] }))
    verify(Model.debugVariablesForStop(null, { stopId: 1, variables: [] }))
    verify(!Model.debugVariablesForStop(stop, null))
  }
}
