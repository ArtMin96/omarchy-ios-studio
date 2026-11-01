import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import "../Model.js" as Model

// One metric over time for the iPhone page's Performance tab: a header (title, latest value, legend)
// and a Canvas plot with one y-axis from zero, event markers and a crosshair shared across charts.
//   PerfChart {
//     title: "Memory"; unit: "MB"; samples: view.samples; revision: view.revision; t0: view.t0; t1: view.t1
//     series: [{ key: "mem", label: "Footprint" }, { key: "rss", label: "Resident", dashed: true }]
//     events: view.events; hoverTs: view.hoverTs; onHoverAt: function(ts) { view.hoverTs = ts }
//   }
// series[i].color defaults to the accent (first) / the dim ink (others); `stacked: true` stacks the series
// as areas with categorical colours from PerfChart.categorical(i). Samples are rows with `ts` (unix s)
// and the series keys; gaps longer than `gapS` break the line.
ColumnLayout {
  id: root

  property string title: ""
  property string unit: ""
  property string note: ""
  property var series: []
  property bool stacked: false
  property var samples: []
  property int revision: 0
  property var events: []
  property real t0: 0
  property real t1: 0
  property real yFloor: 1          // the axis top is at least this
  property real yFixed: 0          // fixed axis top (0 = automatic)
  property bool zeroBase: true     // false: the axis starts near the lowest value (memory, where the trend matters)
  property int decimals: 0
  property real gapS: 3.5
  property real hoverTs: -1
  property int plotHeight: Style.space(112)

  signal hoverAt(real ts)

  spacing: Style.spacing.md

  // ---------------------------------------------------------------- colours
  // Categorical slots for stacked charts: fixed OKLCH hues (blue, orange, magenta, green) at a lightness
  // picked for the theme's light/dark background, so they follow the theme. Checked with the dataviz
  // validator (all pairs pass; blue/magenta is in the 6-8 band, so stacked bands keep a gap and a legend).
  readonly property var _hues: [255, 65, 330, 160]
  function categorical(i) {
    var L = Theme.isDark ? 0.64 : 0.55
    var h = _hues[i % _hues.length] * Math.PI / 180
    for (var c = 0.13; c > 0; c -= 0.005) {
      var a = c * Math.cos(h), b = c * Math.sin(h)
      var l_ = L + 0.3963377774 * a + 0.2158037573 * b, m_ = L - 0.1055613458 * a - 0.0638541728 * b,
          s_ = L - 0.0894841775 * a - 1.2914855480 * b
      var l = l_ * l_ * l_, m = m_ * m_ * m_, s = s_ * s_ * s_
      var lin = [4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
                 -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
                 -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s]
      var ok = true
      for (var k = 0; k < 3; k++) {
        lin[k] = lin[k] <= 0.0031308 ? 12.92 * lin[k] : 1.055 * Math.pow(lin[k], 1 / 2.4) - 0.055
        if (lin[k] < 0 || lin[k] > 1) ok = false
      }
      if (ok) return Qt.rgba(lin[0], lin[1], lin[2], 1)
    }
    return Theme.dim
  }

  function colorOf(i) {
    var s = root.series[i]
    if (s && s.color) return s.color
    if (root.stacked) return categorical(i)
    return i === 0 ? Commons.Color.accent : Theme.dim
  }

  function css(c, a) {
    var q = Qt.color(c)
    return "rgba(" + Math.round(q.r * 255) + "," + Math.round(q.g * 255) + "," + Math.round(q.b * 255) + "," + (a === undefined ? q.a : a) + ")"
  }

  function eventColor(kind) {
    if (kind === "jetsam" || kind === "crash" || kind === "watchdog") return Theme.error
    if (kind === "hang" || kind === "hitch") return Theme.warn
    return Theme.faint
  }

  // ---------------------------------------------------------------- data helpers
  function fmt(v) {
    if (v === undefined || v === null || isNaN(v)) return "–"
    var d = Math.abs(v) >= 100 ? 0 : root.decimals
    return Number(v).toFixed(d)
  }

  // index of the first sample with ts >= t
  function lowerBound(t) {
    var a = root.samples, lo = 0, hi = a.length
    while (lo < hi) { var mid = (lo + hi) >> 1; if (a[mid].ts < t) lo = mid + 1; else hi = mid }
    return lo
  }

  function sampleAt(t) {
    var a = root.samples
    if (!a.length || t < 0) return null
    var i = lowerBound(t)
    var best = null
    for (var j = Math.max(0, i - 1); j <= Math.min(a.length - 1, i); j++)
      if (!best || Math.abs(a[j].ts - t) < Math.abs(best.ts - t)) best = a[j]
    return best && Math.abs(best.ts - t) <= root.gapS ? best : null
  }

  function eventNear(t) {
    var best = null, span = (root.t1 - root.t0) / Math.max(1, plot.width) * 6
    for (var i = 0; i < root.events.length; i++) {
      var e = root.events[i]
      var d = Math.abs(e.ts - t)
      if (d <= Math.max(1.5, span) && (!best || d < Math.abs(best.ts - t))) best = e
    }
    return best
  }

  function valueOf(row, i) {
    if (!row) return undefined
    var v = row[root.series[i].key]
    return (typeof v === "number") ? v : undefined
  }

  function stackTop(row) {
    var s = 0, any = false
    for (var i = 0; i < root.series.length; i++) { var v = valueOf(row, i); if (v !== undefined) { s += v; any = true } }
    return any ? s : undefined
  }

  readonly property var latest: {
    root.revision
    return root.samples.length ? root.samples[root.samples.length - 1] : null
  }
  readonly property var hoverRow: root.hoverTs > 0 ? sampleAt(root.hoverTs) : null

  function niceTop(v) {
    if (root.yFixed > 0) return Math.max(root.yFixed, v)
    v = Math.max(v, root.yFloor)
    var p = Math.pow(10, Math.floor(Math.log(v) / Math.LN10))
    var steps = [1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10]
    for (var i = 0; i < steps.length; i++) if (steps[i] * p >= v * 1.05) return steps[i] * p
    return 10 * p
  }

  // ---------------------------------------------------------------- header
  RowLayout {
    Layout.fillWidth: true
    spacing: Style.spacing.lg
    AppText { text: root.title; strong: true }
    AppText { visible: root.note !== ""; text: root.note; size: "caption"; tone: "faint" }
    Item { Layout.fillWidth: true }
    Repeater {
      model: root.series.length > 1 ? root.series : []
      RowLayout {
        spacing: Style.spacing.sm
        required property var modelData
        required property int index
        Canvas {
          // legend swatch: the line style (solid/dashed) or the band colour
          implicitWidth: Style.space(14); implicitHeight: Style.space(10)
          property color c: root.colorOf(index)
          onCChanged: requestPaint()
          onPaint: {
            var ctx = getContext("2d"); ctx.reset()
            if (root.stacked) { ctx.fillStyle = root.css(c); ctx.fillRect(0, 1, width, height - 2); return }
            ctx.strokeStyle = root.css(c); ctx.lineWidth = 2
            if (modelData.dashed) ctx.setLineDash([3, 2])
            ctx.beginPath(); ctx.moveTo(0, height / 2); ctx.lineTo(width, height / 2); ctx.stroke()
          }
        }
        AppText { text: modelData.label; size: "caption"; tone: "dim" }
      }
    }
    AppText {
      // the value under the crosshair, else the latest one
      readonly property var row: root.hoverRow || root.latest
      text: {
        if (!row) return "–"
        var v = root.stacked ? root.stackTop(row) : root.valueOf(row, 0)
        return root.fmt(v) + (root.unit ? " " + root.unit : "")
      }
      font.features: { "tnum": 1 }
      tone: root.hoverRow ? "accent" : "primary"
      strong: true
    }
  }

  // ---------------------------------------------------------------- plot
  Item {
    id: plot
    Layout.fillWidth: true
    Layout.preferredHeight: root.plotHeight

    property real yTop: 1
    property real yLow: 0

    Canvas {
      id: canvas
      anchors.fill: parent
      renderStrategy: Canvas.Cooperative

      property int rev: root.revision
      onRevChanged: requestPaint()
      onWidthChanged: requestPaint()
      Connections {
        target: root
        function onT0Changed() { canvas.requestPaint() }
        function onSeriesChanged() { canvas.requestPaint() }
        function onEventsChanged() { canvas.requestPaint() }
      }
      Connections { target: Commons.Color; function onAccentChanged() { canvas.requestPaint() } function onForegroundChanged() { canvas.requestPaint() } }

      onPaint: {
        var ctx = getContext("2d")
        ctx.reset()
        var W = width, H = height, a = root.samples
        var t0 = root.t0, t1 = Math.max(root.t1, t0 + 1)
        var labelW = Style.space(4)
        var bottom = H - 1
        // visible range, thinned to about one point per pixel
        var i0 = Math.max(0, root.lowerBound(t0) - 1)
        var n = a.length - i0
        var stride = Math.max(1, Math.floor(n / Math.max(1, W)))
        var top = 0, low = Infinity
        for (var i = i0; i < a.length; i++) {
          var v = root.stacked ? root.stackTop(a[i]) : undefined
          if (!root.stacked) for (var k = 0; k < root.series.length; k++) { var x = root.valueOf(a[i], k); if (x !== undefined && (v === undefined || x > v)) v = x }
          if (v !== undefined && v > top) top = v
          if (!root.zeroBase && !root.stacked) for (var k2 = 0; k2 < root.series.length; k2++) { var x2 = root.valueOf(a[i], k2); if (x2 !== undefined && x2 < low) low = x2 }
        }
        var lo = 0
        if (!root.zeroBase && low !== Infinity && low > 0) {
          var span = Math.max(top - low, root.yFloor)
          var stepP = Math.pow(10, Math.floor(Math.log(span) / Math.LN10))
          lo = Math.max(0, Math.floor((low - span * 0.15) / stepP) * stepP)
          top = lo + root.niceTop(top - lo)
        } else top = root.niceTop(top)
        plot.yTop = top
        plot.yLow = lo
        function X(t) { return (t - t0) / (t1 - t0) * W }
        function Y(v) { return bottom - ((v - lo) / (top - lo)) * (bottom - Style.space(2)) }
        // one stacked band: filled to its base, with a surface-coloured gap on its top edge
        var band = function(seg) {
          if (seg.length < 2) return
          ctx.beginPath(); ctx.moveTo(seg[0][0], seg[0][1])
          for (var u = 1; u < seg.length; u++) ctx.lineTo(seg[u][0], seg[u][1])
          for (var w = seg.length - 1; w >= 0; w--) ctx.lineTo(seg[w][0], seg[w][2])
          ctx.closePath(); ctx.fill()
          ctx.strokeStyle = root.css(Commons.Color.background); ctx.lineWidth = 1.5
          ctx.beginPath(); ctx.moveTo(seg[0][0], seg[0][1])
          for (var z = 1; z < seg.length; z++) ctx.lineTo(seg[z][0], seg[z][1])
          ctx.stroke()
        }

        // recessive grid: baseline, half, top
        ctx.lineWidth = 1
        ctx.strokeStyle = root.css(Theme.hairline)
        for (var g = 0; g <= 2; g++) {
          var gy = Math.round(Y(lo + (top - lo) * g / 2)) + 0.5
          ctx.beginPath(); ctx.moveTo(0, gy); ctx.lineTo(W, gy); ctx.stroke()
        }

        // event markers: serious ones full height, hitches as short ticks at the bottom
        for (var e = 0; e < root.events.length; e++) {
          var ev = root.events[e]
          if (ev.ts < t0 || ev.ts > t1) continue
          var ex = Math.round(X(ev.ts)) + 0.5
          ctx.strokeStyle = root.css(root.eventColor(ev.kind), ev.kind === "hitch" ? 0.9 : 0.55)
          ctx.setLineDash(ev.kind === "launch" || ev.kind === "exit" ? [2, 3] : [])
          ctx.beginPath()
          ctx.moveTo(ex, ev.kind === "hitch" ? bottom - Style.space(6) : 0)
          ctx.lineTo(ex, bottom); ctx.stroke()
          if (ev.kind !== "hitch" && ev.kind !== "launch" && ev.kind !== "exit") {
            ctx.fillStyle = root.css(root.eventColor(ev.kind))
            ctx.beginPath(); ctx.arc(ex, Style.space(4), Style.space(3), 0, Math.PI * 2); ctx.fill()
          }
        }
        ctx.setLineDash([])

        if (root.stacked) {
          // bands from the top series down, each a filled area to the running sum below it
          var sums = []
          for (var s = root.series.length - 1; s >= 0; s--) {
            ctx.fillStyle = root.css(root.colorOf(s), 0.9)
            var pts = []
            for (var j = i0; j < a.length; j += stride) {
              var row = a[j]
              if (row.ts > t1) break
              var base = 0
              for (var q = 0; q < s; q++) { var bv = root.valueOf(row, q); if (bv !== undefined) base += bv }
              var sv = root.valueOf(row, s)
              if (sv === undefined) continue
              pts.push([X(row.ts), Y(base + sv), Y(base), row.ts])
            }
            // draw as segments broken at gaps
            var seg = []
            for (var p = 0; p < pts.length; p++) {
              if (seg.length && pts[p][3] - seg[seg.length - 1][3] > root.gapS * stride) { band(seg); seg = [] }
              seg.push(pts[p])
            }
            band(seg)
          }
        } else {
          for (var si = root.series.length - 1; si >= 0; si--) {
            ctx.strokeStyle = root.css(root.colorOf(si))
            ctx.lineWidth = 2
            ctx.lineJoin = "round"
            ctx.setLineDash(root.series[si].dashed ? [4, 3] : [])
            ctx.beginPath()
            var started = false, last = -1
            for (var r = i0; r < a.length; r += stride) {
              var rr = a[r]
              if (rr.ts > t1) break
              var vv = root.valueOf(rr, si)
              if (vv === undefined) { started = false; continue }
              var px = X(rr.ts), py = Y(vv)
              if (!started || rr.ts - last > root.gapS * stride) { ctx.moveTo(px, py); started = true }
              else ctx.lineTo(px, py)
              last = rr.ts
            }
            ctx.stroke()
          }
          ctx.setLineDash([])
        }

        // crosshair
        if (root.hoverTs > 0 && root.hoverTs >= t0 && root.hoverTs <= t1) {
          var hx = Math.round(X(root.hoverTs)) + 0.5
          ctx.strokeStyle = root.css(Commons.Color.foreground, 0.35); ctx.lineWidth = 1
          ctx.beginPath(); ctx.moveTo(hx, 0); ctx.lineTo(hx, bottom); ctx.stroke()
        }
      }
      Connections { target: root; function onHoverTsChanged() { canvas.requestPaint() } }
    }

    // axis labels (text ink, never the series colour)
    AppText {
      anchors.left: parent.left; anchors.top: parent.top; anchors.leftMargin: Style.spacing.xs
      text: root.fmt(plot.yTop) + (root.unit ? " " + root.unit : "")
      size: "caption"; tone: "faint"; font.features: { "tnum": 1 }
    }
    AppText {
      anchors.left: parent.left; anchors.leftMargin: Style.spacing.xs
      y: parent.height / 2 - height
      text: root.fmt(plot.yLow + (plot.yTop - plot.yLow) / 2)
      size: "caption"; tone: "faint"; font.features: { "tnum": 1 }
    }

    AppText {
      visible: !root.zeroBase
      anchors.left: parent.left; anchors.bottom: parent.bottom; anchors.leftMargin: Style.spacing.xs; anchors.bottomMargin: Style.spacing.xs
      text: root.fmt(plot.yLow)
      size: "caption"; tone: "faint"; font.features: { "tnum": 1 }
    }

    HoverHandler {
      id: hover
      onPointChanged: {
        if (!hovered) return
        root.hoverAt(root.t0 + point.position.x / Math.max(1, plot.width) * (root.t1 - root.t0))
      }
      onHoveredChanged: if (!hovered) root.hoverAt(-1)
    }

    // tooltip
    Rectangle {
      id: tip
      readonly property var row: root.hoverRow
      readonly property var ev: root.hoverTs > 0 ? root.eventNear(root.hoverTs) : null
      visible: hover.hovered && (!!row || !!ev)
      color: Commons.Color.tooltip.background
      border.color: Theme.hairline
      radius: Style.cornerRadius
      width: Math.min(Style.space(260), tipCol.implicitWidth + Style.spacing.lg * 2)
      height: tipCol.implicitHeight + Style.spacing.md * 2
      z: 5
      readonly property real hx: (root.hoverTs - root.t0) / Math.max(1, root.t1 - root.t0) * plot.width
      x: hx + Style.spacing.lg + width > plot.width ? Math.max(0, hx - width - Style.spacing.lg) : hx + Style.spacing.lg
      y: Style.spacing.sm
      ColumnLayout {
        id: tipCol
        anchors.fill: parent
        anchors.margins: Style.spacing.md
        anchors.leftMargin: Style.spacing.lg
        anchors.rightMargin: Style.spacing.lg
        spacing: Style.spacing.xs
        AppText {
          text: tip.row ? Qt.formatTime(new Date(tip.row.ts * 1000), "HH:mm:ss") : (tip.ev ? Qt.formatTime(new Date(tip.ev.ts * 1000), "HH:mm:ss") : "")
          size: "caption"; tone: "dim"
        }
        Repeater {
          model: tip.row ? root.series : []
          AppText {
            required property var modelData
            required property int index
            text: modelData.label + "  " + root.fmt(root.valueOf(tip.row, index)) + (root.unit ? " " + root.unit : "")
            size: "small"; font.features: { "tnum": 1 }
          }
        }
        AppText {
          visible: !!tip.ev
          Layout.fillWidth: true
          Layout.maximumWidth: Style.space(236)
          wrapMode: Text.WordWrap
          text: tip.ev ? tip.ev.text : ""
          size: "small"
          tone: tip.ev && (tip.ev.kind === "jetsam" || tip.ev.kind === "crash" || tip.ev.kind === "watchdog") ? "error"
              : (tip.ev && (tip.ev.kind === "hang" || tip.ev.kind === "hitch") ? "warn" : "dim")
          maximumLineCount: 4
          elide: Text.ElideRight
        }
      }
    }
  }
}
