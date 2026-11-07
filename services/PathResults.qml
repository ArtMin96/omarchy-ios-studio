import QtQuick

// One helper answer per project path: { state: "loading"|"ready"|"error", result, error, at }. The map is
// reassigned (not mutated), so bindings that read it through get() update. run(p) asks now and keeps the
// previous result while it runs; soon(p) queues it, one at a time, skipped while it runs or when fresh.
// ProjectService keeps its project checks, editor status and lint results in these:
//   PathResults { id: checks; cli: app.cli; argv: path => ["projects", "check", "--path", path] }
//   checks.get(p) · checks.run(p, onDone, onFail) · checks.soon(p) · checks.put(path, fields)
QtObject {
  id: cache
  property var cli: null
  property var argv: null               // function(path) → helper argv
  property int timeout: 20000
  property var rejects: null            // optional function(answer) → an error object when the answer is a failure
  property var timeOf: null             // optional function(answer) → ms it's from (default: now)
  property int freshMs: 60000
  property var byPath: ({})
  property var _queue: []
  property bool _busy: false

  function pathOf(p) { return p && p.path ? p.path : (p || "") }
  function get(p) { var path = pathOf(p); return path ? (cache.byPath[path] || null) : null }
  function put(path, fields) {
    var all = Object.assign({}, cache.byPath)
    all[path] = Object.assign({}, all[path] || {}, fields)
    cache.byPath = all
  }
  function run(p, onDone, onFail) {
    var path = pathOf(p)
    if (!path) return
    cache.put(path, { state: "loading", error: null })
    var failed = function(e) {
      cache.put(path, { state: "error", error: e, at: Date.now() })
      if (onFail) onFail(e)
    }
    cache.cli.call(cache.argv(path), "", function(d) {
      var bad = cache.rejects ? cache.rejects(d) : null
      if (bad) { failed(bad); return }
      cache.put(path, { state: "ready", result: d, error: null, at: cache.timeOf ? cache.timeOf(d) : Date.now() })
      if (onDone) onDone(d)
    }, failed, { timeout: cache.timeout })
  }
  function soon(p) {
    var path = pathOf(p)
    if (!path) return
    var c = cache.byPath[path]
    if (c && (c.state === "loading" || (c.at && Date.now() - c.at < cache.freshMs))) return
    if (cache._queue.indexOf(path) >= 0) return
    cache._queue = cache._queue.concat([path])
    cache._next()
  }
  function _next() {
    if (cache._busy || cache._queue.length === 0) return
    var path = cache._queue[0]
    cache._queue = cache._queue.slice(1)
    cache._busy = true
    var next = function() { cache._busy = false; Qt.callLater(cache._next) }
    cache.run(path, next, next)
  }
}
