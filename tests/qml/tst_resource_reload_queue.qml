import QtQuick
import QtTest
import "../../core"

// Resource.load(true) while a fetch is in flight: exactly one more fetch runs after it settles (the
// running one may predate the change that asked for the reload). A plain load() while busy is still a
// no-op, and reset() drops the queued reload. Run by tests/qml/test_qml_core.py through qmltestrunner.
TestCase {
  id: tc
  name: "ResourceReloadQueue"

  property var pending: []
  property int calls: 0
  function fakeFetch(done, fail) { tc.calls++; tc.pending.push({ done: done, fail: fail }) }

  Component { id: resComp; Resource { fetch: tc.fakeFetch } }

  function init() { tc.pending = []; tc.calls = 0 }
  function make(props) { return createTemporaryObject(resComp, tc, props || {}) }

  function test_forceWhileBusyRunsOnceMoreAfterSuccess() {
    var r = make()
    verify(r.load())
    verify(!r.load(true)); verify(!r.load(true)); verify(!r.load(true))   // many asks, one follow-up
    compare(tc.calls, 1)
    tc.pending[0].done("old")
    compare(r.data, "old")
    tryCompare(tc, "calls", 2)
    verify(r.busy)
    tc.pending[1].done("new")
    compare(r.data, "new")
    wait(20)
    compare(tc.calls, 2)                                               // no third fetch
  }

  function test_forceWhileBusyRunsOnceMoreAfterFailure() {
    var r = make()
    r.load()
    r.load(true)
    tc.pending[0].fail({ code: "x", message: "boom" })
    tryCompare(tc, "calls", 2)
    tc.pending[1].done("ok")
    compare(r.status, "ready"); compare(r.error, null)
  }

  function test_plainLoadWhileBusyIsNotQueued() {
    var r = make()
    r.load(); verify(!r.load())
    tc.pending[0].done("a")
    wait(20)
    compare(tc.calls, 1)
  }

  function test_resetDropsQueuedReload() {
    var r = make()
    r.load(); r.load(true)
    r.reset()
    tc.pending[0].done("late")
    wait(20)
    compare(tc.calls, 1); compare(r.status, "idle")
  }
}
