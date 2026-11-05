import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import qs.components
import "../Model.js" as Model

// Toolchain: what's installed (versions, SDKs), how much disk each piece
// takes, safe cleanup, and removing everything.
PageFrame {
  id: page
  required property var app
  readonly property var svc: app.toolchain

  title: "Toolchain"
  subtitle: "Versions, the iOS SDK, disk usage and cleanup."
  actions: [
    IconButton { glyph: Model.Glyph.refresh; tooltip: "Refresh (r)"; busy: page.svc.status === "loading" || page.svc.diskStatus === "loading"; onClicked: page.svc.refresh() },
    ActionButton { text: "Open Setup"; glyph: Model.Glyph.rocket; kind: "ghost"; onClicked: page.app.nav("setup") }
  ]

  property int cursor: -1
  property bool removeAuth: false

  function refresh() { page.svc.refresh() }
  // ios-studio toolchain / nav("toolchain", { action: "remove-all" }) from Settings › Uninstall
  function activated(params) {
    if (params && params.action === "remove-all") Qt.callLater(function() { page.scrollToItem(dangerZone) })
  }

  // keyboard: j/k over the version rows then the disk rows; Enter = Repair / Clean
  readonly property var diskRows: page.svc.disk && page.svc.disk.items
    ? page.svc.disk.items.filter(function(i) { return i.size > 0 }).sort(function(a, b) { return b.size - a.size }) : []
  function handleKey(event) {
    var a = Model.keyAction(event)
    var n = page.svc.items.length + page.diskRows.length
    if (!n) return false
    if (a === "down") { page.cursor = Math.min(n - 1, page.cursor + 1); return true }
    if (a === "up") { page.cursor = Math.max(0, page.cursor - 1); return true }
    if (a === "activate" && page.cursor >= 0) {
      if (page.cursor < page.svc.items.length) {
        var it = page.svc.items[page.cursor]
        if (it.status !== "ok") page.app.openSetupStep(it.step)
      } else {
        var d = page.diskRows[page.cursor - page.svc.items.length]
        if (d && d.cleanable) page.askClean(d)
      }
      return true
    }
    return false
  }

  function levelOf(status) { return status === "ok" ? "ok" : status === "error" ? "error" : status === "warn" ? "warn" : "info" }
  function statusText(i) {
    if (i.status === "ok") return i.version ? i.version : "installed"
    if (i.status === "error") return "needs repair"
    return i.optional ? "not installed" : "missing"
  }

  function askClean(d) {
    page.app.confirm({
      title: "Delete " + d.name.toLowerCase() + "?",
      message: d.note + "\n\nThis frees " + Model.formatBytes(d.size) + ".",
      confirmText: "Delete " + Model.formatBytes(d.size),
      destructive: true,
      glyph: Model.Glyph.broom,
      onConfirm: function() { page.svc.clean(d.id) }
    })
  }

  // ------------------------------------------------------------ loading / error
  Skeleton { Layout.fillWidth: true; visible: page.svc.status === "loading" && page.svc.items.length === 0; lines: 6; rowHeight: Style.space(40) }
  ErrorState { Layout.fillWidth: true; visible: page.svc.status === "error" && page.svc.items.length === 0; app: page.app; error: page.svc.error; onRetry: page.svc.refresh() }

  // ------------------------------------------------------------ versions
  ColumnLayout {
    Layout.fillWidth: true
    visible: page.svc.items.length > 0
    spacing: Style.spacing.md

    SectionHeader {
      Layout.fillWidth: true
      text: "Installed"
      detail: page.svc.items.filter(function(i) { return i.status === "ok" }).length + " of " + page.svc.items.length
    }

    Repeater {
      model: page.svc.items
      delegate: ListRow {
        required property var modelData
        required property int index
        Layout.fillWidth: true
        glyph: modelData.id === "sdk" ? Model.Glyph.apple : modelData.id === "swift" ? Model.Glyph.swift
             : modelData.id === "pymobiledevice3" ? Model.Glyph.phone : modelData.id === "xtool" ? Model.Glyph.hammer
             : modelData.id === "apple-dev" ? Model.Glyph.folder : Model.Glyph.wrench
        glyphColor: modelData.status === "ok" ? Theme.dim : Theme.levelColor(page.levelOf(modelData.status))
        title: modelData.name
        subtitle: {
          var s = modelData.detail || ""
          if (modelData.id === "swift" && modelData.held) s += " · held at this version"
          if (modelData.id === "xtool" && modelData.hasFix) s += " · Apple sign-in fix included"
          if (modelData.pinned && modelData.version && modelData.pinned !== modelData.version && modelData.status === "ok") s += " · tested: " + modelData.pinned
          return s
        }
        hasCursor: page.cursor === index
        onHovered: function(h) { if (h) page.cursor = index }
        trailing: [
          StatusPill { level: page.levelOf(modelData.status); text: page.statusText(modelData); quiet: modelData.status === "ok" },
          ActionButton {
            visible: modelData.status !== "ok"
            compact: true
            text: modelData.status === "error" ? "Repair" : "Install"
            glyph: modelData.status === "error" ? Model.Glyph.wrench : Model.Glyph.download
            onClicked: page.app.openSetupStep(modelData.step)
          }
        ]
      }
    }

    // SDK copies
    AppText {
      Layout.fillWidth: true
      Layout.topMargin: Style.spacing.md
      visible: !!page.svc.sdks.caches && page.svc.sdks.caches.length > 0
      size: "small"; tone: "dim"; wrapMode: Text.WordWrap
      text: {
        var c = page.svc.sdks.caches || []
        var spare = c.filter(function(x) { return x.current && x.complete }).map(function(x) { return "iOS " + x.ios })
        var old = c.filter(function(x) { return !x.current })
        return (spare.length ? "Spare SDK copy for repairs without Xcode: " + spare.join(", ") + "." : "")
             + (old.length ? " " + old.length + " outdated cop" + (old.length === 1 ? "y" : "ies") + " can be cleaned below." : "")
      }
    }
  }

  // ------------------------------------------------------------ disk
  ColumnLayout {
    Layout.fillWidth: true
    spacing: Style.spacing.lg

    SectionHeader {
      Layout.fillWidth: true
      text: "Disk usage"
      detail: page.svc.disk ? Model.formatBytes(page.svc.disk.total) + (page.svc.diskStale ? " · updating…" : "") : ""
    }

    Skeleton { Layout.fillWidth: true; visible: !page.svc.disk && page.svc.diskStatus === "loading"; lines: 4; rowHeight: Style.space(28) }
    ErrorState { Layout.fillWidth: true; visible: !page.svc.disk && page.svc.diskStatus === "error"; banner: true; app: page.app; error: page.svc.diskError; onRetry: page.svc.refreshDisk() }

    // headline + one stacked bar for the whole toolchain
    ColumnLayout {
      Layout.fillWidth: true
      visible: !!page.svc.disk
      spacing: Style.spacing.md
      readonly property var d: page.svc.disk || { items: [], total: 0, reclaimable: 0, free: 0 }

      // headline: wraps onto two lines in a narrow window instead of pushing the page wider
      Flow {
        id: diskHead
        Layout.fillWidth: true
        spacing: Style.spacing.lg
        readonly property var d: parent.d
        Row {
          spacing: Style.spacing.lg
          AppText { anchors.baseline: usedLbl.baseline; text: Model.formatBytes(diskHead.d.total); size: "heading"; strong: true }
          AppText { id: usedLbl; text: "used by iOS tools"; tone: "dim"; topPadding: Style.spacing.sm }
        }
        AppText {
          visible: diskHead.d.reclaimable > 0
          topPadding: Style.spacing.sm
          text: Model.formatBytes(diskHead.d.reclaimable) + " can be freed safely"
          tone: "accent"
        }
        AppText { topPadding: Style.spacing.sm + Style.spacing.xxs; text: Model.formatBytes(diskHead.d.free) + " free on disk"; size: "small"; tone: "faint" }
      }

      Item {
        id: stack
        Layout.fillWidth: true
        implicitHeight: Style.space(10)
        readonly property var d: parent.d
        Rectangle { anchors.fill: parent; radius: Style.cornerRadius > 0 ? height / 2 : 0; color: Util.alpha(Commons.Color.foreground, 0.06) }
        Row {
          anchors.fill: parent
          spacing: Style.space(1)
          Repeater {
            model: page.diskRows
            delegate: Rectangle {
              required property var modelData
              height: stack.height
              width: stack.d.total > 0 ? Math.max(Style.space(2), (stack.width - page.diskRows.length) * modelData.size / stack.d.total) : 0
              color: modelData.cleanable && modelData.safe ? Commons.Color.accent
                   : modelData.cleanable ? Theme.warn
                   : Util.alpha(Commons.Color.foreground, 0.32)
              opacity: segHover.hovered ? 1 : 0.85
              HoverHandler { id: segHover }
              PanelToolTip { visible: segHover.hovered; text: modelData.name + " · " + Model.formatBytes(modelData.size) }
            }
          }
        }
      }

      Flow {
        Layout.fillWidth: true
        spacing: Style.spacing.xxl
        Repeater {
          model: [
            { c: Util.alpha(Commons.Color.foreground, 0.32), t: "In use" },
            { c: Commons.Color.accent, t: "Safe to clean" },
            { c: Theme.warn, t: "Clean with care" }
          ]
          delegate: RowLayout {
            required property var modelData
            spacing: Style.spacing.sm
            Rectangle { implicitWidth: Style.space(8); implicitHeight: implicitWidth; radius: Style.cornerRadius > 0 ? width / 2 : 0; color: modelData.c }
            AppText { text: modelData.t; size: "small"; tone: "dim" }
          }
        }
      }
    }

    // one row per piece
    Repeater {
      model: page.diskRows
      delegate: BorderSurface {
        id: drow
        required property var modelData
        required property int index
        readonly property int flat: page.svc.items.length + index
        readonly property bool hot: page.cursor === flat
        readonly property bool running: page.svc.busyItem === modelData.id
        readonly property real maxSize: page.diskRows.length ? page.diskRows[0].size : 1
        Layout.fillWidth: true
        radius: Style.cornerRadius
        color: hot ? Theme.surfaceRaised : Theme.surface
        borderSpec: Border.flat(hot ? Util.alpha(Commons.Color.foreground, 0.22) : Theme.hairline, Style.normalBorderWidth)
        implicitHeight: dcol.implicitHeight + Style.spacing.xl * 2
        HoverHandler { onHoveredChanged: if (hovered) page.cursor = drow.flat }

        ColumnLayout {
          id: dcol
          anchors.fill: parent
          anchors.margins: Style.spacing.xl
          anchors.leftMargin: Style.spacing.xxl
          anchors.rightMargin: Style.spacing.xxl
          spacing: Style.spacing.md

          RowLayout {
            Layout.fillWidth: true
            spacing: Style.spacing.lg
            AppText { text: drow.modelData.name; strong: true; elide: Text.ElideRight; Layout.fillWidth: true }
            AppText { text: Model.formatBytes(drow.modelData.size); strong: true; tone: drow.modelData.cleanable && drow.modelData.safe ? "accent" : "primary" }
            ActionButton {
              visible: drow.modelData.cleanable
              compact: true
              text: "Clean"
              glyph: Model.Glyph.broom
              kind: "secondary"
              busy: drow.running
              enabled: !page.svc.busy || drow.running
              onClicked: page.askClean(drow.modelData)
            }
          }
          // share of the biggest piece
          Rectangle {
            Layout.fillWidth: true
            implicitHeight: Style.space(3)
            radius: Style.cornerRadius > 0 ? height / 2 : 0
            color: Util.alpha(Commons.Color.foreground, 0.06)
            Rectangle {
              width: parent.width * Math.min(1, drow.modelData.size / Math.max(1, drow.maxSize))
              height: parent.height; radius: parent.radius
              color: drow.modelData.cleanable && drow.modelData.safe ? Commons.Color.accent : drow.modelData.cleanable ? Theme.warn : Util.alpha(Commons.Color.foreground, 0.32)
              Behavior on width { NumberAnimation { duration: 260; easing.type: Easing.OutCubic } }
            }
          }
          AppText { Layout.fillWidth: true; text: drow.modelData.note; size: "small"; tone: "dim"; wrapMode: Text.WordWrap }
          AppText {
            Layout.fillWidth: true
            visible: drow.hot && drow.modelData.paths.length > 0
            text: drow.modelData.paths.map(function(p) { return p.path }).join("\n")
            size: "caption"; tone: "faint"; wrapMode: Text.WrapAnywhere
          }
          ProgressTrack {
            Layout.fillWidth: true
            visible: drow.running
            value: page.svc.busyFraction >= 0 ? page.svc.busyFraction : 0
            indeterminate: page.svc.busyFraction < 0
            label: page.svc.busyDetail
          }
        }
      }
    }
  }

  // ------------------------------------------------------------ danger zone
  BorderSurface {
    id: dangerZone
    Layout.fillWidth: true
    Layout.topMargin: Style.spacing.xxl
    radius: Style.cornerRadius
    color: Util.alpha(Commons.Color.urgent, 0.04)
    borderSpec: Border.flat(Util.alpha(Commons.Color.urgent, 0.35), Style.normalBorderWidth)
    implicitHeight: dz.implicitHeight + Style.spacing.panelPadding * 2

    ColumnLayout {
      id: dz
      anchors.fill: parent
      anchors.margins: Style.spacing.panelPadding
      spacing: Style.spacing.lg

      RowLayout {
        Layout.fillWidth: true
        spacing: Style.spacing.huge
        ColumnLayout {
          Layout.fillWidth: true
          spacing: Style.spacing.xs
          AppText { text: "Remove everything"; size: "title"; strong: true }
          AppText {
            Layout.fillWidth: true
            text: "Uninstalls Swift, xtool, the iOS SDK and all caches (about " + (page.svc.disk ? Model.formatBytes(page.svc.disk.total - (page.diskRows.filter(function(i) { return i.id === "project-builds" || i.id === "xip" }).reduce(function(a, i) { return a + i.size }, 0))) : "20 GB") + "). Your projects, your Xcode download and your iPhone pairing stay. You can set everything up again from Setup."
            tone: "dim"; wrapMode: Text.WordWrap; lineHeight: 1.2
          }
        }
        ActionButton {
          Layout.alignment: Qt.AlignTop
          text: "Remove everything"
          glyph: Model.Glyph.trash
          kind: "danger"
          busy: page.svc.busyItem === "remove-all"
          enabled: !page.svc.busy || page.svc.busyItem === "remove-all"
          onClicked: page.app.confirm({
            title: "Remove the whole iOS toolchain?",
            message: "Swift, xtool, the iOS SDK and their caches are deleted, and you'll be asked for your password once to uninstall Swift."
                     + (page.removeAuth ? " You'll also be signed out of your Apple Account." : " You stay signed in to your Apple Account.")
                     + "\n\nYour projects and iPhone pairing are kept.",
            confirmText: "Remove everything",
            destructive: true,
            glyph: Model.Glyph.trash,
            onConfirm: function() { page.svc.removeAll(page.removeAuth) }
          })
        }
      }

      Toggle {
        Layout.fillWidth: true
        label: "Also sign out of Apple"
        description: "Deletes the saved Apple Account sign-in. Signing in again needs your password and a 2-factor code."
        checked: page.removeAuth
        onClicked: page.removeAuth = !page.removeAuth
      }

      ProgressTrack {
        Layout.fillWidth: true
        visible: page.svc.busyItem === "remove-all"
        value: page.svc.busyFraction >= 0 ? page.svc.busyFraction : 0
        indeterminate: page.svc.busyFraction < 0
        label: page.svc.busyDetail
      }
    }
  }
}
