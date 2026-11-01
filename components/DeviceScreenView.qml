import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import Quickshell.Io
import "../Model.js" as Model

// Live screen: a big drawn phone with the mirror, plus capture controls.
// Narrow windows stack the phone above the controls.
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


  // AirPlay receiver (UxPlay): smoother mirroring, started from the iPhone's Control Center.
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

  function saveShot() {
    var src = root.currentFile
    if (!src) { root.svc.screenshot(function(ok, d) { if (ok) _save(d.path) }); return }
    _save(src)
  }
  function _save(src) {
    var dir = root.app.home + "/Pictures"
    var name = "iPhone " + Qt.formatDateTime(new Date(), "yyyy-MM-dd 'at' HH.mm.ss") + ".png"
    Util.execArgv(["sh", "-c", "mkdir -p \"$1\" && cp -f \"$2\" \"$1/$3\"", "sh", dir, src, name])
    root.app.toast("Saved to Pictures as “" + name + "”", "ok")
  }
  function copyShot() {
    var go = function(p) {
      Util.execArgv(["sh", "-c", "wl-copy --type image/png < \"$1\"", "sh", p])
      root.app.toast("Screenshot copied to the clipboard", "ok")
    }
    if (root.currentFile) go(root.currentFile)
    else root.svc.screenshot(function(ok, d) { if (ok) go(d.path) })
  }

  DevicePhoneFrame {
    Layout.preferredWidth: root.rotationSteps % 2 ? Style.space(560) : Style.space(260)
    Layout.preferredHeight: root.rotationSteps % 2 ? Style.space(260) : Style.space(564)
    Layout.alignment: root.narrow ? (Qt.AlignTop | Qt.AlignHCenter) : Qt.AlignTop
    source: root.svc.screenSource
    live: root.svc.mirroring
    dimmed: !root.svc.connected
    rotationSteps: root.rotationSteps
    glyph: Model.Glyph.camera
    placeholder: root.svc.connected ? "Press Start to watch your iPhone's screen here" : "Connect your iPhone to see its screen"
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
      AppText { text: root.svc.mirroring ? "Watching your iPhone" : "Screen"; size: "heading"; strong: true }
      AppText {
        Layout.fillWidth: true
        wrapMode: Text.WordWrap
        tone: "dim"
        text: "A live view made of fast screenshots (about 2 per second over Wi-Fi). It's for watching your app, not for controlling the phone."
      }
    }

    Flow {
      Layout.fillWidth: true
      spacing: Style.spacing.lg
      ActionButton {
        text: root.svc.mirroring ? "Stop" : "Start live view"
        glyph: root.svc.mirroring ? Model.Glyph.stop : Model.Glyph.play
        kind: "primary"
        enabled: root.svc.connected
        onClicked: root.svc.mirroring ? root.svc.stopMirror() : root.svc.startMirror()
      }
      AppText { text: "Speed"; tone: "dim"; size: "small"; height: speedGroup.height; verticalAlignment: Text.AlignVCenter }
      ButtonGroupLite {
        id: speedGroup
        options: [{ value: "0.5", label: "Slow" }, { value: "1", label: "1 fps" }, { value: "2", label: "2 fps" }, { value: "4", label: "Max" }]
        value: String(root.svc.mirrorFps)
        onPicked: function(v) { root.svc.setFps(Number(v)) }
      }
    }

    RowLayout {
      Layout.fillWidth: true
      spacing: Style.spacing.lg
      ActionButton {
        visible: root.uxplayInstalled
        text: uxplay.running ? "Stop AirPlay receiver" : "Mirror via AirPlay (smoother)"
        glyph: uxplay.running ? Model.Glyph.stop : Model.Glyph.window
        onClicked: {
          if (uxplay.running) { uxplay.running = false; return }
          uxplay.running = true
          root.app.toast("On your iPhone: Control Center › Screen Mirroring › iOS Studio", "info")
        }
      }
      ActionButton {
        visible: !root.uxplayInstalled
        text: "Set up AirPlay mirroring"; glyph: Model.Glyph.download; kind: "ghost"
        onClicked: root.app.openSetupStep("uxplay")
      }
      AppText {
        Layout.fillWidth: true
        visible: uxplay.running
        text: "Waiting for your iPhone: open Control Center › Screen Mirroring and pick “iOS Studio”. Same Wi-Fi needed."
        size: "small"; tone: "dim"; wrapMode: Text.WordWrap
      }
    }

    SectionHeader { Layout.fillWidth: true; text: "Capture" }
    Flow {
      Layout.fillWidth: true
      spacing: Style.spacing.lg
      ActionButton { text: "Take screenshot"; glyph: Model.Glyph.camera; busy: root.svc.shooting; enabled: root.svc.connected; onClicked: root.svc.screenshot() }
      ActionButton { text: "Save to Pictures"; glyph: Model.Glyph.save; enabled: root.svc.connected || root.currentFile !== ""; onClicked: root.saveShot() }
      ActionButton { text: "Copy image"; glyph: Model.Glyph.copy; enabled: root.svc.connected || root.currentFile !== ""; onClicked: root.copyShot() }
      ActionButton { text: "Rotate"; glyph: Model.Glyph.sync; onClicked: root.rotationSteps = (root.rotationSteps + 1) % 4 }
    }

    ErrorState {
      Layout.fillWidth: true
      visible: !!root.svc.mirrorError
      banner: true
      app: root.app
      error: root.svc.mirrorError
    }

    AppText {
      Layout.fillWidth: true
      wrapMode: Text.WordWrap
      size: "small"; tone: "faint"
      visible: !root.uxplayInstalled
      text: "Want a smooth, full-speed mirror? AirPlay Screen Mirroring needs the free UxPlay receiver, which Setup can install."
    }
  }
}
