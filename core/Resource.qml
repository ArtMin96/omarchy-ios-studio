import QtQuick

// One piece of backend data with its loading state: the "loading → ready, keep stale data on error"
// pattern in one place. Non-visual; pair it with <StateView resource: …> for the skeleton / error /
// empty states.
//
//   Resource {
//     id: profiles
//     active: app.device.connected                           // never loads while false
//     autoLoad: true                                         // load() whenever it becomes active
//     ttl: 60000                                             // load() within a minute of the last one is a no-op
//     fetch: app.device.fetcher("profiles.list")             // or app.cli.fetcher(["account", "status"])
//   }
//   profiles.load()          // fetch unless fresh (within ttl) or already loading
//   profiles.load(true)      // fetch even if fresh (Reload buttons, retry, after a change); while a fetch
//                            // runs, one more fetch follows it (the running one may predate the change)
//   profiles.invalidate()    // the data is out of date: the next load() fetches (and loads now if autoLoad)
//   profiles.set(list)       // replace the data locally (after a change you made), counts as fresh
//   profiles.reset()         // forget everything (device switched); a reply still in flight is dropped
//
// `fetch` is any function(done(data), fail(error)). status: "idle" (nothing yet) | "loading" (first
// load) | "ready" | "error" (failed with no data). A reload keeps status "ready" and sets `busy`; a
// failed reload keeps the old data, status stays "ready" and `error` holds the failure.
QtObject {
  id: root

  property var fetch: null
  property bool active: true
  property bool autoLoad: false
  property int ttl: 0                       // ms; 0 = every load() fetches
  property var initial: null                // data before the first load and after reset() ([] for lists)

  property string status: "idle"
  property var data: initial
  property var error: null
  property double loadedAt: 0               // Date.now() of the last successful load (0 = never)
  property bool busy: false                 // a fetch is in flight (first load or reload)

  readonly property bool loaded: loadedAt > 0

  signal succeeded(var data)
  signal failed(var error)

  property int _gen: 0                      // bumped by reset(): replies from before it are dropped
  property bool _stale: false               // invalidate() called
  property bool _reloadQueued: false        // load(true) while busy: fetch once more when this one settles

  // Returns true when a fetch started.
  function load(force) {
    if (!root.active || typeof root.fetch !== "function") return false
    if (root.busy) { if (force) root._reloadQueued = true; return false }
    if (!force && !root._stale && root.loaded && root.ttl > 0 && Date.now() - root.loadedAt < root.ttl) return false
    var gen = root._gen
    root.busy = true
    if (root.status !== "ready") root.status = "loading"
    var settled = false
    try {
      root.fetch(function(d) {
        if (settled || gen !== root._gen) return
        settled = true
        root.data = d
        root.error = null
        root.loadedAt = Date.now()
        root._stale = false
        root.status = "ready"
        root.busy = false
        root.succeeded(d)
        root._runQueued()
      }, function(e) {
        if (settled || gen !== root._gen) return
        settled = true
        root.error = e || { code: "failed", message: "Something went wrong." }
        if (root.status !== "ready") root.status = "error"
        root.busy = false
        root.failed(root.error)
        root._runQueued()
      })
    } catch (ex) {
      console.warn("Resource fetch threw:", ex)
      if (!settled) { settled = true; root.busy = false; root.error = { code: "internal", message: String(ex) }; if (root.status !== "ready") root.status = "error"; root._runQueued() }
    }
    return true
  }

  // After a fetch settles: the load(true) that arrived while it ran (one, however many were asked).
  // Deferred so a reply handler finishes before the next fetch starts.
  function _runQueued() {
    if (!root._reloadQueued) return
    root._reloadQueued = false
    var gen = root._gen
    Qt.callLater(function() { if (gen === root._gen) root.load(true) })
  }

  function invalidate() {
    root._stale = true
    if (root.autoLoad && root.active) root.load()
  }

  function set(d) {
    root.data = d
    root.error = null
    root.loadedAt = Date.now()
    root._stale = false
    root.status = "ready"
  }

  function reset() {
    root._gen++
    root.busy = false
    root._reloadQueued = false
    root.data = root.initial
    root.error = null
    root.loadedAt = 0
    root._stale = false
    root.status = "idle"
  }

  onActiveChanged: if (active && autoLoad) Qt.callLater(root.load)
  Component.onCompleted: if (active && autoLoad) Qt.callLater(root.load)
}
