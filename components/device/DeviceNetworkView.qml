import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../common"
import "../../core"
import "../../lib/Model.js" as Model

// iPhone › Network: simulate a slow network, heat or a throttled GPU (DVT condition inducer) and capture
// the phone's traffic (DeviceCaptureView). Protocol: condition.* / pcap.* in docs/deviced-protocol.md.
// The active condition lives in app.device (condition, conditionActive) because the page header and the
// sidebar show it too.
ColumnLayout {
  id: root
  required property var app
  readonly property var svc: app.device
  property bool active: false               // the tab is on screen (DevicePage)
  readonly property bool wifi: (svc.current && svc.current.transport === "wifi") || svc.connection.transport === "wifi"

  // the conditions the phone offers, reloaded each time the tab is shown
  Resource {
    id: conditions
    active: root.active && root.svc.connected
    autoLoad: true
    initial: ({ groups: [] })
    fetch: root.svc.fetcher("condition.list")
  }
  readonly property var groups: (conditions.data && conditions.data.groups) || []
  property string picked: ""                // profile id
  property int minutes: 5
  property bool applying: false
  property bool clearing: false
  property real now: Date.now() / 1000

  readonly property var pickedProfile: {
    for (var i = 0; i < groups.length; i++)
      for (var j = 0; j < groups[i].profiles.length; j++)
        if (groups[i].profiles[j].id === picked) return Object.assign({ groupName: groups[i].name, kind: groups[i].kind }, groups[i].profiles[j])
    return null
  }
  readonly property var cond: svc.condition
  readonly property int remaining: cond ? Math.max(0, Math.round((cond.expiresAt || 0) - now)) : 0

  spacing: Style.spacing.huge

  function groupGlyph(kind) {
    return kind === "thermal" ? Model.Glyph.thermometer : kind === "gpu" ? Model.Glyph.chip : Model.Glyph.speed
  }

  function apply() {
    var p = root.pickedProfile
    if (!p) return
    var wifiNote = p.note && root.wifi ? "\n\n" + p.note : ""
    root.app.confirm({
      title: "Simulate " + p.name + "?",
      message: "This affects the whole iPhone, every app included, not only yours"
               + (p.summary ? " (" + p.summary + ")" : "") + ". It switches itself off after "
               + root.minutes + (root.minutes === 1 ? " minute" : " minutes")
               + ", when you press Reset now, or when you close iOS Studio." + wifiNote,
      confirmText: "Apply", cancelText: "Cancel", destructive: false, glyph: root.groupGlyph(p.kind),
      onConfirm: function() {
        root.applying = true
        root.svc.request("condition.set", { profile: p.id, timeoutS: root.minutes * 60, acceptWifiRisk: true }, function(ok, d) {
          root.applying = false
          if (ok) root.app.toast("Your iPhone now simulates " + p.name, "ok")
          else root.app.toastError(d)
        }, 60000)
      }
    })
  }

  function reset() {
    root.clearing = true
    root.svc.request("condition.clear", {}, function(ok, d) {
      root.clearing = false
      if (ok) root.app.toast("Your iPhone is back to normal", "ok")
      else root.app.toastError(d)
    })
  }

  Timer { interval: 1000; repeat: true; running: root.active && root.svc.conditionActive; onTriggered: root.now = Date.now() / 1000 }
  onActiveChanged: if (active) root.now = Date.now() / 1000
  Connections {
    target: root.svc
    function onDaemonEvent(name, data) {
      if (name === "condition" || (name === "connection" && data && data.transport)) {
        root.now = Date.now() / 1000
        if (name === "connection") conditions.load()   // Wi-Fi rules follow the transport
      }
    }
  }

  // ---------------------------------------------------------------- active banner
  Rectangle {
    Layout.fillWidth: true
    visible: root.svc.conditionActive
    radius: Style.cornerRadius
    color: Theme.levelFill("warn")
    border.color: Theme.levelColor("warn")
    border.width: Style.normalBorderWidth
    implicitHeight: banner.implicitHeight + Style.spacing.xl * 2

    ColumnLayout {
      id: banner
      anchors.fill: parent
      anchors.margins: Style.spacing.xl
      spacing: Style.spacing.lg
      RowLayout {
        Layout.fillWidth: true
        spacing: Style.spacing.xl
        Icon { glyph: root.cond ? root.groupGlyph(root.cond.kind) : Model.Glyph.speed; size: Style.font.display; color: Theme.levelColor("warn") }
        ColumnLayout {
          Layout.fillWidth: true
          spacing: Style.spacing.xxs
          AppText {
            Layout.fillWidth: true
            text: root.cond ? "Simulating " + root.cond.name : ""
            size: "heading"; strong: true; elide: Text.ElideRight
          }
          AppText {
            Layout.fillWidth: true
            wrapMode: Text.Wrap; tone: "dim"
            text: root.cond ? (root.cond.groupName + (root.cond.summary ? " · " + root.cond.summary : "")
                               + ". The whole iPhone is affected.") : ""
          }
        }
        ColumnLayout {
          spacing: Style.spacing.xxs
          Layout.alignment: Qt.AlignVCenter
          AppText { Layout.alignment: Qt.AlignRight; text: Model.clock(root.remaining); size: "title"; strong: true }
          AppText { Layout.alignment: Qt.AlignRight; text: "until it resets"; size: "caption"; tone: "dim" }
        }
        ActionButton {
          text: "Reset now"; glyph: Model.Glyph.close; kind: "primary"
          busy: root.clearing
          onClicked: root.reset()
        }
      }
      ProgressTrack {
        Layout.fillWidth: true
        level: "warn"
        value: root.cond && root.cond.timeoutS ? root.remaining / root.cond.timeoutS : 0
        thickness: Style.space(3)
      }
    }
  }

  // ---------------------------------------------------------------- picker
  ColumnLayout {
    Layout.fillWidth: true
    spacing: Style.spacing.md

    AppText { text: "Test how your app copes"; size: "heading"; strong: true }
    AppText {
      Layout.fillWidth: true
      wrapMode: Text.Wrap; tone: "dim"
      text: "Make the iPhone act as if it's on a slow network, running hot or with a slow GPU. Pick one below; only one is on at a time, and it always switches itself off."
    }
    RowLayout {
      Layout.topMargin: Style.spacing.sm
      spacing: Style.spacing.lg
      AppText { text: "Switch off after"; tone: "dim"; size: "small" }
      ButtonGroupLite {
        options: [{ value: "1", label: "1 min" }, { value: "5", label: "5 min" }, { value: "10", label: "10 min" }, { value: "30", label: "30 min" }]
        value: String(root.minutes)
        onPicked: function(v) { root.minutes = Number(v) }
      }
    }
  }

  QuietNote {
    Layout.fillWidth: true
    visible: root.wifi && conditions.status === "ready"
    glyph: Model.Glyph.wifi; level: "warn"
    title: "Your iPhone is connected over Wi-Fi"
    text: "Network conditions also slow down this computer's connection to the phone, and the two that cut the network off are only available over a USB cable. Heat and GPU states are fine either way."
  }

  StateView {
    Layout.fillWidth: true
    app: root.app
    resource: conditions
    isEmpty: function(d) { return !d || !d.groups || d.groups.length === 0 }
    errorTitle: "Couldn't read the conditions"
    emptyGlyph: Model.Glyph.speed
    emptyTitle: "No conditions offered"
    emptyText: "This iPhone doesn't offer any conditions to simulate."
    skeletonLines: 6; skeletonRowHeight: Style.space(44)
    contentSpacing: Style.spacing.huge

    Repeater {
      model: root.groups
      delegate: ColumnLayout {
        id: grp
        required property var modelData
        Layout.fillWidth: true
        spacing: Style.spacing.xs
        SectionHeader { Layout.fillWidth: true; text: grp.modelData.name; detail: String(grp.modelData.profiles.length) }
        Repeater {
          model: grp.modelData.profiles
          delegate: ListRow {
            id: row
            required property var modelData
            property bool hov: false
            Layout.fillWidth: true
            opacity: modelData.allowed ? 1 : 0.55
            glyph: root.groupGlyph(grp.modelData.kind)
            glyphColor: current ? Commons.Color.accent : Theme.dim
            title: modelData.name
            subtitle: modelData.summary || ""
            current: root.picked === modelData.id
            hasCursor: hov && modelData.allowed
            onHovered: function(h) { row.hov = h }
            onClicked: if (modelData.allowed) root.picked = modelData.id
            trailing: [
              StatusPill {
                visible: !row.modelData.allowed
                quiet: true; level: "error"; glyph: Model.Glyph.usb
                text: "USB only"
              },
              StatusPill {
                visible: root.svc.conditionActive && root.cond.profile === row.modelData.id
                level: "warn"; text: "Active"
              },
              ActionButton {
                visible: row.current && !(root.svc.conditionActive && root.cond.profile === row.modelData.id)
                text: root.svc.conditionActive ? "Switch to this" : "Apply"
                glyph: Model.Glyph.play; kind: "primary"; compact: true
                enabled: root.svc.connected && !root.applying
                busy: root.applying
                tooltip: root.svc.conditionActive ? "Replaces the active condition" : ""
                onClicked: root.apply()
              }
            ]
          }
        }
      }
    }
  }

  // ---------------------------------------------------------------- capture
  DeviceCaptureView {
    Layout.fillWidth: true
    Layout.topMargin: Style.spacing.huge
    app: root.app
    active: root.active && visible
  }
}
