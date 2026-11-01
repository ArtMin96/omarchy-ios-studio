import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import Quickshell.Io
import "../Model.js" as Model

// Live screen: a big drawn phone with the mirror, the way it's shown (smooth stream, screenshots or
// AirPlay), short recordings for bug reports, and stills. Narrow windows stack the phone above.
GridLayout {
  id: root
  readonly property bool narrow: width > 0 && width < Style.space(680)
  columns: narrow ? 1 : 2
  columnSpacing: Style.spacing.huge * 2
  rowSpacing: Style.spacing.huge
  required property var app
  readonly property var svc: app.device
  property int rotationSteps: 0
  readonly property string currentFile: svc.framePath !== "" ? svc.framePath : svc.lastShot

  // "auto" (smooth stream, falls back to screenshots), "screenshot" or "airplay" (UxPlay, its own window)
  property string view: app.setting("mirrorView", svc.mirrorMode)
  readonly property bool airplayView: view === "airplay"
  function pickView(v) {
    root.view = v
    root.app.setSetting("mirrorView", v)
    if (v === "airplay") { if (root.svc.mirroring) root.svc.stopMirror() }
    else root.svc.setMirrorMode(v)
  }

  // AirPlay receiver (UxPlay): the smoothest mirror, started from the iPhone's Control Center.
  // `-p` = the fixed legacy ports that Setup's firewall step opens.
  property bool uxplayInstalled: false
  Process {
    id: uxCheck
    command: ["sh", "-c", "command -v uxplay"]
    running: true
    stdout: StdioCollector { onStreamFinished: root.uxplayInstalled = String(text || "").trim() !== "" }
  }
  Process {
    id: uxplay
    command: ["uxplay", "-p", "-n", "iOS Studio", "-nh"]
    onExited: function(code) { if (code !== 0 && code !== 15 && code !== 143) root.app.toast("The AirPlay receiver stopped (exit " + code + ")", "warn") }
  }
  Component.onDestruction: if (uxplay.running) uxplay.running = false

  // ---------------------------------------------------------------- recording
  readonly property var rec: svc.recording
  readonly property var recNow: rec || ({ path: "" })   // rec, or a blank one while bindings catch up after it goes null
  readonly property bool recActive: svc.recordingActive
  readonly property bool recDone: !!rec && rec.state === "done" && !!rec.path
  property int recLimit: 60
  property real _now: Date.now() / 1000
  Timer { interval: 250; repeat: true; running: root.recActive; onTriggered: root._now = Date.now() / 1000 }
  readonly property real recElapsed: !rec ? 0 : (rec.state === "recording" && rec.started
                                     ? Math.max(0, Math.min(rec.maxSeconds || 60, root._now - rec.started)) : (rec.seconds || 0))
  function clock(s) {
    s = Math.max(0, Math.floor(s))
    return Math.floor(s / 60) + ":" + ("0" + (s % 60)).slice(-2)
  }
  function showRecording() {
    var p = root.rec.path
    Util.execArgv(["xdg-open", p.slice(0, p.lastIndexOf("/"))])
  }
  function copyRecordingPath() {
    Util.execArgv(["wl-copy", "--", root.rec.path])
    root.app.toast("Copied the recording's path", "ok")
  }

  // ---------------------------------------------------------------- stills
  function saveShot() {
    var src = root.currentFile
    if (!src) { root.svc.screenshot(function(ok, d) { if (ok) _save(d.path) }); return }
    _save(src)
  }
  function _save(src) {
    var dir = root.app.home + "/Pictures"
    var ext = src.slice(src.lastIndexOf(".")) || ".png"
    var name = "iPhone " + Qt.formatDateTime(new Date(), "yyyy-MM-dd 'at' HH.mm.ss") + ext
    Util.execArgv(["sh", "-c", "mkdir -p \"$1\" && cp -f \"$2\" \"$1/$3\"", "sh", dir, src, name])
    root.app.toast("Saved to Pictures as “" + name + "”", "ok")
  }
  function copyShot() {
    var go = function(p) {
      var type = /\.jpe?g$/i.test(p) ? "image/jpeg" : "image/png"
      Util.execArgv(["sh", "-c", "wl-copy --type \"$2\" < \"$1\"", "sh", p, type])
      root.app.toast("Screenshot copied to the clipboard", "ok")
    }
    if (root.currentFile) go(root.currentFile)
    else root.svc.screenshot(function(ok, d) { if (ok) go(d.path) })
  }

  // ---------------------------------------------------------------- status text
  readonly property string liveText: {
    if (!svc.mirroring) return ""
    var what = svc.mirrorActive === "stream" ? "Smooth" : (svc.mirrorActive === "screenshot" ? "Screenshots" : "Starting")
    return svc.measuredFps > 0 ? what + " · " + (svc.measuredFps >= 10 ? Math.round(svc.measuredFps) : Math.round(svc.measuredFps * 10) / 10) + " fps" : what
  }
  readonly property string description: airplayView
    ? "AirPlay Screen Mirroring sends your iPhone's screen to this computer in its own window, smooth and with sound."
    : (view === "screenshot"
       ? "A live view made of screenshots, a few per second. Works on every iPhone and leaves the camera and microphone free."
       : "A smooth live view streamed from your iPhone (iOS 27 or later). Older iPhones use screenshots instead.")

  DevicePhoneFrame {
    Layout.preferredWidth: root.rotationSteps % 2 ? Style.space(560) : Style.space(260)
    Layout.preferredHeight: root.rotationSteps % 2 ? Style.space(260) : Style.space(564)
    Layout.alignment: root.narrow ? (Qt.AlignTop | Qt.AlignHCenter) : Qt.AlignTop
    source: root.svc.screenSource
    live: root.svc.mirroring || root.recActive
    liveLabel: root.recActive ? "REC " + root.clock(root.recElapsed) : "LIVE"
    dimmed: !root.svc.connected
    rotationSteps: root.rotationSteps
    glyph: Model.Glyph.camera
    placeholder: !root.svc.connected ? "Connect your iPhone to see its screen"
               : (root.airplayView ? "AirPlay opens its own window" : "Press Start to watch your iPhone's screen here")
    onFrameShown: function(url) { root.svc.ackFrame(url) }
    Behavior on Layout.preferredWidth { NumberAnimation { duration: 260; easing.type: Easing.OutCubic } }
    Behavior on Layout.preferredHeight { NumberAnimation { duration: 260; easing.type: Easing.OutCubic } }
  }

  ColumnLayout {
    Layout.fillWidth: true
    Layout.alignment: Qt.AlignTop
    spacing: Style.spacing.huge

    ColumnLayout {
      Layout.fillWidth: true
      spacing: Style.spacing.md
      RowLayout {
        Layout.fillWidth: true
        spacing: Style.spacing.lg
        AppText { text: root.svc.mirroring ? "Watching your iPhone" : "Screen"; size: "heading"; strong: true }
        StatusPill { visible: root.liveText !== ""; level: root.svc.mirrorActive === "stream" ? "ok" : "info"; text: root.liveText; showGlyph: false }
        Item { Layout.fillWidth: true }
      }
      AppText { Layout.fillWidth: true; wrapMode: Text.WordWrap; tone: "dim"; text: root.description }
    }

    // how it's shown
    Flow {
      Layout.fillWidth: true
      spacing: Style.spacing.lg
      ButtonGroupLite {
        id: viewGroup
        options: [{ value: "auto", label: "Smooth" }, { value: "screenshot", label: "Screenshots" }, { value: "airplay", label: "AirPlay" }]
        value: root.view
        onPicked: function(v) { root.pickView(v) }
      }
      AppText { visible: root.view === "screenshot"; text: "Speed"; tone: "dim"; size: "small"; height: viewGroup.height; verticalAlignment: Text.AlignVCenter }
      ButtonGroupLite {
        visible: root.view === "screenshot"
        options: [{ value: "1", label: "1 fps" }, { value: "2", label: "2 fps" }, { value: "8", label: "Max" }]
        value: String(root.svc.mirrorFps >= 8 ? 8 : root.svc.mirrorFps)
        onPicked: function(v) { root.svc.setFps(Number(v)) }
      }
    }

    // in-app live view
    Flow {
      Layout.fillWidth: true
      visible: !root.airplayView
      spacing: Style.spacing.lg
      ActionButton {
        text: root.svc.mirroring ? "Stop" : "Start live view"
        glyph: root.svc.mirroring ? Model.Glyph.stop : Model.Glyph.play
        kind: "primary"
        enabled: root.svc.connected
        onClicked: root.svc.mirroring ? root.svc.stopMirror() : root.svc.startMirror()
      }
    }

    // AirPlay (UxPlay)
    ColumnLayout {
      Layout.fillWidth: true
      visible: root.airplayView
      spacing: Style.spacing.md
      RowLayout {
        Layout.fillWidth: true
        spacing: Style.spacing.lg
        ActionButton {
          visible: root.uxplayInstalled
          kind: "primary"
          text: uxplay.running ? "Stop AirPlay receiver" : "Start AirPlay receiver"
          glyph: uxplay.running ? Model.Glyph.stop : Model.Glyph.window
          onClicked: {
            if (uxplay.running) { uxplay.running = false; return }
            uxplay.running = true
            root.app.toast("On your iPhone: Control Center › Screen Mirroring › iOS Studio", "info")
          }
        }
        ActionButton {
          visible: !root.uxplayInstalled
          kind: "primary"
          text: "Set up AirPlay mirroring"; glyph: Model.Glyph.download
          onClicked: root.app.openSetupStep("uxplay")
        }
        AppText {
          Layout.fillWidth: true
          visible: uxplay.running
          text: "Waiting for your iPhone: open Control Center › Screen Mirroring and pick “iOS Studio”. Same Wi-Fi needed."
          size: "small"; tone: "dim"; wrapMode: Text.WordWrap
        }
      }
      AppText {
        Layout.fillWidth: true
        visible: !root.uxplayInstalled
        text: "AirPlay needs the free UxPlay receiver. Setup installs it in a few minutes."
        size: "small"; tone: "faint"; wrapMode: Text.WordWrap
      }
    }

    // why it isn't smooth / what streaming does to the phone
    RowLayout {
      Layout.fillWidth: true
      visible: !root.airplayView && (root.svc.mirrorNotice !== "" || root.view === "auto")
      spacing: Style.spacing.md
      Icon { glyph: Model.Glyph.info; color: root.svc.mirrorNotice !== "" ? Theme.levelColor("warn") : Theme.faint; Layout.alignment: Qt.AlignTop }
      AppText {
        Layout.fillWidth: true
        wrapMode: Text.WordWrap
        size: "small"
        tone: root.svc.mirrorNotice !== "" ? "dim" : "faint"
        text: root.svc.mirrorNotice !== "" ? root.svc.mirrorNotice
            : "While it streams, your iPhone shows a screen-sharing indicator and apps can't use the camera or microphone. Pick Screenshots if your app needs them."
      }
    }

    ErrorState {
      Layout.fillWidth: true
      visible: !!root.svc.mirrorError && !root.airplayView
      banner: true
      app: root.app
      error: root.svc.mirrorError
      onRetry: root.svc.startMirror()
    }

    // ---------------------------------------------------------------- record
    SectionHeader { Layout.fillWidth: true; text: "Record"; detail: root.recActive ? "" : "Short videos for bug reports" }
    Flow {
      Layout.fillWidth: true
      spacing: Style.spacing.lg
      visible: !root.airplayView
      ActionButton {
        text: !root.recActive ? "Record" : (root.recNow.state === "finishing" ? "Saving…" : "Stop recording")
        glyph: root.recActive ? Model.Glyph.stop : Model.Glyph.dot
        kind: root.recActive ? "danger" : "secondary"
        busy: root.recActive && root.recNow.state === "finishing"
        enabled: root.svc.connected || root.recActive
        onClicked: root.recActive ? root.svc.stopRecording() : root.svc.startRecording(root.recLimit)
      }
      AppText {
        visible: root.recActive
        height: recLimitGroup.height; verticalAlignment: Text.AlignVCenter
        text: root.clock(root.recElapsed) + " / " + root.clock(root.rec ? (root.rec.maxSeconds || root.recLimit) : root.recLimit)
              + (root.rec && root.rec.bytes ? "  ·  " + Model.formatBytes(root.rec.bytes) : "")
        tone: "dim"
      }
      AppText { visible: !root.recActive; text: "Up to"; tone: "dim"; size: "small"; height: recLimitGroup.height; verticalAlignment: Text.AlignVCenter }
      ButtonGroupLite {
        id: recLimitGroup
        visible: !root.recActive
        options: [{ value: "30", label: "30 s" }, { value: "60", label: "1 min" }, { value: "300", label: "5 min" }]
        value: String(root.recLimit)
        onPicked: function(v) { root.recLimit = Number(v) }
      }
    }
    AppText {
      Layout.fillWidth: true
      visible: root.airplayView
      text: "Recording works with the Smooth and Screenshots views."
      size: "small"; tone: "faint"; wrapMode: Text.WordWrap
    }

    ErrorState {
      Layout.fillWidth: true
      visible: !!root.rec && root.rec.state === "failed" && !!root.rec.error
      banner: true
      app: root.app
      error: root.rec && root.rec.error ? root.rec.error : null
      onRetry: root.svc.startRecording(root.recLimit)
    }

    // the finished recording
    ColumnLayout {
      Layout.fillWidth: true
      visible: root.recDone
      spacing: Style.spacing.md
      AppText {
        Layout.fillWidth: true
        wrapMode: Text.WordWrap
        text: !root.recDone ? "" : (root.recNow.placeholder ? (root.recNow.notice || "")
              : "Saved a " + root.clock(root.recNow.seconds) + " video (" + Model.formatBytes(root.recNow.bytes) + ", "
                + (root.recNow.mode === "stream" ? "smooth" : "from screenshots") + ")")
      }
      AppText {
        Layout.fillWidth: true
        visible: root.recDone && !root.recNow.placeholder
        text: !root.recDone ? "" : (root.recNow.path.indexOf(root.app.home + "/") === 0 ? "~" + root.recNow.path.slice(root.app.home.length) : root.recNow.path)
        size: "small"; tone: "faint"; elide: Text.ElideMiddle
      }
      Flow {
        Layout.fillWidth: true
        spacing: Style.spacing.lg
        ActionButton { visible: root.recDone && !root.recNow.placeholder; text: "Show in folder"; glyph: Model.Glyph.folderOpen; onClicked: root.showRecording() }
        ActionButton { visible: root.recDone && !root.recNow.placeholder; text: "Copy path"; glyph: Model.Glyph.copy; onClicked: root.copyRecordingPath() }
        ActionButton { text: "Done"; kind: "ghost"; onClicked: root.svc.dismissRecording() }
      }
      // inline player; left out when Qt Multimedia isn't installed (the Loader fails quietly)
      Loader {
        id: preview
        Layout.preferredWidth: Style.space(160)
        Layout.preferredHeight: active && status === Loader.Ready ? Style.space(348) : 0
        active: root.recDone && !root.recNow.placeholder
        visible: status === Loader.Ready
        source: "DeviceRecordingPreview.qml"
        onLoaded: item.source = Qt.binding(function() { return root.recDone ? "file://" + root.recNow.path : "" })
      }
    }

    // ---------------------------------------------------------------- stills
    SectionHeader { Layout.fillWidth: true; text: "Capture" }
    Flow {
      Layout.fillWidth: true
      spacing: Style.spacing.lg
      ActionButton { text: "Take screenshot"; glyph: Model.Glyph.camera; busy: root.svc.shooting; enabled: root.svc.connected; onClicked: root.svc.screenshot() }
      ActionButton { text: "Save to Pictures"; glyph: Model.Glyph.save; enabled: root.svc.connected || root.currentFile !== ""; onClicked: root.saveShot() }
      ActionButton { text: "Copy image"; glyph: Model.Glyph.copy; enabled: root.svc.connected || root.currentFile !== ""; onClicked: root.copyShot() }
      ActionButton { text: "Rotate"; glyph: Model.Glyph.sync; onClicked: root.rotationSteps = (root.rotationSteps + 1) % 4 }
    }
  }
}
