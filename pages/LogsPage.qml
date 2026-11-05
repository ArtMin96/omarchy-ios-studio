import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import qs.components
import "../Model.js" as Model

// Logs (logs slice): live device log, crash reports, build logs.
PageFrame {
  id: page
  required property var app
  readonly property var svc: app.logs

  property string tab: "device"
  readonly property bool onScreen: app.page === "logs"

  title: "Logs"
  subtitle: tab === "device" ? "What your iPhone is saying right now, filtered to your apps."
          : tab === "console" ? "What your app prints (print(), stdout and stderr) when iOS Studio launches it."
          : tab === "crashes" ? "Crash reports from your iPhone, explained."
          : "Every build you ran, with its log and problems."
  scrollable: false
  maxContentWidth: Style.space(1500)

  readonly property var tabs: [
    { key: "device", label: "Device log", glyph: Model.Glyph.console },
    { key: "console", label: "Console", glyph: Model.Glyph.consoleLine },
    { key: "crashes", label: "Crashes", glyph: Model.Glyph.crash, count: svc.newCrashCount > 0 ? svc.newCrashCount : undefined, level: "warn" },
    { key: "builds", label: "Build logs", glyph: Model.Glyph.hammer }
  ]

  // The live stream runs only while this tab is on screen (unless "Keep streaming" is on).
  Binding { target: page.svc; property: "viewVisible"; value: page.onScreen && page.tab === "device" }

  // Crash "new" badges: clear the sidebar badge on open, keep row badges during the visit.
  property bool _inCrashes: false
  readonly property bool crashesShown: onScreen && tab === "crashes"
  onCrashesShownChanged: {
    if (crashesShown) { svc.beginCrashVisit(); _inCrashes = true }
    else if (_inCrashes) { svc.endCrashVisit(); _inCrashes = false }
  }

  function setTab(k) { page.tab = k }

  // ---- page hooks
  function activated(params) {
    var p = params || {}
    var t = String(p.tab || "").toLowerCase()
    if (t === "log" || t === "live" || t === "syslog") t = "device"
    if (t === "print" || t === "stdout" || t === "output") t = "console"
    if (t === "build") t = "builds"
    if (page.tabs.some(function(x) { return x.key === t })) page.tab = t
    else if (p.crash || p.action === "crashes") page.tab = "crashes"
    if (svc.builds.length === 0) svc.refreshBuilds()
  }

  function refresh() {
    if (tab === "crashes") svc.refreshCrashes()
    else if (tab === "builds") svc.refreshBuilds()
    else svc.refresh()
  }

  function focusSearch() {
    if (tab === "device") deviceTab.search.focusField()
    else if (tab === "console") consoleTab.search.focusField()
  }

  // Esc: deselect the line, then clear the search. (A QML method can't be named `escape`,
  // the shell calls goBack() instead.)
  function goBack() {
    if (tab === "device" && deviceTab.view.selectedSeq >= 0) { deviceTab.view.deselect(); return true }
    if (tab === "device" && deviceTab.search.text !== "") { deviceTab.search.clear(); return true }
    if (tab === "console" && consoleTab.search.text !== "") { consoleTab.search.clear(); return true }
    return false
  }

  function handleKey(event) {
    if (event.key === Qt.Key_Escape) return goBack()
    var a = Model.keyAction(event)
    if (a === "left" || a === "right") {
      var keys = ["device", "console", "crashes", "builds"]
      var i = keys.indexOf(page.tab)
      page.tab = keys[(i + (a === "right" ? 1 : -1) + keys.length) % keys.length]
      return true
    }
    if (tab === "device") {
      if (!deviceTab.ready) return false
      if (a === "down") { deviceTab.view.selectNext(1); return true }
      if (a === "up") { deviceTab.view.selectNext(-1); return true }
      if (a === "end") { deviceTab.view.jumpToLatest(); return true }
      if (a === "copy") { deviceTab.copySelected(); return true }
      if (event.key === Qt.Key_Space) { deviceTab.togglePause(); return true }
      if (event.text === "s") { svc.streaming ? svc.stop() : svc.start(); return true }
      return false
    }
    if (tab === "console") {
      if (!consoleTab.ready) return false
      if (a === "end") { consoleTab.view.scrollToEnd(); return true }
      if (a === "copy") { consoleTab.copySelected(); return true }
      if (event.key === Qt.Key_Space) { consoleTab.togglePause(); return true }
      return false
    }
    if (tab === "crashes") {
      if (a === "down") return crashTab.moveCursor(1)
      if (a === "up") return crashTab.moveCursor(-1)
      if (a === "copy") { crashTab.copyReport(); return true }
      return false
    }
    if (tab === "builds") {
      if (a === "down") return buildsTab.moveCursor(1)
      if (a === "up") return buildsTab.moveCursor(-1)
      return false
    }
    return false
  }

  // Same gaps around the tabs as the iPhone page.
  PageTabs {
    Layout.fillWidth: true
    Layout.topMargin: Style.spacing.md
    tabs: page.tabs
    current: page.tab
    onSelected: function(k) { page.setTab(k) }
  }

  Item {
    Layout.fillWidth: true
    Layout.fillHeight: true

    LogDeviceTab { id: deviceTab; anchors.fill: parent; app: page.app; visible: page.tab === "device" }
    LogConsoleTab { id: consoleTab; anchors.fill: parent; app: page.app; visible: page.tab === "console" }
    CrashBrowser { id: crashTab; anchors.fill: parent; app: page.app; visible: page.tab === "crashes" }
    LogBuildsTab { id: buildsTab; anchors.fill: parent; app: page.app; visible: page.tab === "builds" }
  }
}
