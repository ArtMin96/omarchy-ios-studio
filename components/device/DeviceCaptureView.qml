import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../common"
import "../../core"
import "../../lib/Model.js" as Model

// iPhone › Network › Packet capture: record an app's (or the whole phone's) traffic to a pcapng file and
// summarize it from the packet headers. USB only. The running capture's state is app.device.capture
// (the daemon's `pcap` events), so it survives switching tabs.
ColumnLayout {
  id: root
  required property var app
  readonly property var svc: app.device
  readonly property bool usb: !!svc.current && svc.current.transport === "usb"
  readonly property var cap: svc.capture || { running: false }
  readonly property bool running: svc.capturing
  readonly property var summary: cap.summary || null
  readonly property bool demo: app.demo !== ""

  property string target: ""          // bundle id; "" = whole iPhone
  property int seconds: 30
  property bool starting: false
  property bool stopping: false
  property var startError: null
  property bool active: false          // on screen (DeviceNetworkView passes its own)
  readonly property bool wiresharkInstalled: CommandCheck.found.wireshark === true

  spacing: Style.spacing.xl

  readonly property var appOptions: {
    var opts = [{ value: "", label: "Whole iPhone" }]
    var mine = svc.apps.filter(function(a) { return a.developer })
    var others = svc.apps.filter(function(a) { return !a.developer && !a.system })
    for (var i = 0; i < mine.length; i++) opts.push({ value: mine[i].bundleId, label: mine[i].name + " (yours)" })
    for (var j = 0; j < others.length; j++) opts.push({ value: others[j].bundleId, label: others[j].name })
    return opts
  }

  function start() {
    root.starting = true
    root.startError = null
    var args = { maxSeconds: root.seconds }
    if (root.target !== "") args.bundleId = root.target
    root.svc.request("pcap.start", args, function(ok, d) {
      root.starting = false
      if (ok) root.svc.capture = d
      else root.startError = d
    }, 60000)
  }

  function stop() {
    root.stopping = true
    root.svc.request("pcap.stop", {}, function(ok, d) {
      root.stopping = false
      if (ok) root.svc.capture = d
      else root.app.toastError(d)
    })
  }

  function openWireshark() { Util.execArgv(["wireshark", root.cap.file]) }
  function showInFolder() { root.app.showInFolder(root.cap.file) }

  function stopText(r) {
    return { time: "Stopped at the time limit", size: "Stopped at the size limit", stopped: "Stopped",
             "client-gone": "Stopped because iOS Studio closed", "tunnel-lost": "Stopped: the cable was unplugged or the connection dropped",
             shutdown: "Stopped: the iPhone service stopped", error: "Stopped by an error" }[r] || "Finished"
  }

  // The daemon remembers the running or last capture: show it after a restart of the window too.
  function syncStatus() {
    if (!root.svc.daemonReady) return
    root.svc.request("pcap.status", {}, function(ok, d) { if (ok && (d.running || d.file)) root.svc.capture = d })
  }
  // Each time it's shown: is Wireshark installed (yet), and the daemon's capture.
  function activate() {
    CommandCheck.check("wireshark")
    if (!root.cap.file) syncStatus()
  }
  onActiveChanged: if (active) activate()
  Component.onCompleted: if (active) activate()

  // ---------------------------------------------------------------- header
  ColumnLayout {
    Layout.fillWidth: true
    spacing: Style.spacing.md
    AppText { text: "Packet capture"; size: "heading"; strong: true }
    AppText {
      Layout.fillWidth: true
      wrapMode: Text.WordWrap; tone: "dim"
      text: "Record what an app sends and receives, and see which servers it talks to. The recording is a standard file you can open in Wireshark. The summary below reads only the packet headers, never their contents."
    }
  }

  // ---------------------------------------------------------------- USB only
  EmptyState {
    Layout.fillWidth: true
    visible: !root.usb && !root.running
    compact: true
    glyph: Model.Glyph.usb
    title: "Packet capture needs a USB cable"
    text: "Your iPhone is connected over Wi-Fi, and it only shares its network traffic over a cable. Plug it in and this turns on by itself."
    actionText: root.demo ? "Plug in the cable (demo)" : ""
    actionGlyph: Model.Glyph.usb
    onAction: root.svc.request("demo.transport", { transport: "usb" }, null)
  }

  // ---------------------------------------------------------------- controls
  RowLayout {
    Layout.fillWidth: true
    visible: root.usb || root.running
    spacing: Style.spacing.lg
    Dropdown {
      showLabel: false
      Layout.preferredWidth: Style.space(240)
      enabled: !root.running
      value: root.target
      options: root.appOptions
      onChanged: function(v) { root.target = v }
    }
    ButtonGroupLite {
      enabled: !root.running
      options: [{ value: "10", label: "10 s" }, { value: "30", label: "30 s" }, { value: "60", label: "1 min" }, { value: "300", label: "5 min" }]
      value: String(root.seconds)
      onPicked: function(v) { root.seconds = Number(v) }
    }
    Item { Layout.fillWidth: true }
    ActionButton {
      text: root.running ? "Stop" : "Start capture"
      glyph: root.running ? Model.Glyph.stop : Model.Glyph.record
      kind: root.running ? "danger" : "secondary"
      enabled: root.svc.connected || root.running
      busy: root.starting || root.stopping
      onClicked: root.running ? root.stop() : root.start()
    }
    ActionButton {
      visible: root.demo && root.usb && !root.running
      text: "Unplug (demo)"; kind: "ghost"; compact: true
      onClicked: root.svc.request("demo.transport", { transport: "wifi" }, null)
    }
  }

  ErrorState {
    Layout.fillWidth: true
    visible: !!root.startError
    app: root.app
    error: root.startError
    banner: true
    onRetry: root.start()
  }

  // ---------------------------------------------------------------- live
  ColumnLayout {
    Layout.fillWidth: true
    visible: root.running
    spacing: Style.spacing.lg
    ProgressTrack {
      Layout.fillWidth: true
      value: root.cap.maxSeconds ? Math.min(1, (root.cap.seconds || 0) / root.cap.maxSeconds) : 0
      label: "Recording " + (root.cap.filter && root.cap.filter.process ? root.cap.filter.process : "the whole iPhone") + "…"
      trailing: Math.round(root.cap.seconds || 0) + " of " + (root.cap.maxSeconds || 0) + " s"
    }
    KeyValueList {
      Layout.fillWidth: true
      rows: [
        { label: "Packets", value: String(root.cap.packets || 0) },
        { label: "Data", value: Model.formatBytes(root.cap.bytes || 0) }
      ].concat((root.cap.byProcess || []).map(function(p) {
        return { label: p.process, value: p.packets + " packets · " + Model.formatBytes(p.bytes) }
      }))
    }
    AppText {
      Layout.fillWidth: true; visible: (root.cap.packets || 0) === 0
      text: "Nothing yet. Use the app now so it goes online."
      tone: "faint"; size: "small"; wrapMode: Text.WordWrap
    }
  }

  // ---------------------------------------------------------------- result
  ColumnLayout {
    Layout.fillWidth: true
    visible: !root.running && !!root.cap.file && !!root.summary
    spacing: Style.spacing.lg

    ErrorState {
      Layout.fillWidth: true
      visible: !!root.cap.error
      app: root.app
      error: root.cap.error || null
      banner: true
      showRetry: false
    }

    SectionHeader {
      Layout.fillWidth: true
      text: "Last capture"
      detail: root.cap.endedAt ? Model.relativeTime(root.cap.endedAt) : ""
      trailing: [
        ActionButton {
          visible: root.wiresharkInstalled
          text: "Open in Wireshark"; glyph: Model.Glyph.openExternal; compact: true
          onClicked: root.openWireshark()
        },
        ActionButton {
          visible: !root.wiresharkInstalled
          text: "Install Wireshark"; glyph: Model.Glyph.download; compact: true; kind: "ghost"
          tooltip: "Optional: Setup installs it"
          onClicked: root.app.openSetupStep("wireshark")
        },
        IconButton { glyph: Model.Glyph.folderOpen; tooltip: "Show in folder"; onClicked: root.showInFolder() },
        IconButton {
          glyph: Model.Glyph.copy; tooltip: "Copy the file's path"
          onClicked: root.app.copyText(root.cap.file, "the file's path")
        }
      ]
    }

    KeyValueList {
      Layout.fillWidth: true
      rows: root.summary ? [
        { label: "Recorded", value: (root.cap.filter && root.cap.filter.process ? root.cap.filter.process : "Whole iPhone")
                                    + " · " + Math.round(root.summary.seconds) + " s · " + root.stopText(root.cap.stopReason) },
        { label: "Packets", value: root.summary.packets + " · " + Model.formatBytes(root.summary.bytes) },
        { label: "Sent / received", value: Model.formatBytes(root.summary.sentBytes || 0) + " / " + Model.formatBytes(root.summary.receivedBytes || 0) },
        { label: "Protocols", value: Object.keys(root.summary.protocols || {}).map(function(k) { return k + " " + root.summary.protocols[k] }).join(" · ") || "–" },
        { label: "Services", value: Object.keys(root.summary.services || {}).slice(0, 6).map(function(k) { return k + " " + root.summary.services[k] }).join(" · ") || "–" },
        { label: "File", value: root.cap.file, copy: true }
      ] : []
      onCopied: function(v) { root.app.toast("Copied") }
    }

    QuietNote {
      Layout.fillWidth: true
      visible: !!root.summary && root.summary.packets === 0
      glyph: Model.Glyph.info
      title: "No traffic was recorded"
      text: "The app didn't go online while recording. Start a capture, then use the app."
    }

    SectionHeader {
      Layout.fillWidth: true
      visible: !!root.summary && (root.summary.byProcess || []).length > 1
      text: "By process"
    }
    Repeater {
      model: root.summary && root.summary.byProcess.length > 1 ? root.summary.byProcess : []
      delegate: ListRow {
        required property var modelData
        Layout.fillWidth: true
        glyph: Model.Glyph.apps
        title: modelData.process
        meta: modelData.packets + " packets · " + Model.formatBytes(modelData.bytes)
      }
    }

    SectionHeader {
      Layout.fillWidth: true
      visible: !!root.summary && (root.summary.topHosts || []).length > 0
      text: "Servers it talked to"
      detail: root.summary ? String(root.summary.hostCount) : ""
    }
    Repeater {
      model: root.summary ? root.summary.topHosts : []
      delegate: ListRow {
        required property var modelData
        Layout.fillWidth: true
        glyph: Model.Glyph.network
        title: modelData.host + (modelData.port !== null && modelData.port !== undefined ? " : " + modelData.port : "")
        subtitle: (modelData.service || modelData.protocol)
        meta: modelData.packets + " packets · " + Model.formatBytes(modelData.bytes)
      }
    }
    AppText {
      Layout.fillWidth: true
      visible: !!root.summary && root.summary.excludedTunnelPackets > 0
      text: "Left out: " + (root.summary ? root.summary.excludedTunnelPackets : 0) + " packets of iOS Studio's own connection to the phone."
      tone: "faint"; size: "small"; wrapMode: Text.WordWrap
    }
  }
}
