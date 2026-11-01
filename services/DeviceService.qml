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

  // apps
  property var apps: []
  property string appsStatus: "unknown"    // unknown | loading | ready | error
  property var appsError: null
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
  property string lastShot: ""             // latest one-off screenshot
  property bool shooting: false
  property var mirrorError: null
  readonly property string screenSource: framePath !== "" ? "file://" + framePath + "?" + frameSeq
                                       : (lastShot !== "" ? "file://" + lastShot : "")

  // location
  property var location: null              // {lat, lon} while simulated
  property bool locating: false

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
      root.mirroring = false
      if (code === 3) {         // no pymobiledevice3 python (bin/iosstudio-deviced)
        root._needsSetup()
      }
    }
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
        _updateCurrent()
      }
      break
    case "frame":
      if (root.mirroring && data) { root.framePath = data.path; root.frameSeq = data.seq }
      break
    case "mirror":
      root.mirroring = !!(data && data.running)
      if (data && data.fps) root.mirrorFps = data.fps
      if (data && data.error) { root.mirrorError = data.error; root.app.toast(Model.errorMessage(data.error), "warn") }
      if (!root.mirroring) root.framePath = ""
      break
    case "appIcon":
      _patchApp(data.bundleId, { icon: data.path })
      break
    case "appSizes":
      if (data && data.sizes) {
        var list = root.apps.slice()
        for (var i = 0; i < list.length; i++) {
          var s = data.sizes[list[i].bundleId]
          if (s !== undefined) list[i] = Object.assign({}, list[i], { size: s })
        }
        root.apps = list
      }
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
      refreshInfo(true)
      if (root.appsStatus !== "ready") refreshApps()
      if (root.profilesStatus !== "ready") refreshProfiles()
    } else if (c.state !== "connected" && c.state !== "locked" && (prev === "connected" || prev === "locked")) {
      root._disconnectedAt = Date.now()
      root.mirroring = false
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
        root.apps = copy
        return
      }
    }
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
      return { title: c.name || name, detail: parts.join(" · "), level: warn ? "warn" : "ok", badge: "",
               hint: warn ? "Developer Mode is off" : "" }
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
    if (root.connected) { refreshInfo(true); refreshApps(); refreshProfiles() }
  }

  function reconnect() { request("device.reconnect", {}, null) }

  function select(udid) {
    request("device.select", { udid: udid }, function(ok, d) {
      if (!ok) root.app.toast(Model.errorMessage(d), "error")
      else root.app.setSetting("defaultDevice", udid)
    })
  }

  function refreshInfo(fresh) {
    request("device.info", { fresh: !!fresh }, function(ok, d) {
      if (ok) { root.info = d; root.location = d.location || null; _updateCurrent() }
    })
  }

  function refreshApps() {
    if (root.appsStatus !== "ready") root.appsStatus = "loading"
    request("apps.list", { icons: true }, function(ok, d) {
      if (ok) {
        // keep sizes we already know until the new appSizes event lands
        var old = {}
        for (var i = 0; i < root.apps.length; i++) old[root.apps[i].bundleId] = root.apps[i].size
        for (var j = 0; j < d.length; j++) if (d[j].size === null && old[d[j].bundleId]) d[j].size = old[d[j].bundleId]
        root.apps = d; root.appsStatus = "ready"; root.appsError = null
      } else { root.appsError = d; if (root.appsStatus !== "ready") root.appsStatus = "error" }
    })
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
            root.apps = root.apps.filter(function(x) { return x.bundleId !== a.bundleId })
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

  function startMirror(fps) {
    root.mirrorError = null
    request("mirror.start", { fps: fps || root.mirrorFps }, function(ok, d) {
      if (ok) { root.mirroring = true; root.mirrorFps = d.fps }
      else { root.mirrorError = d; root.app.toast(Model.errorMessage(d), "error") }
    })
  }

  function stopMirror() {
    // keep the last frame visible as a still
    // (no more frames are written after mirror.stop, so the rotating file is stable)
    if (root.framePath !== "") root.lastShot = root.framePath
    request("mirror.stop", {}, function() { root.mirroring = false; root.framePath = "" })
  }

  function setFps(fps) {
    root.mirrorFps = fps
    if (root.mirroring) request("mirror.start", { fps: fps }, null)
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
