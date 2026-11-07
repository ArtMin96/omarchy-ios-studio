import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../Model.js" as Model

// Installed apps on the iPhone: search, launch, stop, remove; click a row for the app's details
// (DeviceAppDetailView: its data and files). The page owns the cursor.
ColumnLayout {
  id: root
  required property var app
  property int cursor: -1
  readonly property var svc: app.device
  property alias search: searchField
  property bool devFirst: true
  signal cursorMoved(int index)
  signal openApp(string bundleId)

  readonly property var shown: {
    var q = searchField.text
    var list = svc.apps.filter(function(a) { return Model.matches(a.name + " " + a.bundleId, q) })
    list.sort(function(a, b) {
      if (root.devFirst && a.developer !== b.developer) return a.developer ? -1 : 1
      return String(a.name).toLowerCase().localeCompare(String(b.name).toLowerCase())
    })
    return list
  }
  readonly property int devCount: svc.apps.filter(function(a) { return a.developer }).length

  function current() { return cursor >= 0 && cursor < shown.length ? shown[cursor] : null }

  spacing: Style.spacing.lg

  RowLayout {
    Layout.fillWidth: true
    spacing: Style.spacing.lg
    FilterField { id: searchField; Layout.fillWidth: true; placeholder: "Filter " + root.svc.apps.length + " apps by name or bundle ID" }
    ButtonGroupLite {
      options: [{ value: "dev", label: "Yours first" }, { value: "az", label: "A–Z" }]
      value: root.devFirst ? "dev" : "az"
      onPicked: function(v) { root.devFirst = v === "dev" }
    }
    IconButton { glyph: Model.Glyph.refresh; tooltip: "Reload apps"; busy: root.svc.appsStatus === "loading"; onClicked: root.svc.refreshApps() }
  }

  Skeleton { Layout.fillWidth: true; visible: root.svc.appsStatus === "loading" && root.svc.apps.length === 0; lines: 6; rowHeight: Style.space(40); gap: Style.spacing.md }

  ErrorState {
    Layout.fillWidth: true
    visible: root.svc.appsStatus === "error" && root.svc.apps.length === 0
    app: root.app
    error: root.svc.appsError
    title: "Couldn't list the apps"
    onRetry: root.svc.refreshApps()
  }

  EmptyState {
    Layout.fillWidth: true
    visible: root.svc.appsStatus === "ready" && root.shown.length === 0
    compact: true
    glyph: Model.Glyph.apps
    title: searchField.text ? "No app matches “" + searchField.text + "”" : "No apps installed yet"
    text: searchField.text ? "Try part of the name or the bundle ID." : "Build one in Projects and run it on this iPhone."
    actionText: searchField.text ? "Clear filter" : "Open Projects"
    onAction: searchField.text ? searchField.clear() : root.app.nav("projects")
  }

  ColumnLayout {
    Layout.fillWidth: true
    spacing: Style.spacing.xxs
    Repeater {
      model: root.shown
      delegate: ListRow {
        id: row
        required property var modelData
        required property int index
        readonly property string busy: root.svc.busyApps[modelData.bundleId] || ""
        Layout.fillWidth: true
        hasCursor: root.cursor === index
        title: modelData.name
        subtitle: modelData.bundleId
        meta: {
          var p = []
          if (modelData.version) p.push(modelData.version + (modelData.build && modelData.build !== modelData.version ? " (" + modelData.build + ")" : ""))
          if (modelData.size) p.push(Model.formatBytes(modelData.size))
          return p.join("  ·  ")
        }
        onHovered: function(h) { if (h) root.cursorMoved(index) }
        onClicked: root.openApp(modelData.bundleId)

        leading: [
          Item {
            width: Style.font.iconLarge * 2.2; height: width
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
              source: row.modelData.icon ? "file://" + row.modelData.icon : ""
              sourceSize.width: width * 2; sourceSize.height: height * 2
              asynchronous: true; smooth: true; mipmap: true
              fillMode: Image.PreserveAspectFit
            }
          }
        ]
        trailing: [
          // the pills get a real gap between them (the trailing row's spacing is for icon buttons)
          Row {
            spacing: Style.spacing.xl
            anchors.verticalCenter: parent ? parent.verticalCenter : undefined
            StatusPill { visible: row.modelData.developer && !row.narrow; quiet: true; level: "info"; glyph: Model.Glyph.developer; text: "Yours"; anchors.verticalCenter: parent.verticalCenter }
            StatusPill { visible: row.modelData.running; quiet: true; level: "ok"; glyph: Model.Glyph.dot; text: "Running"; anchors.verticalCenter: parent.verticalCenter }
          },
          IconButton { glyph: Model.Glyph.play; tooltip: "Open on iPhone"; busy: row.busy === "launch"; enabled: row.busy === ""; onClicked: root.svc.launchApp(row.modelData) },
          // Stop keeps its slot while the app isn't running, so ▶, ■, copy and trash stay in fixed columns
          IconButton { glyph: Model.Glyph.stop; tooltip: row.modelData.running ? "Stop" : ""; opacity: row.modelData.running ? 1 : 0; busy: row.busy === "kill"; enabled: row.modelData.running && row.busy === ""; onClicked: root.svc.stopApp(row.modelData) },
          IconButton { glyph: Model.Glyph.copy; tooltip: "Copy bundle ID"; visible: !row.narrow; onClicked: { Util.execArgv(["wl-copy", row.modelData.bundleId]); root.app.toast("Copied " + row.modelData.bundleId) } },
          IconButton { glyph: Model.Glyph.trash; tooltip: "Remove from iPhone"; danger: true; visible: row.modelData.removable; busy: row.busy === "uninstall"; enabled: row.busy === ""; onClicked: root.svc.uninstallApp(row.modelData) }
        ]
      }
    }
  }

  AppText {
    Layout.fillWidth: true
    visible: root.svc.apps.length > 0
    text: "Enter opens details · o opens on the iPhone · x removes · c copies the bundle ID"
    size: "caption"; tone: "faint"; wrapMode: Text.WordWrap
    horizontalAlignment: Text.AlignRight
  }
}
