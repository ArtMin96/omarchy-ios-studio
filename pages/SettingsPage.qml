import QtQuick
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../components/common"
import "../core"
import "../components/settings"
import "../lib/Model.js" as Model

// Settings: preferences in ~/.config/ios-studio/settings.json, plus desktop
// integration (launcher key, window rule) done by the helper's `app` group.
PageFrame {
  id: page
  required property var app

  title: "Settings"
  subtitle: "Preferences are saved as you change them."

  Resource { id: integrationRes; fetch: page.app.cli.fetcher(["app", "status"]) }   // helper `app status`
  Resource { id: aboutRes; fetch: page.app.cli.fetcher(["app", "about"]) }          // helper `app about`
  readonly property var integration: integrationRes.data
  readonly property var integrationError: integrationRes.error
  readonly property var about: aboutRes.data
  property bool keyBusy: false
  property bool windowBusy: false
  property bool opaqueBusy: false

  function refresh() {
    integrationRes.load(true)
    aboutRes.load(true)
  }
  function activated(params) {
    if (!page.integration) refresh()
    var p = params || {}
    if (p.section === "editor") {
      page.app.projects.loadEditors()
      Qt.callLater(function() {
        var f = page.flickable
        var y = editorSection.mapToItem(f.contentItem, 0, 0).y - Style.spacing.lg
        f.contentY = Math.max(0, Math.min(y, f.contentHeight - f.height))
      })
    }
    if (p.section === "agents" || p.tab === "agents" || String(p.action || "").indexOf("agents") === 0) {
      agents.refresh()
      if (p.action === "agents-repair") agents.prepare(true)
      Qt.callLater(function() {   // bring the section to the top
        var f = page.flickable
        var y = agents.mapToItem(f.contentItem, 0, 0).y - Style.spacing.lg
        f.contentY = Math.max(0, Math.min(y, f.contentHeight - f.height))
      })
    }
  }
  Component.onCompleted: refresh()

  // Saved folders, or (until the user edits the list) the detected defaults the helper scans.
  function roots() {
    var saved = page.app.settings.projectRoots || []
    if (saved.length) return saved
    return page.app.projects ? (page.app.projects.defaultRoots || []) : []
  }
  function addRoot(path) {
    var p = String(path || "").trim()
    if (p === "") return
    if (p.indexOf("~/") === 0) p = page.app.home + p.substring(1)
    var r = roots().slice()
    if (r.indexOf(p) >= 0) { page.app.toast("That folder is already in the list"); return }
    r.push(p)
    page.app.setSetting("projectRoots", r)
    page.app.toast("Added " + p, "ok")
    if (page.app.projects && typeof page.app.projects.refresh === "function") page.app.projects.refresh()
  }
  function removeRoot(path) {
    page.app.confirm({
      title: "Stop looking in this folder?",
      message: path + "\n\nYour files stay where they are; iOS Studio just won't list projects from here.",
      confirmText: "Remove", destructive: true,
      onConfirm: function() {
        var r = roots().filter(function(x) { return x !== path })
        page.app.setSetting("projectRoots", r)
        if (page.app.projects && typeof page.app.projects.refresh === "function") page.app.projects.refresh()
      }
    })
  }

  function setKeybinding(on) {
    page.keyBusy = true
    page.app.cli.call(["app", "keybinding", "--enabled", on ? "true" : "false"], "",
      function(d) { page.keyBusy = false; page.app.toast(on ? "Super + Shift + I now opens iOS Studio" : "Shortcut removed", "ok"); page.refresh() },
      function(e) { page.keyBusy = false; page.app.toastError(e); page.refresh() })
  }
  function setWindowMode(mode) {
    page.windowBusy = true
    page.app.cli.call(["app", "window-rule", "--mode", mode], "",
      function(d) { page.windowBusy = false; page.app.toast(mode === "float" ? "iOS Studio will open as a floating window" : "iOS Studio will tile like other apps", "ok"); page.refresh() },
      function(e) { page.windowBusy = false; page.app.toastError(e); page.refresh() })
  }

  function setOpaque(on) {
    page.opaqueBusy = true
    page.app.cli.call(["app", "opacity", "--opaque", on ? "on" : "off"], "",
      function(d) { page.opaqueBusy = false; page.app.setSetting("opaqueWindow", on); page.app.toast(on ? "The window is now solid" : "The window now follows Omarchy's see-through look", "ok"); page.refresh() },
      function(e) { page.opaqueBusy = false; page.app.toastError(e); page.refresh() })
  }

  // Uninstall: install.sh --uninstall removes the launcher entry, icon, hooks and Hyprland rules,
  // then the app quits. The checkout, settings and toolchain stay (Toolchain › Remove everything).
  property bool uninstalling: false
  Process {
    id: uninstaller
    command: [page.app.repoPath + "/install.sh", "--uninstall"]
    onExited: function(code) {
      page.uninstalling = false
      if (code === 0) Qt.quit()
      else page.app.toast("Couldn't uninstall iOS Studio. Run ios-studio --uninstall in a terminal to see why.", "error")
    }
  }
  function askUninstall() {
    page.app.confirm({
      title: "Uninstall iOS Studio?",
      message: "Removes iOS Studio from the app launcher, its icon, the ios-studio command, the theme hook and its Hyprland rules and shortcut, then quits."
               + "\n\nSwift, xtool, the iOS SDK, your projects and your settings stay. To remove the toolchain too, use Toolchain › Remove everything first."
               + "\n\nThe app folder (" + page.app.repoPath.replace(page.app.home, "~") + ") stays; delete it to finish.",
      confirmText: "Uninstall",
      destructive: true,
      glyph: Model.Glyph.trash,
      onConfirm: function() { page.uninstalling = true; uninstaller.running = true }
    })
  }

  Process {
    id: picker
    command: ["zenity", "--file-selection", "--directory", "--title=Choose a projects folder"]
    stdout: StdioCollector { onStreamFinished: { var p = String(text || "").trim(); if (p) page.addRoot(p) } }
  }

  // ---------------------------------------------------------------- projects
  Card {
    Layout.fillWidth: true
    title: "Project folders"
    subtitle: "iOS Studio looks for xtool projects (Package.swift + xtool.yml) in these folders."
    glyph: Model.Glyph.folder
    contentSpacing: Style.spacing.md

    Repeater {
      model: page.roots()
      delegate: ListRow {
        required property var modelData
        required property int index
        Layout.fillWidth: true
        // bleed into the card padding so the folder icon lines up with the card's own content
        Layout.leftMargin: -Style.spacing.rowPaddingX
        Layout.rightMargin: -Style.spacing.md
        glyph: Model.Glyph.folderOpen
        title: String(modelData).replace(page.app.home, "~")
        subtitle: String(modelData).indexOf(page.app.home + "/") === 0 ? "In your home folder" : String(modelData)
        hasCursor: rowHover.hovered
        HoverHandler { id: rowHover }
        onClicked: page.app.openPath(modelData)
        trailing: [
          IconButton { glyph: Model.Glyph.openExternal; tooltip: "Open folder"; onClicked: page.app.openPath(modelData) },
          IconButton { glyph: Model.Glyph.trash; tooltip: "Remove from list"; danger: true; onClicked: page.removeRoot(String(modelData)) }
        ]
      }
    }
    AppText {
      visible: page.roots().length === 0
      text: "No folders yet. Add the folder where you keep your code."
      tone: "dim"
    }
    RowLayout {
      Layout.fillWidth: true
      Layout.topMargin: Style.spacing.sm
      spacing: Style.spacing.md
      TextField {
        id: rootField
        Layout.fillWidth: true
        placeholderText: "~/Developer"
        onAccepted: { page.addRoot(text); text = "" }
      }
      ActionButton { text: "Add"; glyph: Model.Glyph.plus; enabled: rootField.text.trim() !== ""; onClicked: { page.addRoot(rootField.text); rootField.text = "" } }
      ActionButton { text: "Browse…"; glyph: Model.Glyph.folderPlus; kind: "ghost"; onClicked: if (!picker.running) picker.running = true }
    }
  }

  // ---------------------------------------------------------------- general
  Card {
    Layout.fillWidth: true
    title: "General"
    glyph: Model.Glyph.gear
    contentSpacing: 0

    SettingRow {
      separator: false
      title: "Default iPhone"
      description: "Used for Run when more than one iPhone is connected."
      Dropdown {
        showLabel: false
        width: Style.space(240)
        value: page.app.settings.defaultDevice || ""
        options: {
          var opts = [{ value: "", label: "First connected" }]
          var devs = page.app.device && page.app.device.devices ? page.app.device.devices : []
          for (var i = 0; i < devs.length; i++) {
            var d = devs[i]
            opts.push({ value: d.udid || d.id || "", label: d.name || d.udid || "iPhone" })
          }
          var cur = page.app.settings.defaultDevice || ""
          if (cur !== "" && !opts.some(function(o) { return o.value === cur })) opts.push({ value: cur, label: "Saved device (not connected)" })
          return opts
        }
        onChanged: function(v) { page.app.setSetting("defaultDevice", v) }
      }
    }
    SettingRow {
      title: "Notifications"
      description: "Desktop notifications when a build finishes, your iPhone connects or signing is about to expire — only while you're in another window."
      ToggleSwitch {
        checked: page.app.settings.notifications !== false
        onToggled: page.app.setSetting("notifications", !checked)
      }
    }
    SettingRow {
      title: "Re-sign apps automatically"
      description: "Free Apple IDs sign apps for 7 days. When an installed app is about to expire and your iPhone is connected, iOS Studio rebuilds and reinstalls it."
      note: page.app.settings.autoRenew ? "Renews " + (page.app.settings.autoRenewDays || 2) + " days before expiry" : ""
      ToggleSwitch {
        checked: !!page.app.settings.autoRenew
        onToggled: page.app.setSetting("autoRenew", !checked)
      }
    }
  }

  // ---------------------------------------------------------------- code editor (+ Swift completion)
  EditorSection { id: editorSection; Layout.fillWidth: true; app: page.app }

  // ---------------------------------------------------------------- AI agents
  AgentsSection { id: agents; Layout.fillWidth: true; app: page.app }

  // ---------------------------------------------------------------- desktop
  Card {
    Layout.fillWidth: true
    title: "Desktop integration"
    subtitle: "Changes your Hyprland config safely: a backup is made first and the change is undone if Hyprland rejects it."
    glyph: Model.Glyph.window
    contentSpacing: 0
    actions: [ IconButton { glyph: Model.Glyph.refresh; tooltip: "Re-check"; onClicked: page.refresh() } ]

    ErrorState { Layout.fillWidth: true; visible: !!page.integrationError; banner: true; app: page.app; error: page.integrationError; onRetry: page.refresh() }

    SettingRow {
      separator: false
      title: "Open with Super + Shift + I"
      description: "Adds a keyboard shortcut to " + (page.integration ? page.integration.paths.bindings.replace(page.app.home, "~") : "~/.config/hypr/bindings.lua") + "."
      note: page.integration && page.integration.keybinding.conflict ? "Already used for “" + page.integration.keybinding.conflict + "”, so it can't be added." : ""
      noteLevel: "warn"
      ToggleSwitch {
        checked: page.integration ? page.integration.keybinding.enabled : false
        busy: page.keyBusy || !page.integration
        interactive: !!page.integration && !(page.integration.keybinding.conflict && !page.integration.keybinding.enabled)
        onToggled: page.setKeybinding(!checked)
      }
    }
    SettingRow {
      title: "Window"
      description: "Tile like other apps, or open as a centred floating window."
      Spinner { visible: page.windowBusy; running: page.windowBusy; size: Style.font.body; anchors.verticalCenter: parent.verticalCenter }
      ButtonGroupLite {
        enabled: !page.windowBusy && !!page.integration
        options: [{ value: "tile", label: "Tile" }, { value: "float", label: "Float" }]
        value: page.integration ? page.integration.windowRule.mode : "tile"
        onPicked: function(v) { if (page.integration && v !== page.integration.windowRule.mode) page.setWindowMode(v) }
      }
    }
    SettingRow {
      title: "Opaque window"
      description: "Keep iOS Studio solid so text stays easy to read. Turn off to use Omarchy's slightly see-through windows."
      ToggleSwitch {
        checked: page.integration ? page.integration.opaque === true : true
        busy: page.opaqueBusy || !page.integration
        interactive: !!page.integration && !page.opaqueBusy
        onToggled: page.setOpaque(!checked)
      }
    }
  }

  // ---------------------------------------------------------------- about
  Card {
    Layout.fillWidth: true
    title: "About iOS Studio"
    subtitle: "Version " + page.app.version + (page.about && page.about.commit ? " · " + page.about.commit : "")
    glyph: Model.Glyph.info

    KeyValueList {
      Layout.fillWidth: true
      rows: [
        { label: "Omarchy", value: page.about ? page.about.omarchy : "" },
        { label: "Quickshell", value: page.about ? String(page.about.quickshell).replace(/ \(revision.*$/, "") : "" },
        { label: "App folder", value: page.app.repoPath.replace(page.app.home, "~"), copy: true },
        { label: "Settings", value: page.app.configDir.replace(page.app.home, "~") + "/settings.json", copy: true },
        { label: "Data", value: page.app.stateDir.replace(page.app.home, "~"), copy: true }
      ]
      onCopied: function(v) { page.app.toast("Copied") }
    }
    Flow {
      Layout.fillWidth: true
      spacing: Style.spacing.md
      ActionButton { text: "Run setup again"; glyph: Model.Glyph.rocket; onClicked: page.app.nav("setup") }
      ActionButton { text: "Open settings folder"; glyph: Model.Glyph.folderOpen; kind: "ghost"; onClicked: page.app.openPath(page.app.configDir) }
      ActionButton { text: "Keyboard shortcuts"; glyph: Model.Glyph.keyboard; kind: "ghost"; onClicked: page.app.shortcutsOpen = true }
    }
  }

  // ---------------------------------------------------------------- danger zone
  BorderSurface {
    Layout.fillWidth: true
    Layout.topMargin: Style.spacing.xxl
    radius: Style.cornerRadius
    color: Util.alpha(Commons.Color.urgent, 0.04)
    borderSpec: Border.flat(Util.alpha(Commons.Color.urgent, 0.35), Style.normalBorderWidth)
    implicitHeight: uz.implicitHeight + Style.spacing.panelPadding * 2

    RowLayout {
      id: uz
      anchors.fill: parent
      anchors.margins: Style.spacing.panelPadding
      spacing: Style.spacing.huge
      ColumnLayout {
        Layout.fillWidth: true
        spacing: Style.spacing.xs
        AppText { text: "Uninstall iOS Studio"; size: "title"; strong: true }
        AppText {
          Layout.fillWidth: true
          text: "Removes the launcher entry, the ios-studio command and iOS Studio's Hyprland rules, then quits. Your projects, settings and the iOS toolchain stay."
          tone: "dim"; wrapMode: Text.WordWrap; lineHeight: 1.2
        }
        ActionButton {
          Layout.topMargin: Style.spacing.sm
          text: "Remove the toolchain first…"
          glyph: Model.Glyph.arrowRight
          kind: "ghost"
          onClicked: page.app.nav("toolchain", { action: "remove-all" })
        }
      }
      ActionButton {
        Layout.alignment: Qt.AlignTop
        text: "Uninstall"
        glyph: Model.Glyph.trash
        kind: "danger"
        busy: page.uninstalling
        enabled: !page.uninstalling
        onClicked: page.askUninstall()
      }
    }
  }
}
