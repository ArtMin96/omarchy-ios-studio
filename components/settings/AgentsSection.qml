import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../common"
import "../../core"
import "../../lib/Model.js" as Model

// Settings › AI agents: one-click registration of iOS Studio's MCP server with the AI coding tools
// installed on this computer (helper `agents` group, see docs/mcp.md).
//   AgentsSection { Layout.fillWidth: true; app: page.app }
// prepare(repair) and refresh() are public so the page can deep-link into them.
Card {
  id: root
  required property var app

  title: "AI agents"
  subtitle: "Let AI coding tools like Claude Code build, run and debug your iPhone apps through iOS Studio."
  glyph: Model.Glyph.robot
  contentSpacing: Style.spacing.md
  actions: [
    IconButton { glyph: Model.Glyph.help; tooltip: "How it works"; onClicked: root.app.openPath(root.app.repoPath + "/docs/mcp.md") },
    IconButton { glyph: Model.Glyph.refresh; tooltip: "Check again"; busy: root.loading; onClicked: root.refresh() }
  ]

  Resource { id: listRes; fetch: root.app.cli.fetcher(["agents", "list"]) }   // helper `agents list`
  readonly property var info: listRes.data
  readonly property var error: listRes.error
  readonly property bool loading: listRes.busy
  property var busy: ({})              // client id -> "connect" | "disconnect" | "test"
  property bool preparing: false
  property string prepareDetail: ""
  property var prepareError: null

  readonly property var server: info ? info.server : null
  readonly property var clients: info ? info.clients : []
  readonly property int installedCount: clients.filter(function(c) { return c.installed }).length
  readonly property int connectedCount: clients.filter(function(c) { return c.registered }).length
  readonly property bool demoMode: !!root.app.settings.agentsDemo
  readonly property real actionSlotWidth: Math.max(probeConnect.implicitWidth, probeDisconnect.implicitWidth, probeUpdate.implicitWidth)

  readonly property var glyphs: ({
    claude: Model.Glyph.asterisk, codex: Model.Glyph.hexagon, opencode: Model.Glyph.consoleLine,
    cursor: Model.Glyph.cursorArrow, gemini: Model.Glyph.starFour, crush: Model.Glyph.heart, zed: Model.Glyph.lightning
  })

  function refresh() { listRes.load(true) }
  Component.onCompleted: refresh()

  function _busy(id, what) {
    var b = Object.assign({}, root.busy)
    if (what) b[id] = what; else delete b[id]
    root.busy = b
  }

  function connect(c) {
    _busy(c.id, "connect")
    var args = ["agents", "connect", "--client", c.id]
    if (root.demoMode) args.push("--demo")
    root.app.cli.call(args, "",
      function(d) {
        _busy(c.id, "")
        root.app.toast(c.name + " is connected" + (d.demo ? " in demo mode" : "") + ". " + (d.restartHint || ""), "ok")
        refresh()
        if (root.server && !root.server.ready && !root.preparing) prepare(false)
      },
      function(e) { _busy(c.id, ""); root.app.toastError(e); refresh() })
  }

  function askDisconnect(c) {
    root.app.confirm({
      title: "Disconnect " + c.name + "?",
      message: c.name + " won't be able to use iOS Studio anymore. Nothing else in its settings changes"
               + (c.method === "file" ? ", and a backup of " + c.whereShort + " is saved first." : ".")
               + "\n\nYou can connect it again any time.",
      confirmText: "Disconnect",
      destructive: true,
      glyph: Model.Glyph.linkOff,
      onConfirm: function() {
        _busy(c.id, "disconnect")
        root.app.cli.call(["agents", "disconnect", "--client", c.id], "",
          function(d) { _busy(c.id, ""); root.app.toast(c.name + " is disconnected", "ok"); refresh() },
          function(e) { _busy(c.id, ""); root.app.toastError(e); refresh() })
      }
    })
  }

  function test(c) {
    _busy(c.id, "test")
    root.app.cli.call(["agents", "test", "--client", c.id], "",
      function(d) {
        _busy(c.id, "")
        root.app.toast(c.name + " can use " + d.tools + " iOS Studio tools" + (d.demo ? " (demo data)" : ""), "ok")
      },
      function(e) { _busy(c.id, ""); root.app.toastError(e) },
      { timeout: 200000 })
  }

  function copySnippet(c) {
    if (!c.snippet) return
    root.app.copyText(c.snippet.text, c.snippet.where ? "the config. Paste it into " + c.snippet.where
                                                      : "the command. Run it in a terminal to connect " + c.name)
  }

  function prepare(repair) {
    if (prepStream.running) return
    root.preparing = true
    root.prepareError = null
    root.prepareDetail = "Starting…"
    prepStream.start(repair ? ["agents", "prepare", "--repair"] : ["agents", "prepare"])
  }

  Stream {
    id: prepStream
    onEvent: function(ev) {
      if (ev.event === "progress" && ev.detail) root.prepareDetail = ev.detail
      else if (ev.event === "step" && ev.state === "running") root.prepareDetail = ev.detail || ev.title
    }
    onFinished: function(ok, data, error) {
      root.preparing = false
      if (ok) { root.app.toast("The iOS Studio server is ready", "ok"); root.prepareError = null }
      else root.prepareError = error || { code: "failed", message: "Couldn't set up the server." }
      root.refresh()
    }
  }

  // ---------------------------------------------------------------- server status
  readonly property string serverLevel: root.preparing ? "busy"
                                      : root.prepareError ? "error"
                                      : !root.server ? "info"
                                      : root.server.ready ? "ok" : "warn"

  RowLayout {
    Layout.fillWidth: true
    Layout.topMargin: Style.spacing.lg
    spacing: Style.spacing.xxl

    Rectangle {
      Layout.alignment: Qt.AlignTop
      implicitWidth: Style.space(40); implicitHeight: Style.space(40)
      radius: Style.cornerRadius
      color: Theme.levelFill(root.serverLevel === "busy" ? "info" : root.serverLevel)
      Icon {
        anchors.centerIn: parent
        glyph: root.serverLevel === "ok" ? Model.Glyph.checkCircle
             : root.serverLevel === "error" ? Model.Glyph.error
             : root.serverLevel === "busy" ? Model.Glyph.sync
             : Model.Glyph.download
        spinning: root.preparing
        size: Style.font.iconLarge
        color: Theme.levelColor(root.serverLevel === "busy" ? "info" : root.serverLevel)
      }
    }
    ColumnLayout {
      Layout.fillWidth: true
      spacing: Style.spacing.xxs
      AppText { Layout.fillWidth: true; text: "iOS Studio server"; size: "subtitle" }
      AppText {
        Layout.fillWidth: true
        wrapMode: Text.Wrap; lineHeight: 1.2
        size: "small"; tone: "dim"
        text: root.preparing ? "Getting the server ready. This takes a few seconds, longer the first time."
            : root.prepareError ? "The server isn't ready yet."
            : !root.server ? "Checking…"
            : root.server.ready ? "Ready. Your AI tool starts it by itself whenever it needs iOS Studio."
            : root.server.state === "outdated" ? "An update is waiting. Prepare it now so your AI tool doesn't have to wait."
            : "Not set up yet. Prepare it now (a one-time download of about 30 MB) so your AI tool starts instantly."
      }
      ProgressTrack {
        Layout.fillWidth: true
        Layout.topMargin: Style.spacing.sm
        visible: root.preparing
        indeterminate: true
        label: root.prepareDetail
      }
    }
    ActionButton {
      Layout.alignment: Qt.AlignTop
      visible: !!root.server
      text: root.server && root.server.ready && !root.prepareError ? "Check" : "Prepare"
      glyph: root.server && root.server.ready && !root.prepareError ? Model.Glyph.refresh : Model.Glyph.download
      kind: root.server && !root.server.ready ? "primary" : "ghost"
      compact: root.server && root.server.ready
      busy: root.preparing
      tooltip: root.server ? "Python environment: " + root.server.venvShort : ""
      onClicked: root.prepare(false)
    }
  }

  ErrorState {
    Layout.fillWidth: true
    visible: !!root.prepareError && !root.preparing
    banner: true
    app: root.app
    error: root.prepareError
    showRetry: true
    retryText: "Rebuild"
    onRetry: root.prepare(true)
  }

  // ---------------------------------------------------------------- tools
  SectionHeader {
    Layout.fillWidth: true
    Layout.topMargin: Style.spacing.lg
    text: "Your AI tools"
    detail: root.info ? (root.connectedCount > 0 ? root.connectedCount + " connected" : root.installedCount + " found") : ""
  }

  ErrorState {
    Layout.fillWidth: true
    visible: !!root.error && !root.info
    banner: true
    app: root.app
    error: root.error
    onRetry: root.refresh()
  }

  Skeleton {
    Layout.fillWidth: true
    visible: !root.info && !root.error
    lines: 4
    rowHeight: Style.space(44)
    gap: Style.spacing.sm
  }

  // Width probes for the rows' button slot (hidden, so the layout skips them).
  ActionButton { id: probeConnect; visible: false; compact: true; text: "Connect"; glyph: Model.Glyph.link }
  ActionButton { id: probeDisconnect; visible: false; compact: true; text: "Disconnect" }
  ActionButton { id: probeUpdate; visible: false; compact: true; text: "Update"; glyph: Model.Glyph.sync }

  Repeater {
    model: root.clients
    delegate: Item {
      id: row
      required property var modelData
      required property int index
      readonly property var c: modelData
      readonly property string busyState: root.busy[c.id] || ""
      readonly property bool narrow: width > 0 && width < Style.space(600)
      readonly property string pillLevel: c.stale ? "warn" : c.registered ? "ok" : "info"
      readonly property string pillText: c.stale ? "Needs update" : c.registered ? (c.demo ? "Connected · demo" : "Connected") : "Not connected"

      Layout.fillWidth: true
      implicitHeight: rowLayout.implicitHeight + Style.spacing.lg * 2
      opacity: c.installed || c.registered ? 1 : 0.55

      Rectangle {
        visible: row.index > 0
        anchors.top: parent.top
        width: parent.width
        height: Style.spacing.hairline
        color: Theme.hairline
      }

      RowLayout {
        id: rowLayout
        anchors.fill: parent
        anchors.topMargin: Style.spacing.lg
        anchors.bottomMargin: Style.spacing.lg
        spacing: Style.spacing.xxl

        IconTile {
          Layout.alignment: Qt.AlignVCenter
          glyph: root.glyphs[row.c.id] || Model.Glyph.robot
          level: row.c.registered ? "info" : ""
          glyphColor: row.c.registered ? Commons.Color.accent : (row.c.installed ? Commons.Color.foreground : Theme.dim)
        }

        ColumnLayout {
          Layout.fillWidth: true
          spacing: Style.spacing.xxs
          RowLayout {
            Layout.fillWidth: true
            spacing: Style.spacing.lg
            AppText { text: row.c.name; strong: row.c.registered; elide: Text.ElideRight; Layout.maximumWidth: rowLayout.width * 0.4 }
            StatusPill {
              visible: row.c.installed || row.c.registered
              level: row.pillLevel
              text: row.pillText
              showGlyph: row.c.registered
              quiet: !row.c.registered
            }
            Item { Layout.fillWidth: true }
          }
          AppText {
            Layout.fillWidth: true
            size: "small"; tone: row.c.error ? "warn" : "dim"
            elide: Text.ElideMiddle
            text: row.c.error ? row.c.error.message
                : !row.c.installed && !row.c.registered ? "Not installed"
                : row.c.stale ? "Points to an old copy of iOS Studio (" + (row.c.command || "").replace(root.app.home, "~") + ")"
                : row.c.registered ? (row.c.method === "cli" ? "Set up through " + row.c.name + "'s own settings" : "Saved in " + row.c.whereShort)
                : row.c.method === "cli" ? "Connects through " + row.c.name + "'s own settings"
                : "Adds one entry to " + row.c.fileShort
          }
        }

        Row {
          Layout.alignment: Qt.AlignVCenter
          spacing: Style.spacing.xs
          IconButton {
            glyph: Model.Glyph.copy
            tooltip: "Copy setup snippet"
            visible: !!row.c.snippet && (!row.narrow || !row.c.installed)
            anchors.verticalCenter: parent.verticalCenter
            onClicked: root.copySnippet(row.c)
          }
          IconButton {
            // Hidden by opacity, not visible: it keeps its place so the copy icons line up.
            readonly property bool shown: row.c.registered && !row.c.stale
            glyph: Model.Glyph.testTube
            tooltip: "Test the connection"
            opacity: shown ? 1 : 0
            busy: row.busyState === "test"
            enabled: shown && row.busyState === ""
            anchors.verticalCenter: parent.verticalCenter
            onClicked: root.test(row.c)
          }
          // Fixed-width slot (the widest label), so the icons to its left line up on every row,
          // with or without a button and whatever it says.
          Item {
            width: root.actionSlotWidth
            height: rowAction.implicitHeight
            anchors.verticalCenter: parent.verticalCenter
            ActionButton {
              id: rowAction
              visible: row.c.installed || row.c.registered
              anchors.fill: parent
              compact: true
              text: row.c.stale ? "Update" : row.c.registered ? "Disconnect" : "Connect"
              glyph: row.c.stale ? Model.Glyph.sync : row.c.registered ? "" : Model.Glyph.link
              kind: row.c.registered && !row.c.stale ? "ghost" : "secondary"
              busy: row.busyState === "connect" || row.busyState === "disconnect"
              enabled: row.busyState === "" && !row.c.error
              onClicked: row.c.registered && !row.c.stale ? root.askDisconnect(row.c) : root.connect(row.c)
            }
          }
        }
      }
    }
  }

  // ---------------------------------------------------------------- demo + safety
  SettingRow {
    title: "Connect in demo mode"
    description: "Tools you connect next see a made-up iPhone, Apple ID and projects, so you can try things out without changing anything real."
    ToggleSwitch {
      checked: root.demoMode
      onToggled: root.app.setSetting("agentsDemo", !checked)
    }
  }

  Rectangle {
    Layout.fillWidth: true
    radius: Style.cornerRadius
    color: Theme.surface
    implicitHeight: safety.implicitHeight + Style.spacing.lg * 2
    RowLayout {
      id: safety
      anchors.fill: parent
      anchors.margins: Style.spacing.lg
      anchors.leftMargin: Style.spacing.xl
      spacing: Style.spacing.xl
      Icon { glyph: Model.Glyph.shieldCheck; color: Theme.success; size: Style.font.iconLarge; Layout.alignment: Qt.AlignTop }
      ColumnLayout {
        Layout.fillWidth: true
        spacing: Style.spacing.xxs
        AppText { Layout.fillWidth: true; text: "You stay in charge"; tone: "primary" }
        AppText {
          Layout.fillWidth: true
          wrapMode: Text.Wrap; lineHeight: 1.2
          size: "small"; tone: "dim"
          text: "Agents can't sign in to your Apple ID, pair your iPhone or delete anything on their own. Those steps open here in iOS Studio for you, and anything that removes something asks you first."
        }
      }
    }
  }
}
