import QtQuick
import QtQuick.Layouts
import QtQuick.Dialogs
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../Model.js" as Model

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
  property var items: []
  property int total: 0
  property int photos: 0
  property int videos: 0
  property string kind: ""                  // "" | photo | video
  property string status: "unknown"         // unknown | loading | ready | error
  property var error: null
  property bool loadingMore: false
  property var selected: ({})               // id -> true
  readonly property int selCount: Object.keys(selected).length
  property int anchorIndex: -1
  property var transfer: null               // running import (transfer event)
  property string transferId: ""

  readonly property string defaultDest: (app.demo !== "" ? app.demoDir + "/Imports" : app.home + "/Pictures") + "/iPhone"
  readonly property string dest: app.setting("mediaImportDir", "") || defaultDest
  readonly property bool convertHeic: app.setting("mediaConvertHeic", true) !== false

  // thumbnails: id -> local file, filled by a small request queue (4 at a time)
  property var _thumbCache: ({})
  property var _thumbQueue: []
  property int _thumbBusy: 0

  spacing: Style.spacing.xl

  onActiveChanged: if (active && status === "unknown" && svc.connected) load(true)
  Connections {
    target: root.svc
    function onConnStateChanged() { if (root.active && root.svc.connected && root.status !== "ready") root.load(true) }
    function onDaemonEvent(name, data) {
      if (name === "transfer" && data && data.id === root.transferId && data.state === "running") root.transfer = data
    }
  }

  function load(reset) {
    if (reset) { root.status = root.items.length ? root.status : "loading"; root.error = null }
    else root.loadingMore = true
    var off = reset ? 0 : root.items.length
    var args = { offset: off, limit: root.pageSize, refresh: !!reset && root.status === "ready" }
    if (root.kind) args.kind = root.kind
    root.svc.request("media.list", args, function(ok, d) {
      root.loadingMore = false
      if (!ok) { root.error = d; if (reset || !root.items.length) root.status = "error"; else root.app.toast(Model.errorMessage(d), "error"); return }
      root.items = reset ? d.items : root.items.concat(d.items)
      root.total = d.total; root.photos = d.photos; root.videos = d.videos
      root.status = "ready"
      if (reset) { root.selected = ({}); root.anchorIndex = -1 }
    }, 120000)
  }

  function setKind(k) { if (k === root.kind) return; root.kind = k; root.items = []; root.status = "loading"; load(true) }

  function thumbFor(item, cb) {
    var c = root._thumbCache[item.id] || item.thumb
    if (c) { cb(c); return }
    root._thumbQueue.push({ id: item.id, cb: cb })
    _pump()
  }
  function _pump() {
    while (root._thumbBusy < 4 && root._thumbQueue.length) {
      var job = root._thumbQueue.shift()
      root._thumbBusy++
      root.svc.request("media.thumb", { id: job.id }, (function(j) {
        return function(ok, d) {
          root._thumbBusy--
          if (ok) { root._thumbCache[j.id] = d.path; j.cb(d.path) } else j.cb("")
          root._pump()
        }
      })(job), 60000)
    }
  }
  function forget(id) { root._thumbQueue = root._thumbQueue.filter(function(j) { return j.id !== id }) }

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
        if (d.code !== "cancelled") root.app.toast(Model.errorMessage(d), "error")
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
    currentFolder: "file://" + root.dest
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
    IconButton { glyph: Model.Glyph.refresh; tooltip: "Reload the camera roll"; busy: root.status === "loading"; enabled: root.svc.connected; onClicked: root.load(true) }
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
    onRetry: root.load(true)
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
  Flow {
    id: grid
    Layout.fillWidth: true
    visible: root.items.length > 0 && root.status !== "error"
    spacing: Style.spacing.sm
    readonly property real tile: {
      var w = width > 0 ? width : Style.space(600)
      var n = Math.max(3, Math.floor((w + spacing) / (Style.space(116) + spacing)))
      return Math.floor((w - (n - 1) * spacing) / n)
    }
    Repeater {
      model: root.items
      delegate: Item {
        id: tile
        required property var modelData
        required property int index
        readonly property bool sel: !!root.selected[modelData.id]
        property string src: ""
        property bool failed: false
        width: grid.tile; height: grid.tile

        Component.onCompleted: root.thumbFor(modelData, function(p) { if (tile) { tile.src = p; tile.failed = p === "" } })
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
            source: tile.src ? "file://" + tile.src : ""
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
                text: {
                  var s = Math.round(tile.modelData.duration || 0)
                  return Math.floor(s / 60) + ":" + ("0" + (s % 60)).slice(-2)
                }
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
    onClicked: root.load(false)
  }

  AppText {
    Layout.fillWidth: true
    visible: root.items.length > 0
    text: "Shift+click selects a range. Nothing is changed or deleted on the iPhone."
    size: "caption"; tone: "faint"
    horizontalAlignment: Text.AlignRight
  }
}
