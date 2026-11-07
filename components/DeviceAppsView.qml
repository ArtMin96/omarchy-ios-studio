pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../Model.js" as Model

// Installed apps on the iPhone: search, launch, stop, remove; click a row for the app's details
// (DeviceAppDetailView: its data and files). The page owns the cursor.
// The rows are a ListView over `shown`, which changes only with the app list, the filter or the sort;
// icons and sizes come from svc.appIcons / svc.appSizes, so a batch of icons only rebinds those images.
// The page scrolls (PageFrame's Flickable), not the list: the list is as tall as its content and only
// creates the rows inside the page's viewport (negative display margins), so long lists stay cheap.
ColumnLayout {
  id: root
  required property var app
  property int cursor: -1
  readonly property var svc: app.device
  property alias search: searchField
  property string sortBy: "dev"            // dev (yours first) | az | size (largest first; asks for sizes)
  signal cursorMoved(int index)
  signal openApp(string bundleId)

  readonly property var shown: {
    var q = searchField.text
    var list = svc.apps.filter(function(a) { return Model.matches(a.name + " " + a.bundleId, q) })
    var by = root.sortBy, sizes = by === "size" ? svc.appSizes : null
    list.sort(function(a, b) {
      if (sizes) {
        var sa = sizes[a.bundleId] || 0, sb = sizes[b.bundleId] || 0
        if (sa !== sb) return sb - sa
      }
      if (by === "dev" && a.developer !== b.developer) return a.developer ? -1 : 1
      return String(a.name).toLowerCase().localeCompare(String(b.name).toLowerCase())
    })
    return list
  }
  onSortByChanged: if (sortBy === "size") svc.wantAppSizes()
  readonly property int devCount: svc.apps.filter(function(a) { return a.developer }).length

  function current() { return cursor >= 0 && cursor < shown.length ? shown[cursor] : null }

  spacing: Style.spacing.lg

  RowLayout {
    Layout.fillWidth: true
    spacing: Style.spacing.lg
    FilterField { id: searchField; Layout.fillWidth: true; placeholder: "Filter " + root.svc.apps.length + " apps by name or bundle ID" }
    ButtonGroupLite {
      options: [{ value: "dev", label: "Yours first" }, { value: "az", label: "A–Z" }, { value: "size", label: "Size" }]
      value: root.sortBy
      onPicked: function(v) { root.sortBy = v }
    }
    IconButton { glyph: Model.Glyph.refresh; tooltip: "Reload apps"; busy: root.svc.appsStatus === "loading"; onClicked: root.svc.refreshApps(true) }
  }

  Skeleton { Layout.fillWidth: true; visible: root.svc.appsStatus === "loading" && root.svc.apps.length === 0; lines: 6; rowHeight: Style.space(40); gap: Style.spacing.md }

  ErrorState {
    Layout.fillWidth: true
    visible: root.svc.appsStatus === "error" && root.svc.apps.length === 0
    app: root.app
    error: root.svc.appsError
    title: "Couldn't list the apps"
    onRetry: root.svc.refreshApps(true)
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

  ListView {
    id: list
    Layout.fillWidth: true
    Layout.preferredHeight: contentHeight
    interactive: false
    spacing: Style.spacing.xxs
    model: root.shown
    reuseItems: true

    // Only rows inside the page's viewport (plus a screen of slack each way) exist.
    property Flickable pageFlick: null
    property real listTop: 0                // this list's y in the page's content
    readonly property real slack: pageFlick ? pageFlick.height : 0
    displayMarginBeginning: pageFlick ? -Math.max(0, pageFlick.contentY - listTop - slack) : 0
    displayMarginEnd: pageFlick ? -Math.max(0, listTop + height - pageFlick.contentY - pageFlick.height - slack) : 0
    function place() { if (pageFlick) listTop = list.mapToItem(pageFlick.contentItem, 0, 0).y }
    Component.onCompleted: {
      var p = root.parent
      while (p && !(p instanceof Flickable)) p = p.parent
      pageFlick = p
      place()
    }
    onYChanged: place()
    onHeightChanged: place()
    Connections { target: root; function onYChanged() { list.place() } }

    // Keyboard moves keep the cursor's row on screen (rows have one height, so it may not exist yet).
    function reveal(i) {
      if (!pageFlick || i < 0 || i >= count || count === 0) return
      var stride = (contentHeight + spacing) / count
      var y0 = listTop + i * stride, y1 = y0 + stride - spacing
      if (y0 < pageFlick.contentY) pageFlick.contentY = Math.max(0, y0 - Style.spacing.lg)
      else if (y1 > pageFlick.contentY + pageFlick.height) pageFlick.contentY = y1 - pageFlick.height + Style.spacing.lg
    }
    Connections { target: root; function onCursorChanged() { list.reveal(root.cursor) } }

    delegate: ListRow {
      id: row
      required property var modelData
      required property int index
      readonly property string bid: modelData.bundleId
      readonly property string busy: root.svc.busyApps[bid] || ""
      readonly property var size: root.svc.appSizes[bid] || modelData.size
      readonly property string iconPath: root.svc.appIcons[bid] || modelData.icon || ""
      width: ListView.view.width
      hasCursor: root.cursor === index
      title: modelData.name
      subtitle: modelData.bundleId
      meta: {
        var p = []
        if (modelData.version) p.push(modelData.version + (modelData.build && modelData.build !== modelData.version ? " (" + modelData.build + ")" : ""))
        if (row.size) p.push(Model.formatBytes(row.size))
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
            source: row.iconPath ? "file://" + row.iconPath : ""
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

  AppText {
    Layout.fillWidth: true
    visible: root.svc.apps.length > 0
    text: "Enter opens details · o opens on the iPhone · x removes · c copies the bundle ID"
    size: "caption"; tone: "faint"; wrapMode: Text.WordWrap
    horizontalAlignment: Text.AlignRight
  }
}
