import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../common"
import "../../lib/Model.js" as Model

// Simulated GPS location for testing location-aware apps.
ColumnLayout {
  id: root
  required property var app
  readonly property var svc: app.device

  readonly property var presets: [
    { label: "Apple Park", lat: 37.3349, lon: -122.0090 },
    { label: "San Francisco", lat: 37.7749, lon: -122.4194 },
    { label: "New York", lat: 40.7128, lon: -74.0060 },
    { label: "London", lat: 51.5074, lon: -0.1278 },
    { label: "Paris", lat: 48.8566, lon: 2.3522 },
    { label: "Yerevan", lat: 40.1792, lon: 44.4991 },
    { label: "Tokyo", lat: 35.6762, lon: 139.6503 },
    { label: "Sydney", lat: -33.8688, lon: 151.2093 }
  ]

  function parse(s) { var v = parseFloat(String(s).replace(",", ".")); return isFinite(v) ? v : NaN }
  readonly property real latV: parse(latField.text)
  readonly property real lonV: parse(lonField.text)
  readonly property bool valid: isFinite(latV) && isFinite(lonV) && Math.abs(latV) <= 90 && Math.abs(lonV) <= 180

  function apply(label) { if (root.valid) root.svc.setLocation(root.latV, root.lonV, label || "") }

  spacing: Style.spacing.huge

  // current state
  RowLayout {
    Layout.fillWidth: true
    spacing: Style.spacing.xl
    Icon { glyph: Model.Glyph.location; size: Style.font.display; color: root.svc.location ? Commons.Color.accent : Theme.faint }
    ColumnLayout {
      Layout.fillWidth: true
      spacing: Style.spacing.xxs
      AppText {
        text: root.svc.location ? (root.svc.location.label || "Custom place") : "Real location"
        size: "heading"; strong: true
      }
      AppText {
        Layout.fillWidth: true
        tone: "dim"; wrapMode: Text.Wrap
        text: root.svc.location
              ? root.svc.location.lat.toFixed(4) + ", " + root.svc.location.lon.toFixed(4) + " — every app on the iPhone sees this until you restore it or restart the phone."
              : "Apps see where the iPhone really is. Pick a place to make them think it's somewhere else."
      }
    }
    ActionButton {
      visible: !!root.svc.location
      text: "Restore real location"; glyph: Model.Glyph.close
      busy: root.svc.locating
      onClicked: root.svc.clearLocation()
    }
  }

  SectionHeader { Layout.fillWidth: true; text: "Places" }
  Flow {
    Layout.fillWidth: true
    spacing: Style.spacing.md
    Repeater {
      model: root.presets
      delegate: ActionButton {
        required property var modelData
        text: modelData.label
        compact: true
        kind: root.svc.location && root.svc.location.label === modelData.label ? "primary" : "secondary"
        enabled: root.svc.connected && !root.svc.locating
        onClicked: {
          latField.text = String(modelData.lat); lonField.text = String(modelData.lon)
          root.svc.setLocation(modelData.lat, modelData.lon, modelData.label)
        }
      }
    }
  }

  SectionHeader { Layout.fillWidth: true; text: "Coordinates" }
  RowLayout {
    Layout.fillWidth: true
    spacing: Style.spacing.lg
    ColumnLayout {
      spacing: Style.spacing.xs
      AppText { text: "Latitude"; size: "small"; tone: "dim" }
      TextField { id: latField; Layout.preferredWidth: Style.space(160); placeholderText: "37.3349"; onAccepted: lonField.forceActiveFocus() }
    }
    ColumnLayout {
      spacing: Style.spacing.xs
      AppText { text: "Longitude"; size: "small"; tone: "dim" }
      TextField { id: lonField; Layout.preferredWidth: Style.space(160); placeholderText: "-122.0090"; onAccepted: root.apply() }
    }
    ActionButton {
      Layout.alignment: Qt.AlignBottom
      text: "Set location"; glyph: Model.Glyph.location; kind: "primary"
      enabled: root.valid && root.svc.connected
      busy: root.svc.locating
      onClicked: root.apply()
    }
    Item { Layout.fillWidth: true }
  }
  AppText {
    Layout.fillWidth: true
    visible: (latField.text !== "" || lonField.text !== "") && !root.valid
    text: "Latitude goes from -90 to 90 and longitude from -180 to 180, e.g. 51.5074 and -0.1278."
    size: "small"; tone: "warn"; wrapMode: Text.Wrap
  }
  AppText {
    Layout.fillWidth: true
    text: "Tip: in Google Maps, right-click a place and click the numbers to copy them."
    size: "small"; tone: "faint"; wrapMode: Text.Wrap
  }
}
