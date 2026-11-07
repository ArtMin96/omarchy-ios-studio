import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import "../core"
import "../lib/Model.js" as Model

// Toolchain slice service: versions (`toolchain info`), disk usage (`toolchain disk`),
// cleanup (`toolchain clean --item`) and removal (`toolchain remove-all`).
Item {
  id: root
  required property var app

  // `toolchain info`: versions and SDKs (cheap; the sidebar shows its summary).
  Resource {
    id: infoRes
    fetch: root.app.cli.fetcher(["toolchain", "info"], "", { timeout: 30000 })
  }
  // `toolchain disk`: du over ~20 GB, a second or two. Only fetched once the Toolchain page has been
  // opened (diskWanted); until the real numbers arrive the helper's cached copy shows (diskStale).
  Resource {
    id: diskRes
    fetch: root.app.cli.fetcher(["toolchain", "disk"], "", { timeout: 90000 })
  }
  readonly property alias infoResource: infoRes
  readonly property alias diskResource: diskRes

  readonly property var items: infoRes.data && infoRes.data.items ? infoRes.data.items : []   // versions
  readonly property var sdks: infoRes.data && infoRes.data.sdks ? infoRes.data.sdks : ({})
  readonly property var _bad: items.filter(function(i) { return !i.optional && i.status !== "ok" })

  // "unknown" | "loading" | "ready" | "needs-setup" | "error" (the last check failed)
  readonly property string status: infoRes.error ? "error"
                                   : infoRes.status === "idle" ? "unknown"
                                   : infoRes.status === "loading" ? "loading"
                                   : _bad.length ? "needs-setup" : "ready"
  readonly property var error: infoRes.error
  readonly property var summary: {
    if (infoRes.error) return { title: "Toolchain", detail: Model.errorMessage(infoRes.error), level: "error", badge: "" }
    if (!infoRes.loaded) return { title: "Toolchain", detail: "Not checked yet", level: "info", badge: "" }
    var bad = root._bad
    return {
      title: "Toolchain",
      detail: bad.length ? bad.length + " piece" + (bad.length === 1 ? "" : "s") + " need attention" : "All pieces installed",
      level: bad.some(function(i) { return i.status === "error" }) ? "error" : (bad.length ? "warn" : "ok"),
      badge: ""
    }
  }

  property bool diskWanted: false                               // the Toolchain page sets it when it opens
  property var _cachedDisk: null
  readonly property var disk: diskRes.data || _cachedDisk        // {items, total, reclaimable, free}
  readonly property bool diskStale: !diskRes.data && !!_cachedDisk

  // running clean / remove
  property string busyItem: ""    // disk item id, or "remove-all"
  property real busyFraction: -1
  property string busyDetail: ""
  property var prompt: null
  readonly property bool busy: job.running

  function refresh() {
    infoRes.load(true)
    if (root.diskWanted) root.refreshDisk()
  }

  function refreshDisk() {
    // Instant stale read first, then the real numbers.
    if (!root.disk) root.app.cli.call(["toolchain", "disk", "--cached"], "", function(data) {
      if (data && data.items && !diskRes.data) root._cachedDisk = data
    }, function() {})
    diskRes.load(true)
  }
  onDiskWantedChanged: if (diskWanted && !diskRes.loaded && !diskRes.busy) root.refreshDisk()

  function clean(id) {
    if (job.running) return
    root.busyItem = id
    root._begin()
    job.start(["toolchain", "clean", "--item", id])
  }

  function removeAll(removeAuth) {
    if (job.running) return
    root.busyItem = "remove-all"
    root._begin()
    job.start(["toolchain", "remove-all", "--yes"].concat(removeAuth ? ["--remove-auth"] : []))
  }

  function _begin() { root.busyFraction = -1; root.busyDetail = "Starting…" }

  function answer(value) { if (root.prompt) { job.write({ prompt: root.prompt.id, value: String(value) }); root.prompt = null } }

  Stream {
    id: job
    program: [root.app.helperPath]
    onEvent: function(ev) {
      if (ev.event === "progress") {
        root.busyFraction = ev.fraction === null || ev.fraction === undefined ? -1 : ev.fraction
        root.busyDetail = ev.detail || ""
      } else if (ev.event === "prompt") {
        root.prompt = ev
      }
    }
    onFinished: function(ok, data, error) { root._done(ok, data, error) }
    // A helper that exits 0 without a `done` line: clear busyItem anyway (exit errors come through onFinished).
    onExited: function(code) {
      Qt.callLater(function() {
        if (root.busyItem !== "" && !job.running && !job.lastDone)
          root._done(false, {}, { code: "exited", message: "The cleanup stopped without reporting a result." })
      })
    }
  }

  function _done(ok, data, error) {
    var what = root.busyItem
    root.busyItem = ""
    root.prompt = null
    if (ok) {
      if (what === "remove-all") root.app.toast("The iOS toolchain was removed", "ok")
      else root.app.toast("Freed " + Model.formatBytes(data.freed || 0), "ok")
    } else if (error && error.code !== "cancelled") {
      root.app.toastError(error)
    }
    root.refresh()
    if (root.app.setup) root.app.setup.refresh()
  }

  Component.onCompleted: root.refresh()
}
