import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../common"
import "../../lib/Model.js" as Model

// Pairing flow sheet, driven by the daemon's `pair` events (app.device.pair).
//   DevicePairSheet { id: pairSheet; app: page.app }   pairSheet.begin("wifi")
Sheet {
  id: root
  required property var app
  readonly property var svc: app.device
  readonly property var ev: svc.pair || ({})
  readonly property string phase: ev.phase || ""
  readonly property string method: ev.method || _method
  property string _method: "usb"

  title: method === "wifi" ? "Pair over Wi-Fi" : "Pair over USB"
  subtitle: method === "wifi" ? "iOS 27 or later, same Wi-Fi network" : "One-time setup with the cable"
  glyph: method === "wifi" ? Model.Glyph.wifi : Model.Glyph.usb
  preferredWidth: Style.space(520)
  busy: phase === "starting"

  function begin(m) {
    root._method = m
    root.svc.startPair(m)
    root.open()
  }

  onClosed: if (root.svc.pairing) root.svc.cancelPair()

  // ---- code to type on the phone (Wi-Fi)
  ColumnLayout {
    Layout.fillWidth: true
    visible: root.phase === "show-code"
    spacing: Style.spacing.lg
    AppText { Layout.fillWidth: true; text: "Type this code on your iPhone"; tone: "dim"; horizontalAlignment: Text.AlignHCenter }
    Row {
      Layout.alignment: Qt.AlignHCenter
      spacing: Style.spacing.md
      Repeater {
        model: String(root.ev.code || "").split("")
        delegate: Rectangle {
          required property string modelData
          width: Style.font.displayLarge * 1.6
          height: Style.font.displayLarge * 2.1
          radius: Style.cornerRadius
          color: Theme.surfaceRaised
          border.width: Style.normalBorderWidth
          border.color: Util.alpha(Commons.Color.accent, 0.5)
          AppText { anchors.centerIn: parent; text: modelData; size: "displayLarge"; strong: true }
        }
      }
    }
    AppText { Layout.fillWidth: true; text: "The iPhone asks for it after you tap this computer in Paired Macs."; size: "small"; tone: "faint"; horizontalAlignment: Text.AlignHCenter; wrapMode: Text.WordWrap }
  }

  // ---- code shown on the phone, typed here (rare PIN flows)
  ColumnLayout {
    Layout.fillWidth: true
    visible: root.phase === "need-code"
    spacing: Style.spacing.lg
    AppText { Layout.fillWidth: true; text: "Type the code your iPhone shows"; tone: "dim" }
    TextField { id: codeField; Layout.fillWidth: true; placeholderText: "6-digit code"; onAccepted: root.svc.sendPairCode(text) }
  }

  // ---- walkthrough while waiting
  DeviceSteps {
    Layout.fillWidth: true
    visible: root.phase !== "show-code" && root.phase !== "done" && root.phase !== "failed" && root.phase !== "need-code"
    steps: root.method === "wifi" ? [
      { text: "Turn on Developer Mode on the iPhone (Settings › Privacy & Security › Developer Mode).", done: false },
      { text: "Open Settings › Developer › Paired Macs on the iPhone.", active: root.phase === "waiting" },
      { text: "Tap “" + "iOS Studio" + "” under Other Devices, then Pair." },
      { text: "Type the 6-digit code that appears here." }
    ] : [
      { text: "Keep your iPhone plugged in and unlocked.", done: root.phase !== "starting" },
      { text: "Tap “Trust” on the iPhone and enter its passcode.", active: root.phase === "trust", done: root.phase === "waiting" },
      { text: "Wi-Fi access is set up automatically.", active: root.phase === "waiting" }
    ]
  }

  RowLayout {
    Layout.fillWidth: true
    visible: root.svc.pairing
    spacing: Style.spacing.lg
    Spinner { size: Style.font.body; running: root.svc.pairing; color: Commons.Color.accent }
    AppText { Layout.fillWidth: true; text: root.ev.message || "Working…"; tone: "dim"; size: "small"; wrapMode: Text.WordWrap }
  }

  // ---- outcome
  EmptyState {
    Layout.fillWidth: true
    visible: root.phase === "done"
    compact: true
    level: "ok"
    glyph: Model.Glyph.checkCircle
    title: "Paired" + (root.ev.name ? " with " + root.ev.name : "")
    text: root.ev.message || ""
  }
  ErrorState {
    Layout.fillWidth: true
    visible: root.phase === "failed"
    compact: true
    app: root.app
    title: "Pairing didn't finish"
    error: ({ code: "pair", message: root.ev.message || "Pairing didn't finish.", detail: root.ev.detail || "" })
    showRetry: true
    retryText: "Try again"
    onRetry: root.svc.startPair(root.method)
  }

  footer: [
    ActionButton {
      text: root.phase === "done" ? "Done" : "Cancel"
      kind: root.phase === "done" ? "primary" : "ghost"
      onClicked: root.close()
    }
  ]
}
