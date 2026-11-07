import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../Model.js" as Model

// iPhone › Apps › an app: opened from a row in the Apps list, in place of the list. A header with the
// app and its actions (the row's: open, stop, copy bundle ID, remove), then its data (your own builds:
// Defaults, Stores, Reset) and Files (your own builds, or App Store apps that share files).
// One switch under the header: Defaults | Stores | Reset (your own builds) | Files.
//   DeviceAppDetailView { app: page.app; bundleId: page.appDetail; section: page.appSection
//                         onBack: page.closeApp(); onSectionPicked: function(s) { page.appSection = s } }
ColumnLayout {
  id: root
  required property var app
  readonly property var svc: app.device
  property string bundleId: ""
  property bool active: false
  property string section: "defaults"        // defaults | stores | reset | files; falls back to one the app has
  signal back()
  signal sectionPicked(string section)

  readonly property var current: {
    for (var i = 0; i < svc.apps.length; i++) if (svc.apps[i].bundleId === bundleId) return svc.apps[i]
    return null
  }
  property var last: null                    // the app as last seen, for the header while it reloads
  onCurrentChanged: if (current) last = current
  onBundleIdChanged: { last = current; lastBusy = "" }
  readonly property var shown: current || last
  readonly property string busy: shown ? (svc.busyApps[shown.bundleId] || "") : ""
  property string lastBusy: ""               // "uninstall": we removed it ourselves, the service already said so
  onBusyChanged: if (busy) lastBusy = busy
  readonly property bool hasData: !!shown && !!shown.developer
  readonly property bool hasFiles: !!shown && (!!shown.developer || !!shown.fileSharing)
  // One switch for everything the app has: its data (your own builds) and its files.
  readonly property var sections: {
    var out = []
    if (hasData) out.push({ value: "defaults", label: "Defaults" }, { value: "stores", label: "Stores" }, { value: "reset", label: "Reset" })
    if (hasFiles) out.push({ value: "files", label: "Files" })
    return out
  }
  readonly property string currentSection: {
    for (var i = 0; i < sections.length; i++) if (sections[i].value === section) return section
    return sections.length ? sections[0].value : ""
  }
  readonly property bool narrow: width > 0 && width < Style.space(560)

  spacing: Style.spacing.xl

  // The app went away (removed here, on the phone, or the iPhone disconnected): back to the list.
  Connections {
    target: root.svc
    function onAppsChanged() {
      if (root.active && root.bundleId && root.svc.appsStatus === "ready" && !root.current) {
        if (root.lastBusy !== "uninstall") root.app.toast((root.last ? root.last.name : "The app") + " isn't on the iPhone anymore", "info")
        root.back()
      }
    }
    function onConnectedChanged() {
      if (root.active && root.bundleId && !root.svc.connected) {
        root.app.toast("The iPhone disconnected", "warn")
        root.back()
      }
    }
  }

  function copyId() {
    Util.execArgv(["wl-copy", root.shown.bundleId])
    root.app.toast("Copied " + root.shown.bundleId)
  }

  // ---- header: back, icon, name, bundle id and version, pills, the row's actions
  RowLayout {
    Layout.fillWidth: true
    spacing: Style.spacing.xxl
    // Back: the header's icon-button size, outlined so it reads as a button, its left edge on the content edge
    Item {
      implicitWidth: backButton.implicitWidth; implicitHeight: backButton.implicitHeight
      Rectangle {
        anchors.fill: parent
        radius: Style.cornerRadius
        color: "transparent"
        border.width: Style.normalBorderWidth
        border.color: Theme.hairline
      }
      IconButton {
        id: backButton
        anchors.fill: parent
        glyph: Model.Glyph.arrowLeft
        tooltip: "Back to the apps (Esc)"
        onClicked: root.back()
      }
    }
    Item {
      implicitWidth: Style.font.iconLarge * 2.2; implicitHeight: implicitWidth
      Rectangle {
        anchors.fill: parent
        radius: width * 0.225
        color: Theme.surfaceRaised
        visible: icon.status !== Image.Ready
        Icon { anchors.centerIn: parent; glyph: Model.Glyph.apps; size: Style.font.icon; color: Theme.faint }
      }
      Image {
        id: icon
        anchors.fill: parent
        source: root.shown && root.shown.icon ? "file://" + root.shown.icon : ""
        sourceSize.width: width * 2; sourceSize.height: height * 2
        asynchronous: true; smooth: true; mipmap: true
        fillMode: Image.PreserveAspectFit
      }
    }
    ColumnLayout {
      Layout.fillWidth: true
      spacing: Style.spacing.xxs
      AppText { Layout.fillWidth: true; text: root.shown ? root.shown.name : ""; size: "title"; strong: true; elide: Text.ElideRight }
      AppText {
        Layout.fillWidth: true
        text: {
          var a = root.shown
          if (!a) return ""
          var p = [a.bundleId]
          if (a.version && !root.narrow) p.push(a.version + (a.build && a.build !== a.version ? " (" + a.build + ")" : ""))
          if (a.size && !root.narrow) p.push(Model.formatBytes(a.size))
          return p.join("  ·  ")
        }
        // a long bundle id keeps both ends (the team prefix and the app's own name) when it must be cut
        size: "small"; tone: "dim"; elide: Text.ElideMiddle
        HoverHandler { id: idHover }
        PanelToolTip { visible: idHover.hovered && parent.truncated; text: root.shown ? root.shown.bundleId : "" }
      }
    }
    StatusPill { visible: root.hasData && !root.narrow; quiet: true; level: "info"; glyph: Model.Glyph.developer; text: "Yours" }
    StatusPill { visible: !!root.shown && !!root.shown.running && !root.narrow; quiet: true; level: "ok"; glyph: Model.Glyph.dot; text: "Running" }   // narrow: Stop says it
    Row {
      spacing: Style.spacing.xxs
      IconButton { glyph: Model.Glyph.play; tooltip: "Open on iPhone (o)"; busy: root.busy === "launch"; enabled: !!root.current && root.busy === ""; onClicked: root.svc.launchApp(root.current) }
      // Stop keeps its slot while the app isn't running (as in the list rows), so ▶ never moves under the pointer
      IconButton {
        readonly property bool on: !!root.shown && !!root.shown.running
        glyph: Model.Glyph.stop; tooltip: on ? "Stop" : ""
        opacity: on ? 1 : 0
        busy: root.busy === "kill"
        enabled: on && !!root.current && root.busy === ""
        onClicked: root.svc.stopApp(root.current)
      }
      IconButton { glyph: Model.Glyph.copy; tooltip: "Copy bundle ID (c)"; enabled: !!root.shown; onClicked: root.copyId() }
      IconButton { glyph: Model.Glyph.trash; tooltip: "Remove from iPhone"; danger: true; visible: !!root.shown && !!root.shown.removable; busy: root.busy === "uninstall"; enabled: !!root.current && root.busy === ""; onClicked: root.svc.uninstallApp(root.current) }
    }
  }

  // ---- sections + reload (a single section, an App Store app's Files, needs no switch)
  RowLayout {
    Layout.fillWidth: true
    visible: root.sections.length > 1
    spacing: Style.spacing.lg
    ButtonGroupLite {
      options: root.sections
      value: root.currentSection
      onPicked: function(v) { root.sectionPicked(v) }
    }
    Item { Layout.fillWidth: true }
    IconButton {
      glyph: Model.Glyph.refresh
      tooltip: root.currentSection === "stores" ? "Reload the stores" : root.currentSection === "defaults" ? "Reload the defaults"
             : root.currentSection === "files" ? "Reload this folder" : ""
      // kept in the row on Reset (nothing to reload there) so the row keeps its height
      opacity: root.currentSection === "reset" ? 0 : 1
      enabled: root.currentSection !== "reset"
      busy: root.currentSection === "files" && filesView.status === "loading"
      onClicked: root.currentSection === "files" ? filesView.open(filesView.path) : dataView.reload()
    }
  }

  QuietNote {
    Layout.fillWidth: true
    visible: !!root.shown && root.sections.length === 0
    glyph: Model.Glyph.lock
    title: "This app keeps its data to itself"
    text: "App Store apps only show their files when their developer turns on file sharing. Your own builds show their data and files."
  }

  DeviceAppDataView {
    id: dataView
    Layout.fillWidth: true
    visible: ["defaults", "stores", "reset"].indexOf(root.currentSection) >= 0
    pane: root.currentSection
    active: root.active && visible
    app: root.app
    fixedApp: root.hasData ? root.bundleId : ""
  }
  DeviceContainerView {
    id: filesView
    Layout.fillWidth: true
    visible: root.currentSection === "files"
    showReload: root.sections.length === 1
    active: root.active && visible
    app: root.app
    fixedApp: root.hasFiles ? root.bundleId : ""
  }
}
