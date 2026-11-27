import QtQuick
import QtTest
import "../../lib/Model.js" as Model

// An older iOS build scripts checkout on the Toolchain page: the Installed item (status "update") and the Updates
// row for the same checkout are one update (Model.updateOffered, Model.toolchainUpdateIds). Run by
// tests/qml/test_qml_core.py.
TestCase {
  name: "ToolchainUpdateIds"

  function st(components) { return { channel: "tested", components: components } }
  function c(id, state, extra) {
    var o = { id: id, name: id, state: state, applicable: true }
    for (var k in extra || {}) o[k] = extra[k]
    return o
  }

  function test_updateOffered() {
    var s = st([c("apple-dev", "update"), c("ipsw", "current"), c("xtool", "blocked")])
    verify(Model.updateOffered(s, "apple-dev"))
    verify(!Model.updateOffered(s, "ipsw"))
    verify(!Model.updateOffered(s, "xtool"))                                  // blocked: not a row with a button
    verify(!Model.updateOffered(st([c("apple-dev", "update", { applicable: false })]), "apple-dev"))
    verify(!Model.updateOffered(null, "apple-dev"))
  }

  function test_toolchainUpdateIds_countsAPartOnce() {
    var items = [{ id: "apple-dev", status: "update" }, { id: "swift", status: "ok" }]
    compare(Model.toolchainUpdateIds(items, st([c("apple-dev", "update"), c("ipsw", "update")])), ["apple-dev", "ipsw"])
    // before the first `updates status` (or with the Updates check failing) the Installed item still counts
    compare(Model.toolchainUpdateIds(items, null), ["apple-dev"])
    compare(Model.toolchainUpdateIds([], st([c("ipsw", "update")])), ["ipsw"])
    compare(Model.toolchainUpdateIds(null, null), [])
  }
}
