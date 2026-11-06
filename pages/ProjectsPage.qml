import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import qs.components
import "../Model.js" as Model

// Projects (projects slice): your xtool apps, Run on iPhone, .ipa builds, 7-day renewal,
// new-app templates, project settings checks (on each card), watch mode (compile on save, app.watch:
// in each card's Run menu, `w` for the highlighted one) and the build drawer.
Item {
  id: page
  required property var app
  readonly property var svc: app.projects

  property int cursor: 0
  property bool panelOpen: false
  property string query: ""

  readonly property real panelWidth: Math.max(Style.space(440), Math.min(Style.space(620), width * 0.48))
  readonly property bool pushLayout: width - panelWidth >= Style.space(620)

  readonly property var shown: svc.items.filter(function(p) {
    return Model.matches((p.name || "") + " " + (p.bundleId || "") + " " + (p.displayPath || ""), page.query)
  })
  readonly property var expiring: svc.items.filter(function(p) {
    return p.signing && p.signing.daysLeft !== null && p.signing.daysLeft !== undefined && p.signing.daysLeft < 2
  }).sort(function(a, b) { return a.signing.daysLeft - b.signing.daysLeft })

  // ---------------------------------------------------------------- page contract
  function activated(params) {
    var p = params || {}
    if (svc.status === "unknown") svc.refresh()
    else svc.checkAll()            // project settings checks (skipped for ones checked in the last minute)
    if (p.action === "new") Qt.callLater(function() { newSheet.open() })
    else if (p.action === "renew") {
      var target = p.path ? svc.projectByPath(p.path) : (page.expiring[0] || null)
      if (target) { svc.renew(target); page.panelOpen = true }
      else app.toast("Nothing needs re-signing right now.", "info")
    }
    else if (p.action === "run" && p.path) { var t = svc.projectByPath(p.path); if (t) { svc.run(t); page.panelOpen = true } }
    else if (p.action === "build" || p.action === "log" || p.action === "open-diagnostic") page.panelOpen = true
    else if (p.action === "add") page.addExisting()
    else if (p.action === "retry") { if (svc.build.path) { svc.runAgain(); page.panelOpen = true } }
    else if (p.action === "reveal" && p.path) { var r = svc.projectByPath(p.path); svc.reveal(r || { path: p.path }) }
    else if (p.action === "convert" && p.path) svc.convert(p.path)
    else if (p.action === "build-settings") {
      var bp = p.path ? svc.projectByPath(p.path) : (svc.build.path ? svc.projectByPath(svc.build.path) : null)
      if (bp) Qt.callLater(function() { buildSettingsSheet.openFor(bp) })
    }
    else if (p.action === "remove" && p.path) {
      var gone = svc.projectByPath(p.path) || { path: p.path, name: p.path.split("/").pop(), displayPath: p.path.replace(app.home, "~") }
      svc.remove(gone)
    }
  }

  function refresh() { svc.refresh() }
  function lookIn() {
    var home = page.app.home
    var r = (svc.roots || []).map(function(x) { return String(x).indexOf(home) === 0 ? "~" + String(x).substring(home.length) : String(x) })
    var dir = svc.defaultDir ? (svc.defaultDir.indexOf(home) === 0 ? "~" + svc.defaultDir.substring(home.length) : svc.defaultDir) : "~/Developer"
    return r.length ? r.join(", ") : dir + " (new apps go there)"
  }
  function focusSearch() { search.focusField() }

  function goBack() { if (page.panelOpen) page.panelOpen = false }

  function handleKey(event) {
    var a = Model.keyAction(event)
    var n = page.shown.length
    if (a === "down" && n) { page.cursor = Math.min(n - 1, page.cursor + 1); frame.scrollToItem(cards.itemAt(page.cursor)); return true }
    if (a === "up" && n) { page.cursor = Math.max(0, page.cursor - 1); frame.scrollToItem(cards.itemAt(page.cursor)); return true }
    if (a === "home" && n) { page.cursor = 0; frame.scrollToItem(cards.itemAt(0)); return true }
    if (a === "end" && n) { page.cursor = n - 1; frame.scrollToItem(cards.itemAt(n - 1)); return true }
    var cur = n ? page.shown[Math.min(page.cursor, n - 1)] : null
    if (a === "activate" && cur) { page.runProject(cur); return true }
    if (event.text === "n") { newSheet.open(); return true }
    if (event.text === "B" && cur) { page.startKind(cur, "compile"); return true }
    if (event.text === "b") { page.panelOpen = !page.panelOpen; return true }
    if (event.text === "e" && cur) { svc.openInEditor(cur); return true }
    if (event.text === "o" && cur) { svc.reveal(cur); return true }
    if (event.text === "m" && cur) { var it = cards.itemAt(page.cursor); if (it) it.expanded = !it.expanded; return true }
    if (event.text === "a") { page.addExisting(); return true }
    if (event.text === "w" && cur) { page.toggleWatch(cur); return true }
    return false
  }

  // ---------------------------------------------------------------- actions
  function runProject(p) { svc.run(p); if (svc.building) page.panelOpen = true }
  function startKind(p, kind) {
    if (kind === "ipa") svc.buildIpa(p)
    else if (kind === "compile") svc.compile(p)
    else if (kind === "renew") svc.renew(p)
    if (svc.building) page.panelOpen = true
  }

  // Watch mode: the card's state line opens the last compile-on-save build's problems.
  function toggleWatch(p) {
    if (!app.watch.isWatching(p) && svc.blocked(p)) { app.toast("Allow its generated xtool.yml in Build settings first.", "warn"); return }
    app.watch.toggle(p)
  }
  function showWatchProblems(p) {
    var w = app.watch.forProject(p)
    if (w && w.last && !svc.building && !(svc.build.watch && svc.build.path === p.path)) app.watch.showLast()
    panel.tab = "problems"
    page.panelOpen = true
  }

  function addExisting() {
    svc.pickFolder("", function(d) {
      if (!d.path) return
      if (d.isProject) { svc.addFolder(d.path); return }
      if (d.isPackage) {
        app.confirm({
          title: "Make this package an iPhone app?",
          message: "This Swift package has no xtool.yml yet. iOS Studio can add one (with a bundle ID), so it builds as an app. Your code isn't changed.",
          confirmText: "Add xtool.yml",
          glyph: Model.Glyph.package,
          onConfirm: function() { svc.convert(d.path) }
        })
        return
      }
      app.toast("That folder doesn't contain an iPhone app (Package.swift and xtool.yml).", "warn")
    })
  }

  Connections {
    target: page.svc
    function onBuildStarted() { page.panelOpen = true }
  }

  // ---------------------------------------------------------------- content
  PageFrame {
    id: frame
    anchors.top: parent.top
    anchors.bottom: parent.bottom
    anchors.left: parent.left
    width: page.panelOpen && page.pushLayout ? page.width - page.panelWidth : page.width
    Behavior on width { NumberAnimation { duration: 220; easing.type: Easing.OutCubic } }

    title: "Projects"
    subtitle: "Build your apps and run them on your iPhone."
    status: StatusPill {
      visible: page.svc.building
      level: "busy"
      text: page.svc.building ? "Building " + page.svc.build.name : ""
    }
    actions: [
      IconButton { glyph: Model.Glyph.folderPlus; tooltip: "Add an existing project folder (a)"; onClicked: page.addExisting() },
      ActionButton { text: "New app"; glyph: Model.Glyph.plus; tooltip: "Create an app from a template (n)"; onClicked: newSheet.open() },
      IconButton {
        glyph: Model.Glyph.console
        tooltip: page.panelOpen ? "Hide build panel (b)" : "Show build panel (b)"
        visible: !!page.svc.build.kind
        onClicked: page.panelOpen = !page.panelOpen
      }
    ]

    // ---- loading / error / setup
    Skeleton {
      Layout.fillWidth: true
      visible: page.svc.status === "loading" && !page.svc.loadedOnce
      lines: 3
      rowHeight: Style.space(92)
      gap: Style.spacing.lg
    }

    ErrorState {
      Layout.fillWidth: true
      visible: page.svc.status === "error"
      app: page.app
      title: "Couldn't look for your projects"
      error: page.svc.error
      onRetry: page.svc.refresh()
    }

    ErrorState {
      Layout.fillWidth: true
      visible: page.svc.status === "needs-setup"
      app: page.app
      banner: true
      showRetry: false
      error: ({ code: "missing-xtool", message: "xtool isn't installed yet, so apps can't be built. Setup installs it for you.", fix: { label: "Install xtool", page: "setup", step: "xtool" } })
    }

    // ---- hints: no iPhone, signing about to expire
    BorderSurface {
      Layout.fillWidth: true
      visible: page.svc.loadedOnce && page.svc.items.length > 0 && !page.svc.canRun
      radius: Style.cornerRadius
      color: Theme.levelFill("info")
      borderSpec: Border.flat(Util.alpha(Commons.Color.accent, 0.35), Style.normalBorderWidth)
      padding: Style.spacing.xl
      implicitHeight: hintRow.implicitHeight + contentTopInset + contentBottomInset
      RowLayout {
        id: hintRow
        anchors.fill: parent
        anchors.topMargin: parent.contentTopInset; anchors.bottomMargin: parent.contentBottomInset
        anchors.leftMargin: parent.contentLeftInset; anchors.rightMargin: parent.contentRightInset
        spacing: Style.spacing.xl
        Icon { glyph: Model.Glyph.phone; color: Commons.Color.accent; size: Style.font.iconLarge }
        AppText { Layout.fillWidth: true; wrapMode: Text.WordWrap; text: "Connect your iPhone to run apps on it. You can still build .ipa files without it." }
        ActionButton { text: "Open iPhone"; compact: true; onClicked: page.app.nav("device") }
      }
    }

    BorderSurface {
      Layout.fillWidth: true
      visible: page.expiring.length > 0
      radius: Style.cornerRadius
      color: Theme.levelFill("warn")
      borderSpec: Border.flat(Util.alpha(Theme.warn, 0.45), Style.normalBorderWidth)
      padding: Style.spacing.xl
      implicitHeight: expRow.implicitHeight + contentTopInset + contentBottomInset
      RowLayout {
        id: expRow
        anchors.fill: parent
        anchors.topMargin: parent.contentTopInset; anchors.bottomMargin: parent.contentBottomInset
        anchors.leftMargin: parent.contentLeftInset; anchors.rightMargin: parent.contentRightInset
        spacing: Style.spacing.xl
        Icon { glyph: Model.Glyph.timer; color: Theme.warn; size: Style.font.iconLarge }
        ColumnLayout {
          Layout.fillWidth: true
          spacing: Style.spacing.xxs
          AppText {
            Layout.fillWidth: true; strong: true; wrapMode: Text.WordWrap
            text: {
              var p = page.expiring[0]
              if (!p) return ""
              var d = p.signing.daysLeft
              var when = d <= 0 ? "has stopped opening" : d < 1 ? "stops opening in " + Math.max(1, Math.round(d * 24)) + " hours" : "stops opening tomorrow"
              return p.name + " " + when + (page.expiring.length > 1 ? " (and " + Model.plural(page.expiring.length - 1, "more app") + ")" : "")
            }
          }
          AppText { Layout.fillWidth: true; tone: "dim"; size: "small"; wrapMode: Text.WordWrap; text: "Free Apple IDs sign apps for 7 days. Re-signing reinstalls it on your iPhone; its data stays." }
        }
        ActionButton {
          text: "Re-sign now"; glyph: Model.Glyph.sync; compact: true
          enabled: !page.svc.building
          onClicked: page.startKind(page.expiring[0], "renew")
        }
      }
    }

    // ---- empty
    EmptyState {
      Layout.fillWidth: true
      Layout.topMargin: Style.spacing.huge
      visible: page.svc.loadedOnce && page.svc.status !== "error" && page.svc.items.length === 0
      glyph: Model.Glyph.sparkles
      title: "Make your first iPhone app"
      text: "Start from a template that builds on Linux, or add a project you already have. iOS Studio looks for apps in " + page.lookIn() + "."
      actionText: "New app"
      actionGlyph: Model.Glyph.plus
      onAction: newSheet.open()
      secondaryText: "Add existing folder"
      secondaryGlyph: Model.Glyph.folderPlus
      onSecondary: page.addExisting()
    }

    // ---- list
    RowLayout {
      Layout.fillWidth: true
      visible: page.svc.items.length > 0
      spacing: Style.spacing.lg
      SectionHeader { Layout.fillWidth: true; text: "Your apps"; detail: page.query ? page.shown.length + " of " + page.svc.items.length : String(page.svc.items.length) }
      FilterField {
        id: search
        visible: page.svc.items.length > 3
        Layout.preferredWidth: Style.space(220)
        placeholder: "Filter apps  /"
        onTextChanged: { page.query = text; page.cursor = 0 }
      }
    }

    Repeater {
      id: cards
      model: page.shown
      delegate: ProjectCard {
        required property var modelData
        required property int index
        Layout.fillWidth: true
        project: modelData
        svc: page.svc
        watchSvc: page.app.watch
        hasCursor: page.cursor === index
        onHovered: function(h) { if (h) page.cursor = index }
        onRun: page.runProject(modelData)
        onBuildIpa: page.startKind(modelData, "ipa")
        onCheckBuild: page.startKind(modelData, "compile")
        onRenew: page.startKind(modelData, "renew")
        onOpenEditor: page.svc.openInEditor(modelData)
        onOpenFolder: page.svc.reveal(modelData)
        onRemoveFromList: page.svc.remove(modelData)
        onToggleAutoRenew: page.svc.setAutoRenew(modelData, !modelData.autoRenew)
        onOpenBuildSettings: buildSettingsSheet.openFor(modelData)
        onEditPresets: runPresetsSheet.openFor(modelData)
        onShowBuild: page.panelOpen = true
        onShowProblems: page.showWatchProblems(modelData)
      }
    }

    AppText {
      Layout.fillWidth: true
      visible: page.query !== "" && page.shown.length === 0 && page.svc.items.length > 0
      text: "No app matches “" + page.query + "”."
      tone: "dim"
      horizontalAlignment: Text.AlignHCenter
    }

    // ---- packages that could become apps
    SectionHeader {
      Layout.fillWidth: true
      Layout.topMargin: Style.spacing.lg
      visible: page.svc.convertible.length > 0
      text: "Swift packages that could be apps"
      detail: String(page.svc.convertible.length)
    }
    Repeater {
      model: page.svc.convertible
      delegate: ListRow {
        required property var modelData
        Layout.fillWidth: true
        glyph: Model.Glyph.package
        title: modelData.name
        subtitle: modelData.displayPath
        trailing: [ ActionButton { text: "Make it an app"; compact: true; kind: "ghost"; onClicked: page.svc.convert(modelData.path) } ]
      }
    }

    // ---- recent builds
    SectionHeader {
      Layout.fillWidth: true
      Layout.topMargin: Style.spacing.lg
      visible: page.svc.history.length > 0
      text: "Recent builds"
      trailing: [ ActionButton { text: "All build logs"; kind: "ghost"; compact: true; onClicked: page.app.nav("logs", { tab: "builds" }) } ]
    }
    Repeater {
      model: page.svc.history.slice(0, 6)
      delegate: ListRow {
        required property var modelData
        Layout.fillWidth: true
        readonly property string lv: modelData.result === "ok" ? "ok" : modelData.result === "cancelled" ? "info" : "error"
        glyph: modelData.result === "ok" ? Model.Glyph.checkCircle : modelData.result === "cancelled" ? Model.Glyph.stop : Model.Glyph.error
        glyphColor: Theme.levelColor(lv)
        title: modelData.name + " · " + (Model.isWatchBuild(modelData) && modelData.kind === "compile" ? "Compile on save"
                                         : Model.isWatchBuild(modelData) && modelData.kind === "run" ? "Run on save"
                                         : ({ run: "Run", ipa: ".ipa", renew: "Re-sign", install: "Reinstall", compile: "Build" }[modelData.kind] || modelData.kind))
        subtitle: modelData.result === "ok"
                  ? "Finished in " + Model.formatDuration(modelData.duration) + (modelData.warnings ? " · " + Model.plural(modelData.warnings, "warning") : "")
                  : (modelData.errorMessage || "Failed")
        meta: Model.relativeTime(modelData.time)
        trailing: [ IconButton { glyph: Model.Glyph.file; tooltip: "Open this build's log"; visible: !!modelData.logFile; onClicked: page.svc.openBuildLog(modelData.logFile) } ]
      }
    }

    Item { Layout.preferredHeight: Style.spacing.huge }
  }

  // ---------------------------------------------------------------- build drawer
  BuildPanel {
    id: panel
    anchors.top: parent.top
    anchors.bottom: parent.bottom
    anchors.right: parent.right
    width: page.panelWidth
    app: page.app
    svc: page.svc
    opened: page.panelOpen
    onCloseRequested: page.panelOpen = false
  }

  ProjectBuildSettingsSheet {
    id: buildSettingsSheet
    app: page.app
    svc: page.svc
  }

  RunPresetsSheet {
    id: runPresetsSheet
    app: page.app
    svc: page.svc
  }

  ProjectNewSheet {
    id: newSheet
    app: page.app
    svc: page.svc
    onCreated: function(p) {
      page.query = ""
      page.cursor = 0
    }
  }
}
