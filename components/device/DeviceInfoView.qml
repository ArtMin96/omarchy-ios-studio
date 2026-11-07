import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../common"
import "../../lib/Model.js" as Model

// Every useful detail of the iPhone, each copyable.
ColumnLayout {
  id: root
  required property var app
  readonly property var svc: app.device
  readonly property var d: svc.info || ({})
  readonly property var b: d.battery || ({})
  readonly property var s: d.storage || null
  readonly property var dev: svc.current || ({})

  function v(x) { return x === undefined || x === null || x === "" ? "—" : String(x) }
  function copied(val) { root.app.toast("Copied " + (val.length > 40 ? val.substring(0, 40) + "…" : val)) }

  // groups sit apart like the page's own sections; a header sits close to its list
  spacing: Style.spacing.huge

  ColumnLayout {
    Layout.fillWidth: true
    spacing: Style.spacing.lg
    SectionHeader { Layout.fillWidth: true; text: "Device" }
    KeyValueList {
      Layout.fillWidth: true
      labelWidth: Style.space(170)
      onCopied: function(val) { root.copied(val) }
      rows: [
        { label: "Name", value: root.v(root.d.name || root.dev.name), copy: true },
        { label: "Model", value: root.v(root.d.model || root.dev.model) + (root.d.productType ? "  (" + root.d.productType + ")" : ""), copy: true },
        { label: "iOS", value: root.v(root.d.ios || root.dev.ios) + (root.d.build ? "  (" + root.d.build + ")" : ""), copy: true },
        { label: "UDID", value: root.v(root.d.udid || root.dev.udid), copy: true },
        { label: "Serial number", value: root.v(root.d.serial), copy: true },
        { label: "Hardware", value: root.v(root.d.hardwareModel) + (root.d.cpu ? " · " + root.d.cpu : ""), copy: true },
        { label: "Time zone", value: root.v(root.d.timeZone), copy: true }
      ]
    }
  }

  ColumnLayout {
    Layout.fillWidth: true
    spacing: Style.spacing.lg
    SectionHeader { Layout.fillWidth: true; text: "Connection" }
    KeyValueList {
      Layout.fillWidth: true
      labelWidth: Style.space(170)
      onCopied: function(val) { root.copied(val) }
      rows: [
        { label: "Connected", value: root.svc.connected ? (root.dev.transport === "usb" ? "USB cable" : "Wi-Fi") : (root.svc.connection.message || "No"), level: root.svc.connected ? "ok" : "warn" },
        { label: "Address", value: root.v(root.d.address), copy: true },
        { label: "Wi-Fi MAC", value: root.v(root.d.wifiAddress), copy: true },
        { label: "Bluetooth MAC", value: root.v(root.d.bluetoothAddress), copy: true },
        { label: "Paired for Wi-Fi", value: root.dev.wifiPaired ? "Yes" : "No", level: root.dev.wifiPaired ? "ok" : "warn" },
        { label: "Trusted over USB", value: root.dev.usbPaired ? "Yes" : "No" },
        { label: "pymobiledevice3", value: root.v(root.svc.pmd3Version), copy: true }
      ]
    }
  }

  ColumnLayout {
    Layout.fillWidth: true
    spacing: Style.spacing.lg
    SectionHeader { Layout.fillWidth: true; text: "Health" }
    KeyValueList {
      Layout.fillWidth: true
      labelWidth: Style.space(170)
      onCopied: function(val) { root.copied(val) }
      rows: [
        { label: "Battery", value: root.b.level !== undefined ? root.b.level + "%" + (root.b.charging ? ", charging" : (root.b.external ? ", plugged in" : "")) : "—" },
        { label: "Charge cycles", value: root.v(root.b.cycleCount) },
        { label: "Storage", value: root.s ? Model.formatBytes(root.s.free) + " free of " + Model.formatBytes(root.s.total) : "—" },
        { label: "Developer Mode", value: root.d.developerMode === true ? "On" : (root.d.developerMode === false ? "Off" : "—"), level: root.d.developerMode === true ? "ok" : (root.d.developerMode === false ? "warn" : "") },
        { label: "Developer tools (DDI)", value: root.d.ddiMounted === true ? "Loaded" : (root.d.ddiMounted === false ? "Not loaded" : "—"), level: root.d.ddiMounted === true ? "ok" : "" }
      ]
    }
  }
}
