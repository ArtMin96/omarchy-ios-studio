import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../common"
import "../../lib/Model.js" as Model

// "Connect your iPhone": shown when no iPhone is known (or the known one isn't paired).
// Two ways in: a cable (Trust once, Wi-Fi is set up for free) or Wi-Fi only (iOS 27+).
ColumnLayout {
  id: root
  required property var app
  readonly property var svc: app.device
  readonly property bool usbPresent: {
    for (var i = 0; i < svc.devices.length; i++) if (svc.devices[i].usb) return true
    return false
  }
  signal pairRequested(string method)

  spacing: Style.spacing.huge

  RowLayout {
    Layout.fillWidth: true
    Layout.topMargin: Style.spacing.huge
    spacing: Style.spacing.huge * 2

    DevicePhoneFrame {
      Layout.preferredWidth: Style.space(150)
      Layout.preferredHeight: Style.space(326)
      Layout.alignment: Qt.AlignTop
      glyph: root.usbPresent ? Model.Glyph.usb : Model.Glyph.phoneLink
      placeholder: root.usbPresent ? "Cable detected" : ""
    }

    ColumnLayout {
      Layout.fillWidth: true
      Layout.alignment: Qt.AlignVCenter
      spacing: Style.spacing.lg

      AppText { text: "Connect your iPhone"; size: "displayLarge"; strong: true; Layout.fillWidth: true; wrapMode: Text.Wrap }
      AppText {
        Layout.fillWidth: true
        Layout.maximumWidth: Style.space(520)
        wrapMode: Text.Wrap
        tone: "dim"
        text: "There's no iOS Simulator on Linux, so your iPhone is where your apps run. Pair it once — after that iOS Studio finds it on its own, over the cable or over Wi-Fi."
      }
      RowLayout {
        Layout.topMargin: Style.spacing.md
        spacing: Style.spacing.lg
        visible: root.svc.status === "needs-setup"
        StatusPill { level: "warn"; text: "The iPhone tools aren't installed yet" }
        ActionButton { text: "Install them"; glyph: Model.Glyph.download; kind: "primary"; onClicked: root.app.openSetupStep("pymobiledevice3") }
      }
      AppText {
        visible: root.svc.status !== "needs-setup" && root.svc.connection && root.svc.connection.message
        text: root.svc.connection.message || ""
        size: "small"; tone: "faint"
      }
    }
  }

  // The two ways in, side by side.
  RowLayout {
    Layout.fillWidth: true
    spacing: Style.spacing.panelGap
    visible: root.svc.status !== "needs-setup"

    Card {
      Layout.fillWidth: true
      Layout.preferredWidth: 1
      Layout.fillHeight: true
      title: "With a cable"
      subtitle: "Works with every iPhone · recommended"
      glyph: Model.Glyph.usb
      glyphColor: root.usbPresent ? Theme.success : Theme.dim
      accentEdge: root.usbPresent
      edgeColor: Theme.success

      DeviceSteps {
        Layout.fillWidth: true
        steps: [
          { text: "Plug your iPhone into this computer and unlock it.", done: root.usbPresent },
          { text: "Tap “Trust” on the iPhone and enter its passcode." },
          { text: "iOS Studio sets up Wi-Fi access too, so next time you won't need the cable." }
        ]
      }
      Item { Layout.fillHeight: true }
      RowLayout {
        Layout.fillWidth: true
        AppText {
          Layout.fillWidth: true
          text: root.usbPresent ? "Your iPhone is plugged in." : "Waiting for a cable…"
          size: "small"; tone: root.usbPresent ? "ok" : "faint"
        }
        ActionButton {
          text: "Pair over USB"
          glyph: Model.Glyph.link
          kind: root.usbPresent ? "primary" : "secondary"
          enabled: root.usbPresent && !root.svc.pairing
          onClicked: root.pairRequested("usb")
        }
      }
    }

    Card {
      Layout.fillWidth: true
      Layout.preferredWidth: 1
      Layout.fillHeight: true
      title: "Over Wi-Fi only"
      subtitle: "iOS 27 or later · no cable needed"
      glyph: Model.Glyph.wifi

      DeviceSteps {
        Layout.fillWidth: true
        steps: [
          { text: "Put the iPhone on the same Wi-Fi as this computer." },
          { text: "Turn on Developer Mode: Settings › Privacy & Security › Developer Mode." },
          { text: "Open Settings › Developer › Paired Macs and tap this computer, then type the code iOS Studio shows." }
        ]
      }
      Item { Layout.fillHeight: true }
      RowLayout {
        Layout.fillWidth: true
        Item { Layout.fillWidth: true }
        ActionButton {
          text: "Pair over Wi-Fi"
          glyph: Model.Glyph.wifi
          kind: root.usbPresent ? "secondary" : "primary"
          enabled: !root.svc.pairing
          onClicked: root.pairRequested("wifi")
        }
      }
    }
  }
}
