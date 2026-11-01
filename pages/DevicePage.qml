import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import qs.components
import "../Model.js" as Model

PageFrame {
  id: page
  required property var app
  readonly property var svc: app.device
  // A device to show: known, and either connected or at least paired somehow.
  readonly property bool showDevice: !!svc.current && svc.connState !== "not-paired" && svc.status !== "needs-setup"
  property string tab: "apps"
  property int cursor: -1

  title: "iPhone"
  subtitle: showDevice ? "Apps, screen, location, network and details of your iPhone." : "Pair your iPhone once and iOS Studio keeps it connected."

  // A simulated condition changes the whole phone: say so next to the title on every tab.
  status: StatusPill {
    visible: page.svc.conditionActive
    level: "warn"; glyph: Model.Glyph.speed
    text: page.svc.conditionActive ? "Simulating " + page.svc.condition.name : ""
    MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor; onClicked: page.tab = "network" }
  }

  actions: [
    IconButton { glyph: Model.Glyph.link; tooltip: "Pair another iPhone"; visible: page.showDevice; onClicked: pairSheet.begin(page.usbPresent() ? "usb" : "wifi") },
    IconButton { glyph: Model.Glyph.refresh; tooltip: "Refresh (r)"; onClicked: page.refresh() }
  ]

  function usbPresent() {
    for (var i = 0; i < svc.devices.length; i++) if (svc.devices[i].usb) return true
    return false
  }

  readonly property var tabs: [
    { key: "apps", label: "Apps", glyph: Model.Glyph.apps, count: svc.apps.length || undefined },
    { key: "screen", label: "Screen", glyph: Model.Glyph.camera },
    { key: "location", label: "Location", glyph: Model.Glyph.location },
    { key: "network", label: "Network", glyph: Model.Glyph.network, level: svc.conditionActive || svc.capturing ? "warn" : "",
      count: svc.conditionActive ? "On" : (svc.capturing ? "Rec" : undefined) },
    { key: "performance", label: "Performance", glyph: Model.Glyph.speedometer },
    { key: "profiles", label: "Profiles", glyph: Model.Glyph.signature, count: profileWarn() || undefined, level: profileWarn() ? "warn" : "" },
    { key: "info", label: "Info", glyph: Model.Glyph.info }
  ]

  function profileWarn() {
    var n = 0
    for (var i = 0; i < svc.profiles.length; i++) {
      var p = svc.profiles[i]
      if (!p.expired && p.expires * 1000 - Date.now() < 2 * 86400000) n++
    }
    return n
  }

  // ---- page hooks
  function activated(params) {
    var p = params || {}
    if (p.tab) page.tab = p.tab
    if (p.action === "pair") Qt.callLater(function() { pairSheet.begin(p.method || (page.usbPresent() ? "usb" : "wifi")) })
    else if (p.action === "devmode" || p.action === "developer-mode") Qt.callLater(function() { devModeSheet.open() })
    else if (p.action === "reconnect") svc.reconnect()
    else if (p.action === "ddi") svc.mountDdi()
    else if (p.action === "screenshot") { page.tab = "screen"; svc.screenshot() }
    else if (p.action === "mirror") { page.tab = "screen"; if (svc.connected && !svc.mirroring) svc.startMirror() }
    if (svc.connected && svc.appsStatus === "unknown") svc.refreshApps()
  }

  function refresh() { svc.refresh() }
  function focusSearch() { if (page.showDevice) { page.tab = "apps"; appsView.search.focusField() } }

  function handleKey(event) {
    if (!page.showDevice) return false
    var a = Model.keyAction(event)
    if (a === "left" || a === "right") {
      var i = 0
      for (var k = 0; k < page.tabs.length; k++) if (page.tabs[k].key === page.tab) i = k
      i = (i + (a === "right" ? 1 : -1) + page.tabs.length) % page.tabs.length
      page.tab = page.tabs[i].key
      return true
    }
    if (page.tab !== "apps") {
      if (page.tab === "screen" && event.text === "s") { svc.screenshot(); return true }
      return false
    }
    var n = appsView.shown.length
    if (n === 0) return false
    if (a === "down") { page.cursor = Math.min(n - 1, page.cursor + 1); return true }
    if (a === "up") { page.cursor = Math.max(0, page.cursor - 1); return true }
    if (a === "home") { page.cursor = 0; return true }
    if (a === "end") { page.cursor = n - 1; return true }
    var cur = appsView.current()
    if (!cur) return false
    if (a === "activate") { svc.launchApp(cur); return true }
    if (a === "delete") { if (cur.removable) svc.uninstallApp(cur); return true }
    if (a === "copy") { Util.execArgv(["wl-copy", cur.bundleId]); app.toast("Copied " + cur.bundleId); return true }
    return false
  }

  // ---- onboarding
  DeviceConnect {
    Layout.fillWidth: true
    visible: !page.showDevice
    app: page.app
    onPairRequested: function(m) { pairSheet.begin(m) }
  }

  // ---- device switcher (several iPhones)
  RowLayout {
    Layout.fillWidth: true
    visible: page.showDevice && page.svc.devices.length > 1
    spacing: Style.spacing.lg
    AppText { text: "iPhone"; tone: "dim"; size: "small" }
    ButtonGroupLite {
      options: page.svc.devices.map(function(d) { return { value: d.udid, label: (d.name || d.model || d.udid.substring(0, 8)) + (d.usb ? " · USB" : "") } })
      value: page.svc.current ? page.svc.current.udid : ""
      onPicked: function(v) { page.svc.select(v) }
    }
  }

  // ---- hero
  DeviceHero {
    Layout.fillWidth: true
    visible: page.showDevice
    app: page.app
    onOpenScreen: page.tab = "screen"
    onDevModeHelp: devModeSheet.open()
  }

  // ---- sections
  PageTabs {
    Layout.fillWidth: true
    Layout.topMargin: Style.spacing.md
    visible: page.showDevice
    tabs: page.tabs
    current: page.tab
    onSelected: function(k) { page.tab = k }
  }

  ColumnLayout {
    Layout.fillWidth: true
    visible: page.showDevice && !page.svc.connected && page.tab !== "info"
    EmptyState {
      Layout.fillWidth: true
      compact: true
      glyph: page.svc.connState === "locked" ? Model.Glyph.lock : Model.Glyph.wifiOff
      title: page.svc.connState === "connecting" || page.svc.connState === "searching" ? "Connecting to your iPhone…" : (page.svc.connection.message || "Not connected")
      text: "This part needs the iPhone connected. It reconnects by itself as soon as it's reachable."
      actionText: "Try now"; actionGlyph: Model.Glyph.refresh
      onAction: page.svc.reconnect()
    }
  }

  DeviceAppsView {
    id: appsView
    Layout.fillWidth: true
    visible: page.showDevice && page.tab === "apps" && (page.svc.connected || page.svc.apps.length > 0)
    app: page.app
    cursor: page.cursor
    onCursorMoved: function(i) { page.cursor = i }
  }
  DeviceScreenView {
    Layout.fillWidth: true
    visible: page.showDevice && page.tab === "screen" && page.svc.connected
    app: page.app
  }
  DeviceLocationView {
    Layout.fillWidth: true
    visible: page.showDevice && page.tab === "location" && page.svc.connected
    app: page.app
  }
  DeviceNetworkView {
    Layout.fillWidth: true
    visible: page.showDevice && page.tab === "network" && page.svc.connected
    app: page.app
  }
  DevicePerformanceView {
    Layout.fillWidth: true
    visible: page.showDevice && page.tab === "performance" && page.svc.connected
    app: page.app
  }
  DeviceProfilesView {
    Layout.fillWidth: true
    visible: page.showDevice && page.tab === "profiles" && (page.svc.connected || page.svc.profiles.length > 0)
    app: page.app
  }
  DeviceInfoView {
    Layout.fillWidth: true
    visible: page.showDevice && page.tab === "info"
    app: page.app
  }

  // ---- sheets
  DevicePairSheet { id: pairSheet; app: page.app }

  Sheet {
    id: devModeSheet
    title: "Turn on Developer Mode"
    subtitle: "Needed once, so your own apps can run"
    glyph: Model.Glyph.developer
    preferredWidth: Style.space(520)
    DeviceSteps {
      Layout.fillWidth: true
      steps: [
        { text: "On the iPhone, open Settings › Privacy & Security." },
        { text: "Scroll to the bottom and tap Developer Mode, then switch it on." },
        { text: "Tap Restart. After the restart, unlock the iPhone and tap Turn On." }
      ]
    }
    AppText {
      Layout.fillWidth: true; wrapMode: Text.WordWrap; size: "small"; tone: "faint"
      text: "Don't see Developer Mode? Plug the iPhone in and press “Show the switch”, or run one of your apps once from Projects."
    }
    footer: [
      ActionButton { text: "Show the switch"; glyph: Model.Glyph.eye; enabled: page.usbPresent(); tooltip: page.usbPresent() ? "" : "Needs the cable"; onClicked: page.svc.revealDevMode() },
      ActionButton { text: "Done"; kind: "primary"; onClicked: { devModeSheet.close(); page.svc.refreshInfo(true) } }
    ]
  }

  Connections {
    target: page.svc
    function onConnStateChanged() {
      if (page.svc.connected && page.svc.appsStatus === "unknown" && page.app.page === "device") page.svc.refreshApps()
    }
  }
}
