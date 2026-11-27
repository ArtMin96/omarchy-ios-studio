import QtQuick
import QtTest
import "../../lib/Model.js" as Model

// Model.isSetupUpdate / setupUpdatesOnly / setupAutoText: an outdated iOS build scripts checkout (state "broken",
// update: true) reads as an update on the Setup page, not as a failure. Run by tests/qml/test_qml_core.py.
TestCase {
  name: "ModelSetupUpdate"

  readonly property var update: ({ id: "apple-dev", title: "iOS build scripts", state: "broken", update: true })
  readonly property var broken: ({ id: "xtool", title: "xtool", state: "broken" })
  readonly property var missing: ({ id: "swift", title: "Swift compiler", state: "missing" })

  function test_isSetupUpdate() {
    verify(Model.isSetupUpdate(update))
    verify(!Model.isSetupUpdate(broken))
    verify(!Model.isSetupUpdate(missing))
    verify(!Model.isSetupUpdate({ state: "done", update: true }))     // updated: done, whatever the flag says
    verify(!Model.isSetupUpdate({ state: "broken", update: false }))
    verify(!Model.isSetupUpdate(null))
  }

  function test_setupUpdatesOnly() {
    verify(Model.setupUpdatesOnly([update]))
    verify(!Model.setupUpdatesOnly([update, missing]))
    verify(!Model.setupUpdatesOnly([broken]))
    verify(!Model.setupUpdatesOnly([]))
    verify(!Model.setupUpdatesOnly(null))
  }

  function test_setupAutoText() {
    compare(Model.setupAutoText([update], "a few minutes"), "An update to the iOS build scripts installs by itself (a few minutes, mostly waiting)")
    compare(Model.setupAutoText([update, update], "a few minutes"), "2 updates install by themselves (a few minutes, mostly waiting)")
    compare(Model.setupAutoText([update, missing], "about 15–25 minutes"), "2 pieces install by themselves (about 15–25 minutes, mostly waiting)")
    compare(Model.setupAutoText([missing], "a few minutes"), "1 piece installs by itself (a few minutes, mostly waiting)")
    compare(Model.setupAutoText([], "a few minutes"), "")
  }
}
