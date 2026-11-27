import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import "../core"
import "../lib/Model.js" as Model

// Toolchain slice service: versions (`toolchain info`), disk usage (`toolchain disk`),
// cleanup (`toolchain clean --item`), removal (`toolchain remove-all`) and toolchain updates
// (`updates status|check|apply|channel`: the update channel, a daily check while the window is focused,
// one-click updates that roll back on failure).
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
  // `updates status`: the cached check (no network, < 20 ms); check/apply streams replace it with their result.
  Resource {
    id: updatesRes
    fetch: root.app.cli.fetcher(["updates", "status"], "", { timeout: 15000 })
    onSucceeded: root._autoCheck()
  }
  readonly property alias infoResource: infoRes
  readonly property alias diskResource: diskRes
  readonly property alias updatesResource: updatesRes

  readonly property var items: infoRes.data && infoRes.data.items ? infoRes.data.items : []   // versions
  readonly property var sdks: infoRes.data && infoRes.data.sdks ? infoRes.data.sdks : ({})
  // what needs fixing; "update" (an older iOS build scripts checkout) works as it is and counts with the updates
  readonly property var _bad: items.filter(function(i) { return !i.optional && i.status !== "ok" && i.status !== "update" })

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
    // Updates (Updates rows and an older iOS build scripts checkout, once each) are a quiet count (the sidebar
    // draws it dim), and only while nothing needs fixing.
    var n = Model.toolchainUpdateIds(root.items, root.updates).length
    var quiet = !bad.length && n > 0
    return {
      title: "Toolchain",
      detail: bad.length ? bad.length + " piece" + (bad.length === 1 ? "" : "s") + " need attention"
            : n ? (n === 1 ? "1 update ready" : n + " updates ready") : "All pieces installed",
      level: bad.some(function(i) { return i.status === "error" }) ? "error" : (bad.length ? "warn" : quiet ? "info" : "ok"),
      badge: quiet ? String(n) : "",
      quiet: quiet,
      tip: quiet ? Model.plural(n, "toolchain update") + " available" : ""
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
    updatesRes.load(true)
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

  // ------------------------------------------------------------ updates
  //   updates            the `updates status` object, or null before the first read
  //   channel            "tested" | "latest" (a switch in flight shows at once)
  //   checking, applying the check / apply streams
  //   applyTarget        component id, "all" or "restore"; applyStep the component being updated, applyDetail its progress
  //   lastFailure        {id, name, error} of the last update that failed and was put back, until the next good
  //                      check or update (or dismissFailure()); the section keeps a note for it
  //   checkUpdates(force), update(id), updateAll(), putBack(), setChannel(ch), cancelUpdate(), dismissFailure()
  readonly property var updates: updatesRes.data && updatesRes.data.components ? updatesRes.data : null
  readonly property int updateCount: updates && updates.count ? updates.count : 0
  property string pendingChannel: ""
  readonly property string channel: pendingChannel || (updates && updates.channel ? updates.channel : "tested")
  readonly property bool checking: checkJob.running
  readonly property bool applying: applyJob.running
  property string applyTarget: ""
  property string applyStep: ""
  property real applyFraction: -1
  property string applyDetail: ""
  property var lastFailure: null
  property bool _manualCheck: false
  property real _autoAt: 0
  property string _applyChannel: ""
  property var _prompt: null          // confirm handle for a helper prompt

  function checkUpdates(force) {
    if (checkJob.running || applyJob.running) return
    root._manualCheck = !!force
    checkJob.start(["updates", "check"].concat(force ? ["--force"] : []))
  }

  // At most one background check an hour, only while the window is focused and the cached one is due.
  function _autoCheck() {
    if (!root.app.windowFocused || checkJob.running || applyJob.running) return
    if (!Model.updatesDue(root.updates) || Date.now() - root._autoAt < 3600000) return
    root._autoAt = Date.now()
    root.checkUpdates(false)
  }

  function _component(id) {
    var cs = root.updates ? root.updates.components : []
    for (var i = 0; i < cs.length; i++) if (cs[i].id === id) return cs[i]
    return null
  }

  // Updating pymobiledevice3 restarts the iPhone connection: ask once here, then the helper gets --yes.
  function _askRestart(comps, go) {
    var names = comps.filter(function(c) { return c && (c.restarts || []).indexOf("iphone-connection") >= 0 })
                     .map(function(c) { return c.name })
    if (!names.length) { go(false); return }
    root.app.confirm({
      title: "Restart the iPhone connection?",
      message: "Updating " + names.join(" and ") + " restarts the connection to your iPhone for a moment. "
               + "The iPhone reconnects on its own.",
      confirmText: "Update",
      glyph: Model.Glyph.phoneLink,
      onConfirm: function() { go(true) }
    })
  }

  // On Tested this also puts the tested version back over a newer, untested one (Model.updateIsBack).
  function update(id) {
    var c = root._component(id)
    if (!c || applyJob.running) return
    root._askRestart([c], function(yes) { root._apply(["--component", id], id, "", yes) })
  }

  // An update that was interrupted (status.interrupted): `updates apply` with no component only puts back
  // the previous version it had set aside.
  function putBack() {
    if (applyJob.running || !root.updates || !root.updates.interrupted) return
    root._apply([], "restore", "", false)
  }

  function dismissFailure() { root.lastFailure = null }

  function updateAll() {
    if (applyJob.running) return
    var todo = Model.updateRows(root.updates).filter(function(c) { return c.state === "update" })
    root._askRestart(todo, function(yes) { root._apply(["--all"], "all", "", yes) })
  }

  // Latest only changes what's offered; going back to Tested reinstalls the tested versions (asks first).
  function setChannel(ch) {
    if (ch === root.channel || applyJob.running || root.pendingChannel !== "") return
    var back = ch === "tested" ? Model.updatesReinstall(root.updates) : []
    if (back.length) {
      var comps = back.map(function(b) { return root._component(b.id) })
      root.app.confirm({
        title: "Go back to the tested versions?",
        message: "These are reinstalled at the versions this iOS Studio release was tested with:\n\n"
                 + back.map(function(b) { return b.name + "  " + b.from + " → " + b.to }).join("\n")
                 + "\n\nEach keeps working until its replacement has passed its check.",
        confirmText: back.length === 1 ? "Reinstall " + back[0].name : "Reinstall " + back.length + " tools",
        glyph: Model.Glyph.reinstall,
        onConfirm: function() {
          root._askRestart(comps, function(yes) { root._apply(["--all", "--channel", "tested"], "all", "tested", yes) })
        }
      })
      return
    }
    root.pendingChannel = ch
    root.app.cli.call(["updates", "channel", "--set", ch], "", function(d) {
      root.pendingChannel = ""
      root.app.setSetting("toolchainChannel", d && d.channel ? d.channel : ch)
      updatesRes.load(true)
      if (ch === "latest" && root.updates && !root.updates.checkedAt) root.checkUpdates(true)
    }, function(e) {
      root.pendingChannel = ""
      root.app.toastError(e)
    })
  }

  function cancelUpdate() { if (applyJob.running) applyJob.write({ cancel: true }) }

  function _apply(args, target, channel, yes) {
    if (applyJob.running) return
    if (checkJob.running) checkJob.stop()
    root._manualCheck = false
    root.applyTarget = target
    root.applyStep = target === "all" ? "" : target
    root.applyFraction = -1
    root.applyDetail = "Starting…"
    root._applyChannel = channel || ""
    applyJob.start(["updates", "apply"].concat(args, yes ? ["--yes"] : []))
  }

  Stream {
    id: checkJob
    program: [root.app.helperPath]
    onFinished: function(ok, data, error) {
      if (data && data.components) updatesRes.set(data)
      else updatesRes.load(true)
      if (ok) root.lastFailure = null
      if (!ok && root._manualCheck && error && error.code !== "cancelled") root.app.toast(Model.updateErrorText(error), "error")
      root._manualCheck = false
    }
  }

  Stream {
    id: applyJob
    program: [root.app.helperPath]
    onEvent: function(ev) {
      switch (ev.event) {
      case "step":
        if (ev.state === "running") { root.applyStep = ev.id || ""; root.applyFraction = -1; root.applyDetail = ev.title || "" }
        break
      case "progress":
        root.applyFraction = ev.fraction === null || ev.fraction === undefined ? -1 : ev.fraction
        if (ev.detail) root.applyDetail = ev.detail
        break
      case "prompt":
        root._ask(ev)
        break
      }
    }
    onFinished: function(ok, data, error) { root._applied(ok, data, error) }
    // A helper that exits 0 without a `done` line: settle anyway (exit errors come through onFinished).
    onExited: function(code) {
      Qt.callLater(function() {
        if (root.applyTarget !== "" && !applyJob.running && !applyJob.lastDone)
          root._applied(false, {}, { code: "exited", message: "The update stopped without reporting a result." })
      })
    }
  }

  // A question from the helper (only when it wasn't given --yes): the shared confirm dialog, on any page.
  function _ask(p) {
    if (root._prompt) root.app.closeConfirm(root._prompt)
    var yes = (p.options || []).filter(function(o) { return o.value === "yes" })[0]
    root._prompt = root.app.confirm({
      title: "Before updating",
      message: p.message || "",
      confirmText: yes ? yes.label : "Continue",
      glyph: Model.Glyph.help,
      onConfirm: function() { root._prompt = null; applyJob.write({ prompt: p.id, value: "yes" }) },
      onCancel: function() { root._prompt = null; applyJob.write({ prompt: p.id, cancel: true }) }
    })
    if (!root.app.windowFocused) root.app.notify("iOS Studio needs you", p.message || "", Model.Glyph.help)
  }

  function _applied(ok, data, error) {
    if (root.applyTarget === "") return
    if (root._prompt) { root.app.closeConfirm(root._prompt); root._prompt = null }
    var channel = root._applyChannel
    root.applyTarget = ""
    root.applyStep = ""
    root.applyFraction = -1
    root.applyDetail = ""
    root._applyChannel = ""
    if (data && data.status && data.status.components) updatesRes.set(data.status)
    else updatesRes.load(true)
    var rolled = data && data.rolledBack ? data.rolledBack : []
    if (ok) {
      root.lastFailure = null
      if (channel) root.app.setSetting("toolchainChannel", channel)
      root.app.toast(Model.updatesDoneText(data), "ok")
    } else if (error && error.code === "cancelled") {
      if (rolled.length) root.app.toast(Model.updateErrorText(error), "info")
    } else {
      // a failure that was rolled back keeps a note in the section, not only a toast
      if (rolled.length) {
        var failed = rolled[rolled.length - 1]
        var c = root._component(failed)
        root.lastFailure = { id: failed, name: c ? c.name : failed, error: error }
      }
      root.app.toast(Model.updateErrorText(error), "error")
    }
    infoRes.load(true)
    if (root.diskWanted) root.refreshDisk()
    if (root.app.setup) root.app.setup.refresh()
  }

  Timer {
    interval: 3600000
    repeat: true
    triggeredOnStart: true
    running: root.app.windowFocused && updatesRes.loaded
    onTriggered: root._autoCheck()
  }

  Component.onCompleted: root.refresh()
}
