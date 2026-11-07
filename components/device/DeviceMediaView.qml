import QtQuick
import QtQuick.Layouts
import QtQuick.Dialogs
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../common"
import "../../core"
import "../../lib/Model.js" as Model

// Files › Photos & videos: the iPhone's camera roll as a lazy thumbnail grid. Click to select
// (Shift+click selects a range), then import to a folder, optionally turning HEIC into JPEG.
// Read-only on the phone. Daemon: media.list, media.thumb, media.pull, transfer.cancel.
ColumnLayout {
  id: root
  required property var app
  readonly property var svc: app.device
  property bool active: false

  readonly property int pageSize: 120
  readonly property bool narrow: width > 0 && width < Style.space(720)
  property string kind: ""                  // "" | photo | video
  // The first page of the camera roll, read the first time the tab shows with the iPhone connected (and
  // again after a failed read when it reconnects); Reload and the kind switch read it again. "Show more"
  // appends pages to it (loadMore).
  Resource {
    id: media
    active: root.active && root.svc.connected
    autoLoad: true
    ttl: 2147483647                         // never stale by age
    initial: ({ items: [], total: 0, photos: 0, videos: 0 })
    fetch: function(done, fail) {
      var args = { offset: 0, limit: root.pageSize, refresh: media.loaded }
      if (root.kind) args.kind = root.kind
      root.svc.fetch("media.list", args, done, fail, 120000)
    }
    onSucceeded: { root.selected = ({}); root.anchorIndex = -1 }
  }
  readonly property var items: media.data.items
  readonly property int total: media.data.total
  readonly property int photos: media.data.photos
  readonly property int videos: media.data.videos
  readonly property var error: media.error
  // loading: a read runs (what's shown stays) · error: the last read failed
  readonly property string status: media.busy ? "loading" : media.error ? "error" : media.loaded ? "ready" : "unknown"
  property bool loadingMore: false
  property var selected: ({})               // id -> true
  readonly property int selCount: Object.keys(selected).length
  property int anchorIndex: -1
  property var transfer: null               // running import (transfer event)
  property string transferId: ""

  readonly property string defaultDest: (app.demo !== "" ? app.demoDir + "/Imports" : app.home + "/Pictures") + "/iPhone"
  readonly property string dest: app.setting("mediaImportDir", "") || defaultDest
  readonly property bool convertHeic: app.setting("mediaConvertHeic", true) !== false

  // thumbnails: id -> local file ("" = failed), filled by a small request queue (4 at a time) for the
  // tiles that exist (the grid only creates the ones near the viewport). _thumbRev bumps on each one.
  property var _thumbCache: ({})
  property int _thumbRev: 0
  property var _thumbQueue: []
  property int _thumbBusy: 0

  spacing: Style.spacing.xl

  Connections {
    target: root.svc
    function onDaemonEvent(name, data) {
      if (name === "transfer" && data && data.id === root.transferId && data.state === "running") root.transfer = data
    }
  }

  function load() { media.load(true) }
  // the next page, appended; dropped when the list was read again meanwhile
  function loadMore() {
    if (root.loadingMore || media.busy) return
    var before = media.data
    var args = { offset: before.items.length, limit: root.pageSize, refresh: false }
    if (root.kind) args.kind = root.kind
    root.loadingMore = true
    root.svc.fetch("media.list", args, function(d) {
      root.loadingMore = false
      if (media.data !== before) return
      media.set({ items: before.items.concat(d.items), total: d.total, photos: d.photos, videos: d.videos })
    }, function(e) {
      root.loadingMore = false
      root.app.toastError(e)
    }, 120000)
  }

  function setKind(k) { if (k === root.kind) return; root.kind = k; media.reset(); media.load(true) }

  // A tile asks for its thumbnail when it's created and forgets it when it goes (scrolled away).
  function wantThumb(id) {
    if (root._thumbCache[id] !== undefined || root._thumbQueue.indexOf(id) >= 0) return
    root._thumbQueue.push(id)
    _pump()
  }
  function _pump() {
    while (root._thumbBusy < 4 && root._thumbQueue.length) {
      var id = root._thumbQueue.shift()
      root._thumbBusy++
      root.svc.request("media.thumb", { id: id }, (function(j) {
        return function(ok, d) {
          root._thumbBusy--
          root._thumbCache[j] = ok ? d.path : ""
          root._thumbRev++
          root._pump()
        }
      })(id), 60000)
    }
  }
  function forget(id) { var i = root._thumbQueue.indexOf(id); if (i >= 0) root._thumbQueue.splice(i, 1) }

  function toggle(index, shift) {
    var s = Object.assign({}, root.selected)
    if (shift && root.anchorIndex >= 0) {
      var a = Math.min(root.anchorIndex, index), b = Math.max(root.anchorIndex, index)
      for (var i = a; i <= b; i++) s[root.items[i].id] = true
    } else {
      var id = root.items[index].id
      if (s[id]) delete s[id]; else s[id] = true
      root.anchorIndex = index
    }
    root.selected = s
  }
  function selectAll() { var s = {}; for (var i = 0; i < root.items.length; i++) s[root.items[i].id] = true; root.selected = s }

  function importSelected() {
    var ids = Object.keys(root.selected)
    if (!ids.length) return
    root.transferId = "m" + Date.now()
    root.transfer = { bytes: 0, total: 0, files: 0, count: ids.length, file: "" }
    root.svc.request("media.pull", { ids: ids, dest: root.dest, convertHeic: root.convertHeic, transferId: root.transferId }, function(ok, d) {
      root.transfer = null
      if (!ok) {
        if (d.code !== "cancelled") root.app.toastError(d)
        else root.app.toast("Import stopped", "info")
        return
      }
      var n = d.files.length, parts = []
      if (n) parts.push("Imported " + n + (n === 1 ? " item" : " items"))
      if (d.skipped.length) parts.push(d.skipped.length + " already there")
      if (d.failed.length) parts.push(d.failed.length + " missing on the iPhone")
      if (d.notConverted) parts.push(d.notConverted + " kept as HEIC")
      root.app.toast(parts.join(", ") + " · " + root.prettyDest(), d.failed.length ? "warn" : "ok")
      root.app.notify("Import finished", parts.join(", "), Model.Glyph.image)
      root.selected = ({})
    }, 24 * 3600000)
  }
  function cancelImport() { root.svc.request("transfer.cancel", { transferId: root.transferId }, null) }
  function prettyDest() { return root.dest.indexOf(root.app.home) === 0 ? "~" + root.dest.substring(root.app.home.length) : root.dest }
  function shortDest() {
    var p = root.prettyDest()
    var parts = p.split("/").filter(function(x) { return x !== "" })
    return parts.length > 3 ? "…/" + parts.slice(-2).join("/") : p
  }

  FolderDialog {
    id: destDialog
    title: "Import photos and videos to…"
    currentFolder: Util.fileUrl(root.dest)
    onAccepted: root.app.setSetting("mediaImportDir", decodeURIComponent(String(selectedFolder).replace(/^file:\/\//, "")))
  }

  // ---- toolbar
  RowLayout {
    Layout.fillWidth: true
    spacing: Style.spacing.lg
    AppText {
      Layout.fillWidth: true
      tone: "dim"; size: "small"; elide: Text.ElideRight
      text: root.status !== "ready" ? "Camera roll" : (root.total === 0 ? "Nothing here yet"
            : (root.photos + root.videos) + " items  ·  " + root.photos + " photos  ·  " + root.videos + " videos")
    }
    ButtonGroupLite {
      options: [{ value: "", label: "All" }, { value: "photo", label: "Photos" }, { value: "video", label: "Videos" }]
      value: root.kind
      onPicked: function(v) { root.setKind(v) }
    }
    IconButton { glyph: Model.Glyph.refresh; tooltip: "Reload the camera roll"; busy: root.status === "loading"; enabled: root.svc.connected; onClicked: root.load() }
  }

  // ---- import bar
  Rectangle {
    Layout.fillWidth: true
    visible: root.status === "ready" && root.total > 0
    implicitHeight: bar.implicitHeight + Style.spacing.lg * 2
    radius: Style.cornerRadius
    color: Theme.surface
    border.width: Style.normalBorderWidth
    border.color: root.selCount ? Util.alpha(Commons.Color.accent, 0.45) : Theme.hairline

    ColumnLayout {
      id: bar
      anchors.fill: parent
      anchors.margins: Style.spacing.lg
      spacing: Style.spacing.lg
      RowLayout {
        Layout.fillWidth: true
        visible: !root.transfer
        spacing: Style.spacing.lg
        AppText {
          text: root.selCount ? root.selCount + " selected" : "Click photos to select them"
          tone: root.selCount ? "primary" : "dim"; strong: root.selCount > 0
        }
        ActionButton { text: "Select all shown"; kind: "ghost"; compact: true; visible: root.selCount < root.items.length; onClicked: root.selectAll() }
        ActionButton { text: "Clear"; kind: "ghost"; compact: true; visible: root.selCount > 0; onClicked: root.selected = ({}) }
        Item { Layout.fillWidth: true }
        ActionButton {
          id: importBtn
          text: root.selCount ? "Import " + root.selCount : "Import"
          glyph: Model.Glyph.download; kind: "primary"
          enabled: root.selCount > 0 && root.svc.connected
          onClicked: root.importSelected()
        }
      }
      RowLayout {
        Layout.fillWidth: true
        visible: !root.transfer
        spacing: Style.spacing.md
        ToggleSwitch {
          // track lines up with the text above it (the cursor-ring margin sits outside)
          Layout.leftMargin: cursorRing ? -cursorPad : 0
          checked: root.convertHeic
          onToggled: root.app.setSetting("mediaConvertHeic", !checked)
        }
        AppText { text: "Save HEIC photos as JPEG"; size: "small"; tone: "dim" }
        Item { Layout.fillWidth: true }
        AppText { text: "Saves to"; size: "small"; tone: "faint"; visible: !root.narrow }
        ActionButton {
          text: root.shortDest(); glyph: Model.Glyph.folder; kind: "ghost"; compact: true
          tooltip: "Imports go to " + root.prettyDest() + ". Click to change."
          onClicked: destDialog.open()
        }
      }
      DeviceTransferBar {
        Layout.fillWidth: true
        transfer: root.transfer
        verb: "Importing"
        onCancel: root.cancelImport()
      }
    }
  }

  // ---- states
  Skeleton { Layout.fillWidth: true; visible: root.status === "loading" && root.items.length === 0; lines: 3; rowHeight: Style.space(112); gap: Style.spacing.md }
  AppText {
    Layout.fillWidth: true
    visible: root.status === "loading" && root.items.length === 0
    text: "Reading the camera roll. The first time takes a few seconds while the photo library index is copied."
    size: "small"; tone: "faint"; wrapMode: Text.WordWrap
  }
  ErrorState {
    Layout.fillWidth: true
    visible: root.status === "error"
    app: root.app
    error: root.error
    title: "Couldn't read the camera roll"
    onRetry: root.load()
  }
  EmptyState {
    Layout.fillWidth: true
    visible: root.status === "ready" && root.total === 0
    compact: true
    glyph: root.kind === "video" ? Model.Glyph.video : Model.Glyph.image
    title: root.kind === "video" ? "No videos on this iPhone" : (root.kind === "photo" ? "No photos on this iPhone" : "The camera roll is empty")
    text: "Photos kept only in iCloud don't show up here; only what's stored on the iPhone itself."
    actionText: root.kind ? "Show everything" : ""
    onAction: root.setKind("")
  }

  // ---- grid
  // A GridView placed against the page (PageViewport): only the tiles near the viewport exist, so only
  // they ask for thumbnails. Its cells are a tile plus the gap, so it is one gap wider than its slot.
  Item {
    id: gridSlot
    Layout.fillWidth: true
    implicitHeight: grid.rows > 0 ? grid.rows * grid.cellHeight - grid.spacing : 0
    visible: root.items.length > 0 && root.status !== "error"
    GridView {
      id: grid
      width: gridSlot.width + spacing
      height: gridSlot.height + spacing
      interactive: false
      readonly property real spacing: Style.spacing.sm
      readonly property int columns: Math.max(3, Math.floor(((gridSlot.width > 0 ? gridSlot.width : Style.space(600)) + spacing) / (Style.space(116) + spacing)))
      readonly property real tile: Math.floor(((gridSlot.width > 0 ? gridSlot.width : Style.space(600)) - (columns - 1) * spacing) / columns)
      readonly property int rows: Math.ceil(count / columns)
      cellWidth: tile + spacing
      cellHeight: tile + spacing
      model: root.items
      PageViewport { id: viewport; view: grid }
      displayMarginBeginning: viewport.marginBeginning
      displayMarginEnd: viewport.marginEnd
      delegate: Item {
        id: tile
        required property var modelData
        required property int index
        readonly property bool sel: !!root.selected[modelData.id]
        readonly property var cached: (root._thumbRev, root._thumbCache[modelData.id])
        readonly property string src: cached || modelData.thumb || ""
        readonly property bool failed: cached === ""
        width: grid.tile; height: grid.tile

        Component.onCompleted: if (!modelData.thumb) root.wantThumb(modelData.id)
        Component.onDestruction: root.forget(modelData.id)

        Rectangle {
          anchors.fill: parent
          radius: Style.cornerRadius
          color: Theme.surfaceRaised
          clip: true
          Icon {
            anchors.centerIn: parent
            visible: img.status !== Image.Ready
            glyph: tile.modelData.kind === "video" ? Model.Glyph.video : Model.Glyph.image
            size: Style.font.iconLarge; color: tile.failed ? Theme.dim : Theme.faint
          }
          Image {
            id: img
            anchors.fill: parent
            source: Util.fileUrl(tile.src)
            fillMode: Image.PreserveAspectCrop
            asynchronous: true; smooth: true; cache: true
            sourceSize.width: grid.tile * 2; sourceSize.height: grid.tile * 2
            opacity: status === Image.Ready ? (tile.sel ? 0.78 : 1) : 0
            Behavior on opacity { NumberAnimation { duration: 160; easing.type: Easing.OutCubic } }
          }
          // video length, bottom-left
          Rectangle {
            visible: tile.modelData.kind === "video"
            anchors.left: parent.left; anchors.bottom: parent.bottom; anchors.margins: Style.spacing.sm
            radius: Style.cornerRadius
            color: Util.alpha("black", 0.55)
            implicitWidth: vrow.implicitWidth + Style.spacing.md * 2
            implicitHeight: vrow.implicitHeight + Style.spacing.xxs * 2
            Row {
              id: vrow
              anchors.centerIn: parent
              spacing: Style.spacing.xs
              Icon { glyph: Model.Glyph.play; size: Style.font.caption; color: "white" }
              AppText {
                size: "caption"; tone: "inherit"; color: "white"
                text: Model.clock(Math.round(tile.modelData.duration || 0))
              }
            }
          }
          // selection
          Rectangle {
            anchors.fill: parent
            radius: Style.cornerRadius
            color: "transparent"
            border.width: tile.sel ? Style.space(3) : (hover.hovered ? Style.normalBorderWidth : 0)
            border.color: tile.sel ? Commons.Color.accent : Theme.hairline
          }
          Rectangle {
            visible: tile.sel || hover.hovered
            anchors.right: parent.right; anchors.top: parent.top; anchors.margins: Style.spacing.sm
            width: Style.font.iconLarge * 1.3; height: width; radius: width / 2
            color: tile.sel ? Commons.Color.accent : Util.alpha("black", 0.35)
            border.width: tile.sel ? 0 : Style.normalBorderWidth
            border.color: "white"
            Icon { anchors.centerIn: parent; visible: tile.sel; glyph: Model.Glyph.check; size: Style.font.icon; color: Commons.Color.background }
          }
        }
        HoverHandler { id: hover; cursorShape: Qt.PointingHandCursor }
        // MouseArea, not TapHandler: tapped() hands over a QEventPoint, which has no modifiers.
        MouseArea {
          anchors.fill: parent
          cursorShape: Qt.PointingHandCursor
          onClicked: function(mouse) { root.toggle(tile.index, (mouse.modifiers & Qt.ShiftModifier) !== 0) }
        }
        PanelToolTip { visible: hover.hovered; text: tile.modelData.name + (tile.modelData.date ? "  ·  " + Qt.formatDateTime(new Date(tile.modelData.date), "d MMM yyyy, HH:mm") : "") + (tile.modelData.size ? "  ·  " + Model.formatBytes(tile.modelData.size) : "") }
      }
    }
  }

  ActionButton {
    Layout.alignment: Qt.AlignHCenter
    visible: root.status === "ready" && root.items.length < root.total
    text: "Show " + Math.min(root.pageSize, root.total - root.items.length) + " more  (" + (root.total - root.items.length) + " left)"
    glyph: Model.Glyph.chevronDown; kind: "ghost"
    busy: root.loadingMore
    onClicked: root.loadMore()
  }

  AppText {
    Layout.fillWidth: true
    visible: root.items.length > 0
    text: "Shift+click selects a range. Nothing is changed or deleted on the iPhone."
    size: "caption"; tone: "faint"
    horizontalAlignment: Text.AlignRight
  }
}
