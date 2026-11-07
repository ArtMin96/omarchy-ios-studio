import QtQuick
import QtTest
import "../.."
import "../../Model.js" as Model

// Resource.qml (loading state, ttl, de-dup, stale data on error, reset) and Model.clock/elapsedSeconds.
// Run by tests/qml/test_qml_core.py through qmltestrunner.
TestCase {
  id: tc
  name: "Resource"

  property var pending: []       // queued {done, fail} of the fake backend
  property int calls: 0
  function fakeFetch(done, fail) { tc.calls++; tc.pending.push({ done: done, fail: fail }) }

  Component { id: resComp; Resource { fetch: tc.fakeFetch } }

  function init() { tc.pending = []; tc.calls = 0 }
  function make(props) { return createTemporaryObject(resComp, tc, props || {}) }

  function test_firstLoadThenReady() {
    var r = make({ initial: [] })
    compare(r.status, "idle"); compare(r.data, [])
    verify(r.load())
    compare(r.status, "loading"); verify(r.busy)
    tc.pending[0].done([1, 2])
    compare(r.status, "ready"); compare(r.data, [1, 2]); verify(!r.busy); verify(r.loadedAt > 0)
  }

  function test_concurrentLoadsAreDeduped() {
    var r = make()
    verify(r.load()); verify(!r.load()); verify(!r.load(true))
    compare(tc.calls, 1)
  }

  function test_ttlSkipsFreshLoadsButForceAndInvalidateDont() {
    var r = make({ ttl: 60000 })
    r.load(); tc.pending[0].done("a")
    verify(!r.load()); compare(tc.calls, 1)
    verify(r.load(true)); tc.pending[1].done("b")
    r.invalidate(); verify(r.load()); compare(tc.calls, 3)
  }

  function test_ttlZeroAlwaysReloads() {
    var r = make()
    r.load(); tc.pending[0].done("a")
    verify(r.load()); compare(r.status, "ready"); verify(r.busy)     // reload keeps showing the data
  }

  function test_errorWithoutDataThenStaleKeptOnError() {
    var r = make()
    r.load(); tc.pending[0].fail({ code: "x", message: "boom" })
    compare(r.status, "error"); compare(r.error.message, "boom")
    r.load(); tc.pending[1].done("data")
    compare(r.status, "ready"); compare(r.error, null)
    r.load(); tc.pending[2].fail({ code: "y", message: "again" })
    compare(r.status, "ready"); compare(r.data, "data"); compare(r.error.code, "y")
  }

  function test_inactiveNeverLoads() {
    var r = make({ active: false })
    verify(!r.load(true)); compare(tc.calls, 0)
  }

  function test_autoLoadWhenActivated() {
    var r = make({ active: false, autoLoad: true })
    r.active = true
    tryCompare(tc, "calls", 1)
  }

  function test_resetDropsLateReplies() {
    var r = make({ initial: [] })
    r.load()
    r.reset()
    tc.pending[0].done(["old device"])
    compare(r.status, "idle"); compare(r.data, []); verify(!r.busy)
    verify(r.load()); compare(tc.calls, 2)
  }

  function test_setCountsAsLoaded() {
    var r = make({ ttl: 60000 })
    r.set({ n: 1 })
    compare(r.status, "ready"); verify(!r.load())
  }

  function test_clock() {
    compare(Model.clock(0), "0:00")
    compare(Model.clock(7.9), "0:07")
    compare(Model.clock(299), "4:59")
    compare(Model.clock(3723), "1:02:03")
    compare(Model.clock(-3), "0:00")
  }

  function test_elapsedSeconds() {
    var now = 1800000000000                                       // ms
    compare(Model.elapsedSeconds(now - 5000, now), 5)
    compare(Model.elapsedSeconds(now / 1000 - 10, now), 10)        // seconds epoch
    compare(Model.elapsedSeconds(now + 5000, now), 0)
    verify(isNaN(Model.elapsedSeconds(null)))
  }
}
