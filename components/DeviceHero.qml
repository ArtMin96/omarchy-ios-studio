import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../Model.js" as Model

// The iPhone hero: drawn phone with the latest screen, identity, vitals and readiness pills.
RowLayout {
  id: root
  required property var app
  readonly property var svc: app.device
  readonly property var dev: svc.current || ({})
  readonly property var info: svc.info || ({})
  readonly property var batt: info.battery || null
  readonly property var store: info.storage || null
  readonly property string st: svc.connState
  readonly property bool online: svc.connected

  signal openScreen()
  signal devModeHelp()

  spacing: Style.spacing.huge * 2

  DevicePhoneFrame {
    Layout.preferredWidth: Style.space(128)
    Layout.preferredHeight: Style.space(279)
    Layout.alignment: Qt.AlignTop
    source: root.svc.screenSource
    live: root.svc.mirroring
    dimmed: !root.online
    clickable: root.online
    glyph: root.online ? Model.Glyph.camera : (root.st === "locked" ? Model.Glyph.lock : Model.Glyph.phoneLink)
    placeholder: root.online ? "Click to see the screen" : (root.svc.connection.message || "")
    onClicked: root.openScreen()
  }

  ColumnLayout {
    Layout.fillWidth: true
    Layout.alignment: Qt.AlignVCenter
    spacing: Style.spacing.lg

    // identity
    ColumnLayout {
      Layout.fillWidth: true
      spacing: Style.spacing.xs
      AppText {
        Layout.fillWidth: true
        text: root.dev.name || root.dev.model || "Your iPhone"
        size: "displayLarge"; strong: true
        elide: Text.ElideRight
      }
      AppText {
        Layout.fillWidth: true
        tone: "dim"
        elide: Text.ElideRight
        text: {
          var parts = []
          if (root.dev.model) parts.push(root.dev.model)
          if (root.dev.ios) parts.push("iOS " + root.dev.ios + (root.dev.build ? " (" + root.dev.build + ")" : ""))
          return parts.length ? parts.join("  ·  ") : "Details appear once it's connected"
        }
      }
    }

    // readiness pills
    Flow {
      Layout.fillWidth: true
      Layout.topMargin: Style.spacing.sm
      spacing: Style.spacing.md

      StatusPill {
        level: root.st === "connected" ? "ok" : (root.st === "connecting" || root.st === "searching" ? "busy"
               : (root.st === "locked" || root.st === "untrusted" || root.st === "not-paired" ? "warn" : "info"))
        glyph: root.st === "connected" ? (root.dev.transport === "usb" ? Model.Glyph.usb : Model.Glyph.wifi)
               : (root.st === "locked" ? Model.Glyph.lock : "")
        text: root.svc.connection.message || "…"
      }
      StatusPill {
        visible: root.online && root.batt && root.batt.level !== null && root.batt.level !== undefined
        level: !root.batt ? "info" : (root.batt.level <= 15 && !root.batt.charging ? "warn" : "info")
        glyph: root.batt && root.batt.charging ? Model.Glyph.batteryCharging : Model.Glyph.battery
        text: root.batt ? root.batt.level + "%" + (root.batt.charging ? " · charging" : "") : ""
      }
      StatusPill {
        visible: root.online && root.info.developerMode !== undefined && root.info.developerMode !== null
        level: root.info.developerMode ? "ok" : "warn"
        glyph: Model.Glyph.developer
        text: root.info.developerMode ? "Developer Mode on" : "Developer Mode off"
      }
      StatusPill {
        visible: root.online && root.info.ddiMounted !== undefined && root.info.ddiMounted !== null
        level: root.info.ddiMounted ? "ok" : "info"
        glyph: Model.Glyph.toolbox
        text: root.info.ddiMounted ? "Developer tools ready" : "Developer tools not loaded"
      }
      StatusPill {
        visible: !!root.svc.location
        level: "info"
        glyph: Model.Glyph.location
        text: root.svc.location ? "Simulated location" + (root.svc.location.label ? ": " + root.svc.location.label : "") : ""
      }
    }

    // fixes, shown only when something needs the user
    RowLayout {
      Layout.fillWidth: true
      spacing: Style.spacing.md
      visible: root.online && (root.info.developerMode === false || root.info.ddiMounted === false)
      AppText {
        Layout.fillWidth: true
        wrapMode: Text.WordWrap
        size: "small"; tone: "dim"
        text: root.info.developerMode === false
              ? "Developer Mode is needed to run your own apps."
              : "The screen view, location and app launching need the developer tools loaded on the iPhone (about 20 MB, once per restart)."
      }
      ActionButton {
        visible: root.info.developerMode === false
        text: "Turn on Developer Mode"; glyph: Model.Glyph.developer; kind: "primary"; compact: true
        onClicked: root.devModeHelp()
      }
      ActionButton {
        visible: root.info.developerMode !== false && root.info.ddiMounted === false
        text: "Load developer tools"; glyph: Model.Glyph.download; compact: true
        busy: root.svc.mountingDdi
        onClicked: root.svc.mountDdi()
      }
    }

    // unreachable / untrusted guidance
    RowLayout {
      Layout.fillWidth: true
      spacing: Style.spacing.md
      visible: !root.online && root.st !== "connecting" && root.st !== "searching"
      AppText {
        Layout.fillWidth: true
        wrapMode: Text.WordWrap
        size: "small"; tone: "dim"
        text: root.st === "untrusted" ? "Unlock your iPhone and tap “Trust”, or pair it again."
            : root.st === "not-paired" ? "This iPhone isn't paired with this computer yet."
            : root.st === "locked" ? "Unlock your iPhone to continue."
            : "Wake the iPhone and keep it on the same Wi-Fi, or plug it in." + (root.svc.connection.retryIn ? " Trying again in " + root.svc.connection.retryIn + " s." : "")
      }
      ActionButton {
        visible: root.st === "untrusted" || root.st === "not-paired"
        text: "Pair"; glyph: Model.Glyph.link; kind: "primary"; compact: true
        onClicked: root.app.nav("device", { action: "pair" })
      }
      ActionButton { text: "Try now"; glyph: Model.Glyph.refresh; compact: true; onClicked: root.svc.reconnect() }
    }

    // storage
    ColumnLayout {
      Layout.fillWidth: true
      Layout.topMargin: Style.spacing.md
      Layout.maximumWidth: Style.space(560)
      visible: !!root.store
      spacing: Style.spacing.sm
      ProgressTrack {
        Layout.fillWidth: true
        value: root.store && root.store.total ? root.store.used / root.store.total : 0
        level: root.store && root.store.total && root.store.free / root.store.total < 0.08 ? "warn" : ""
        label: root.store ? Model.formatBytes(root.store.free) + " free" : ""
        trailing: root.store ? "of " + Model.formatBytes(root.store.total) : ""
      }
    }
  }
}
