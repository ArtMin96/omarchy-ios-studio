import QtQuick
import QtQuick.Layouts
import QtQuick.Dialogs
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../components/common"
import "../lib/Model.js" as Model

// Setup: the guided onboarding wizard. Every piece iOS Studio needs, grouped,
// in install order, with one "Install everything" and per-step Install/Repair.
// Deep link: app.nav("setup", {step: "xtool"}) scrolls to and highlights a step.
PageFrame {
  id: page
  required property var app
  readonly property var svc: app.setup

  title: "Setup"
  subtitle: "Everything iOS Studio needs to build apps for your iPhone, installed for you."
  actions: [
    IconButton {
      glyph: Model.Glyph.refresh; tooltip: "Check again (r)"
      busy: page.svc.checking
      onClicked: page.svc.refresh()
    }
  ]

  property int cursor: -1
  property string highlight: ""
  property var rowItems: ({})

  // Install logs go straight to the step's row (StepRow.appendLog), one line at a time.
  Connections {
    target: page.svc
    function onStepLog(stepId, text) { var it = page.rowItems[stepId]; if (it) it.appendLog(text) }
    function onStepLogCleared(stepId) {
      for (var id in page.rowItems) if (stepId === "" || id === stepId) page.rowItems[id].clearLog()
    }
  }

  // ------------------------------------------------------------ page hooks
  function activated(params) {
    if (params && params.step) {
      page.highlight = params.step
      highlightTimer.restart()
      scrollTimer.target = params.step
      scrollTimer.restart()
      var i = page.flatIndex(params.step)
      if (i >= 0) page.cursor = i
    }
  }

  function refresh() { page.svc.refresh() }

  // Esc (the shell calls goBack when no overlay or field has focus)
  function goBack() { page.cursor = -1; page.highlight = "" }


  function handleKey(event) {
    var a = Model.keyAction(event)
    var list = page.ordered
    var n = list.length
    if (!n) return false
    if (a === "down") { page.cursor = Math.min(n - 1, page.cursor + 1); page.scrollToStep(list[page.cursor].id); return true }
    if (a === "up") { page.cursor = Math.max(0, page.cursor - 1); page.scrollToStep(list[page.cursor].id); return true }
    if (a === "home") { page.cursor = 0; page.flickable.contentY = 0; return true }
    if (a === "end") { page.cursor = n - 1; page.scrollToStep(list[n - 1].id); return true }
    if (a === "activate" && page.cursor >= 0) { page.actOn(list[page.cursor]); return true }
    if (event.text === "i" && page.svc.autoCount > 0 && !page.svc.installing) { page.installAll(); return true }
    return false
  }

  function scrollToStep(id) { var it = page.rowItems[id]; if (it) page.scrollToItem(it) }
  // steps in display order (by group), which is also the numbering and the cursor order
  readonly property var ordered: {
    var out = []
    for (var g = 0; g < page.svc.groups.length; g++) out = out.concat(page.stepsIn(page.svc.groups[g]))
    return out
  }
  function flatIndex(id) { for (var i = 0; i < page.ordered.length; i++) if (page.ordered[i].id === id) return i; return -1 }
  function stepsIn(group) { return page.svc.steps.filter(function(s) { return s.group === group && s.id !== "platform" }) }
  function groupDone(group) { return page.stepsIn(group).filter(function(s) { return s.state === "done" }).length }

  // ------------------------------------------------------------ view model
  function run(id) { return page.svc.runs[id] || null }

  function viewState(s) {
    var r = page.run(s.id)
    if (r && r.state === "running" && page.svc.installing) return "running"
    if (r && r.state === "failed" && s.state !== "done") return "failed"
    if (s.state === "broken") return "failed"
    if (s.state === "unknown") return "pending"
    return s.state
  }

  function actionLabel(s) {
    if (s.id === "xcode") return ""
    var a = s.action || {}
    if (a.kind === "none") return ""
    if (s.state === "done") return ""
    if (s.state === "unknown") return a.kind === "page" ? (a.label || "Open") : ""
    if (s.state === "broken") return "Repair"
    var r = page.run(s.id)
    if (r && r.state === "failed") return "Try again"
    return a.label || "Install"
  }

  function actionGlyph(s) {
    var a = s.action || {}
    if (a.kind === "page") return a.page === "account" ? Model.Glyph.apple : Model.Glyph.phone
    if (s.state === "broken") return Model.Glyph.wrench
    return Model.Glyph.download
  }

  function metaFor(s) {
    if (s.state === "done") return ""
    var parts = []
    if (s.size && s.size !== "none" && s.size !== "tiny") parts.push(s.size)
    if (s.time && s.time !== "seconds") parts.push(s.time)
    if (s.needsRoot) parts.push("asks for your password")
    return parts.join(" · ")
  }

  function etaText(sec) {
    if (sec === null || sec === undefined || sec < 0) return ""
    if (sec < 50) return "less than a minute left"
    return "about " + Model.formatDuration(sec) + " left"
  }

  // steps this one needs first that aren't done yet
  function blockers(s) {
    var out = []
    var deps = s.depends || []
    for (var i = 0; i < deps.length; i++) {
      var d = page.svc.step(deps[i])
      if (d && d.state !== "done") out.push(d)
    }
    return out
  }

  function blockedText(s, blocked) {
    if (!blocked.length) return s.detail
    var manual = blocked.filter(function(b) { return b.interactive })
    if (manual.length) return "Needs the " + manual.map(function(b) { return b.id === "xcode" ? "Xcode download" : b.title }).join(" and ") + " first."
    var names = blocked.map(function(b) { return b.title })
    var list = names.length > 1 ? names.slice(0, -1).join(", ") + " and " + names[names.length - 1] : names[0]
    return "Comes after " + list + ". “Install everything” does them in order."
  }

  function actOn(s) {
    if (!s) return
    if (page.svc.installing && (s.action || {}).kind !== "page") { page.app.toast("Wait for the current step to finish.", "warn"); return }
    page.svc.act(s)
  }

  function installAll() {
    page.svc.installAll()
  }

  readonly property var next: page.svc.nextStep()
  readonly property bool ready: page.svc.loaded && page.svc.essentialMissing.length === 0
  readonly property var counted: page.svc.steps.filter(function(s) { return s.id !== "platform" && !s.optional && s.state !== "optional" })
  readonly property int doneCount: page.counted.filter(function(s) { return s.state === "done" }).length
  readonly property var autoSteps: page.svc.steps.filter(function(s) { return s.canAutoInstall && !s.optional })
  readonly property string autoTime: {
    var ids = page.autoSteps.map(function(s) { return s.id })
    if (ids.indexOf("sdk") >= 0) return "about 30–45 minutes"
    if (ids.indexOf("xtool") >= 0 || ids.indexOf("swift") >= 0) return "about 15–25 minutes"
    return "a few minutes"
  }
  readonly property var manualSteps: page.svc.steps.filter(function(s) { return s.essential && s.interactive && (s.state === "missing" || s.state === "broken") })

  Timer { id: highlightTimer; interval: 4000; onTriggered: page.highlight = "" }
  Timer { id: scrollTimer; property string target: ""; interval: 120; onTriggered: page.scrollToStep(target) }

  // ------------------------------------------------------------ loading / error
  Skeleton {
    Layout.fillWidth: true
    visible: !page.svc.loaded && page.svc.status !== "error"
    lines: 5; rowHeight: Style.space(72); gap: Style.spacing.lg
  }

  ErrorState {
    Layout.fillWidth: true
    visible: !page.svc.loaded && page.svc.status === "error"
    app: page.app
    title: "Couldn't check this computer"
    error: page.svc.error
    onRetry: page.svc.refresh()
  }

  // ------------------------------------------------------------ hero
  BorderSurface {
    id: hero
    Layout.fillWidth: true
    visible: page.svc.loaded
    radius: Style.cornerRadius
    color: Theme.surface
    borderSpec: Border.flat(Theme.hairline, Style.normalBorderWidth)
    implicitHeight: heroCol.implicitHeight + Style.spacing.panelPadding * 2

    // same insets as the Home and Apple ID heroes
    ColumnLayout {
      id: heroCol
      anchors.fill: parent
      anchors.margins: Style.spacing.panelPadding
      anchors.leftMargin: Style.spacing.panelPadding + Style.spacing.md
      anchors.rightMargin: Style.spacing.panelPadding + Style.spacing.md
      spacing: Style.spacing.xxl

      RowLayout {
        Layout.fillWidth: true
        spacing: Style.spacing.huge

        ColumnLayout {
          Layout.fillWidth: true
          spacing: Style.spacing.md
          AppText {
            Layout.fillWidth: true
            size: "display"; strong: true
            wrapMode: Text.Wrap
            text: page.svc.runningAll ? "Setting things up…"
                : page.ready ? "Ready to build for iPhone"
                : page.doneCount * 2 < page.counted.length ? "Let's get your computer ready"
                : "Almost there"
          }
          AppText {
            Layout.fillWidth: true
            tone: "dim"; wrapMode: Text.Wrap; lineHeight: 1.25
            text: {
              if (page.svc.runningAll) return "You can keep using your computer. iOS Studio will tell you if it needs anything."
              if (page.ready) return "Swift, xtool and the iOS SDK are installed and you're signed in. Build an app and run it on your iPhone from Projects."
              var parts = []
              if (page.autoSteps.length) parts.push((page.autoSteps.length === 1 ? "1 piece installs by itself" : page.autoSteps.length + " pieces install by themselves") + " (" + page.autoTime + ", mostly waiting)")
              var manual = page.manualSteps.map(function(s) { return s.id === "xcode" ? "download Xcode" : s.id === "apple-id" ? "sign in to Apple" : s.id === "iphone" ? "pair your iPhone" : s.title.toLowerCase() })
              if (manual.length) parts.push("a few things need you: " + manual.join(", "))
              return parts.length ? parts.join("; ") + "." : "A few optional extras are left."
            }
          }
        }

        // readiness count: the number is the status, not decoration
        ColumnLayout {
          Layout.alignment: Qt.AlignTop
          spacing: 0
          AppText {
            Layout.alignment: Qt.AlignRight
            text: page.doneCount + "/" + page.counted.length
            size: "heading"; strong: true
            tone: page.ready ? "ok" : "primary"
          }
          AppText { Layout.alignment: Qt.AlignRight; text: "ready"; size: "small"; tone: "dim" }
        }
      }

      // one segment per step, in install order
      RowLayout {
        Layout.fillWidth: true
        spacing: Style.spacing.xs
        Repeater {
          model: page.counted
          delegate: Rectangle {
            id: seg
            required property var modelData
            readonly property string vs: page.viewState(modelData)
            Layout.fillWidth: true
            implicitHeight: Style.space(6)
            radius: Style.cornerRadius > 0 ? height / 2 : 0
            color: vs === "done" ? Theme.success
                 : vs === "running" ? Commons.Color.accent
                 : vs === "failed" ? Commons.Color.urgent
                 : Util.alpha(Commons.Color.foreground, 0.12)
            Behavior on color { ColorAnimation { duration: 220 } }
            SequentialAnimation on opacity {
              running: seg.vs === "running"; loops: Animation.Infinite
              NumberAnimation { to: 0.45; duration: 700; easing.type: Easing.InOutSine }
              NumberAnimation { to: 1; duration: 700; easing.type: Easing.InOutSine }
              onStopped: seg.opacity = 1
            }
            HoverHandler { id: segHover; cursorShape: Qt.PointingHandCursor }
            TapHandler { onTapped: { page.highlight = modelData.id; highlightTimer.restart(); scrollTimer.target = modelData.id; scrollTimer.restart() } }
            PanelToolTip { visible: segHover.hovered; text: modelData.title + ": " + (modelData.detail || modelData.state) }
          }
        }
      }

      // overall progress while "Install everything" runs
      ProgressTrack {
        Layout.fillWidth: true
        visible: page.svc.runningAll
        readonly property var r: page.svc.runningStep ? page.run(page.svc.runningStep) : null
        readonly property var s: page.svc.runningStep ? page.svc.step(page.svc.runningStep) : null
        value: r && r.fraction >= 0 ? r.fraction : 0
        indeterminate: !r || r.fraction < 0
        label: (s ? s.title : "Preparing") + (r && r.detail ? " · " + r.detail : "")
        trailing: r ? page.etaText(r.eta) : ""
      }

      RowLayout {
        Layout.fillWidth: true
        spacing: Style.spacing.lg
        visible: !page.ready || page.svc.runningAll

        ActionButton {
          visible: !page.svc.installing && page.autoSteps.length > 0
          text: "Install everything"
          glyph: Model.Glyph.download
          kind: "primary"
          tooltip: "Installs " + page.autoSteps.map(function(s) { return s.title }).join(", ") + " (i)"
          onClicked: page.installAll()
        }
        ActionButton {
          visible: !page.svc.installing && page.next && page.next.interactive
          text: page.next ? (page.next.id === "xcode" ? "Download Xcode " + (page.svc.xcode.needed || "") : (page.next.action && page.next.action.label) || page.next.title) : ""
          glyph: page.next && page.next.id === "xcode" ? Model.Glyph.cloudDownload : page.actionGlyph(page.next || {})
          kind: page.autoSteps.length > 0 ? "secondary" : "primary"
          onClicked: { page.scrollToStep(page.next.id); page.actOn(page.next) }
        }
        ActionButton {
          visible: page.svc.installing
          text: "Stop"
          glyph: Model.Glyph.stop
          kind: "ghost"
          onClicked: page.app.confirm({
            title: "Stop installing?",
            message: "The current step stops where it is. You can start it again later; finished steps stay installed.",
            confirmText: "Stop", cancelText: "Keep going", destructive: true, glyph: Model.Glyph.stop,
            onConfirm: function() { page.svc.cancel() }
          })
        }
        Item { Layout.fillWidth: true }
        AppText {
          visible: !page.svc.installing && page.autoSteps.some(function(s) { return s.needsRoot })
          text: "You'll be asked for your password once."
          size: "small"; tone: "faint"
        }
      }

      RowLayout {
        Layout.fillWidth: true
        visible: page.ready && !page.svc.runningAll
        spacing: Style.spacing.lg
        ActionButton { text: "Go to Projects"; glyph: Model.Glyph.hammer; kind: "primary"; onClicked: page.app.nav("projects") }
        ActionButton { text: "See the toolchain"; glyph: Model.Glyph.toolbox; kind: "ghost"; onClicked: page.app.nav("toolchain") }
      }
    }
  }

  ErrorState {
    Layout.fillWidth: true
    visible: page.svc.loaded && page.svc.status === "error"
    banner: true
    app: page.app
    error: page.svc.error
    onRetry: page.svc.refresh()
  }

  // platform problem: nothing else matters
  ErrorState {
    Layout.fillWidth: true
    readonly property var p: page.svc.step("platform")
    visible: !!p && p.state !== "done"
    app: page.app
    title: "This computer isn't supported"
    message: p ? p.detail + ". iOS Studio needs an Arch-based Linux such as Omarchy on a 64-bit Intel/AMD or ARM computer." : ""
    showRetry: false
  }

  // ------------------------------------------------------------ steps, by group
  Repeater {
    model: page.svc.loaded ? page.svc.groups : []
    delegate: ColumnLayout {
      id: groupCol
      required property var modelData
      readonly property var list: page.stepsIn(modelData)
      // a group that is all done (or optional) folds away; the user can open it
      readonly property bool settled: list.every(function(s) { return s.state === "done" || s.state === "optional" }) && !list.some(function(s) { var r = page.run(s.id); return r && (r.state === "running" || r.state === "failed") })
      property bool userOpen: false
      readonly property bool open: !settled || userOpen || list.some(function(s) { return s.id === page.highlight })
      Layout.fillWidth: true
      spacing: Style.spacing.lg
      visible: list.length > 0

      SectionHeader {
        Layout.fillWidth: true
        text: groupCol.modelData
        detail: groupCol.settled ? "all done" : page.groupDone(groupCol.modelData) + " of " + groupCol.list.length + " done"
        trailing: [
          ActionButton {
            visible: groupCol.settled
            compact: true
            kind: "ghost"
            text: groupCol.open ? "Hide" : "Show " + groupCol.list.length
            glyph: groupCol.open ? Model.Glyph.chevronUp : Model.Glyph.chevronDown
            onClicked: groupCol.userOpen = !groupCol.userOpen
          }
        ]
      }

      Repeater {
        model: groupCol.open ? groupCol.list : []
        delegate: StepRow {
          id: row
          required property var modelData
          readonly property var s: modelData
          readonly property var r: page.run(s.id)
          readonly property int flat: page.flatIndex(s.id)
          readonly property string vs: page.viewState(s)
          Layout.fillWidth: true

          number: flat + 1
          title: s.title
          description: s.state === "done" ? s.detail : s.description
          stepState: vs
          meta: page.metaFor(s)
          actionText: page.actionLabel(s)
          actionGlyph: page.actionGlyph(s)
          readonly property var blocked: page.blockers(s)
          actionKind: vs === "failed" ? "primary" : "secondary"
          actionEnabled: blocked.length === 0 && (!page.svc.installing || (s.action || {}).kind === "page")
          actionBusy: vs === "running"
          progress: vs === "running" && r && r.fraction >= 0 ? r.fraction : -1
          progressIndeterminate: vs === "running" && (!r || r.fraction < 0)
          progressText: vs === "running" && r ? (r.detail || "Working…") : ""
          progressTrailing: vs === "running" && r ? page.etaText(r.eta) : ""
          errorText: vs === "failed" ? (r && r.error && r.error.message ? r.error.message : s.detail) : ""
          hasCursor: page.cursor === flat || page.highlight === s.id
          onHovered: function(h) { if (h) page.cursor = flat }
          onAction: page.actOn(s)

          Component.onCompleted: { setLog(page.svc.logLines(s.id)); var m = page.rowItems; m[s.id] = row; page.rowItems = m }
          Component.onDestruction: { if (page.rowItems[s.id] === row) { var m = page.rowItems; delete m[s.id]; page.rowItems = m } }

          extra: [
            // what's missing, under the plain-language description
            AppText {
              Layout.fillWidth: true
              visible: row.s.state !== "done" && row.vs !== "failed" && row.s.detail !== "" && row.s.id !== "xcode" && row.s.id !== "iphone"
              text: page.blockedText(row.s, row.blocked)
              size: "small"
              tone: row.s.state === "unknown" || row.s.state === "optional" ? "faint" : "warn"
              wrapMode: Text.Wrap
            },
            Loader {
              Layout.fillWidth: true
              active: row.s.id === "xcode" && row.s.state !== "done"
              visible: active
              sourceComponent: xcodeGuide
            },
            Loader {
              Layout.fillWidth: true
              active: row.s.id === "iphone" && row.s.state !== "done"
              visible: active
              sourceComponent: pairGuide
            },
            Loader {
              Layout.fillWidth: true
              active: row.s.id === "developer-mode" && row.s.state !== "done"
              visible: active
              sourceComponent: devModeGuide
            }
          ]
        }
      }
    }
  }

  // footnote
  AppText {
    Layout.fillWidth: true
    visible: page.svc.loaded
    text: "iOS Studio installs only into your home folder, except system packages and Swift, which go through your package manager after you enter your password."
    size: "small"; tone: "faint"; wrapMode: Text.Wrap
  }

  // ------------------------------------------------------------ guides
  Component {
    id: xcodeGuide
    ColumnLayout {
      id: xg
      readonly property var xc: page.svc.xcode || {}
      readonly property var w: page.svc.xip || { state: "idle" }
      readonly property string ver: xc.needed || "27.1"
      spacing: Style.spacing.lg

      GuideLine {
        n: 1
        title: "Download Xcode " + xg.ver + " from Apple"
        text: "About " + Model.formatBytes(xg.xc.expectedSize || 2.0e9) + ". Apple asks you to sign in with any Apple Account first (free is fine). Pick exactly “Xcode " + xg.ver + "”, not a beta."
      }
      GuideLine {
        n: 2
        title: "Leave the rest to iOS Studio"
        text: "Keep the file in Downloads. iOS Studio notices it, checks it's the real thing, and builds the iOS SDK from it."
      }

      // live watcher state
      BorderSurface {
        Layout.fillWidth: true
        radius: Style.cornerRadius
        visible: xg.w.state !== "idle"
        color: xg.w.state === "rejected" ? Theme.levelFill("error") : xg.w.state === "found" ? Theme.levelFill("ok") : Theme.levelFill("info")
        borderSpec: Border.flat(Theme.hairline, Style.normalBorderWidth)
        implicitHeight: watchCol.implicitHeight + Style.spacing.xl * 2
        ColumnLayout {
          id: watchCol
          anchors.fill: parent
          anchors.margins: Style.spacing.xl
          spacing: Style.spacing.md
          readonly property var w: xg.w
          RowLayout {
            spacing: Style.spacing.lg
            Spinner { size: Style.font.title; running: ["watching", "downloading", "checking"].indexOf(watchCol.w.state) >= 0; visible: running }
            Icon {
              visible: watchCol.w.state === "found" || watchCol.w.state === "rejected"
              glyph: watchCol.w.state === "found" ? Model.Glyph.checkCircle : Model.Glyph.warn
              color: watchCol.w.state === "found" ? Theme.success : Commons.Color.urgent
              size: Style.font.title
            }
            AppText {
              Layout.fillWidth: true
              wrapMode: Text.Wrap
              strong: true
              text: {
                var w = watchCol.w
                switch (w.state) {
                case "watching": return "Watching your Downloads folder for Xcode…"
                case "downloading": return "Downloading " + (w.file || "Xcode").replace(/\.(part|crdownload|download|tmp)$/, "")
                case "checking": return "Checking " + (w.file || "the file") + "…"
                case "found": return "Got it: Xcode " + (w.version || "") + " is ready to use."
                case "rejected": return w.message || "That file can't be used."
                }
                return ""
              }
            }
          }
          ProgressTrack {
            Layout.fillWidth: true
            visible: watchCol.w.state === "downloading" || watchCol.w.state === "checking"
            value: watchCol.w.fraction >= 0 ? watchCol.w.fraction : 0
            indeterminate: !(watchCol.w.fraction >= 0)
            label: watchCol.w.state === "downloading" ? Model.formatBytes(watchCol.w.size || 0) + " of about " + Model.formatBytes(watchCol.w.expected || 0)
                 : (watchCol.w.detail || "This takes about two minutes")
            trailing: page.etaText(watchCol.w.eta)
          }
        }
      }

      Flow {
        Layout.fillWidth: true
        spacing: Style.spacing.lg
        ActionButton {
          text: "Download Xcode " + xg.ver
          glyph: Model.Glyph.cloudDownload
          kind: "primary"
          onClicked: page.svc.openXcodeDownload(false)
        }
        ActionButton {
          text: "All Apple downloads"
          glyph: Model.Glyph.openExternal
          kind: "ghost"
          tooltip: "Opens developer.apple.com/download, if the direct link asks you to sign in and then shows a list"
          onClicked: page.svc.openXcodeDownload(true)
        }
        ActionButton {
          text: "I already have it…"
          glyph: Model.Glyph.folderOpen
          kind: "ghost"
          onClicked: xipDialog.open()
        }
        ActionButton {
          visible: page.svc.watchingXip
          text: "Stop watching"
          kind: "ghost"
          onClicked: page.svc.stopWatch()
        }
      }
    }
  }

  Component {
    id: pairGuide
    ColumnLayout {
      spacing: Style.spacing.lg
      id: pg
      readonly property var s: page.svc.step("iphone") || {}
      AppText {
        Layout.fillWidth: true
        visible: !!pg.s.detail
        text: pg.s.detail || ""
        size: "small"; tone: "warn"; wrapMode: Text.Wrap
      }
      GuideLine { n: 1; glyph: Model.Glyph.usb; title: "Connect"; text: "Plug your iPhone in with a USB cable. On iOS 27 you can also pair over Wi-Fi: iOS Studio shows a 6-digit code and you type it on the phone." }
      GuideLine { n: 2; glyph: Model.Glyph.shieldCheck; title: "Tap Trust"; text: "Unlock the phone. When it asks “Trust This Computer?”, tap Trust and enter your passcode." }
      GuideLine { n: 3; glyph: Model.Glyph.developer; title: "Turn on Developer Mode"; text: "Settings › Privacy & Security › Developer Mode. The phone restarts; confirm with your passcode." }
      GuideLine { n: 4; glyph: Model.Glyph.cert; title: "Trust your own apps"; last: true; text: "After the first install: Settings › General › VPN & Device Management › your Apple Account › Trust." }
    }
  }

  Component {
    id: devModeGuide
    AppText {
      text: "Developer Mode only appears after a developer app was installed once, or after iOS Studio asks the phone to reveal it. The iPhone page can do that for you."
      size: "small"; tone: "faint"; wrapMode: Text.Wrap
    }
  }

  // A numbered instruction line with a connector, used by the guides.
  component GuideLine: RowLayout {
    id: gl
    property int n: 0
    property string glyph: ""
    property string title: ""
    property string text: ""
    property bool last: false
    Layout.fillWidth: true
    spacing: Style.spacing.xl

    Rectangle {
      Layout.alignment: Qt.AlignTop
      implicitWidth: Style.font.title * 1.8
      implicitHeight: implicitWidth
      radius: Style.cornerRadius > 0 ? width / 2 : 0
      color: Theme.levelFill("info")
      border.width: Style.normalBorderWidth
      border.color: Util.alpha(Commons.Color.accent, 0.35)
      AppText { anchors.centerIn: parent; visible: gl.glyph === ""; text: String(gl.n); size: "small"; strong: true; tone: "accent" }
      Icon { anchors.centerIn: parent; visible: gl.glyph !== ""; glyph: gl.glyph; color: Commons.Color.accent; size: Style.font.body }
    }
    ColumnLayout {
      Layout.fillWidth: true
      spacing: Style.spacing.xxs
      AppText { Layout.fillWidth: true; text: gl.title; strong: true; wrapMode: Text.Wrap }
      AppText { Layout.fillWidth: true; text: gl.text; size: "small"; tone: "dim"; wrapMode: Text.Wrap; lineHeight: 1.2 }
    }
  }

  FileDialog {
    id: xipDialog
    title: "Choose the Xcode .xip file"
    nameFilters: ["Xcode archive (*.xip)"]
    currentFolder: Util.fileUrl(page.app.home + "/Downloads")
    onAccepted: page.svc.useXip(String(selectedFile))
  }

  // ------------------------------------------------------------ helper prompts
  // wrapper keeps the Sheet out of the layout until it lifts itself into the overlay
  Item {
    visible: false
    Sheet {
      id: promptSheet
      readonly property var p: page.svc.prompt
      opened: !!p
      title: "Before we start"
      glyph: Model.Glyph.help
      dismissible: true
      onClosed: if (page.svc.prompt) page.svc.decline()

      AppText {
        Layout.fillWidth: true
        text: promptSheet.p ? promptSheet.p.message : ""
        wrapMode: Text.Wrap
        lineHeight: 1.25
      }
      footer: [
        ActionButton { text: "Cancel"; kind: "ghost"; onClicked: page.svc.decline() },
        ActionButton {
          kind: "primary"
          glyph: Model.Glyph.check
          text: {
            var p = promptSheet.p
            if (!p || !p.options) return "Continue"
            for (var i = 0; i < p.options.length; i++) if (p.options[i].value === "yes") return p.options[i].label
            return "Continue"
          }
          onClicked: page.svc.answer("yes")
        }
      ]
    }
  }
}
