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
  // Apps › an app's details (DeviceAppDetailView) in place of the list: its bundle id ("" = the list), and
  // the section last shown (Data or Files), kept for this session so the next app opens on it too
  property string appDetail: ""
  property string appSection: "defaults"     // defaults | stores | reset | files
  property string appDataSection: "defaults" // the last of defaults/stores/reset, for section: "data" links
  onAppSectionChanged: if (appSection !== "files") appDataSection = appSection

  // section: defaults | stores | reset | files, or "data" for the last data section
  function openApp(bundleId, section) {
    if (section === "data") page.appSection = page.appDataSection
    else if (["defaults", "stores", "reset", "files"].indexOf(section) >= 0) page.appSection = section
    page.tab = "apps"
    page.appDetail = bundleId || ""
  }
  function closeApp() {
    var bid = page.appDetail
    page.appDetail = ""
    for (var i = 0; i < appsView.shown.length; i++) if (appsView.shown[i].bundleId === bid) page.cursor = i
  }

  title: "iPhone"
  subtitle: showDevice ? "Apps, screen, files, network, performance, debugging and details of your iPhone." : "Pair your iPhone once and iOS Studio keeps it connected."

  // A simulated condition changes the whole phone: say so next to the title on every tab.
  status: StatusPill {
    visible: page.svc.conditionActive
    level: "warn"; glyph: Model.Glyph.speed
    text: page.svc.conditionActive ? "Simulating " + page.svc.condition.name : ""
    MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor; onClicked: page.tab = "network" }
  }

  actions: [
    IconButton { glyph: Model.Glyph.link; tooltip: "Pair another iPhone"; visible: page.showDevice; onClicked: page.beginPair(page.usbPresent() ? "usb" : "wifi") },
    IconButton { glyph: Model.Glyph.refresh; tooltip: "Refresh (r)"; onClicked: page.refresh() }
  ]

  function usbPresent() {
    for (var i = 0; i < svc.devices.length; i++) if (svc.devices[i].usb) return true
    return false
  }

  readonly property var tabs: [
    { key: "apps", label: "Apps", glyph: Model.Glyph.apps, count: svc.apps.length || undefined },
    { key: "screen", label: "Screen", glyph: Model.Glyph.camera },
    { key: "files", label: "Files", glyph: Model.Glyph.folder },
    { key: "location", label: "Location", glyph: Model.Glyph.location },
    { key: "network", label: "Network", glyph: Model.Glyph.network, level: svc.conditionActive || svc.capturing ? "warn" : "",
      count: svc.conditionActive ? "On" : (svc.capturing ? "Rec" : undefined) },
    { key: "performance", label: "Performance", glyph: Model.Glyph.speedometer },
    { key: "debug", label: "Debug", glyph: Model.Glyph.bug },
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
    if (p.action === "pair") Qt.callLater(function() { page.beginPair(p.method || (page.usbPresent() ? "usb" : "wifi")) })
    else if (p.action === "devmode" || p.action === "developer-mode") Qt.callLater(page.openDevMode)
    else if (p.action === "reconnect") svc.reconnect()
    else if (p.action === "ddi") svc.mountDdi()
    else if (p.action === "screenshot") { page.tab = "screen"; svc.screenshot() }
    else if (p.action === "compare") { page.tab = "screen"; screenTab.withItem(function(v) { v.pane = "compare" }) }
    else if (p.action === "mirror") { page.tab = "screen"; if (svc.connected && !svc.mirroring) svc.startMirror() }
    else if (p.tab === "files" && ["media", "camera"].indexOf(p.action) >= 0) filesTab.withItem(function(v) { v.show(p.action) })
    // an app's details: {tab: "apps", app: bundleId, section: "data"|"files"}; the old Files › App files and
    // App data links ("device:files:apps", "device:files:data", with an optional bundleId) land there too
    else if (p.tab === "files" && (p.action === "apps" || p.action === "data")) {
      var target = p.app || p.bundleId || ""
      if (target) page.openApp(target, p.action === "data" ? "data" : "files")
      else { page.tab = "apps"; page.appDetail = "" }
    }
    else if (p.tab === "apps" && (p.app || p.bundleId)) page.openApp(p.app || p.bundleId, p.section)
    // a fix from the daemon ("Stop the app", e.g. appdata's app-running): the app whose details are open
    else if (p.action === "stop-app") {
      var open = page.appDetail ? svc.apps.filter(function(x) { return x.bundleId === page.appDetail })[0] : null
      if (open) svc.stopApp(open)
      else page.tab = "apps"
    }
    if (svc.connected && svc.appsStatus === "unknown") svc.refreshApps()
  }

  function refresh() { svc.refresh() }
  // Esc: from an app's details back to the list
  function goBack() { if (page.appDetail !== "") page.closeApp() }
  function focusSearch() {
    if (!page.showDevice) return
    var screen = screenTab.item
    if (page.tab === "screen" && screen && screen.pane === "elements") { screen.focusFilter(); return }
    page.tab = "apps"; page.appDetail = ""; appsView.search.focusField()
  }

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
      if (page.tab === "screen") return !!screenTab.item && screenTab.item.handleKey(a)   // Elements/Audit: j/k, Enter presses
      return false
    }
    if (page.appDetail !== "") {
      if (event.key === Qt.Key_Backspace) { page.closeApp(); return true }
      var open = svc.apps.filter(function(x) { return x.bundleId === page.appDetail })[0]
      if (!open) return false
      if (event.text === "o") { svc.launchApp(open); return true }
      if (a === "copy") { Util.execArgv(["wl-copy", open.bundleId]); app.toast("Copied " + open.bundleId); return true }
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
    if (a === "activate") { page.openApp(cur.bundleId); return true }
    if (event.text === "o") { svc.launchApp(cur); return true }
    if (a === "delete") { if (cur.removable) svc.uninstallApp(cur); return true }
    if (a === "copy") { Util.execArgv(["wl-copy", cur.bundleId]); app.toast("Copied " + cur.bundleId); return true }
    return false
  }

  // ---- the daemon keeps crashing (DeviceService gave up restarting it)
  ErrorState {
    Layout.fillWidth: true
    visible: page.svc.status === "error"
    banner: true
    app: page.app
    error: page.svc.error
    retryText: "Retry"
    onRetry: page.svc.start()
  }

  // ---- onboarding
  DeviceConnect {
    Layout.fillWidth: true
    visible: !page.showDevice
    app: page.app
    onPairRequested: function(m) { page.beginPair(m) }
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
    onDevModeHelp: page.openDevMode()
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
    visible: page.showDevice && page.tab === "apps" && page.appDetail === "" && (page.svc.connected || page.svc.apps.length > 0)
    app: page.app
    cursor: page.cursor
    onCursorMoved: function(i) { page.cursor = i }
    onOpenApp: function(bid) { page.openApp(bid) }
  }
  // Each tab below is built the first time it's opened, in the background, and then kept (its state
  // survives switching tabs). Loaded by file, so not even its QML is compiled before that. `active`
  // (onScreen) tells it whether it's showing: hidden tabs do no work.
  component TabLoader: Loader {
    id: tl
    required property var app
    required property string file           // in components/
    property bool shown: false              // this tab is the current one
    property bool onScreen: false           // and the iPhone page is the current page
    property bool hasActive: true           // the view has an `active` property
    property var props: ({})                // more initial properties (keep them current with a Binding)
    property var _pending: []
    property bool hurry: false              // finish the load synchronously (see the Timer)
    Layout.fillWidth: true
    visible: shown && status === Loader.Ready
    asynchronous: !hurry
    // `active` isn't an initial property: the Binding below sets it once the view exists, so the view
    // sees one change to true and starts its work once.
    function initial() { return Object.assign({ app: tl.app }, tl.props) }
    function ensure() {
      if (item || status === Loader.Loading) return
      setSource(Qt.resolvedUrl("../components/" + file), initial())
    }
    // Started in the background so the tab bar answers at once; what's left after a frame or two is
    // finished in one go (much quicker than trickling between frames). Turning `asynchronous` off
    // mid-load would drop the initial properties, so the load starts over synchronously.
    Timer {
      interval: 50
      running: tl.shown && tl.status === Loader.Loading && !tl.hurry
      onTriggered: {
        tl.setSource("")
        tl.hurry = true
        tl.setSource(Qt.resolvedUrl("../components/" + tl.file), tl.initial())
      }
    }
    onShownChanged: if (shown) ensure()
    Component.onCompleted: if (shown) ensure()
    // fn(item) now, or as soon as the tab has loaded (a deep link into a tab not built yet)
    function withItem(fn) {
      if (item) { fn(item); return }
      _pending = _pending.concat([fn])
      ensure()
    }
    onLoaded: {
      var p = _pending
      _pending = []
      for (var i = 0; i < p.length; i++) p[i](item)
    }
    onStatusChanged: if (status === Loader.Error) console.warn("iPhone tab failed to load:", file)
    Binding { target: tl.item; property: "active"; value: tl.onScreen; when: tl.hasActive && !!tl.item }
  }
  readonly property bool pageShown: page.app.page === "device"
  // Profiles load when a tab that shows them opens (DeviceService.wantProfiles: a no-op once they're
  // loaded or loading; older services don't have it).
  function wantProfiles() { if (typeof page.svc.wantProfiles === "function") page.svc.wantProfiles() }

  // A tab that takes a moment to build shows a spinner, after a short delay so a quick one doesn't flash.
  readonly property var tabLoaders: ({ apps: appDetailTab, screen: screenTab, files: filesTab, location: locationTab, network: networkTab,
                                       performance: performanceTab, debug: debugTab, profiles: profilesTab, info: infoTab })
  readonly property bool tabLoading: {
    var l = page.tabLoaders[page.tab]
    return !!l && l.shown && l.status === Loader.Loading
  }
  property bool tabSlow: false
  onTabLoadingChanged: tabSlow = false
  Timer { interval: 150; running: page.tabLoading; onTriggered: page.tabSlow = true }
  Item {
    Layout.fillWidth: true
    implicitHeight: Style.space(160)
    visible: page.tabLoading && page.tabSlow
    Spinner { anchors.centerIn: parent; running: parent.visible; size: Style.font.iconLarge }
  }

  TabLoader {
    id: appDetailTab
    app: page.app
    file: "DeviceAppDetailView.qml"
    shown: page.showDevice && page.tab === "apps" && page.appDetail !== "" && page.svc.connected
    onScreen: shown && page.pageShown
    props: ({ bundleId: page.appDetail, section: page.appSection })
    Binding { target: appDetailTab.item; property: "bundleId"; value: page.appDetail; when: !!appDetailTab.item }
    Binding { target: appDetailTab.item; property: "section"; value: page.appSection; when: !!appDetailTab.item }
    Connections {
      target: appDetailTab.item
      function onSectionPicked(s) { page.appSection = s }
      function onBack() { page.closeApp() }
    }
  }
  TabLoader {
    id: screenTab
    app: page.app
    file: "DeviceScreenView.qml"
    shown: page.showDevice && page.tab === "screen" && page.svc.connected
    onScreen: shown && page.pageShown
    Connections {
      target: screenTab.item
      function onDevModeHelp() { page.openDevMode() }
    }
  }
  TabLoader {
    id: filesTab
    app: page.app
    file: "DeviceFilesView.qml"
    shown: page.showDevice && page.tab === "files" && page.svc.connected
    onScreen: shown && page.pageShown
  }
  TabLoader {
    id: locationTab
    app: page.app
    file: "DeviceLocationView.qml"
    hasActive: false
    shown: page.showDevice && page.tab === "location" && page.svc.connected
  }
  TabLoader {
    id: networkTab
    app: page.app
    file: "DeviceNetworkView.qml"
    shown: page.showDevice && page.tab === "network" && page.svc.connected
    onScreen: shown && page.pageShown
    onOnScreenChanged: if (onScreen) page.wantProfiles()
  }
  TabLoader {
    id: performanceTab
    app: page.app
    file: "DevicePerformanceView.qml"
    shown: page.showDevice && page.tab === "performance" && page.svc.connected
    onScreen: shown && page.pageShown
  }
  TabLoader {
    id: debugTab
    app: page.app
    file: "DeviceDebugView.qml"
    shown: page.showDevice && page.tab === "debug" && page.svc.connected
    onScreen: shown && page.pageShown
  }
  TabLoader {
    id: profilesTab
    app: page.app
    file: "DeviceProfilesView.qml"
    hasActive: false
    shown: page.showDevice && page.tab === "profiles" && (page.svc.connected || page.svc.profiles.length > 0)
    onScreen: shown && page.pageShown
    onOnScreenChanged: if (onScreen) page.wantProfiles()
  }
  TabLoader {
    id: infoTab
    app: page.app
    file: "DeviceInfoView.qml"
    hasActive: false
    shown: page.showDevice && page.tab === "info"
  }

  // ---- sheets, built the first time they open
  function beginPair(method) {
    pairLoader.ensure()
    if (pairLoader.item) pairLoader.item.begin(method)
  }
  function openDevMode() {
    devModeLoader.active = true
    devModeLoader.item.open()
  }
  Loader {
    id: pairLoader
    visible: false
    function ensure() { if (!item) setSource(Qt.resolvedUrl("../components/DevicePairSheet.qml"), { app: page.app }) }
  }
  Loader {
    id: devModeLoader
    visible: false
    active: false
    sourceComponent: Component {
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
          ActionButton { text: "Show the switch"; glyph: Model.Glyph.eye; disabled: !page.usbPresent(); tooltip: page.usbPresent() ? "" : "Needs the cable"; onClicked: page.svc.revealDevMode() },
          ActionButton { text: "Done"; kind: "primary"; onClicked: { devModeSheet.close(); page.svc.refreshInfo(true) } }
        ]
      }
    }
  }

  Connections {
    target: page.svc
    function onConnStateChanged() {
      if (page.svc.connected && page.svc.appsStatus === "unknown" && page.app.page === "device") page.svc.refreshApps()
    }
  }
}
