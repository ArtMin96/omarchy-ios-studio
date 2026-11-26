import QtQuick
import QtTest
import "../../lib/Model.js" as Model

// Model.setupBlocking / setupUpdateCard: an update-only setup (the iOS build scripts checkout at an older commit,
// state "broken" + update: true) is ready with an update outside Setup too. SetupService keeps status "ready"
// for it (blocking is empty), AppState doesn't open Setup, and Home shows the calm update card instead of
// "Finish installing the tools". Run by tests/qml/test_qml_core.py.
TestCase {
  name: "HomeSetupUpdate"

  readonly property var update: ({ id: "apple-dev", title: "iOS build scripts", state: "broken", update: true })
  readonly property var update2: ({ id: "xtool", title: "xtool", state: "broken", update: true })
  readonly property var broken: ({ id: "xcspec", title: "Swift build settings fix", state: "broken" })
  readonly property var missing: ({ id: "swift", title: "Swift compiler", state: "missing" })

  function ids(l) { return l.map(function(s) { return s.id }) }

  function test_setupBlocking() {
    compare(ids(Model.setupBlocking([update])), [])                     // update only: nothing blocks
    compare(ids(Model.setupBlocking([update, missing])), ["swift"])
    compare(ids(Model.setupBlocking([broken, update])), ["xcspec"])     // a real fault still blocks
    compare(ids(Model.setupBlocking([missing, broken])), ["swift", "xcspec"])
    compare(ids(Model.setupBlocking([null, update])), [])               // step(id) found nothing
    compare(ids(Model.setupBlocking([])), [])
    compare(ids(Model.setupBlocking(null)), [])
  }

  function test_setupUpdateCard() {
    var one = Model.setupUpdateCard([update])
    compare(one.title, "An update is ready for the iOS build scripts")
    compare(one.text, "Everything keeps working as it is. Updating downloads the version iOS Studio is tested with "
                      + "and rebuilds only what changed.")
    var two = Model.setupUpdateCard([update, update2])
    compare(two.title, "2 updates are ready")
    verify(two.text.indexOf("iOS build scripts, xtool. Everything keeps working") === 0)
    // only update rows count; none -> no card (Home falls through to "All set" etc.)
    compare(Model.setupUpdateCard([update, missing]).title, "An update is ready for the iOS build scripts")
    compare(Model.setupUpdateCard([missing, broken]), null)
    compare(Model.setupUpdateCard([]), null)
    compare(Model.setupUpdateCard(null), null)
  }

  // The words carry no fault: no "broken", "repair", "finish" or "missing".
  function test_calmWording() {
    var c = Model.setupUpdateCard([update])
    var all = (c.title + " " + c.text).toLowerCase()
    verify(!/broken|repair|finish|missing|fail/.test(all), all)
  }
}
