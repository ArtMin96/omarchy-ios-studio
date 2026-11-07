import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../../lib/Model.js" as Model

// iPhone › Apps › an app › Data: what one of your own apps has saved. Defaults (its UserDefaults plist,
// editable while the app is stopped), Stores (its SwiftData / Core Data / SQLite files, read-only, paged) and
// Reset (wipe the container, or uninstall and install the last build again; both a dry run first).
// Only your own builds: container access needs a development-signed app.
// Daemon: appdata.defaults/defaults.set/defaults.delete/stores/rows/reset (docs/features/app-data.md).
// The section switch and reload live in DeviceAppDetailView's row.
//   DeviceAppDataView { app: root.app; fixedApp: bundleId; pane: "stores"; active: root.active && visible }
ColumnLayout {
  id: root
  required property var app
  readonly property var svc: app.device
  property bool active: false

  property string fixedApp: ""                // the app (a developer build) whose data this shows
  readonly property string bundleId: fixedApp
  property string pane: "defaults"            // defaults | stores | reset: picked in the details' switch
  readonly property var current: {
    for (var i = 0; i < svc.apps.length; i++) if (svc.apps[i].bundleId === bundleId) return svc.apps[i]
    return null
  }
  readonly property bool running: !!current && !!current.running
  readonly property bool stopping: !!current && svc.busyApps[current.bundleId] === "kill"
  readonly property string appName: current ? current.name : "the app"

  // iOS saves a stopped app's settings 2 to 7 s after the stop, and the daemon waits until 10 s have passed
  // before it reads or writes them. The panes use this to say why a read or a save is slow.
  readonly property int flushGraceMs: 10000
  // when each app was last seen to stop (from the apps list, so a Stop anywhere counts)
  property var stoppedAt: ({})
  property var _wasRunning: ({})
  Connections {
    target: root.svc
    function onAppsChanged() {
      var now = {}, at = root.stoppedAt
      for (var i = 0; i < root.svc.apps.length; i++) {
        var a = root.svc.apps[i]
        if (a.running) now[a.bundleId] = true
        else if (root._wasRunning[a.bundleId]) at[a.bundleId] = Date.now()
      }
      root._wasRunning = now
      root.stoppedAt = at
    }
  }
  function waitLeft() {
    var t = root.stoppedAt[root.bundleId]
    return t ? Math.max(0, root.flushGraceMs - (Date.now() - t)) : 0
  }

  spacing: Style.spacing.xl

  // Daemon replies can carry lists as array-likes that Array.isArray() doesn't see (and JSON.stringify()
  // writes as {"0": …}); the panes turn a reply into plain JS arrays and objects first.
  function norm(v) {
    if (v === null || typeof v !== "object") return v
    if (Array.isArray(v) || (typeof v.length === "number" && !("$type" in v) && Object.keys(v).every(function(k) { return /^\d+$/.test(k) }))) {
      var a = []
      for (var i = 0; i < v.length; i++) a.push(root.norm(v[i]))
      return a
    }
    var o = {}
    for (var k in v) o[k] = root.norm(v[k])
    return o
  }

  function stopApp() { if (root.current) root.svc.stopApp(root.current) }
  function reload() {
    if (root.pane === "defaults") defaultsPane.load()
    else if (root.pane === "stores") storesPane.load()
    root.svc.refreshApps()
  }

  DeviceAppDataDefaults {
    id: defaultsPane
    Layout.fillWidth: true
    visible: root.pane === "defaults" && !!root.current
    active: root.active && visible
    view: root
  }
  DeviceAppDataStores {
    id: storesPane
    Layout.fillWidth: true
    visible: root.pane === "stores" && !!root.current
    active: root.active && visible
    view: root
  }
  DeviceAppDataReset {
    Layout.fillWidth: true
    onDone: { defaultsPane.invalidate(); storesPane.invalidate() }
    visible: root.pane === "reset" && !!root.current
    view: root
  }
}
