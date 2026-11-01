import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.components
import ".."
import "../Model.js" as Model

// Toolchain slice service: versions (`toolchain info`), disk usage (`toolchain disk`),
// cleanup (`toolchain clean --item`) and removal (`toolchain remove-all`).
Item {
  id: root
  required property var app

  // "unknown" | "loading" | "ready" | "needs-setup" | "error"
  property string status: "unknown"
  property var summary: ({ title: "Toolchain", detail: "Not checked yet", level: "info", badge: "" })
  property var error: null

  property var items: []          // versions
  property var sdks: ({})
  property var disk: null         // {items, total, reclaimable, free}
  property string diskStatus: "unknown"
  property var diskError: null
  property bool diskStale: false

  // running clean / remove
  property string busyItem: ""    // disk item id, or "remove-all"
  property real busyFraction: -1
  property string busyDetail: ""
  property var busyLog: []
  property var prompt: null
  readonly property bool busy: job.running

  function refresh() {
    if (root.status !== "ready") root.status = "loading"
    root.app.cli.call(["toolchain", "info"], "", function(data) {
      root.items = data.items || []
      root.sdks = data.sdks || {}
      root.error = null
      var bad = root.items.filter(function(i) { return !i.optional && i.status !== "ok" })
      root.status = bad.length ? "needs-setup" : "ready"
      root.summary = {
        title: "Toolchain",
        detail: bad.length ? bad.length + " piece" + (bad.length === 1 ? "" : "s") + " need attention" : "All pieces installed",
        level: bad.some(function(i) { return i.status === "error" }) ? "error" : (bad.length ? "warn" : "ok"),
        badge: ""
      }
    }, function(err) {
      root.error = err
      root.status = "error"
      root.summary = { title: "Toolchain", detail: Model.errorMessage(err), level: "error", badge: "" }
    }, { timeout: 30000 })
    root.refreshDisk()
  }

  function refreshDisk() {
    if (root.diskStatus !== "ready") root.diskStatus = "loading"
    // Instant stale read first, then the real numbers (du over ~20 GB takes a second or two).
    if (!root.disk) root.app.cli.call(["toolchain", "disk", "--cached"], "", function(data) {
      if (data && data.items && root.diskStatus !== "ready") { root.disk = data; root.diskStale = true }
    }, function() {})
    root.app.cli.call(["toolchain", "disk"], "", function(data) {
      root.disk = data
      root.diskStale = false
      root.diskStatus = "ready"
      root.diskError = null
    }, function(err) {
      root.diskError = err
      root.diskStatus = root.disk ? "ready" : "error"
    }, { timeout: 90000 })
  }

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

  function _begin() { root.busyFraction = -1; root.busyDetail = "Starting…"; root.busyLog = [] }

  function answer(value) { if (root.prompt) { job.write({ prompt: root.prompt.id, value: String(value) }); root.prompt = null } }

  Stream {
    id: job
    program: [root.app.helperPath]
    onEvent: function(ev) {
      if (ev.event === "progress") {
        root.busyFraction = ev.fraction === null || ev.fraction === undefined ? -1 : ev.fraction
        root.busyDetail = ev.detail || ""
      } else if (ev.event === "log") {
        var l = root.busyLog.slice(-299); l.push(ev.text || ""); root.busyLog = l
      } else if (ev.event === "prompt") {
        root.prompt = ev
      }
    }
    onFinished: function(ok, data, error) {
      var what = root.busyItem
      root.busyItem = ""
      root.prompt = null
      if (ok) {
        if (what === "remove-all") root.app.toast("The iOS toolchain was removed", "ok")
        else root.app.toast("Freed " + Model.formatBytes(data.freed || 0), "ok")
      } else if (error && error.code !== "cancelled") {
        root.app.toast(Model.errorMessage(error), "error")
      }
      root.refresh()
      if (root.app.setup) root.app.setup.refresh()
    }
  }

  Component.onCompleted: root.refresh()
}
