import QtQuick
import QtTest
import "../../core"

// The "last outcome" view of a Resource that DeviceInspectView (treeStatus, auditStatus) and
// DeviceDebugView rely on: busy -> "loading", never loaded -> "idle", the last fetch failed -> "error"
// (the data read before stays), else "ready". A success after a failure clears the error.
// Run by tests/qml/test_qml_core.py through qmltestrunner.
TestCase {
  id: tc
  name: "ResourceLastOutcome"

  property var pending: []
  function fakeFetch(done, fail) { tc.pending.push({ done: done, fail: fail }) }

  Component {
    id: comp
    Item {
      property alias res: r
      Resource { id: r; fetch: tc.fakeFetch }
      // same expression as DeviceInspectView.treeStatus
      readonly property string outcome: r.busy ? "loading" : r.status === "idle" ? "idle" : r.error ? "error" : "ready"
    }
  }

  function init() { tc.pending = [] }

  function test_outcomeFollowsTheLastFetch() {
    var v = createTemporaryObject(comp, tc)
    compare(v.outcome, "idle")
    v.res.load(true); compare(v.outcome, "loading")
    tc.pending[0].done({ elements: [1, 2] }); compare(v.outcome, "ready")
    v.res.load(true); compare(v.outcome, "loading")
    tc.pending[1].fail({ code: "x", message: "no" })
    compare(v.outcome, "error")
    compare(v.res.data.elements.length, 2)                 // the earlier read stays listed
    v.res.load(true); compare(v.outcome, "loading")        // retrying hides the error
    tc.pending[2].done({ elements: [] })
    compare(v.outcome, "ready"); compare(v.res.error, null)
  }

  function test_firstFetchFails() {
    var v = createTemporaryObject(comp, tc)
    v.res.load(true); tc.pending[0].fail(null)
    compare(v.outcome, "error"); verify(v.res.error !== null)
  }

  function test_aReadInFlightIsNotStartedTwice() {
    var v = createTemporaryObject(comp, tc)
    verify(v.res.load(true)); verify(!v.res.load(true))
    compare(tc.pending.length, 1)
  }
}
