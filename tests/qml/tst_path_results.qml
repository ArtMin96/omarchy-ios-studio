import QtQuick
import QtTest
import "../../services"

// services/PathResults.qml (per-project answers, the one-at-a-time queue, freshness, rejected answers).
// Run by tests/qml/test_qml_core.py through qmltestrunner.
TestCase {
  id: tc
  name: "PathResults"

  // A fake Cli: call() queues {argv, done, fail, opts}.
  QtObject {
    id: fakeCli
    property var calls: []
    function call(argv, stdin, done, fail, opts) { fakeCli.calls.push({ argv: argv, done: done, fail: fail, opts: opts }); return true }
  }

  Component { id: comp; PathResults { cli: fakeCli; argv: path => ["projects", "check", "--path", path] } }

  function init() { fakeCli.calls = [] }
  function make(props) { return createTemporaryObject(comp, tc, props || {}) }

  function test_runKeepsOldResultWhileLoading() {
    var r = make()
    var got = null
    r.run({ path: "/a" }, function(d) { got = d })
    compare(fakeCli.calls[0].argv, ["projects", "check", "--path", "/a"])
    compare(r.get("/a").state, "loading")
    fakeCli.calls[0].done({ n: 1 })
    compare(got, { n: 1 })
    compare(r.get({ path: "/a" }).state, "ready"); compare(r.get("/a").result, { n: 1 }); verify(r.get("/a").at > 0)
    r.run("/a")
    compare(r.get("/a").state, "loading"); compare(r.get("/a").result, { n: 1 })
    fakeCli.calls[1].fail({ code: "x", message: "nope" })
    compare(r.get("/a").state, "error"); compare(r.get("/a").error.code, "x"); compare(r.get("/a").result, { n: 1 })
  }

  function test_getUnknownAndEmpty() {
    var r = make()
    compare(r.get("/nope"), null); compare(r.get(null), null)
    r.run(""); compare(fakeCli.calls.length, 0)
  }

  function test_rejectsAndTimeOf() {
    var r = make({ rejects: d => d.ok === false ? { code: "missing", message: "no tool" } : null, timeOf: d => d.at })
    var err = null
    r.run("/a", null, function(e) { err = e })
    fakeCli.calls[0].done({ ok: false })
    compare(r.get("/a").state, "error"); compare(err.code, "missing")
    r.run("/a"); fakeCli.calls[1].done({ at: 1234 })
    compare(r.get("/a").at, 1234)
  }

  function test_soonQueuesOneAtATimeAndSkipsFresh() {
    var r = make()
    r.soon("/a"); r.soon("/b"); r.soon("/a")
    compare(fakeCli.calls.length, 1)                 // /b waits for /a; /a isn't queued twice
    fakeCli.calls[0].done({})
    tryCompare(fakeCli.calls, "length", 2)
    compare(fakeCli.calls[1].argv[3], "/b")
    fakeCli.calls[1].fail({ code: "x" })
    r.soon("/a"); r.soon("/b")                       // both answered just now: fresh, skipped
    wait(20)
    compare(fakeCli.calls.length, 2)
    r.freshMs = 0
    r.soon("/a")
    compare(fakeCli.calls.length, 3)
  }

  function test_putMerges() {
    var r = make()
    var seen = 0
    r.byPathChanged.connect(function() { seen++ })
    r.put("/a", { state: "ready", result: 1 })
    r.put("/a", { error: null })
    compare(r.get("/a").result, 1); compare(seen, 2)
  }
}
