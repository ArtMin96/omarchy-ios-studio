pragma Singleton
import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Commons as Commons

// Semantic colours derived from the live theme.
//
// Omarchy themes only guarantee foreground/background/accent/urgent, and the
// named colours in colors.toml are NOT semantic (matte-black's `green` is
// amber). So success / warn / info are picked from colors.toml by hue, and
// synthesised in the theme's own saturation/lightness when no candidate is
// close enough. Re-derived whenever the theme changes.
//
//   import qs.components
//   color: Theme.success          // ok
//   color: Theme.levelColor("warn")
QtObject {
  id: root

  readonly property color fg: Commons.Color.foreground
  readonly property color bg: Commons.Color.background
  readonly property color accent: Commons.Color.accent
  readonly property color urgent: Commons.Color.urgent

  // Secondary / tertiary text, hairlines and surfaces.
  readonly property color dim: Qt.darker(fg, 1.5)
  readonly property color faint: Util.alpha(fg, 0.38)
  readonly property color hairline: Util.alpha(fg, 0.10)
  readonly property color surface: Util.alpha(fg, isDark ? 0.035 : 0.05)
  readonly property color surfaceRaised: Util.alpha(fg, isDark ? 0.06 : 0.08)
  readonly property color scrim: Util.alpha(bg, 0.72)
  readonly property bool isDark: _lum(bg) < 0.5

  property color success: "#7fb77e"
  property color warn: "#d9a44a"
  readonly property color error: urgent
  // The error hue at a saturation and lightness that stay red over any background: a pale urgent colour
  // tinted over a green theme otherwise mixes to brown. For fills and borders behind error content
  // (ErrorState; DiffView's removed lines use the same recipe). Text and glyphs keep `error`.
  readonly property color errorTint: Qt.hsla(urgent.hslHue, Math.max(urgent.hslSaturation, 0.7), 0.5, 1)
  readonly property color info: accent

  function levelColor(level) {
    switch (String(level || "")) {
    case "ok": case "ready": case "success": case "done": return root.success
    case "warn": case "warning": case "needs-setup": return root.warn
    case "error": case "failed": return root.error
    case "busy": case "loading": case "running": return root.accent
    default: return root.dim
    }
  }

  // Tinted background for pills/banners of a level.
  function levelFill(level) { return Util.alpha(levelColor(level), level === "info" || !level ? 0.08 : 0.14) }

  // ---------------------------------------------------------------- derive
  function _lum(c) { return 0.2126 * c.r + 0.7152 * c.g + 0.0722 * c.b }

  function _hsl(hex) {
    var c = Qt.color(hex)
    return { h: c.hslHue * 360, s: c.hslSaturation, l: c.hslLightness, c: c }
  }

  function _hueDist(a, b) { var d = Math.abs(a - b) % 360; return d > 180 ? 360 - d : d }

  function _pick(cands, target, avoidHues, maxDist) {
    var best = null, bestD = 999
    for (var i = 0; i < cands.length; i++) {
      var k = cands[i]
      if (k.s < 0.28 || k.l < 0.3 || k.l > 0.82) continue
      var bad = false
      for (var j = 0; j < avoidHues.length; j++) if (_hueDist(k.h, avoidHues[j]) < 14) bad = true
      if (bad) continue
      var d = _hueDist(k.h, target)
      if (d < bestD) { bestD = d; best = k }
    }
    return bestD <= maxDist ? best.c : null
  }

  function _synth(hue) {
    var a = _hsl(String(root.accent))
    var s = Math.max(0.38, Math.min(0.62, a.s * 0.8))
    var l = root.isDark ? Math.max(0.56, Math.min(0.68, _hsl(String(root.fg)).l * 0.85)) : 0.38
    return Qt.hsla(hue / 360, s, l, 1)
  }

  function derive(raw) {
    var cands = []
    var lines = String(raw || "").split("\n")
    for (var i = 0; i < lines.length; i++) {
      var m = lines[i].match(/^\s*([A-Za-z0-9_-]+)\s*=\s*["']?(#[0-9A-Fa-f]{6})/)
      if (!m || /background|foreground|selection|muted/.test(m[1])) continue
      cands.push(_hsl(m[2]))
    }
    var accentHue = _hsl(String(root.accent)).h
    var urgentHue = _hsl(String(root.urgent)).h
    root.success = _pick(cands, 135, [urgentHue], 35) || _synth(135)
    root.warn = _pick(cands, 42, [urgentHue, accentHue], 18) || _synth(_hueDist(accentHue, 42) < 16 ? 52 : 40)
  }

  property FileView _colors: FileView {
    path: Commons.Color.currentThemePath + "/colors.toml"
    printErrors: false
    onLoaded: root.derive(text())
  }

  property Connections _watch: Connections {
    target: Commons.Color
    function onAccentChanged() { root._colors.reload() }
    function onUrgentChanged() { root._colors.reload() }
    function onBackgroundChanged() { root._colors.reload() }
  }
}
