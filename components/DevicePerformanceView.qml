import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../Model.js" as Model

// iPhone page › Performance: measure one app over time (daemon metrics.*, docs/deviced-protocol.md).
// Toolbar (app, Start/Stop, time window, CSV), a report card with plain-language findings and key
// stats, live charts with event markers and a shared crosshair, and opt-in deeper checks (hitch
// tracking, launch profile, leak check) that go through app.confirm.
// The session lives in the daemon, so leaving the page doesn't stop it; reopening reattaches (and a
// session an AI agent started shows up here too).
ColumnLayout {
  id: root
  required property var app
  readonly property var svc: app.device
  readonly property bool narrow: width > 0 && width < Style.space(860)

  spacing: Style.spacing.huge

  // ---------------------------------------------------------------- state
  property var sess: null              // metrics session (describe())
  property var samples: []
  property var events: []
  property int revision: 0
  property var stats: null
  property string picked: ""           // bundle id in the picker
  property bool starting: false
  property bool stopping: false
  property var startError: null
  property string windowSel: "300"     // seconds shown, or "all"
  property real hoverTs: -1
  property double nowMs: Date.now()

  property var profile: null
  property bool profiling: false
  property var profileError: null
  property var leakReport: null
  property bool leaking: false
  property var leakError: null

  readonly property bool running: !!sess && sess.running
  readonly property var pickedApp: {
    for (var i = 0; i < svc.apps.length; i++) if (svc.apps[i].bundleId === root.picked) return svc.apps[i]
    return null
  }
  readonly property real t1: {
    root.revision
    return samples.length ? samples[samples.length - 1].ts : nowMs / 1000
  }
  readonly property real t0: {
    if (windowSel === "all" || !samples.length) return samples.length ? Math.min(samples[0].ts, t1 - 60) : t1 - 300
    return t1 - Number(windowSel)
  }
  readonly property bool hitching: !!sess && !!sess.hitchTracking && sess.hitchTracking.running && (sess.hitchTracking.until * 1000 > nowMs)

  // ---------------------------------------------------------------- app picker
  // Running apps first, your own (development-signed) apps marked, then by name.
  readonly property var appOptions: {
    var list = svc.apps.slice()
    list.sort(function(a, b) {
      if (!!a.running !== !!b.running) return a.running ? -1 : 1
      if (!!a.developer !== !!b.developer) return a.developer ? -1 : 1
      return String(a.name).toLowerCase() < String(b.name).toLowerCase() ? -1 : 1
    })
    return list.map(function(a) {
      var tags = []
      if (a.developer) tags.push("yours")
      if (a.running) tags.push("running")
      return { value: a.bundleId, label: a.name + (tags.length ? "  ·  " + tags.join(", ") : "") }
    })
  }

  function defaultPick() {
    if (root.picked !== "" && root.pickedApp) return
    var opts = root.appOptions
    if (opts.length) root.picked = opts[0].value
  }
  onAppOptionsChanged: defaultPick()

  // ---------------------------------------------------------------- daemon
  // The energy chart's "Other" band: network + location + overhead.
  function prep(r) {
    if (r.eOther === undefined && (r.eNet !== undefined || r.eLoc !== undefined || r.eOverhead !== undefined))
      r.eOther = Math.round(((r.eNet || 0) + (r.eLoc || 0) + (r.eOverhead || 0)) * 100) / 100
    return r
  }

  function attach(d) {
    root.sess = d
    root.samples = []
    root.events = []
    root.stats = null
    root.revision++
    if (d.bundleId) root.picked = d.bundleId
    var sid = d.sessionId
    svc.request("metrics.read", { sessionId: sid, limit: 4000 }, function(ok, page) {
      if (!ok || !root.sess || root.sess.sessionId !== sid) return
      var have = root.samples
      var rows = (page.entries || []).map(root.prep)
      var last = rows.length ? rows[rows.length - 1].ts : 0
      for (var i = 0; i < have.length; i++) if (have[i].ts > last) rows.push(have[i])
      root.samples = rows
      root.revision++
    })
    svc.request("metrics.read", { sessionId: sid, kind: "events", limit: 2000 }, function(ok, page) {
      if (!ok || !root.sess || root.sess.sessionId !== sid) return
      root.events = (page.entries || []).concat(root.events.filter(function(e) {
        var es = page.entries || []
        return !es.length || e.ts > es[es.length - 1].ts
      }))
    })
    refreshStats()
  }

  function refreshStats() {
    if (!root.sess) return
    var sid = root.sess.sessionId
    svc.request("metrics.snapshot", { sessionId: sid }, function(ok, d) {
      if (ok && root.sess && root.sess.sessionId === sid && d.stats) { root.stats = d.stats; root.sess = d.session }
    })
  }

  function reattach() {
    svc.request("metrics.list", {}, function(ok, d) {
      if (!ok || root.sess) return
      var list = (d && d.sessions) || []
      var pick = null
      for (var i = list.length - 1; i >= 0; i--) if (list[i].running) { pick = list[i]; break }
      if (!pick && list.length) pick = list[list.length - 1]
      if (pick) root.attach(pick)
    })
  }

  function start() {
    var a = root.pickedApp
    if (!a) return
    root.starting = true
    root.startError = null
    svc.request("metrics.start", { bundleId: a.bundleId, name: a.name }, function(ok, d) {
      root.starting = false
      if (ok) root.attach(d)
      else root.startError = d
    }, 90000)
  }

  function stop() {
    if (!root.sess) return
    root.stopping = true
    svc.request("metrics.stop", { sessionId: root.sess.sessionId }, function(ok, d) {
      root.stopping = false
      if (ok) { root.sess = d; if (d.summary) root.stats = d.summary }
      else root.app.toast(Model.errorMessage(d), "error")
    })
  }

  function exportCsv() {
    if (!root.sess) return
    svc.request("metrics.export", { sessionId: root.sess.sessionId }, function(ok, d) {
      if (!ok) { root.app.toast(Model.errorMessage(d), "error"); return }
      var name = d.path.split("/").pop()
      var dir = root.app.home + "/Downloads"
      Util.execArgv(["sh", "-c", "mkdir -p \"$1\" && cp -f \"$2\" \"$1/$3\"", "sh", dir, d.path, name])
      root.app.toast("Saved " + d.rows + " rows to Downloads as “" + name + "”", "ok")
    })
  }

  function trackHitches() {
    root.app.confirm({
      title: "Track hitches for 60 seconds?",
      message: "This reads every frame signpost on the iPhone, which is heavy: the phone and this computer work harder while it runs. It stops by itself after 60 seconds. Use your app's animations meanwhile.",
      confirmText: "Track hitches", cancelText: "Cancel", glyph: Model.Glyph.chart,
      onConfirm: function() {
        svc.request("metrics.hitches", { sessionId: root.sess.sessionId, seconds: 60 }, function(ok, d) {
          if (ok) root.sess = d
          else root.app.toast(Model.errorMessage(d), "error")
        })
      }
    })
  }

  function profileLaunch() {
    var a = root.pickedApp
    if (!a) return
    root.app.confirm({
      title: "Relaunch " + a.name + " to time it?",
      message: "iOS Studio quits " + a.name + ", starts it again and measures how long it takes to show its first frame. Anything unsaved in the app is lost.",
      confirmText: "Quit and relaunch", cancelText: "Cancel", destructive: true, glyph: Model.Glyph.rocket,
      onConfirm: function() {
        root.profiling = true
        root.profileError = null
        svc.request("metrics.launchProfile", { bundleId: a.bundleId }, function(ok, d) {
          root.profiling = false
          if (ok) root.profile = d
          else root.profileError = d
        }, 150000)
      }
    })
  }

  function checkLeaks() {
    var a = root.pickedApp
    if (!a) return
    root.app.confirm({
      title: "Check " + a.name + " for leaks?",
      message: a.name + " pauses for a few seconds while its memory is scanned. It has to be running, and it has to be your own app built by iOS Studio.",
      confirmText: "Check for leaks", cancelText: "Cancel", glyph: Model.Glyph.memory,
      onConfirm: function() {
        root.leaking = true
        root.leakError = null
        svc.request("metrics.leaks", { bundleId: a.bundleId }, function(ok, d) {
          root.leaking = false
          if (ok) root.leakReport = d
          else root.leakError = d
        }, 240000)
      }
    })
  }

  Connections {
    target: root.svc
    function onDaemonEvent(name, data) {
      if (!data) return
      if (name === "metrics.sample") {
        if (!root.sess || data.sessionId !== root.sess.sessionId) return
        var s = root.samples
        if (s.length && data.ts <= s[s.length - 1].ts) return
        s.push(root.prep(data))
        if (s.length > 3700) s.splice(0, s.length - 3700)
        root.revision++
      } else if (name === "metrics.event") {
        if (!root.sess || data.sessionId !== root.sess.sessionId) return
        root.events = root.events.concat([data]).slice(-2000)
        if (data.kind === "jetsam" || data.kind === "crash" || data.kind === "watchdog")
          root.app.notify(data.kind === "crash" ? "App crashed" : "App was killed", data.text, Model.Glyph.warn)
      } else if (name === "metrics.state") {
        if (root.sess && data.sessionId === root.sess.sessionId) root.sess = data
        else if ((!root.sess || !root.sess.running) && data.running) root.attach(data)
      }
    }
    function onConnStateChanged() { if (root.svc.connected && !root.sess) root.reattach() }
  }

  Timer { interval: 5000; repeat: true; running: root.running && root.visible; onTriggered: root.refreshStats() }
  Timer { interval: 1000; repeat: true; running: root.visible; onTriggered: root.nowMs = Date.now() }

  Component.onCompleted: {
    if (svc.connected && svc.appsStatus === "unknown") svc.refreshApps()
    defaultPick()
    reattach()
  }

  // ---------------------------------------------------------------- toolbar
  GridLayout {
    id: bar
    Layout.fillWidth: true
    columns: root.narrow ? 2 : 5
    columnSpacing: Style.spacing.lg
    rowSpacing: Style.spacing.md

    Dropdown {
      showLabel: false
      Layout.fillWidth: root.narrow
      Layout.preferredWidth: Style.space(300)
      enabled: !root.running && !root.starting
      value: root.picked
      options: root.appOptions
      onChanged: function(v) { root.picked = v }
    }
    ActionButton {
      text: root.running ? "Stop" : "Start measuring"
      glyph: root.running ? Model.Glyph.stop : Model.Glyph.play
      kind: root.running ? "secondary" : "primary"
      busy: root.starting || root.stopping
      enabled: root.svc.connected && (root.running || !!root.pickedApp)
      tooltip: root.running ? "Stop measuring (the report stays)" : "Sample this app about once a second"
      onClicked: root.running ? root.stop() : root.start()
    }
    AppText {
      Layout.fillWidth: true
      Layout.columnSpan: root.narrow ? 2 : 1
      elide: Text.ElideRight
      tone: root.sess && (root.sess.status === "reconnecting" || root.sess.status === "waiting-app") ? "warn" : "dim"
      size: "small"
      text: {
        var s = root.sess
        if (!s) return root.svc.connected ? "Pick an app and press Start." : "Connect your iPhone to measure an app."
        var dur = Model.formatDuration(Math.max(0, (s.stopped || root.nowMs / 1000) - s.started))
        if (s.status === "waiting-app") return "Waiting for " + s.name + " to open on the iPhone…"
        if (s.status === "reconnecting") return "Reconnecting to the iPhone…" + (s.error ? " " + s.error.message : "")
        if (s.status === "app-exited") return s.name + " quit. Start again to measure a new run."
        if (!s.running) return "Stopped. " + s.name + ", " + dur + ", " + Model.plural(s.samples, "sample")
        return "Measuring " + s.name + (s.pid ? " (pid " + s.pid + ")" : "") + " for " + dur
             + (s.owner && s.owner.indexOf("mcp") === 0 ? ", started by an AI agent" : "")
      }
    }
    ButtonGroupLite {
      visible: !!root.sess
      options: [{ value: "120", label: "2 min" }, { value: "300", label: "5 min" }, { value: "900", label: "15 min" }, { value: "all", label: "All" }]
      value: root.windowSel
      onPicked: function(v) { root.windowSel = v }
    }
    IconButton {
      visible: !!root.sess
      glyph: Model.Glyph.export
      tooltip: "Export every sample as CSV to Downloads"
      enabled: !!root.sess && root.samples.length > 0
      onClicked: root.exportCsv()
    }
  }

  ErrorState {
    Layout.fillWidth: true
    visible: !!root.startError
    banner: true
    app: root.app
    error: root.startError
    showRetry: true
    onRetry: root.start()
  }

  // a set that stopped answering for a while (the daemon resumes it by itself)
  Repeater {
    model: root.sess && root.sess.running && root.sess.notes ? root.sess.notes : []
    RowLayout {
      required property string modelData
      Layout.fillWidth: true
      spacing: Style.spacing.lg
      Icon { glyph: Model.Glyph.warn; color: Theme.warn; size: Style.font.icon }
      AppText { Layout.fillWidth: true; wrapMode: Text.WordWrap; size: "small"; tone: "dim"; text: modelData }
    }
  }

  // ---------------------------------------------------------------- empty
  EmptyState {
    Layout.fillWidth: true
    visible: !root.sess && !root.starting
    glyph: Model.Glyph.speedometer
    title: "See how your app performs"
    text: "CPU, memory, energy, frame rate, network, disk and battery, about once a second while you use the app, with a report of anything unusual: a possible leak, CPU spikes, dropped frames, hangs or the app being killed for memory."
  }

  // ---------------------------------------------------------------- report
  Card {
    Layout.fillWidth: true
    visible: !!root.sess
    title: "Report"
    subtitle: {
      var w = root.stats && root.stats.window
      if (!w || !w.samples) return "Collecting the first samples…"
      return Model.formatDuration(w.seconds || 0) + " measured, " + Model.plural(w.samples, "sample") + (root.running ? ", updates every 5 s" : "")
    }
    glyph: Model.Glyph.speedometer

    GridLayout {
      Layout.fillWidth: true
      columns: root.narrow ? 1 : 2
      columnSpacing: Style.spacing.huge * 2
      rowSpacing: Style.spacing.xl

      // findings
      ColumnLayout {
        Layout.fillWidth: true
        Layout.alignment: Qt.AlignTop
        spacing: Style.spacing.lg
        Repeater {
          model: root.stats ? root.stats.anomalies : []
          RowLayout {
            required property var modelData
            Layout.fillWidth: true
            spacing: Style.spacing.lg
            Icon {
              Layout.alignment: Qt.AlignTop
              glyph: Model.levelGlyph(modelData.level)
              color: Theme.levelColor(modelData.level === "info" ? "" : modelData.level)
              size: Style.font.icon
            }
            AppText { Layout.fillWidth: true; wrapMode: Text.WordWrap; text: modelData.text }
          }
        }
        QuietNote {
          Layout.fillWidth: true
          visible: !!root.stats && root.stats.window && root.stats.window.samples > 20 && root.stats.anomalies.length === 0
          glyph: Model.Glyph.checkCircle; level: "ok"
          title: "Nothing unusual so far"
          text: "No leak trend, CPU spikes, dropped frames, hangs or kills."
        }
        Skeleton { Layout.fillWidth: true; visible: !root.stats; lines: 3; rowHeight: Style.space(18); gap: Style.spacing.md }
        AppText {
          Layout.fillWidth: true; wrapMode: Text.WordWrap; size: "small"; tone: "faint"
          text: "Hangs are reported only when Hang Detection is on: on the iPhone, open Settings › Developer › Hang Detection. Frame rate is for the whole screen, not only your app."
        }
      }

      // key numbers
      GridLayout {
        id: table
        Layout.alignment: Qt.AlignTop
        Layout.fillWidth: root.narrow
        visible: !!root.stats
        columns: 4
        columnSpacing: Style.spacing.xxl
        rowSpacing: Style.spacing.sm
        readonly property var allRows: [
          { key: "cpu", label: "CPU", unit: "%" }, { key: "mem", label: "Memory", unit: "MB" },
          { key: "threads", label: "Threads", unit: "" }, { key: "energy", label: "Energy impact", unit: "" },
          { key: "fps", label: "Display FPS", unit: "" }, { key: "gpu", label: "GPU", unit: "%" },
          { key: "netRx", label: "Network in", unit: "KB/s" }, { key: "diskW", label: "Disk write", unit: "KB/s" },
          { key: "battMw", label: "Battery power", unit: "mW" }
        ]
        readonly property var shown: {
          var s = root.stats && root.stats.series ? root.stats.series : {}
          return allRows.filter(function(r) { return !!s[r.key] })
        }
        Repeater {
          model: ["", "Avg", "P95", "Max"]
          AppText { required property string modelData; text: modelData; size: "caption"; tone: "faint"; Layout.alignment: Qt.AlignRight }
        }
        Repeater {
          model: table.shown.length * 4
          AppText {
            required property int index
            readonly property var r: table.shown[Math.floor(index / 4)]
            readonly property int col: index % 4
            readonly property var st: root.stats.series[r.key]
            Layout.alignment: col === 0 ? Qt.AlignLeft : Qt.AlignRight
            size: "small"
            tone: col === 0 ? "dim" : "primary"
            font.features: { "tnum": 1 }
            text: col === 0 ? r.label + (r.unit ? " (" + r.unit + ")" : "")
                : String([null, st.avg, st.p95, st.max][col])
          }
        }
        AppText {
          Layout.columnSpan: 4
          Layout.topMargin: Style.spacing.md
          visible: text !== ""
          size: "small"; tone: "dim"
          text: {
            var b = root.stats && root.stats.battery
            if (!b) return ""
            var parts = []
            if (b.drainPctPerHour !== undefined) parts.push("Battery " + (b.charging ? "charging" : "drain " + b.drainPctPerHour + " %/h"))
            if (b.mWh !== undefined) parts.push(b.mWh + " mWh used")
            return parts.join(", ")
          }
        }
        AppText {
          Layout.columnSpan: 4
          visible: text !== ""
          size: "small"; tone: "dim"
          Layout.maximumWidth: Style.space(360)
          wrapMode: Text.WordWrap
          text: {
            var e = root.stats && root.stats.energy && root.stats.energy.byComponent
            if (!e) return ""
            var out = []
            for (var k in e) if (e[k].share) out.push(k + " " + e[k].share + "%")
            return out.length ? "Energy: " + out.join(", ") : ""
          }
        }
      }
    }
  }

  // ---------------------------------------------------------------- charts
  GridLayout {
    Layout.fillWidth: true
    visible: !!root.sess
    columns: root.narrow ? 1 : 2
    columnSpacing: Style.spacing.huge * 2
    rowSpacing: Style.spacing.huge * 1.5

    PerfChart {
      Layout.fillWidth: true
      title: "CPU"; unit: "%"; note: "of one core"; yFloor: 10
      series: [{ key: "cpu", label: "CPU" }]
      samples: root.samples; revision: root.revision; events: root.events; t0: root.t0; t1: root.t1
      hoverTs: root.hoverTs; onHoverAt: function(ts) { root.hoverTs = ts }
    }
    PerfChart {
      Layout.fillWidth: true
      title: "Memory"; unit: "MB"; yFloor: 10; zeroBase: false; note: "axis doesn't start at 0"
      series: [{ key: "mem", label: "Footprint" }, { key: "rss", label: "Resident", dashed: true }]
      samples: root.samples; revision: root.revision; events: root.events; t0: root.t0; t1: root.t1
      hoverTs: root.hoverTs; onHoverAt: function(ts) { root.hoverTs = ts }
    }
    PerfChart {
      Layout.fillWidth: true
      title: "Energy impact"; decimals: 1; stacked: true; yFloor: 1
      series: [{ key: "eCpu", label: "CPU" }, { key: "eGpu", label: "GPU" }, { key: "eDisplay", label: "Display" }, { key: "eOther", label: "Other" }]
      samples: root.samples; revision: root.revision; events: root.events; t0: root.t0; t1: root.t1
      hoverTs: root.hoverTs; onHoverAt: function(ts) { root.hoverTs = ts }
    }
    PerfChart {
      Layout.fillWidth: true
      title: "Display frame rate"; unit: "fps"; note: "whole screen"; yFloor: 60
      series: [{ key: "fps", label: "FPS" }]
      samples: root.samples; revision: root.revision; events: root.events; t0: root.t0; t1: root.t1
      hoverTs: root.hoverTs; onHoverAt: function(ts) { root.hoverTs = ts }
    }
    PerfChart {
      Layout.fillWidth: true
      title: "GPU"; unit: "%"; yFixed: 100
      series: [{ key: "gpu", label: "GPU" }]
      samples: root.samples; revision: root.revision; events: root.events; t0: root.t0; t1: root.t1
      hoverTs: root.hoverTs; onHoverAt: function(ts) { root.hoverTs = ts }
    }
    PerfChart {
      Layout.fillWidth: true
      title: "Threads"; yFloor: 4
      series: [{ key: "threads", label: "Threads" }]
      samples: root.samples; revision: root.revision; events: root.events; t0: root.t0; t1: root.t1
      hoverTs: root.hoverTs; onHoverAt: function(ts) { root.hoverTs = ts }
    }
    PerfChart {
      Layout.fillWidth: true
      title: "Network"; unit: "KB/s"; decimals: 1; yFloor: 1
      series: [{ key: "netRx", label: "In" }, { key: "netTx", label: "Out", dashed: true }]
      samples: root.samples; revision: root.revision; events: root.events; t0: root.t0; t1: root.t1
      hoverTs: root.hoverTs; onHoverAt: function(ts) { root.hoverTs = ts }
    }
    PerfChart {
      Layout.fillWidth: true
      title: "Disk"; unit: "KB/s"; decimals: 1; yFloor: 1
      series: [{ key: "diskR", label: "Read" }, { key: "diskW", label: "Write", dashed: true }]
      samples: root.samples; revision: root.revision; events: root.events; t0: root.t0; t1: root.t1
      hoverTs: root.hoverTs; onHoverAt: function(ts) { root.hoverTs = ts }
    }
    PerfChart {
      Layout.fillWidth: true
      title: "Wakeups"; unit: "/s"; yFloor: 10
      series: [{ key: "wakeups", label: "Wakeups" }]
      samples: root.samples; revision: root.revision; events: root.events; t0: root.t0; t1: root.t1
      hoverTs: root.hoverTs; onHoverAt: function(ts) { root.hoverTs = ts }
    }
    PerfChart {
      Layout.fillWidth: true
      title: "Battery power"; unit: "mW"; note: "whole phone"; yFloor: 100
      series: [{ key: "battMw", label: "Power" }]
      samples: root.samples; revision: root.revision; events: root.events; t0: root.t0; t1: root.t1
      hoverTs: root.hoverTs; onHoverAt: function(ts) { root.hoverTs = ts }
    }
  }

  RowLayout {
    Layout.fillWidth: true
    visible: !!root.sess
    spacing: Style.spacing.xxl
    AppText { text: Qt.formatTime(new Date(root.t0 * 1000), "HH:mm:ss"); size: "caption"; tone: "faint" }
    Item { Layout.fillWidth: true }
    Repeater {
      model: [{ level: "error", label: "Killed or crashed" }, { level: "warn", label: "Hang or hitch" }, { level: "", label: "Launch, exit, thermal" }]
      RowLayout {
        required property var modelData
        spacing: Style.spacing.sm
        Rectangle { implicitWidth: Style.space(8); implicitHeight: Style.space(8); radius: width / 2; color: modelData.level ? Theme.levelColor(modelData.level) : Theme.faint }
        AppText { text: modelData.label; size: "caption"; tone: "dim" }
      }
    }
    Item { Layout.fillWidth: true }
    AppText { text: root.running ? "now" : Qt.formatTime(new Date(root.t1 * 1000), "HH:mm:ss"); size: "caption"; tone: "faint" }
  }

  // ---------------------------------------------------------------- deeper checks
  SectionHeader { Layout.fillWidth: true; Layout.topMargin: Style.spacing.lg; text: "Deeper checks"; detail: root.pickedApp ? root.pickedApp.name : "" }

  GridLayout {
    Layout.fillWidth: true
    columns: root.narrow ? 1 : 3
    columnSpacing: Style.spacing.huge
    rowSpacing: Style.spacing.xl

    Repeater {
      model: [
        { key: "hitch", title: "Hitch tracking", glyph: Model.Glyph.chart,
          text: "60 seconds of frame-by-frame timing: every frame that took longer than two refreshes shows up as a hitch on the charts. Heavy, so it stops by itself." },
        { key: "launch", title: "Profile launch", glyph: Model.Glyph.rocket,
          text: "Quits and relaunches the app, then times loading, static initializers and the first frame. Loader timing needs your own app." },
        { key: "leaks", title: "Check for leaks", glyph: Model.Glyph.memory,
          text: "Scans the running app's memory for objects nothing points to any more. The app pauses for a few seconds. Your own apps only." }
      ]
      ColumnLayout {
        required property var modelData
        Layout.fillWidth: true
        Layout.alignment: Qt.AlignTop
        spacing: Style.spacing.md
        AppText { text: modelData.title; strong: true }
        AppText { Layout.fillWidth: true; wrapMode: Text.WordWrap; size: "small"; tone: "dim"; text: modelData.text }
        ActionButton {
          compact: true
          glyph: modelData.glyph
          text: modelData.key === "hitch" ? (root.hitching ? "Tracking… " + Math.max(0, Math.round(root.sess.hitchTracking.until - root.nowMs / 1000)) + " s" : "Track hitches")
              : modelData.key === "launch" ? "Profile launch" : "Check for leaks"
          busy: modelData.key === "launch" ? root.profiling : modelData.key === "leaks" ? root.leaking : false
          disabled: !(root.svc.connected && !!root.pickedApp && (modelData.key !== "hitch" || (root.running && !root.hitching && !!root.sess.pid)))
          tooltip: modelData.key === "hitch" && !root.running ? "Start measuring first" : ""
          onClicked: modelData.key === "hitch" ? root.trackHitches() : modelData.key === "launch" ? root.profileLaunch() : root.checkLeaks()
        }
      }
    }
  }

  ErrorState { Layout.fillWidth: true; visible: !!root.profileError; banner: true; app: root.app; error: root.profileError; title: "Launch profile failed" }
  ErrorState { Layout.fillWidth: true; visible: !!root.leakError; banner: true; app: root.app; error: root.leakError; title: "Leak check failed" }

  // launch profile result: the phases as proportional bars (one series, one hue)
  Card {
    id: launchCard
    Layout.fillWidth: true
    visible: !!root.profile
    title: "Launch of " + (root.profile ? root.profile.name : "")
    subtitle: root.profile ? root.profile.verdict : ""
    glyph: Model.Glyph.rocket
    actions: [ IconButton { glyph: Model.Glyph.close; tooltip: "Hide"; onClicked: root.profile = null } ]
    readonly property real maxMs: {
      var m = 1, ph = root.profile ? root.profile.phases : []
      for (var i = 0; i < ph.length; i++) m = Math.max(m, ph[i].ms)
      return m
    }
    Repeater {
      model: root.profile ? root.profile.phases : []
      RowLayout {
        id: phaseRow
        required property var modelData
        Layout.fillWidth: true
        spacing: Style.spacing.xl
        AppText { Layout.preferredWidth: Style.space(300); text: modelData.name; size: "small"; tone: "dim"; elide: Text.ElideRight }
        Item {
          Layout.fillWidth: true
          implicitHeight: Style.space(8)
          Rectangle {
            width: Math.max(Style.space(2), parent.width * phaseRow.modelData.ms / launchCard.maxMs)
            height: parent.height
            radius: Style.space(2)
            color: Commons.Color.accent
          }
        }
        AppText { Layout.preferredWidth: Style.space(70); horizontalAlignment: Text.AlignRight; text: Math.round(modelData.ms) + " ms"; size: "small"; font.features: { "tnum": 1 } }
      }
    }
    AppText {
      Layout.fillWidth: true; wrapMode: Text.WordWrap; size: "small"; tone: "faint"
      visible: !!root.profile && !!root.profile.note
      text: root.profile && root.profile.note ? root.profile.note : ""
    }
  }

  Card {
    Layout.fillWidth: true
    visible: !!root.leakReport
    title: root.leakReport ? (root.leakReport.leakCount ? Model.plural(root.leakReport.leakCount, "leak") + " in " + root.leakReport.name : "No leaks in " + root.leakReport.name) : ""
    subtitle: root.leakReport ? "Scanned in " + root.leakReport.seconds + " s" : ""
    glyph: Model.Glyph.memory
    glyphColor: root.leakReport && root.leakReport.leakCount ? Theme.warn : Theme.success
    actions: [ IconButton { glyph: Model.Glyph.close; tooltip: "Hide"; onClicked: root.leakReport = null } ]
    AppText { Layout.fillWidth: true; wrapMode: Text.WordWrap; text: root.leakReport ? root.leakReport.verdict : "" }
    Repeater {
      model: root.leakReport && root.leakReport.leaks ? root.leakReport.leaks : []
      RowLayout {
        required property var modelData
        Layout.fillWidth: true
        AppText { Layout.fillWidth: true; text: modelData.type; size: "small" }
        AppText { text: Model.plural(modelData.count, "object") + ", " + Model.formatBytes(modelData.bytes); size: "small"; tone: "dim"; font.features: { "tnum": 1 } }
      }
    }
    RowLayout {
      visible: !!root.leakReport && !!root.leakReport.memgraphPath
      spacing: Style.spacing.lg
      ActionButton {
        compact: true; glyph: Model.Glyph.folderOpen; text: "Show the memory graph"
        onClicked: Util.execArgv(["xdg-open", root.leakReport.memgraphPath.substring(0, root.leakReport.memgraphPath.lastIndexOf("/"))])
      }
      AppText { size: "small"; tone: "faint"; text: "Open the .memgraph in Xcode or with leaks on a Mac for sizes and stacks." }
    }
  }
}
