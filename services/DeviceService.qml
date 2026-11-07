import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.components
import ".."
import "../Model.js" as Model

// Owns the device daemon (bin/iosstudio-deviced, protocol: docs/deviced-protocol.md).
//   app.device.request("apps.list", {}, function(ok, dataOrError) { … })
//   Connections { target: app.device; function onDaemonEvent(name, data) { … } }   // syslog, crash…
Item {
  id: root
  required property var app

  // "unknown" | "loading" | "ready" | "needs-setup" | "error"
  property string status: "loading"
  property var error: null
  // { title, detail, level, badge } for Home and the sidebar
  property var summary: _summary()

  // ---------------------------------------------------------------- state
  property bool daemonReady: false
  property string pmd3Version: ""
  property var devices: []                 // Device objects (see protocol)
  property var current: null               // the active Device, merged with `info`
  property var info: ({})                  // DeviceInfo of the active device (partial until loaded)
  property var connection: ({ state: "idle", message: "Starting…" })
  readonly property string connState: connection && connection.state ? connection.state : "idle"
  readonly property bool connected: connState === "connected" || connState === "locked"
  readonly property bool hasDevice: devices.length > 0

  // apps. `apps` changes only when the list itself does (apps.list reply, `apps` event, launch/stop/remove);
  // icons and sizes arrive later in batches and land in their own maps so a batch doesn't rebuild every
  // view bound to `apps`. Their values are also written into the app objects in place (no change signal),
  // so code reading `a.icon` / `a.size` sees them on its next evaluation.
  property var apps: []
  property var appIcons: ({})              // bundleId -> icon path (appIcons / appIcon events)
  property var appSizes: ({})              // bundleId -> bytes (appSizes, only after wantAppSizes())
  property string appsStatus: "unknown"    // unknown | loading | ready | error
  property var appsError: null
  property bool appsStale: false           // the reply came from the daemon's cache; it refreshes in the background
  property var appsFetchedAt: null         // when the daemon read the list from the iPhone (as the daemon sends it; null = unknown)
  property double _sizesAskedAt: 0         // wantAppSizes(): once per device per sizesTtl
  readonly property int sizesTtl: 600000
  property string _appsKey: ""             // the list's content without icon/size, to skip no-op updates
  property var busyApps: ({})              // bundleId -> "launch" | "kill" | "uninstall"

  // profiles
  property var profiles: []
  property string profilesStatus: "unknown"
  property var profilesError: null

  // screen
  property bool mirroring: false
  property real mirrorFps: 1
  property string framePath: ""            // latest mirror frame (rotating files)
  property int frameSeq: 0
  property string frameUrl: ""             // file://<framePath>?<seq>, set in one step so a frame is one change
  property string lastShot: ""             // latest one-off screenshot
  property bool shooting: false
  property var mirrorError: null
  readonly property string screenSource: framePath !== "" ? frameUrl
                                       : (lastShot !== "" ? "file://" + lastShot : "")
  // mirror modes (deviced mirror.start {mode}): "auto" tries the smooth stream (iOS 27+), else screenshots
  property string mirrorMode: app.setting("mirrorMode", "auto")   // what the user picked: auto | screenshot
  property string mirrorActive: ""         // what runs now: stream | screenshot ("" = not running)
  property real measuredFps: 0
  property string mirrorNotice: ""         // why it fell back to a lower frame rate
  property var mirrorInfo: null            // last mirror.status (available modes)
  // recording (deviced record.*): null | {state: recording|finishing|done|failed, seconds, bytes, path, …}
  property var recording: null
  readonly property bool recordingActive: !!recording && (recording.state === "recording" || recording.state === "finishing")

  // location
  property var location: null              // {lat, lon} while simulated
  property bool locating: false

  // network tab (DeviceNetworkView): a simulated condition is app-wide state (page header, sidebar)
  property var condition: null             // the active condition {profile, name, groupName, expiresAt, …} or null
  readonly property bool conditionActive: !!condition && condition.active === true
  property var capture: ({ running: false })   // last `pcap` event (see protocol)
  readonly property bool capturing: !!capture && capture.running === true

  // pairing
  property var pair: null                  // last pair event
  readonly property bool pairing: !!pair && ["starting", "waiting", "trust", "show-code", "need-code"].indexOf(pair.phase) >= 0

  signal daemonEvent(string name, var data)

  // ---------------------------------------------------------------- requests
  property int _nextId: 1
  property var _pending: ({})              // id -> { cb, deadline, cmd }

  function _timeoutFor(cmd) {
    if (cmd === "app.install") return 600000
    if (cmd === "crash.list" || cmd === "apps.list" || cmd === "app.launch" || cmd === "ddi.mount") return 90000
    if (cmd === "crash.pull" || cmd === "profiles.list" || cmd === "screenshot" || cmd === "app.uninstall") return 60000
    // Walking the screen element by element takes ~10 s on a busy screen; a press re-reads it.
    if (cmd === "ui.tree" || cmd === "ui.audit" || cmd === "ui.press") return 60000
    return 40000
  }

  // callback(ok, dataOrError). Returns the request id (or -1 when the daemon isn't running).
  function request(cmd, args, callback, timeoutMs) {
    if (!daemon.running || root.status === "needs-setup") {
      var e = root.status === "needs-setup" ? root.error
            : { code: "not-ready", message: "The iPhone service is starting. Try again in a moment." }
      if (callback) Qt.callLater(function() { callback(false, e) })
      return -1
    }
    var id = root._nextId++
    var p = Object.assign({}, root._pending)
    p[id] = { cb: callback || null, deadline: Date.now() + (timeoutMs || _timeoutFor(cmd)), cmd: cmd }
    root._pending = p
    daemon.write({ id: id, cmd: cmd, args: args || {} })
    return id
  }

  function _settle(id, ok, payload) {
    var entry = root._pending[id]
    if (!entry) return
    var p = Object.assign({}, root._pending)
    delete p[id]
    root._pending = p
    if (entry.cb) {
      try { entry.cb(ok, payload) } catch (e) { console.warn("DeviceService callback for", entry.cmd, "threw:", e) }
    }
  }

  function _failAll(err) {
    var ids = Object.keys(root._pending)
    for (var i = 0; i < ids.length; i++) _settle(ids[i], false, err)
  }

  Timer {
    interval: 1000; repeat: true
    running: Object.keys(root._pending).length > 0
    onTriggered: {
      var now = Date.now(), ids = Object.keys(root._pending)
      for (var i = 0; i < ids.length; i++) {
        var e = root._pending[ids[i]]
        if (e && e.deadline < now)
          root._settle(ids[i], false, { code: "timeout", message: "Your iPhone took too long to answer. Make sure it's awake and try again." })
      }
    }
  }

  // ---------------------------------------------------------------- daemon
  Stream {
    id: daemon
    program: [root.app.devicedPath]
    autoRestart: true
    quitMessage: ({ id: 0, cmd: "daemon.shutdown", args: {} })

    onEvent: function(ev) {
      if (ev.event === "log") return            // stderr debug lines
      if (ev.id !== undefined && ev.id !== null && ev.event === undefined) {
        root._settle(ev.id, ev.ok === true, ev.ok === true ? ev.data : (ev.error || { code: "failed", message: "Something went wrong." }))
        return
      }
      if (ev.event) root._onEvent(ev.event, ev.data)
    }
    onExited: function(code) {
      root.daemonReady = false
      root._failAll({ code: "unreachable", message: "The iPhone service restarted. Try again." })
      root.mirroring = false; root.mirrorActive = ""; root.measuredFps = 0
      root.condition = null                     // the daemon's exit switched it off
      if (root.capturing) root.capture = Object.assign({}, root.capture, { running: false })
      if (code === 3) {         // no pymobiledevice3 python (bin/iosstudio-deviced)
        root._needsSetup()
        return
      }
      // Restarts back off (1 s, 2 s, 4 s…). A daemon that keeps dying before it's ready won't
      // come up by itself: stop, say so and offer Retry.
      if (!root._readySinceStart) root._failedStarts++
      root._readySinceStart = false
      if (root._failedStarts >= root.maxFailedStarts) root._crashed(code)
    }
  }

  // Unix runtime dir as bin/iosstudio-deviced picks it (IOSSTUDIO_RUNTIME_DIR, else XDG_RUNTIME_DIR).
  readonly property string logPath: (Quickshell.env("IOSSTUDIO_RUNTIME_DIR") || Quickshell.env("XDG_RUNTIME_DIR") || "/tmp")
                                    + (root.app.demo !== "" ? "/ios-studio-demo" : "/ios-studio") + "/deviced.log"
  readonly property int maxFailedStarts: 4
  property int _failedStarts: 0
  property bool _readySinceStart: false

  function _crashed(code) {
    daemon.autoRestart = false
    daemon.stop()
    root.status = "error"
    root.error = { code: "daemon-crashed",
                   message: "The iPhone service keeps stopping as it starts (exit code " + code + "). What went wrong is in " + root.logPath.replace(root.app.home, "~") + ".",
                   detail: root.logPath }
    root.connection = { state: "idle", message: "The iPhone service stopped" }
  }

  function _needsSetup(detail) {
    daemon.autoRestart = false
    daemon.stop()
    root.status = "needs-setup"
    root.error = { code: "needs-setup", message: "The iPhone tools (pymobiledevice3) aren't installed yet.",
                   fix: { label: "Install iPhone tools", page: "setup", step: "pymobiledevice3" }, detail: detail || "" }
    root.connection = { state: "idle", message: "iPhone tools missing" }
  }

  function start() {
    root.status = "loading"
    root.error = null
    root._failedStarts = 0
    root._readySinceStart = false
    daemon.autoRestart = true
    daemon.start([])
  }

  Component.onCompleted: start()

  // ---------------------------------------------------------------- events
  property string _lastState: ""
  property double _disconnectedAt: 0

  function _onEvent(name, data) {
    switch (name) {
    case "ready":
      if (data && data.error) { _needsSetup(data.error.detail || data.error.message); break }
      root.daemonReady = true
      root._readySinceStart = true
      root._failedStarts = 0
      root.pmd3Version = data && data.pymobiledevice3 ? data.pymobiledevice3 : ""
      root.status = "ready"
      root.error = null
      break
    case "devices":
      root.devices = data || []
      _updateCurrent()
      break
    case "connection":
      _onConnection(data || {})
      break
    case "deviceInfo":
      if (data && (!root.current || data.udid === root.current.udid)) {
        root.info = Object.assign({}, root.info, data)
        if (data.location !== undefined) root.location = data.location
        if (data.condition !== undefined) root.condition = data.condition ? Object.assign({ active: true }, data.condition) : null
        _updateCurrent()
      }
      break
    case "frame":
      if (root.mirroring && data) { root.frameSeq = data.seq; root.frameUrl = "file://" + data.path + "?" + data.seq; root.framePath = data.path }
      break
    case "mirror":
      root.mirroring = !!(data && data.running)
      if (data && data.fps && (data.mode === "screenshot" || !data.mode)) root.mirrorFps = Math.min(8, data.fps)
      root.mirrorActive = root.mirroring && data.mode ? data.mode : ""
      root.measuredFps = root.mirroring && data.measuredFps ? data.measuredFps : 0
      var notice = root.mirroring && data.notice ? data.notice : ""
      if (notice !== "" && notice !== root.mirrorNotice) root.app.toast(notice, "info")
      root.mirrorNotice = notice
      if (data && data.error) { root.mirrorError = data.error; root.app.toast(Model.errorMessage(data.error), "warn") }
      if (!root.mirroring) root.framePath = ""
      break
    case "record":
      if (data) {
        var was = root.recording ? root.recording.state : ""
        root.recording = data
        if (data.state === "done" && was !== "done" && !data.placeholder) root.app.notify("Screen recording saved", (data.seconds || 0).toFixed(0) + " s video", Model.Glyph.camera)
        if (data.state === "failed" && was !== "failed" && data.error) root.app.toast(Model.errorMessage(data.error), "error")
      }
      break
    case "apps":                                // the cached list changed after a background refresh
      if (data && data.apps && _isCurrent(data.udid)) { _setApps(data.apps); root.appsStale = false }
      break
    case "appIcons":                            // batched: {udid, icons: {bundleId: path}}
      if (data && data.icons && _isCurrent(data.udid)) _queueIcons(data.icons)
      break
    case "appIcon":                             // older daemons: one event per icon (coalesced below)
      if (data && data.bundleId && data.path && _isCurrent(data.udid)) { var one = {}; one[data.bundleId] = data.path; _queueIcons(one) }
      break
    case "appSizes":
      if (data && data.sizes && _isCurrent(data.udid)) _mergeSizes(data.sizes)
      break
    case "condition":
      _onCondition(data || {})
      break
    case "pcap":
      root.capture = data || { running: false }
      break
    case "pair":
      root.pair = data
      if (data && data.phase === "done") {
        root.app.toast(data.message || "Paired", "ok")
        root.app.notify("iPhone paired", data.message || "", Model.Glyph.phone)
      }
      break
    }
    root.daemonEvent(name, data)
  }

  function _onCondition(c) {
    var was = root.conditionActive ? root.condition : null
    root.condition = c.active ? c : null
    if (c.active || !was) return
    var why = { timeout: "its time ran out", "client-gone": "the app that set it closed",
                "tunnel-lost": "the connection to the iPhone dropped", shutdown: "the iPhone service stopped" }[c.reason]
    if (why) {
      root.app.toast((was.name || "The condition") + " switched off: " + why, "info")
      root.app.notify("iPhone back to normal", (was.name || "The simulated condition") + " switched off: " + why, Model.Glyph.network)
    }
  }

  function _onConnection(c) {
    var prev = root._lastState
    root.connection = c
    root._lastState = c.state
    if (c.state === "connected" && prev !== "connected" && prev !== "locked") {
      var name = (root.current && root.current.name) || "Your iPhone"
      var via = c.transport === "usb" ? "USB" : "Wi-Fi"
      // Only announce reconnects after a real gap (not every Wi-Fi blip, not app start).
      if (root._disconnectedAt > 0 && Date.now() - root._disconnectedAt > 20000)
        root.app.notify(name + " connected", "Over " + via, Model.Glyph.phone)
      root._disconnectedAt = 0
      // cheap first: the device's info, then the app list (served from the daemon's cache), and the
      // provisioning profiles a little later (only the Profiles tab and its warning badge need them)
      refreshInfo(false, function() {
        if (root.appsStatus !== "ready") refreshApps()
        if (root.profilesStatus !== "ready") profilesLater.restart()
      })
    } else if (c.state !== "connected" && c.state !== "locked" && (prev === "connected" || prev === "locked")) {
      root._disconnectedAt = Date.now()
      root.mirroring = false; root.mirrorActive = ""; root.measuredFps = 0
      root.framePath = ""
    } else if (prev === "" && c.state !== "connected") {
      root._disconnectedAt = Date.now()
    }
  }

  function _updateCurrent() {
    var cur = null
    for (var i = 0; i < root.devices.length; i++) if (root.devices[i].active) cur = root.devices[i]
    if (cur && root.current && root.current.udid !== cur.udid) {
      // switched device: forget the old one's lists
      root.info = ({}); root.apps = []; root.appsStatus = "unknown"; root.profiles = []; root.profilesStatus = "unknown"
      root.appIcons = ({}); root.appSizes = ({}); root._appsKey = ""; root._sizesAskedAt = 0; root.appsStale = false
      root._iconQueue = ({})
      root.lastShot = ""; root.framePath = ""; root.location = null
    }
    root.current = cur ? Object.assign({}, cur, root.info && root.info.udid === cur.udid ? root.info : {}, {
      name: (root.info && root.info.name) || cur.name,
      transport: cur.transport
    }) : null
  }

  function _patchApp(bundleId, patch) {
    var list = root.apps
    for (var i = 0; i < list.length; i++) {
      if (list[i].bundleId === bundleId) {
        var copy = list.slice()
        copy[i] = Object.assign({}, list[i], patch)
        _setApps(copy)
        return
      }
    }
  }

  function _isCurrent(udid) { return !udid || !root.current || udid === root.current.udid }

  // A new list: keep the icons/sizes we know, take any the daemon put inline, and only reassign `apps`
  // when something besides icon/size changed.
  function _setApps(items) {
    // plain JS objects (replies arrive as variant lists), so _writeInto can fill them in place
    var list = []
    for (var n = 0; n < items.length; n++) list.push(Object.assign({}, items[n]))
    var icons = null, sizes = null
    for (var i = 0; i < list.length; i++) {
      var a = list[i], bid = a.bundleId
      if (a.icon && root.appIcons[bid] !== a.icon) { if (!icons) icons = {}; icons[bid] = a.icon }
      if (a.size && root.appSizes[bid] !== a.size) { if (!sizes) sizes = {}; sizes[bid] = a.size }
    }
    if (icons) root.appIcons = Object.assign({}, root.appIcons, icons)
    if (sizes) root.appSizes = Object.assign({}, root.appSizes, sizes)
    var key = JSON.stringify(list, function(k, v) { return k === "icon" || k === "size" ? undefined : v })
    if (key === root._appsKey) return
    for (var j = 0; j < list.length; j++) {
      var b = list[j]
      if (!b.icon && root.appIcons[b.bundleId]) b.icon = root.appIcons[b.bundleId]
      if (!b.size && root.appSizes[b.bundleId]) b.size = root.appSizes[b.bundleId]
    }
    root._appsKey = key
    root.apps = list
  }

  function _writeInto(field, map) {
    var list = root.apps
    for (var i = 0; i < list.length; i++) { var v = map[list[i].bundleId]; if (v !== undefined) list[i][field] = v }
  }

  // Icons arrive in batches (or one by one from older daemons): merge them into appIcons at most
  // once per frame-ish tick, so N icons cost one map change, not N list rebuilds.
  property var _iconQueue: ({})
  function _queueIcons(icons) {
    var q = root._iconQueue
    for (var k in icons) q[k] = icons[k]
    iconFlush.restart()
  }
  Timer {
    id: iconFlush
    interval: 50
    onTriggered: {
      var q = root._iconQueue, known = root.appIcons
      root._iconQueue = ({})
      for (var k in q) if (known[k] === q[k]) delete q[k]
      if (Object.keys(q).length === 0) return
      root.appIcons = Object.assign({}, root.appIcons, q)
      root._writeInto("icon", q)
    }
  }

  function _mergeSizes(sizes) {
    root.appSizes = Object.assign({}, root.appSizes, sizes)
    _writeInto("size", sizes)
  }

  function _setBusy(bundleId, what) {
    var b = Object.assign({}, root.busyApps)
    if (what) b[bundleId] = what; else delete b[bundleId]
    root.busyApps = b
  }

  // ---------------------------------------------------------------- summary
  function batteryText(b) {
    if (!b || b.level === undefined || b.level === null) return ""
    return b.level + "%" + (b.charging ? " charging" : "")
  }

  function _summary() {
    if (root.status === "needs-setup")
      return { title: "iPhone", detail: "iPhone tools need installing", level: "warn", badge: "" }
    if (root.status === "error")
      return { title: "iPhone", detail: "The iPhone service keeps stopping", level: "error", badge: "!" }
    if (root.status === "loading" && !root.current)
      return { title: "iPhone", detail: "Starting…", level: "info", badge: "" }
    if (!root.current)
      return { title: "No iPhone yet", detail: "Plug it in or pair over Wi-Fi", level: "info", badge: "" }
    var c = root.current
    var name = c.model || c.name || "iPhone"
    if (root.connState === "connected") {
      var parts = [name, c.transport === "usb" ? "USB" : "Wi-Fi"]
      var bt = batteryText(root.info.battery)
      if (bt) parts.push(bt)
      var warn = root.info.developerMode === false
      if (root.conditionActive) parts.push("Simulating " + (root.condition.name || "a condition"))
      return { title: c.name || name, detail: parts.join(" · "), level: warn || root.conditionActive ? "warn" : "ok",
               badge: root.conditionActive ? "!" : "",
               hint: root.conditionActive ? "Simulating " + (root.condition.name || "a condition") + " on the whole iPhone"
                     : (warn ? "Developer Mode is off" : "") }
    }
    if (root.connState === "locked") return { title: c.name || name, detail: "Locked — unlock your iPhone", level: "warn", badge: "" }
    if (root.connState === "untrusted") return { title: c.name || name, detail: "Not trusted — tap Trust on your iPhone", level: "warn", badge: "" }
    if (root.connState === "not-paired") return { title: c.name || name, detail: "Not paired yet", level: "warn", badge: "" }
    if (root.connState === "unreachable") return { title: c.name || name, detail: "Not reachable right now", level: "info", badge: "" }
    return { title: c.name || name, detail: root.connection.message || "Looking for your iPhone…", level: "busy", badge: "" }
  }

  // ---------------------------------------------------------------- actions
  function refresh() {
    if (root.status === "needs-setup") { start(); return }
    if (!daemon.running) { start(); return }
    request("device.reconnect", {}, null)
    if (root.connected) { refreshInfo(true); refreshApps(true); refreshProfiles() }
  }

  function reconnect() { request("device.reconnect", {}, null) }

  function select(udid) {
    request("device.select", { udid: udid }, function(ok, d) {
      if (!ok) root.app.toast(Model.errorMessage(d), "error")
      else root.app.setSetting("defaultDevice", udid)
    })
  }

  // then(ok): called after the reply (connect uses it to ask for the app list next)
  function refreshInfo(fresh, then) {
    request("device.info", { fresh: !!fresh }, function(ok, d) {
      if (ok) {
        root.info = d; root.location = d.location || null
        if (d.condition !== undefined) root.condition = d.condition ? Object.assign({ active: true }, d.condition) : null
        _updateCurrent()
      }
      if (then) then(ok)
    })
  }

  // force: re-read the list from the iPhone instead of the daemon's cache (the Reload buttons).
  // Sizes are slow on a real iPhone: they're only asked for through wantAppSizes().
  function refreshApps(force) {
    _listApps({ icons: true, force: !!force })
  }

  // Something shows or sorts by app sizes: ask once per device per sizesTtl (they come as an appSizes event).
  function wantAppSizes() {
    if (!root.connected || Date.now() - root._sizesAskedAt < root.sizesTtl) return
    root._sizesAskedAt = Date.now()
    _listApps({ icons: true, sizes: true })
  }

  function _listApps(args) {
    if (root.appsStatus !== "ready") root.appsStatus = "loading"
    request("apps.list", args, function(ok, d) {
      if (ok) {
        // {apps, stale, fetchedAt}; older daemons answer with the bare list
        var wrapped = !!d && d.apps !== undefined
        root.appsStale = wrapped && !!d.stale
        root.appsFetchedAt = wrapped && d.fetchedAt !== undefined ? d.fetchedAt : null
        _setApps(wrapped ? d.apps : (d || []))
        root.appsStatus = "ready"; root.appsError = null
      } else {
        root.appsError = d
        if (root.appsStatus !== "ready") root.appsStatus = "error"
        if (args.sizes) root._sizesAskedAt = 0
      }
    })
  }

  // Profiles aren't needed to show the page: fetched a moment after the app list on connect.
  Timer {
    id: profilesLater
    interval: 3000
    onTriggered: if (root.connected && root.profilesStatus !== "ready" && root.profilesStatus !== "loading") root.refreshProfiles()
  }
  // The Profiles tab (or anything else that shows them) can call this to have them right away.
  function wantProfiles() {
    if (root.connected && root.profilesStatus !== "ready" && root.profilesStatus !== "loading") { profilesLater.stop(); refreshProfiles() }
  }

  function refreshProfiles() {
    if (root.profilesStatus !== "ready") root.profilesStatus = "loading"
    request("profiles.list", {}, function(ok, d) {
      if (ok) { root.profiles = d; root.profilesStatus = "ready"; root.profilesError = null }
      else { root.profilesError = d; if (root.profilesStatus !== "ready") root.profilesStatus = "error" }
    })
  }

  function launchApp(a) {
    _setBusy(a.bundleId, "launch")
    request("app.launch", { bundleId: a.bundleId }, function(ok, d) {
      _setBusy(a.bundleId, "")
      if (ok) { root.app.toast("Opened " + a.name, "ok"); _patchApp(a.bundleId, { running: true, pid: d.pid }) }
      else root.app.toast(Model.errorMessage(d), "error")
    })
  }

  function stopApp(a) {
    _setBusy(a.bundleId, "kill")
    request("app.kill", { bundleId: a.bundleId }, function(ok, d) {
      _setBusy(a.bundleId, "")
      if (ok) { root.app.toast(d.killed.length ? "Stopped " + a.name : a.name + " wasn't running", "ok"); _patchApp(a.bundleId, { running: false, pid: null }) }
      else root.app.toast(Model.errorMessage(d), "error")
    })
  }

  function uninstallApp(a) {
    root.app.confirm({
      title: "Remove " + a.name + "?",
      message: "This deletes the app and all of its data from your iPhone. You can't undo this.",
      confirmText: "Remove app", cancelText: "Cancel", destructive: true, glyph: Model.Glyph.trash,
      onConfirm: function() {
        _setBusy(a.bundleId, "uninstall")
        request("app.uninstall", { bundleId: a.bundleId }, function(ok, d) {
          _setBusy(a.bundleId, "")
          if (ok) {
            _setApps(root.apps.filter(function(x) { return x.bundleId !== a.bundleId }))
            root.app.toast("Removed " + a.name, "ok")
          } else root.app.toast(Model.errorMessage(d), "error")
        })
      }
    })
  }

  function screenshot(callback) {
    root.shooting = true
    request("screenshot", {}, function(ok, d) {
      root.shooting = false
      if (ok) root.lastShot = d.path
      else root.app.toast(Model.errorMessage(d), "error")
      if (callback) callback(ok, d)
    })
  }

  // fps only caps the screenshot mode; the stream sends what the phone gives (up to 30/s)
  function _mirrorArgs(fps) {
    var a = { mode: root.mirrorMode }
    if (root.mirrorMode === "screenshot") a.fps = fps || root.mirrorFps
    else a.fps = 30
    return a
  }

  function startMirror(fps) {
    root._mirrorPaused = false
    root.mirrorError = null
    request("mirror.start", _mirrorArgs(fps), function(ok, d) {
      if (ok) { root.mirroring = true; if (d.mode && d.mode !== "starting") root.mirrorActive = d.mode }
      else { root.mirrorError = d; root.app.toast(Model.errorMessage(d), "error") }
    })
  }

  function setMirrorMode(mode) {
    if (mode === root.mirrorMode) return
    root.mirrorMode = mode
    root.app.setSetting("mirrorMode", mode)
    root.mirrorNotice = ""
    if (root.mirroring) request("mirror.start", _mirrorArgs(), null)
  }

  // DevicePhoneFrame.frameShown: tells the daemon this frame is on screen (backpressure)
  function ackFrame(url) {
    if (!root.mirroring) return
    var q = String(url).lastIndexOf("?")
    var seq = q >= 0 ? parseInt(String(url).slice(q + 1)) : 0
    if (seq > 0) daemon.write({ id: 0, cmd: "mirror.ack", args: { seq: seq } })
  }

  function refreshMirrorInfo() {
    request("mirror.status", {}, function(ok, d) { if (ok) root.mirrorInfo = d })
  }

  function startRecording(maxSeconds) {
    root.recording = { state: "recording", seconds: 0, bytes: 0, maxSeconds: maxSeconds || 60, starting: true }
    var args = { maxSeconds: maxSeconds || 60 }
    if (!root.mirroring) args.mode = root.mirrorMode
    request("record.start", args, function(ok, d) {
      if (ok) root.recording = d
      else { root.recording = { state: "failed", error: d }; root.app.toast(Model.errorMessage(d), "error") }
    }, 60000)
  }

  function stopRecording() {
    if (root.recording) root.recording = Object.assign({}, root.recording, { state: "finishing" })
    request("record.stop", {}, function(ok, d) {
      if (ok) root.recording = d
      else root.recording = { state: "failed", error: d }
    }, 180000)
  }

  function dismissRecording() { if (!root.recordingActive) root.recording = null }

  // Live view nobody can see (the iPhone page is hidden): stop it after a short grace period and
  // start it again when a DevicePhoneFrame with mirrorService shows up. Recordings don't depend on it.
  property int mirrorViewers: 0
  property bool _mirrorPaused: false
  onMirrorViewersChanged: {
    if (root.mirrorViewers > 0) {
      mirrorPauseTimer.stop()
      if (root._mirrorPaused) { root._mirrorPaused = false; if (root.connected) startMirror() }
    } else if (root.mirroring) {
      mirrorPauseTimer.restart()
    }
  }
  onMirroringChanged: if (root.mirroring && root.mirrorViewers === 0) mirrorPauseTimer.restart()
  Timer {
    id: mirrorPauseTimer
    interval: 5000
    onTriggered: if (root.mirrorViewers === 0 && root.mirroring) { root.stopMirror(); root._mirrorPaused = true }
  }

  function stopMirror() {
    root._mirrorPaused = false
    // keep the last frame visible as a still
    // (no more frames are written after mirror.stop, so the rotating file is stable)
    if (root.framePath !== "") root.lastShot = root.framePath
    request("mirror.stop", {}, function() { root.mirroring = false; root.framePath = "" })
  }

  function setFps(fps) {
    root.mirrorFps = fps
    if (root.mirroring && root.mirrorActive === "screenshot") request("mirror.start", { fps: fps }, null)
  }

  function setLocation(lat, lon, label) {
    root.locating = true
    request("location.set", { lat: lat, lon: lon }, function(ok, d) {
      root.locating = false
      if (ok) { root.location = Object.assign({ label: label || "" }, d); root.app.toast("Your iPhone now thinks it's " + (label ? "in " + label : "at " + lat.toFixed(4) + ", " + lon.toFixed(4)), "ok") }
      else root.app.toast(Model.errorMessage(d), "error")
    })
  }

  function clearLocation() {
    root.locating = true
    request("location.clear", {}, function(ok, d) {
      root.locating = false
      if (ok) { root.location = null; root.app.toast("Real location restored", "ok") }
      else root.app.toast(Model.errorMessage(d), "error")
    })
  }

  property bool mountingDdi: false
  function mountDdi() {
    root.mountingDdi = true
    request("ddi.mount", {}, function(ok, d) {
      root.mountingDdi = false
      if (ok) { root.info = Object.assign({}, root.info, { ddiMounted: true }); root.app.toast("Developer tools are ready on your iPhone", "ok") }
      else root.app.toast(Model.errorMessage(d), "error")
    })
  }

  function revealDevMode(callback) {
    request("devmode.reveal", {}, function(ok, d) {
      if (ok) root.app.toast("Now open Settings › Privacy & Security on your iPhone", "ok")
      else root.app.toast(Model.errorMessage(d), "warn")
      if (callback) callback(ok, d)
    })
  }

  function startPair(method) {
    root.pair = { phase: "starting", method: method, message: "Starting…" }
    request("pair.start", { method: method }, function(ok, d) {
      if (!ok) root.pair = { phase: "failed", method: method, message: Model.errorMessage(d), detail: d.detail || "" }
    })
  }
  function cancelPair() { request("pair.cancel", {}, null); root.pair = null }
  function sendPairCode(code) { request("pair.code", { code: code }, null) }
}
