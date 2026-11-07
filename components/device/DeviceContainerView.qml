import QtQuick
import QtQuick.Layouts
import QtQuick.Dialogs
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../common"
import "../../lib/Model.js" as Model

// iPhone › Apps › an app › Files: browse the app's container on the iPhone, copy files out ("Save to…")
// and in (Add files, or drag files from the desktop onto the list), make folders, delete.
// Your own builds expose the whole container; App Store apps only Documents, when they share files.
// Daemon: container.list/pull/push/mkdir/delete, transfer.cancel.
ColumnLayout {
  id: root
  required property var app
  readonly property var svc: app.device
  property bool active: false
  property bool showReload: true            // false when the host's row already has a reload
  property string fixedApp: ""              // the app whose container this shows (a developer build or one that shares files)

  readonly property var candidates: svc.apps.filter(function(a) { return a.developer || a.fileSharing })
  property string bundleId: ""
  property string path: ""
  property var listing: null                // container.list reply
  property string status: "unknown"         // unknown | loading | ready | error
  property var error: null
  property var selected: ({})               // phone path -> entry
  readonly property int selCount: Object.keys(selected).length
  property var transfer: null
  property string transferId: ""
  property string transferVerb: "Copying"
  readonly property bool busy: !!transfer
  readonly property var current: {
    for (var i = 0; i < candidates.length; i++) if (candidates[i].bundleId === bundleId) return candidates[i]
    return null
  }
  readonly property string rootPath: listing ? listing.root : (current && !current.developer ? "/Documents" : "/")
  readonly property string defaultSave: (app.demo !== "" ? app.demoDir + "/Imports" : app.home + "/Downloads")

  spacing: Style.spacing.xl

  onActiveChanged: if (active) _ensure()
  onCandidatesChanged: if (active) _ensure()
  onFixedAppChanged: { root.bundleId = ""; root.listing = null; root.path = ""; root.status = "unknown"; root.selected = ({}); if (active) _ensure() }
  Connections {
    target: root.svc
    function onConnStateChanged() { if (root.active && root.svc.connected && root.status !== "ready") root.open(root.path) }
    function onDaemonEvent(name, data) {
      if (name === "transfer" && data && data.id === root.transferId && data.state === "running") root.transfer = data
    }
  }

  function _ensure() {
    if (root.svc.connected && root.svc.appsStatus === "unknown") root.svc.refreshApps()
    if (root.current) { if (root.status === "unknown") open(""); return }
    if (root.fixedApp && root.candidates.some(function(a) { return a.bundleId === root.fixedApp })) choose(root.fixedApp)
  }

  function choose(bid) {
    if (bid === root.bundleId && root.status === "ready") return
    root.bundleId = bid
    root.listing = null
    open("")
  }

  function open(p) {
    if (!root.bundleId) return
    root.status = "loading"
    root.error = null
    var args = { bundleId: root.bundleId }
    if (p) args.path = p
    root.svc.request("container.list", args, function(ok, d) {
      if (ok) { root.listing = d; root.path = d.path; root.status = "ready"; root.selected = ({}) }
      else { root.error = d; root.status = "error" }
    }, 60000)
  }

  function up() {
    if (!root.listing || root.path === root.rootPath) return
    var p = root.path.substring(0, root.path.lastIndexOf("/")) || "/"
    open(p)
  }

  function crumbs() {
    // a neutral root: the app's name is already in the header above
    var out = [{ label: root.rootPath === "/" ? "Container" : "Documents", path: root.rootPath }]
    if (!root.listing) return out
    var rest = root.path.substring(root.rootPath.length).split("/").filter(function(x) { return x !== "" })
    var acc = root.rootPath === "/" ? "" : root.rootPath
    for (var i = 0; i < rest.length; i++) { acc += "/" + rest[i]; out.push({ label: rest[i], path: acc }) }
    return out
  }

  function toggle(e) {
    var s = Object.assign({}, root.selected)
    if (s[e.path]) delete s[e.path]; else s[e.path] = e
    root.selected = s
  }

  function _track(verb) {
    root.transferId = "f" + Date.now()
    root.transferVerb = verb
    root.transfer = { bytes: 0, total: 0, files: 0, count: 0, file: "" }
    return root.transferId
  }

  function push(srcs, overwrite) {
    if (!srcs.length || root.busy) return
    var tid = _track("Copying to iPhone:")
    root.svc.request("container.push", { bundleId: root.bundleId, srcs: srcs, path: root.path, overwrite: !!overwrite, transferId: tid }, function(ok, d) {
      root.transfer = null
      if (ok) { root.app.toast("Copied " + d.files.length + (d.files.length === 1 ? " file" : " files") + " to the iPhone", "ok"); root.open(root.path); return }
      if (d.code === "exists") {
        var names = String(d.detail || "").split("\n").filter(function(x) { return x }).map(function(x) { return x.substring(x.lastIndexOf("/") + 1) })
        root.app.confirm({
          title: "Replace files on the iPhone?",
          message: d.message + "\n\n" + names.slice(0, 8).join(", ") + (names.length > 8 ? " and " + (names.length - 8) + " more" : "")
                   + "\n\nThe copies on the iPhone will be overwritten.",
          confirmText: "Replace", cancelText: "Cancel", destructive: true, glyph: Model.Glyph.upload,
          onConfirm: function() { root.push(srcs, true) }
        })
      } else if (d.code === "cancelled") root.app.toast("Copy stopped", "info")
      else root.app.toast(Model.errorMessage(d), "error")
    }, 24 * 3600000)
  }

  function pull(paths, dest) {
    if (!paths.length || root.busy) return
    var tid = _track("Saving")
    root.svc.request("container.pull", { bundleId: root.bundleId, paths: paths, dest: dest, transferId: tid }, function(ok, d) {
      root.transfer = null
      if (ok) {
        var where = dest.indexOf(root.app.home) === 0 ? "~" + dest.substring(root.app.home.length) : dest
        root.app.toast("Saved " + d.files.length + (d.files.length === 1 ? " file" : " files") + " to " + where, "ok")
      } else if (d.code === "cancelled") root.app.toast("Copy stopped", "info")
      else root.app.toast(Model.errorMessage(d), "error")
    }, 24 * 3600000)
  }

  function remove(entries) {
    if (!entries.length) return
    var names = entries.map(function(e) { return e.name })
    var dirs = entries.filter(function(e) { return e.type === "dir" }).length
    root.app.confirm({
      title: entries.length === 1 ? "Delete “" + names[0] + "”?" : "Delete " + entries.length + " items?",
      message: "This deletes " + (entries.length === 1 ? "it" : names.slice(0, 6).join(", ") + (names.length > 6 ? " and " + (names.length - 6) + " more" : ""))
               + " from " + (root.current ? root.current.name : "the app") + " on your iPhone"
               + (dirs ? ", including everything inside the folders" : "") + ". You can't undo this.",
      confirmText: "Delete", cancelText: "Cancel", destructive: true, glyph: Model.Glyph.trash,
      onConfirm: function() {
        root.svc.request("container.delete", { bundleId: root.bundleId, paths: entries.map(function(e) { return e.path }) }, function(ok, d) {
          if (ok) root.app.toast(d.failed.length ? d.failed.length + " couldn't be deleted" : "Deleted", d.failed.length ? "warn" : "ok")
          else root.app.toast(Model.errorMessage(d), "error")
          root.open(root.path)
        })
      }
    })
  }

  function makeFolder(name) {
    var n = String(name || "").trim()
    if (!n || n.indexOf("/") >= 0 || n === "." || n === "..") { root.app.toast("Use a folder name without slashes", "warn"); return }
    root.svc.request("container.mkdir", { bundleId: root.bundleId, path: (root.path === "/" ? "" : root.path) + "/" + n }, function(ok, d) {
      if (ok) { folderSheet.close(); root.open(root.path) }
      else root.app.toast(Model.errorMessage(d), "error")
    })
  }

  property var _pendingSave: []
  FolderDialog {
    id: saveDialog
    title: "Save from the iPhone to…"
    currentFolder: "file://" + root.defaultSave
    onAccepted: root.pull(root._pendingSave, decodeURIComponent(String(selectedFolder).replace(/^file:\/\//, "")))
  }
  function saveTo(paths) { root._pendingSave = paths; saveDialog.open() }

  FileDialog {
    id: addDialog
    title: "Copy files to the iPhone"
    fileMode: FileDialog.OpenFiles
    currentFolder: "file://" + root.app.home
    onAccepted: root.push(selectedFiles.map(function(u) { return decodeURIComponent(String(u).replace(/^file:\/\//, "")) }), false)
  }

  // ---- up, breadcrumb, what this container is, reload
  RowLayout {
    Layout.fillWidth: true
    visible: !!root.bundleId
    spacing: Style.spacing.md
    IconButton { id: upButton; Layout.alignment: Qt.AlignTop; glyph: Model.Glyph.arrowUp; tooltip: "Up one folder"; enabled: root.status === "ready" && root.path !== root.rootPath; onClicked: root.up() }
    Flow {
      Layout.fillWidth: true
      Layout.alignment: Qt.AlignVCenter
      spacing: Style.spacing.md
      Repeater {
        model: root.crumbs()
        delegate: Row {
          required property var modelData
          required property int index
          spacing: Style.spacing.md
          height: upButton.implicitHeight     // each crumb line as tall as the Up button, so they share a centre line
          Icon { visible: index > 0; anchors.verticalCenter: parent.verticalCenter; glyph: Model.Glyph.chevronRight; size: Style.font.caption; color: Theme.faint }
          AppText {
            id: crumbText
            anchors.verticalCenter: parent.verticalCenter
            text: modelData.label
            tone: modelData.path === root.path ? "primary" : "dim"
            strong: modelData.path === root.path
            font.underline: crumbHover.hovered && modelData.path !== root.path
            HoverHandler { id: crumbHover; cursorShape: Qt.PointingHandCursor }
            TapHandler { onTapped: if (modelData.path !== root.path) root.open(modelData.path) }
          }
        }
      }
    }
    StatusPill {
      Layout.alignment: Qt.AlignTop
      Layout.topMargin: (upButton.implicitHeight - implicitHeight) / 2
      visible: !!root.current
      quiet: true
      level: "info"
      glyph: root.current && root.current.developer ? Model.Glyph.developer : Model.Glyph.folder
      text: root.current && root.current.developer ? "Whole container" : "Documents only"
    }
    IconButton { visible: root.showReload; Layout.alignment: Qt.AlignTop; glyph: Model.Glyph.refresh; tooltip: "Reload this folder"; busy: root.status === "loading"; enabled: !!root.bundleId; onClicked: root.open(root.path) }
  }

  Flow {
    Layout.fillWidth: true
    visible: root.status === "ready" && !root.busy
    spacing: Style.spacing.lg
    ActionButton { text: "Add files"; glyph: Model.Glyph.upload; onClicked: addDialog.open() }
    ActionButton { text: "New folder"; glyph: Model.Glyph.folderPlus; kind: "ghost"; onClicked: { folderName.text = ""; folderSheet.open() } }
    ActionButton {
      text: root.selCount ? "Save " + root.selCount + " to…" : "Save to…"
      glyph: Model.Glyph.download
      kind: root.selCount ? "primary" : "secondary"
      disabled: root.selCount === 0
      tooltip: root.selCount ? "" : "Select files or folders first"
      onClicked: root.saveTo(Object.keys(root.selected))
    }
    ActionButton {
      text: "Delete"; glyph: Model.Glyph.trash; kind: "danger"
      visible: root.selCount > 0 && root.path !== "/"
      onClicked: root.remove(Object.keys(root.selected).map(function(k) { return root.selected[k] }))
    }
  }
  DeviceTransferBar {
    Layout.fillWidth: true
    transfer: root.transfer
    verb: root.transferVerb
    onCancel: root.svc.request("transfer.cancel", { transferId: root.transferId }, null)
  }

  // ---- states
  ErrorState {
    Layout.fillWidth: true
    visible: root.status === "error"
    app: root.app
    error: root.error
    title: root.error && root.error.code === "no-file-sharing" ? "This app doesn't share files" : "Couldn't open this folder"
    onRetry: root.open(root.path)
  }
  Skeleton { Layout.fillWidth: true; visible: root.status === "loading" && !root.listing; lines: 5; rowHeight: Style.space(40); gap: Style.spacing.md }

  // ---- the folder, also a drop target for files from the desktop
  Item {
    id: dropZone
    Layout.fillWidth: true
    visible: root.status !== "error" && !!root.listing
    implicitHeight: Math.max(list.implicitHeight, Style.space(160))
    opacity: root.status === "loading" ? 0.5 : 1

    ColumnLayout {
      id: list
      anchors.left: parent.left; anchors.right: parent.right
      spacing: Style.spacing.xxs
      Repeater {
        model: root.listing ? root.listing.entries : []
        delegate: ListRow {
          id: row
          required property var modelData
          required property int index
          readonly property bool isDir: modelData.type === "dir"
          readonly property bool denied: modelData.type === "unknown"
          Layout.fillWidth: true
          property bool hot: false
          current: !!root.selected[modelData.path]
          hasCursor: hot && !current
          onHovered: function(h) { row.hot = h }
          glyph: isDir ? Model.Glyph.folder : (denied ? Model.Glyph.lock : Model.Glyph.file)
          glyphColor: isDir ? Commons.Color.accent : Theme.dim
          title: modelData.name
          subtitle: denied ? "Not readable" : ""
          meta: {
            var p = []
            if (!row.isDir && modelData.size !== null && modelData.size !== undefined) p.push(Model.formatBytes(modelData.size))
            if (modelData.mtime) p.push(Model.relativeTime(modelData.mtime))
            return p.join("  ·  ")
          }
          onClicked: if (!row.denied) root.toggle(modelData)
          onDoubleClicked: if (row.isDir) root.open(modelData.path)
          trailing: [
            IconButton { glyph: Model.Glyph.chevronRight; tooltip: "Open folder"; visible: row.isDir; onClicked: root.open(row.modelData.path) },
            IconButton { glyph: Model.Glyph.download; tooltip: "Save to…"; visible: !row.denied && !root.busy; onClicked: root.saveTo([row.modelData.path]) },
            IconButton { glyph: Model.Glyph.trash; tooltip: "Delete"; danger: true; visible: !row.denied && !root.busy && root.path !== "/"; onClicked: root.remove([row.modelData]) }
          ]
        }
      }
    }

    EmptyState {
      anchors.left: parent.left; anchors.right: parent.right
      visible: root.status === "ready" && root.listing && root.listing.entries.length === 0
      compact: true
      glyph: Model.Glyph.folderOpen
      title: "This folder is empty"
      text: "Drop files here from your desktop, or press Add files."
    }

    DropArea {
      id: drop
      anchors.fill: parent
      enabled: root.status === "ready" && !root.busy
      onEntered: function(ev) { if (!ev.hasUrls) ev.accepted = false }
      onDropped: function(ev) {
        if (!ev.hasUrls) return
        var files = []
        for (var i = 0; i < ev.urls.length; i++) {
          var u = String(ev.urls[i])
          if (u.indexOf("file://") === 0) files.push(decodeURIComponent(u.replace(/^file:\/\//, "")))
        }
        if (files.length) { ev.accept(Qt.CopyAction); root.push(files, false) }
        else root.app.toast("Only files from this computer can be dropped here", "warn")
      }
    }
    Rectangle {
      anchors.fill: parent
      visible: drop.containsDrag
      radius: Style.cornerRadius
      color: Util.alpha(Commons.Color.accent, 0.10)
      border.width: Style.space(2)
      border.color: Commons.Color.accent
      ColumnLayout {
        anchors.centerIn: parent
        spacing: Style.spacing.md
        Icon { Layout.alignment: Qt.AlignHCenter; glyph: Model.Glyph.upload; size: Style.font.iconLarge * 1.6; color: Commons.Color.accent }
        AppText { Layout.alignment: Qt.AlignHCenter; text: "Drop to copy into " + (root.path === "/" ? (root.current ? root.current.name : "the app") : root.path.substring(root.path.lastIndexOf("/") + 1)); strong: true }
      }
    }
  }

  AppText {
    Layout.fillWidth: true
    visible: root.status === "ready"
    text: "Click to select, double-click a folder to open it. Drag files from your desktop onto the list to copy them in."
    size: "caption"; tone: "faint"; wrapMode: Text.WordWrap
    horizontalAlignment: Text.AlignRight
  }

  Item {
    visible: false
    Sheet {
      id: folderSheet
      title: "New folder"
      subtitle: "In " + (root.path === "/" ? (root.current ? root.current.name : "the app") : root.path)
      glyph: Model.Glyph.folderPlus
      TextField { id: folderName; Layout.fillWidth: true; placeholderText: "Folder name"; onAccepted: root.makeFolder(text) }
      footer: [
        ActionButton { text: "Cancel"; kind: "ghost"; onClicked: folderSheet.close() },
        ActionButton { text: "Create"; kind: "primary"; enabled: folderName.text.trim() !== ""; onClicked: root.makeFolder(folderName.text) }
      ]
      onShown: folderName.forceActiveFocus()
    }
  }
}
