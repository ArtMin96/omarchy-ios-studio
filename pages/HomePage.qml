import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import qs.components
import "../Model.js" as Model

// Home: one glance at readiness, the next best action, the iPhone, the Apple ID
// signing countdown, recent apps (one-click Run), builds and new crashes.
// On a fresh machine it centres on Setup; once everything is ready it centres
// on running your app.
PageFrame {
  id: page
  required property var app

  readonly property var setup: app.setup
  readonly property var acct: app.account
  readonly property var dev: app.device
  readonly property var proj: app.projects
  readonly property var logs: app.logs

  readonly property bool wide: width >= Style.space(820)

  // ---------------------------------------------------------------- readiness
  readonly property bool setupLoaded: setup.loaded === true
  // Essential steps still missing in the System/Toolchain/Apple-SDK part (the
  // part Setup installs by itself; Apple ID and iPhone have their own cards).
  readonly property var toolsMissing: {
    var out = []
    var m = setup.essentialMissing || []
    for (var i = 0; i < m.length; i++) if (m[i] !== "apple-id" && m[i] !== "iphone") out.push(m[i])
    return out
  }
  readonly property bool fresh: setupLoaded && toolsMissing.length > 0
  readonly property var sdkStep: setupLoaded ? setup.step("sdk") : null

  readonly property var checks: [
    { key: "tools", label: "Toolchain", ok: setupLoaded && toolsMissing.filter(function(id) { return id !== "sdk" }).length === 0,
      busy: !setupLoaded, go: function() { page.app.openSetupStep(page.toolsMissing[0] || "") } },
    { key: "sdk", label: "iOS SDK", ok: !!sdkStep && sdkStep.state === "done", busy: !setupLoaded,
      go: function() { page.app.openSetupStep("sdk") } },
    { key: "account", label: "Apple ID", ok: acct.signedIn, busy: !acct.auth && acct.status !== "needs-setup" && acct.status !== "error",
      go: function() { page.app.nav("account", acct.signedIn ? {} : { action: "signin" }) } },
    { key: "device", label: "iPhone", ok: dev.connected, busy: dev.status === "loading" && !dev.current,
      go: function() { page.app.nav("device", dev.hasDevice ? {} : { action: "pair" }) } }
  ]
  readonly property int readyCount: checks.filter(function(c) { return c.ok }).length

  // The first recent app that can actually be run from here.
  readonly property var runnable: {
    var r = proj.recent || []
    for (var i = 0; i < r.length; i++) if (!proj.blocked(r[i])) return r[i]
    return null
  }

  // ---------------------------------------------------------------- next best action
  // { level, glyph, title, text, cta, ctaGlyph, go }
  readonly property var next: {
    if (proj.building) {
      var b = proj.build
      return { level: "busy", glyph: Model.Glyph.hammer, title: "Building " + (b.name || "your app"),
               text: (b.detail || proj.stepTitle(b.current) || "Working…") + (b.fraction > 0 ? " · " + Math.round(b.fraction * 100) + "%" : ""),
               cta: "Show build", ctaGlyph: Model.Glyph.arrowRight, go: function() { page.app.nav("projects", { action: "build" }) } }
    }
    if (!setupLoaded) {
      if (setup.status === "error")
        return { level: "error", glyph: Model.Glyph.error, title: "Couldn't check your setup", text: Model.errorMessage(setup.error),
                 cta: "Try again", ctaGlyph: Model.Glyph.refresh, go: function() { page.setup.refresh() } }
      return { level: "busy", glyph: Model.Glyph.sync, title: "Checking your setup…", text: "Looking at the toolchain, your Apple ID and your iPhone.", cta: "" }
    }
    if (toolsMissing.length > 0) {
      var s = setup.step(toolsMissing[0])
      var all = setup.summary.total || 0
      return { level: "warn", glyph: Model.Glyph.rocket, title: readyCount === 0 && (setup.summary.done || 0) < 3 ? "Let's set up iOS development" : "Finish installing the tools",
               text: Model.plural(toolsMissing.length, "step") + " left" + (s ? ", next: " + s.title : "") + ". iOS Studio installs everything for you, no terminal needed.",
               cta: "Continue setup", ctaGlyph: Model.Glyph.arrowRight, go: function() { page.app.openSetupStep(page.toolsMissing[0]) } }
    }
    if (acct.status === "needs-setup")
      return { level: "warn", glyph: Model.Glyph.apple, title: "Install xtool to sign in", text: "Setup installs it, then you can sign in with your Apple ID.",
               cta: "Open Setup", ctaGlyph: Model.Glyph.arrowRight, go: function() { page.app.openSetupStep("xtool") } }
    if (acct.auth && !acct.signedIn)
      return { level: "warn", glyph: Model.Glyph.apple, title: "Sign in to your Apple ID", text: "Apple signs the apps you build with your Apple ID. A free account works.",
               cta: "Sign in", ctaGlyph: Model.Glyph.key, go: function() { page.app.nav("account", { action: "signin" }) } }
    var e = acct.signingExpiry
    if (acct.signedIn && e && (e.level === "warn" || e.level === "error"))
      return { level: e.level, glyph: Model.Glyph.timer,
               title: e.hours <= 0 ? "Your app signing has expired" : "Your app signing " + e.text,
               text: e.hours <= 0 ? "Apps you installed won't open until you re-sign them. It takes about a minute." : "Renew now so your apps keep opening. It takes about a minute.",
               cta: "Renew", ctaGlyph: Model.Glyph.sync, go: function() { page.app.nav("projects", { action: "renew" }) } }
    if (dev.status === "needs-setup")
      return { level: "warn", glyph: Model.Glyph.phone, title: "Install the iPhone tools", text: "They let iOS Studio talk to your iPhone over a cable or Wi-Fi.",
               cta: "Install", ctaGlyph: Model.Glyph.download, go: function() { page.app.openSetupStep("pymobiledevice3") } }
    if (!dev.hasDevice && dev.status !== "loading")
      return { level: "info", glyph: Model.Glyph.phone, title: "Connect your iPhone", text: "Plug it in with a cable, or pair it over Wi-Fi once and it connects by itself.",
               cta: "Connect iPhone", ctaGlyph: Model.Glyph.link, go: function() { page.app.nav("device", { action: "pair" }) } }
    if (dev.connState === "locked")
      return { level: "warn", glyph: Model.Glyph.lock, title: "Unlock your iPhone", text: "It's connected but locked. Unlock it to continue.", cta: "" }
    if (dev.connState === "untrusted" || dev.connState === "not-paired")
      return { level: "warn", glyph: Model.Glyph.phone, title: "Trust this computer on your iPhone", text: "Tap Trust on the phone and enter its passcode.",
               cta: "Pairing help", ctaGlyph: Model.Glyph.help, go: function() { page.app.nav("device", { action: "pair" }) } }
    if (dev.connected && dev.info && dev.info.developerMode === false)
      return { level: "warn", glyph: Model.Glyph.developer, title: "Turn on Developer Mode", text: "Your iPhone needs it to open apps you build. iOS Studio shows you where it is.",
               cta: "Show me how", ctaGlyph: Model.Glyph.arrowRight, go: function() { page.app.nav("device", { action: "devmode" }) } }
    if (proj.loadedOnce && (proj.items || []).length === 0)
      return { level: "ok", glyph: Model.Glyph.sparkles, title: "Create your first app", text: "Start from a template. It builds and runs on your iPhone in a few minutes.",
               cta: "New app", ctaGlyph: Model.Glyph.plus, go: function() { page.app.nav("projects", { action: "new" }) } }
    if (runnable)
      return { level: "ok", glyph: Model.Glyph.checkCircle, title: "All set: run " + runnable.name,
               text: dev.connected ? "Builds it, installs it on " + (dev.current && dev.current.name ? dev.current.name : "your iPhone") + " and opens it."
                                   : "Everything's ready. Connect your iPhone, then press Run.",
               cta: "Run", ctaGlyph: Model.Glyph.play, go: function() { page.runProject(page.runnable) } }
    return { level: "ok", glyph: Model.Glyph.checkCircle, title: "All set", text: "Everything iOS Studio needs is ready. Open Projects to build an app.",
             cta: "Open Projects", ctaGlyph: Model.Glyph.arrowRight, go: function() { page.app.nav("projects") } }
  }

  function runProject(p) {
    if (!p) return
    if (!proj.canRun) { page.app.toast("Connect your iPhone first", "warn"); page.app.nav("device"); return }
    proj.run(p)
    page.app.nav("projects", { action: "build" })
  }

  // ---------------------------------------------------------------- header
  title: Model.greeting()
  subtitle: !setupLoaded ? "Checking what's ready…"
          : fresh ? "Let's get your computer ready to build iPhone apps."
          : readyCount === checks.length ? "Everything's ready. Here's what's happening."
          : readyCount + " of " + checks.length + " things are ready."
  actions: [
    IconButton { glyph: Model.Glyph.refresh; tooltip: "Refresh everything (r)"; onClicked: page.app.refreshAll() }
  ]

  function refresh() { page.app.refreshAll() }

  // keyboard: j/k move over recent apps, Enter runs
  property int cursor: -1
  function handleKey(event) {
    var a = Model.keyAction(event)
    var n = recentRepeater.count
    if (n === 0) return false
    if (a === "down") { page.cursor = Math.min(n - 1, page.cursor + 1); return true }
    if (a === "up") { page.cursor = Math.max(0, page.cursor - 1); return true }
    if (a === "activate" && page.cursor >= 0) { var p = proj.recent[page.cursor]; if (p && !proj.blocked(p)) page.runProject(p); return true }
    return false
  }
  function goBack() { page.cursor = -1 }

  // ================================================================ hero
  BorderSurface {
    id: hero
    Layout.fillWidth: true
    readonly property string lv: page.next.level
    readonly property color tint: lv === "ok" ? Theme.success : (lv === "warn" ? Theme.warn : (lv === "error" ? Commons.Color.urgent : Commons.Color.accent))
    implicitHeight: heroCol.implicitHeight + Style.spacing.panelPadding * 2
    radius: Style.cornerRadius
    color: Util.alpha(tint, Theme.isDark ? 0.07 : 0.09)
    borderSpec: Border.flat(Util.alpha(tint, 0.32), Style.normalBorderWidth)

    ColumnLayout {
      id: heroCol
      anchors.fill: parent
      anchors.margins: Style.spacing.panelPadding
      anchors.leftMargin: Style.spacing.panelPadding + Style.spacing.md
      anchors.rightMargin: Style.spacing.panelPadding + Style.spacing.md
      spacing: Style.spacing.xxl

      RowLayout {
        Layout.fillWidth: true
        spacing: Style.spacing.xxl

        Rectangle {
          id: heroDisc
          Layout.alignment: Qt.AlignTop
          implicitWidth: Style.font.display * 2.1
          implicitHeight: implicitWidth
          radius: Style.cornerRadius > 0 ? width / 2 : 0
          color: Util.alpha(hero.tint, 0.14)
          border.width: Style.normalBorderWidth
          border.color: Util.alpha(hero.tint, 0.45)
          Icon { anchors.centerIn: parent; visible: page.next.level !== "busy"; glyph: page.next.glyph; color: hero.tint; size: Style.font.heading }
          Spinner { anchors.centerIn: parent; running: page.next.level === "busy"; size: parent.width * 0.5; color: hero.tint }
        }

        ColumnLayout {
          Layout.fillWidth: true
          Layout.alignment: Qt.AlignVCenter
          spacing: Style.spacing.sm
          AppText { Layout.fillWidth: true; text: page.next.title; size: "heading"; strong: true; wrapMode: Text.WordWrap }
          AppText { Layout.fillWidth: true; text: page.next.text; tone: "dim"; wrapMode: Text.WordWrap; lineHeight: 1.2 }
          ActionButton {
            Layout.topMargin: Style.spacing.md
            visible: !page.wide && !!page.next.cta
            text: page.next.cta || ""
            glyph: page.next.ctaGlyph || ""
            kind: "primary"
            enabled: page.next.cta !== "Run" || !proj.building
            onClicked: if (page.next.go) page.next.go()
          }
        }

        ActionButton {
          Layout.alignment: Qt.AlignVCenter
          visible: page.wide && !!page.next.cta
          text: page.next.cta || ""
          glyph: page.next.ctaGlyph || ""
          kind: "primary"
          enabled: page.next.cta !== "Run" || !proj.building
          onClicked: if (page.next.go) page.next.go()
        }
      }

      // readiness checklist
      Rectangle { Layout.fillWidth: true; implicitHeight: Style.spacing.hairline; color: Util.alpha(hero.tint, 0.2) }
      Flow {
        Layout.fillWidth: true
        spacing: Style.spacing.xxl
        Repeater {
          model: page.checks
          delegate: Item {
            id: chk
            required property var modelData
            implicitWidth: chkRow.implicitWidth
            implicitHeight: chkRow.implicitHeight
            RowLayout {
              id: chkRow
              spacing: Style.spacing.sm
              Spinner { visible: chk.modelData.busy; running: visible; size: Style.font.bodySmall }
              Icon {
                visible: !chk.modelData.busy
                glyph: chk.modelData.ok ? Model.Glyph.checkCircle : Model.Glyph.circle
                size: Style.font.bodySmall
                color: chk.modelData.ok ? Theme.success : Theme.faint
              }
              AppText {
                text: chk.modelData.label
                size: "small"
                tone: chkHover.hovered ? "primary" : (chk.modelData.ok ? "dim" : "primary")
                font.underline: chkHover.hovered && !chk.modelData.ok
              }
            }
            HoverHandler { id: chkHover; cursorShape: Qt.PointingHandCursor }
            TapHandler { onTapped: chk.modelData.go() }
          }
        }
      }
    }
  }

  // ================================================================ quick actions
  Flow {
    Layout.fillWidth: true
    Layout.topMargin: -Style.spacing.md
    spacing: Style.spacing.md
    ActionButton { text: "New app"; glyph: Model.Glyph.plus; disabled: page.fresh; tooltip: page.fresh ? "Finish Setup first" : "Create an app from a template"; onClicked: page.app.nav("projects", { action: "new" }) }
    ActionButton { text: "Open Setup"; glyph: Model.Glyph.rocket; onClicked: page.app.nav("setup") }
    ActionButton { text: "Mirror screen"; glyph: Model.Glyph.camera; disabled: !page.dev.connected; tooltip: page.dev.connected ? "See your iPhone's screen" : "Connect your iPhone first"; onClicked: page.app.nav("device", { tab: "screen", action: "mirror" }) }
    ActionButton { text: "Connect AI agents"; glyph: Model.Glyph.robot; kind: "ghost"; tooltip: "Let tools like Claude Code build and run your apps"; onClicked: page.app.nav("settings", { section: "agents" }) }
  }

  // ================================================================ iPhone + Apple ID
  GridLayout {
    Layout.fillWidth: true
    columns: page.wide ? 2 : 1
    columnSpacing: Style.spacing.xxl
    rowSpacing: Style.spacing.xxl

    // ------------------------------------------------ iPhone
    Card {
      id: phoneCard
      Layout.fillWidth: true
      Layout.fillHeight: true
      Layout.preferredWidth: 1
      readonly property var c: page.dev.current
      readonly property var info: page.dev.info || ({})
      readonly property string st: page.dev.connState
      title: "iPhone"
      glyph: Model.Glyph.phone
      glyphColor: page.dev.connected ? Commons.Color.accent : Theme.dim
      actions: [ IconButton { glyph: Model.Glyph.chevronRight; tooltip: "Open iPhone (4)"; onClicked: page.app.nav("device") } ]

      Skeleton { Layout.fillWidth: true; visible: page.dev.status === "loading" && !phoneCard.c; lines: 2 }
      EmptyState {
        Layout.fillWidth: true
        visible: page.dev.status === "needs-setup"
        compact: true
        glyph: Model.Glyph.toolbox
        title: "iPhone tools aren't installed"
        text: "Setup installs them so iOS Studio can talk to your iPhone."
        actionText: "Install"; actionGlyph: Model.Glyph.download; actionKind: "secondary"
        onAction: page.app.openSetupStep("pymobiledevice3")
      }
      ErrorState {
        Layout.fillWidth: true
        visible: page.dev.status === "error" && !phoneCard.c
        compact: true
        app: page.app
        error: page.dev.error
        onRetry: page.dev.refresh()
      }
      EmptyState {
        Layout.fillWidth: true
        visible: !phoneCard.c && page.dev.status !== "loading" && page.dev.status !== "needs-setup" && page.dev.status !== "error"
        compact: true
        glyph: Model.Glyph.phone
        title: "No iPhone connected"
        text: "Plug it in with a cable, or pair it over Wi-Fi."
        actionText: "Connect iPhone"; actionGlyph: Model.Glyph.link; actionKind: "secondary"
        onAction: page.app.nav("device", { action: "pair" })
      }

      // connected / known device
      ColumnLayout {
        Layout.fillWidth: true
        visible: !!phoneCard.c
        spacing: Style.spacing.sm
        AppText { Layout.fillWidth: true; text: phoneCard.c ? (phoneCard.c.name || "iPhone") : ""; size: "heading"; strong: true; elide: Text.ElideRight }
        AppText {
          Layout.fillWidth: true
          text: {
            var c = phoneCard.c || {}
            var parts = []
            if (c.model) parts.push(c.model)
            if (c.ios) parts.push("iOS " + c.ios)
            return parts.join(" · ")
          }
          visible: text !== ""
          tone: "dim"; elide: Text.ElideRight
        }
      }
      Flow {
        Layout.fillWidth: true
        visible: !!phoneCard.c
        spacing: Style.spacing.md
        StatusPill {
          level: phoneCard.st === "connected" ? "ok" : (phoneCard.st === "unreachable" ? "info" : (phoneCard.st === "locked" || phoneCard.st === "untrusted" || phoneCard.st === "not-paired" ? "warn" : "busy"))
          glyph: phoneCard.st === "connected" ? (phoneCard.c && phoneCard.c.transport === "usb" ? Model.Glyph.usb : Model.Glyph.wifi) : ""
          text: {
            switch (phoneCard.st) {
            case "connected": return phoneCard.c && phoneCard.c.transport === "usb" ? "USB" : "Wi-Fi"
            case "locked": return "Locked"
            case "untrusted": return "Not trusted"
            case "not-paired": return "Not paired"
            case "unreachable": return "Not nearby"
            default: return "Connecting"
            }
          }
        }
        StatusPill {
          visible: page.dev.connected && !!phoneCard.info.battery && phoneCard.info.battery.level !== undefined && phoneCard.info.battery.level !== null
          level: phoneCard.info.battery && phoneCard.info.battery.level < 20 && !phoneCard.info.battery.charging ? "warn" : "info"
          glyph: phoneCard.info.battery && phoneCard.info.battery.charging ? Model.Glyph.batteryCharging : Model.Glyph.battery
          text: phoneCard.info.battery ? page.dev.batteryText(phoneCard.info.battery) : ""
        }
        StatusPill {
          visible: page.dev.connected && phoneCard.info.developerMode !== undefined && phoneCard.info.developerMode !== null
          level: phoneCard.info.developerMode ? "ok" : "warn"
          glyph: Model.Glyph.developer
          text: phoneCard.info.developerMode ? "Developer Mode on" : "Developer Mode off"
        }
      }
      ActionButton {
        visible: !!phoneCard.c && page.dev.connected && phoneCard.info.developerMode === false
        text: "Turn on Developer Mode"; glyph: Model.Glyph.arrowRight; compact: true
        onClicked: page.app.nav("device", { action: "devmode" })
      }
      ActionButton {
        visible: !!phoneCard.c && !page.dev.connected && (phoneCard.st === "unreachable" || phoneCard.st === "idle")
        text: "Reconnect"; glyph: Model.Glyph.sync; compact: true; kind: "ghost"
        onClicked: page.dev.reconnect()
      }
      Item { Layout.fillHeight: true }
    }

    // ------------------------------------------------ Apple ID
    Card {
      id: idCard
      Layout.fillWidth: true
      Layout.fillHeight: true
      Layout.preferredWidth: 1
      readonly property var e: page.acct.signingExpiry
      readonly property color tint: e ? Theme.levelColor(e.level) : Theme.dim
      title: "Apple ID"
      glyph: Model.Glyph.apple
      glyphColor: page.acct.signedIn ? Commons.Color.accent : Theme.dim
      actions: [ IconButton { glyph: Model.Glyph.chevronRight; tooltip: "Open Apple ID (3)"; onClicked: page.app.nav("account") } ]

      Skeleton { Layout.fillWidth: true; visible: !page.acct.auth && (page.acct.status === "loading" || page.acct.status === "unknown"); lines: 2 }
      EmptyState {
        Layout.fillWidth: true
        visible: page.acct.status === "needs-setup"
        compact: true
        glyph: Model.Glyph.toolbox
        title: "Install xtool first"
        text: "xtool signs your apps. Setup installs it, then you can sign in."
        actionText: "Open Setup"; actionGlyph: Model.Glyph.rocket; actionKind: "secondary"
        onAction: page.app.openSetupStep("xtool")
      }
      ErrorState {
        Layout.fillWidth: true
        visible: page.acct.status === "error" && !page.acct.auth
        compact: true
        app: page.app
        error: page.acct.error
        onRetry: page.acct.refresh()
      }
      EmptyState {
        Layout.fillWidth: true
        visible: !!page.acct.auth && !page.acct.signedIn && page.acct.status !== "needs-setup"
        compact: true
        glyph: Model.Glyph.apple
        title: "Not signed in"
        text: "Sign in with your Apple ID to put apps on your iPhone. A free account works."
        actionText: "Sign in"; actionGlyph: Model.Glyph.key; actionKind: "secondary"
        onAction: page.app.nav("account", { action: "signin" })
      }

      // signed in
      ColumnLayout {
        Layout.fillWidth: true
        visible: page.acct.signedIn
        spacing: Style.spacing.xs
        AppText {
          Layout.fillWidth: true
          text: page.acct.team && page.acct.team.name ? page.acct.team.name : (page.acct.auth && page.acct.auth.appleId ? page.acct.auth.appleId : "Signed in")
          size: "heading"; strong: true; elide: Text.ElideRight
        }
        AppText {
          Layout.fillWidth: true
          text: (page.acct.auth && page.acct.auth.appleId ? page.acct.auth.appleId : "API key") + (page.acct.details ? (page.acct.free ? " · Free account" : " · Developer Program") : "")
          tone: "dim"; elide: Text.ElideRight
        }
      }
      // signing countdown
      ColumnLayout {
        Layout.fillWidth: true
        visible: page.acct.signedIn && !!idCard.e
        spacing: Style.spacing.sm
        RowLayout {
          Layout.fillWidth: true
          spacing: Style.spacing.md
          AppText {
            text: idCard.e ? (idCard.e.hours <= 0 ? "Expired" : (idCard.e.hours < 48 ? Math.max(1, Math.round(idCard.e.hours)) + " h" : idCard.e.days + " days")) : ""
            size: "title"; strong: true; color: idCard.tint; tone: "inherit"
          }
          AppText {
            Layout.fillWidth: true
            text: idCard.e && idCard.e.hours > 0 ? "until your apps need re-signing" : "apps won't open until re-signed"
            tone: "dim"; size: "small"; elide: Text.ElideRight
          }
          ActionButton {
            text: "Renew"; glyph: Model.Glyph.sync; compact: true
            kind: idCard.e && idCard.e.level !== "ok" ? "primary" : "ghost"
            enabled: !page.proj.building
            tooltip: "Re-sign and reinstall your app for another week"
            onClicked: page.app.nav("projects", { action: "renew" })
          }
        }
        ProgressTrack {
          Layout.fillWidth: true
          value: idCard.e ? Model.clamp(idCard.e.hours / (page.acct.free ? 168 : 8760), 0, 1) : 0
          level: idCard.e ? (idCard.e.level === "ok" ? "ok" : idCard.e.level) : ""
        }
      }
      AppText {
        Layout.fillWidth: true
        visible: page.acct.signedIn && !idCard.e
        text: page.acct.detailsLoading ? "Checking your signing…" : "No apps signed yet. Signing starts the first time you run an app."
        tone: "dim"; size: "small"; wrapMode: Text.WordWrap
      }
      Item { Layout.fillHeight: true }
    }
  }

  // ================================================================ recent apps
  ColumnLayout {
    Layout.fillWidth: true
    visible: !page.fresh
    spacing: Style.spacing.md

    SectionHeader {
      Layout.fillWidth: true
      text: "Your apps"
      detail: page.proj.items && page.proj.items.length ? String(page.proj.items.length) : ""
      trailing: [ ActionButton { text: "All projects"; kind: "ghost"; compact: true; glyph: Model.Glyph.arrowRight; onClicked: page.app.nav("projects") } ]
    }
    Skeleton { Layout.fillWidth: true; visible: !page.proj.loadedOnce && page.proj.status !== "error" && page.proj.status !== "needs-setup"; lines: 2; rowHeight: Style.space(44) }
    ErrorState { Layout.fillWidth: true; visible: page.proj.status === "error"; banner: true; app: page.app; error: page.proj.error; onRetry: page.proj.refresh() }
    EmptyState {
      Layout.fillWidth: true
      visible: page.proj.loadedOnce && page.proj.status !== "error" && (page.proj.items || []).length === 0
      compact: true
      glyph: Model.Glyph.sparkles
      title: "No apps yet"
      text: "Create one from a template, or add a folder with an xtool project."
      actionText: "New app"; actionGlyph: Model.Glyph.plus; actionKind: "secondary"
      onAction: page.app.nav("projects", { action: "new" })
      secondaryText: "Add folder"; secondaryGlyph: Model.Glyph.folderPlus
      onSecondary: page.app.nav("projects", { action: "add" })
    }
    Repeater {
      id: recentRepeater
      model: page.proj.loadedOnce ? page.proj.recent : []
      delegate: ListRow {
        id: prow
        required property var modelData
        required property int index
        readonly property var sg: modelData.signing || null
        readonly property bool mine: page.proj.building && page.proj.build.path === modelData.path
        Layout.fillWidth: true
        leading: [
          Rectangle {
            width: Style.space(32); height: width
            radius: Style.cornerRadius > 0 ? Style.cornerRadius + Style.space(2) : 0
            color: Util.alpha(Commons.Color.accent, 0.14)
            border.width: Style.normalBorderWidth
            border.color: Util.alpha(Commons.Color.accent, 0.28)
            clip: true
            Image {
              id: rowIcon
              anchors.fill: parent
              visible: status === Image.Ready
              source: prow.modelData.icon ? "file://" + prow.modelData.icon : ""
              sourceSize.width: Style.space(64); sourceSize.height: Style.space(64)
              fillMode: Image.PreserveAspectCrop
              asynchronous: true
            }
            AppText {
              anchors.centerIn: parent
              visible: !rowIcon.visible
              text: String(prow.modelData.name || "?").replace(/[^A-Za-z0-9]/g, "").substring(0, 2).toUpperCase() || "?"
              size: "small"; strong: true; tone: "accent"
            }
          }
        ]
        title: modelData.name || "App"
        subtitle: modelData.bundleId || modelData.displayPath || ""
        meta: {
          if (prow.mine) return "Building…"
          if (prow.sg && prow.sg.daysLeft !== null && prow.sg.daysLeft !== undefined)
            return prow.sg.daysLeft <= 0 ? "Signing expired" : (prow.sg.daysLeft < 2 ? "Expires soon" : Math.floor(prow.sg.daysLeft) + " days left")
          var lb = modelData.lastBuild
          return lb ? (lb.result === "ok" ? "Built " : "Failed ") + Model.relativeTime(lb.time) : ""
        }
        badgeLevel: prow.sg && prow.sg.daysLeft !== null && prow.sg.daysLeft !== undefined && prow.sg.daysLeft < 2 ? (prow.sg.daysLeft <= 0 ? "error" : "warn")
                  : (modelData.lastBuild && modelData.lastBuild.result === "failed" ? "error" : "")
        hasCursor: page.cursor === index
        onHovered: function(h) { if (h) page.cursor = index }
        onClicked: page.app.nav("projects")
        trailing: [
          ActionButton {
            text: "Run"; glyph: Model.Glyph.play; compact: true
            kind: index === 0 ? "primary" : "secondary"
            busy: prow.mine
            disabled: page.proj.blocked(prow.modelData) || page.proj.building
            tooltip: page.proj.blocked(prow.modelData) ? "Built by its own scripts; allow it in its Build settings (Projects)" : (page.proj.canRun ? "Build, install and open on your iPhone" : "Connect your iPhone first")
            onClicked: page.runProject(prow.modelData)
          }
        ]
      }
    }
  }

  // ================================================================ builds + crashes
  GridLayout {
    Layout.fillWidth: true
    visible: !page.fresh
    columns: page.wide ? 2 : 1
    columnSpacing: Style.spacing.xxl
    rowSpacing: Style.spacing.huge

    // ------------------------------------------------ recent builds
    ColumnLayout {
      id: buildsCol
      Layout.fillWidth: true
      Layout.preferredWidth: 1
      Layout.alignment: Qt.AlignTop
      spacing: Style.spacing.md
      readonly property var builds: (page.proj.history || []).slice(0, 4)

      SectionHeader {
        Layout.fillWidth: true
        text: "Recent builds"
        trailing: [ IconButton { glyph: Model.Glyph.logs; tooltip: "Build logs"; onClicked: page.app.nav("logs", { tab: "builds" }) } ]
      }
      QuietNote {
        Layout.fillWidth: true
        visible: buildsCol.builds.length === 0
        glyph: Model.Glyph.hammer
        title: "No builds yet"
        text: "Builds you run show up here, with their logs."
      }
      Repeater {
        model: buildsCol.builds
        delegate: ListRow {
          required property var modelData
          Layout.fillWidth: true
          glyph: modelData.result === "ok" ? Model.Glyph.checkCircle : (modelData.result === "cancelled" ? Model.Glyph.stop : Model.Glyph.error)
          glyphColor: modelData.result === "ok" ? Theme.success : (modelData.result === "cancelled" ? Theme.dim : Commons.Color.urgent)
          title: (modelData.name || "App") + " · " + (page.logs.buildKindLabel ? page.logs.buildKindLabel(modelData.kind, modelData) : modelData.kind)
          subtitle: Model.relativeTime(modelData.time) + (modelData.duration ? " · " + Model.formatDuration(modelData.duration) : "")
                    + (modelData.errors > 0 ? " · " + Model.plural(modelData.errors, "error") : "")
          onClicked: page.app.nav("logs", { tab: "builds" })
        }
      }
    }

    // ------------------------------------------------ crashes
    ColumnLayout {
      id: crashCol
      Layout.fillWidth: true
      Layout.preferredWidth: 1
      Layout.alignment: Qt.AlignTop
      spacing: Style.spacing.md
      // one row per app (newest report), with how many reports it has
      readonly property var own: {
        var seen = {}, out = []
        var list = (page.logs.crashes || []).filter(function(c) { return page.logs.isOwn(c) })
        for (var i = 0; i < list.length; i++) {
          var k = list[i].process || list[i].name
          if (seen[k]) { seen[k].count++; if (page.logs.isNew(list[i])) seen[k].newCount++; continue }
          seen[k] = { item: list[i], count: 1, newCount: page.logs.isNew(list[i]) ? 1 : 0 }
          out.push(seen[k])
        }
        return out.slice(0, 3)
      }
      readonly property int fresh: page.logs.newCrashCount || 0

      SectionHeader {
        Layout.fillWidth: true
        text: "Crashes"
        detail: crashCol.fresh > 0 ? crashCol.fresh + " new" : ""
        trailing: [ IconButton { glyph: Model.Glyph.crash; tooltip: "All crash reports"; onClicked: page.app.nav("logs", { tab: "crashes" }) } ]
      }
      Skeleton { Layout.fillWidth: true; visible: page.logs.crashStatus === "loading" && crashCol.own.length === 0; lines: 2 }
      ErrorState {
        Layout.fillWidth: true
        visible: page.logs.crashStatus === "error" && crashCol.own.length === 0
        banner: true; app: page.app; error: page.logs.crashError
        onRetry: page.logs.refreshCrashes()
      }
      QuietNote {
        Layout.fillWidth: true
        visible: page.logs.crashStatus !== "loading" && page.logs.crashStatus !== "error" && crashCol.own.length === 0
        glyph: page.dev.connected ? Model.Glyph.checkCircle : Model.Glyph.crash
        level: page.dev.connected && page.logs.crashStatus === "ready" ? "ok" : ""
        title: page.dev.connected && page.logs.crashStatus === "ready" ? "No crashes from your apps" : "No crash reports yet"
        text: page.dev.connected ? "If one of your apps crashes on the iPhone, it shows up here with a plain-language summary."
                                 : "Crash reports are read from your iPhone when it's connected."
      }
      Repeater {
        model: crashCol.own
        delegate: ListRow {
          required property var modelData
          readonly property var c: modelData.item
          readonly property bool isNew: modelData.newCount > 0
          Layout.fillWidth: true
          glyph: Model.Glyph.crash
          glyphColor: isNew ? Theme.warn : Theme.dim
          title: c.process || c.name || "Crash"
          subtitle: page.logs.exceptionText(c) + " · " + Model.relativeTime(c.ts)
          badgeLevel: isNew ? "warn" : ""
          meta: modelData.newCount > 0 ? modelData.newCount + " new" : (modelData.count > 1 ? Model.plural(modelData.count, "report") : "")
          onClicked: page.app.nav("logs", { tab: "crashes", crash: c.name })
        }
      }
    }
  }

  // ================================================================ fresh machine: Setup at a glance
  ColumnLayout {
    Layout.fillWidth: true
    visible: page.fresh
    spacing: Style.spacing.md
    SectionHeader { Layout.fillWidth: true; text: "What Setup does for you" }
    Repeater {
      model: page.setup.groups || []
      delegate: ListRow {
        id: grow
        required property var modelData
        readonly property var items: (page.setup.steps || []).filter(function(s) { return s.group === modelData && s.id !== "platform" && !s.optional && s.state !== "optional" })
        readonly property int doneN: items.filter(function(s) { return s.state === "done" }).length
        readonly property var firstOpen: { for (var i = 0; i < items.length; i++) if (items[i].state !== "done") return items[i]; return null }
        Layout.fillWidth: true
        visible: items.length > 0
        glyph: modelData === "System" ? Model.Glyph.gear : modelData === "Toolchain" ? Model.Glyph.toolbox : modelData === "Apple" ? Model.Glyph.apple : Model.Glyph.phone
        glyphColor: doneN === items.length ? Theme.success : Theme.dim
        title: modelData === "System" ? "System packages" : modelData === "Toolchain" ? "Swift, xtool and iPhone tools" : modelData === "Apple" ? "iOS SDK and your Apple ID" : "Your iPhone"
        subtitle: doneN === items.length ? "Done" : (firstOpen ? "Next: " + firstOpen.title : "")
        meta: doneN + " of " + items.length
        badgeLevel: doneN === items.length ? "ok" : ""
        onClicked: page.app.openSetupStep(firstOpen ? firstOpen.id : "")
      }
    }
  }
}
