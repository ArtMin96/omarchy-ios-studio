.pragma library

// Shared pure helpers for iOS Studio. No Qt types in here, so everything is
// trivially testable and safe to import from any page, service or component:
//   import "../Model.js" as Model      (from pages/, services/, components/)
//   import "Model.js" as Model         (from the repo root)

function g(cp) { return String.fromCodePoint(cp) }

// Nerd Font (Material Design range) glyphs. Add new ones at the end.
var Glyph = {
  home: g(0xF02DC),
  rocket: g(0xF14DE),
  setup: g(0xF14DE),
  apple: g(0xF0035),
  account: g(0xF0009),
  phone: g(0xF011C),
  phoneLink: g(0xF0951),
  hammer: g(0xF08EA),
  build: g(0xF1322),
  play: g(0xF040A),
  pause: g(0xF03E4),
  stop: g(0xF04DB),
  logs: g(0xF09ED),
  console: g(0xF018D),
  crash: g(0xF00E4),
  bug: g(0xF00E4),
  gear: g(0xF0493),
  settings: g(0xF0493),
  sdk: g(0xF03D7),
  box: g(0xF03D6),
  toolbox: g(0xF0328),
  wrench: g(0xF05B7),
  folder: g(0xF024B),
  folderOpen: g(0xF0770),
  folderPlus: g(0xF0257),
  file: g(0xF0214),
  check: g(0xF012C),
  checkCircle: g(0xF05E0),
  circle: g(0xF0766),
  dot: g(0xF0765),
  warn: g(0xF0026),
  error: g(0xF0159),
  info: g(0xF02FD),
  help: g(0xF02D7),
  refresh: g(0xF0450),
  sync: g(0xF04E6),
  download: g(0xF01DA),
  upload: g(0xF0552),
  trash: g(0xF01B4),
  link: g(0xF0337),
  linkOff: g(0xF0338),
  wifi: g(0xF05A9),
  wifiOff: g(0xF05AA),
  usb: g(0xF0553),
  battery: g(0xF0079),
  batteryCharging: g(0xF0084),
  lock: g(0xF033E),
  unlock: g(0xF033F),
  key: g(0xF0306),
  cert: g(0xF0124),
  shield: g(0xF0498),
  shieldCheck: g(0xF0565),
  clock: g(0xF0150),
  timer: g(0xF13AB),
  location: g(0xF034E),
  camera: g(0xF0100),
  image: g(0xF02E9),
  video: g(0xF0567),
  arrowUp: g(0xF005D),
  search: g(0xF0349),
  keyboard: g(0xF030C),
  close: g(0xF0156),
  plus: g(0xF0415),
  minus: g(0xF0374),
  chevronRight: g(0xF0142),
  chevronLeft: g(0xF0141),
  chevronDown: g(0xF0140),
  chevronUp: g(0xF0143),
  arrowRight: g(0xF0054),
  openExternal: g(0xF03CC),
  copy: g(0xF018F),
  save: g(0xF0193),
  export: g(0xF0207),
  edit: g(0xF03EB),
  code: g(0xF0169),
  swift: g(0xF06E5),
  disk: g(0xF02CA),
  memory: g(0xF035B),
  cloudDownload: g(0xF0162),
  broom: g(0xF00E2),
  bell: g(0xF009A),
  bellOff: g(0xF009B),
  eye: g(0xF0208),
  eyeOff: g(0xF0209),
  filter: g(0xF0232),
  star: g(0xF04CE),
  dots: g(0xF01D9),
  window: g(0xF05AF),
  apps: g(0xF003B),
  package: g(0xF03D6),
  signature: g(0xF0124),
  developer: g(0xF0174),
  heart: g(0xF02D1),
  sparkles: g(0xF0674),
  terminal: g(0xF018D),
  layers: g(0xF0328),
  briefcase: g(0xF00D6),
  idCard: g(0xF05D2),
  xml: g(0xF05C0),
  robot: g(0xF06A9),
  testTube: g(0xF0668),
  asterisk: g(0xF06C4),
  hexagon: g(0xF02D9),
  consoleLine: g(0xF07B7),
  cursorArrow: g(0xF01BF),
  starFour: g(0xF0AE2),
  lightning: g(0xF140B),
  network: g(0xF06F3),
  speed: g(0xF04C5),
  thermometer: g(0xF050F),
  chip: g(0xF061A),
  record: g(0xF044A),
  speedometer: g(0xF04C5),
  chart: g(0xF012A),
  translate: g(0xF05CA),
  tune: g(0xF062E)
}

// Sidebar order. `service` names the AppState property whose `summary`
// drives the sidebar badge. `file` is the page component under pages/.
var PAGES = [
  { key: "home",      label: "Home",      glyph: Glyph.home,     service: "",          file: "HomePage.qml",      hint: "Everything at a glance" },
  { key: "setup",     label: "Setup",     glyph: Glyph.rocket,   service: "setup",     file: "SetupPage.qml",     hint: "Install and configure the toolchain" },
  { key: "account",   label: "Apple ID",  glyph: Glyph.apple,    service: "account",   file: "AccountPage.qml",   hint: "Team, certificates and profiles" },
  { key: "device",    label: "iPhone",    glyph: Glyph.phone,    service: "device",    file: "DevicePage.qml",    hint: "Your connected iPhone" },
  { key: "projects",  label: "Projects",  glyph: Glyph.hammer,   service: "projects",  file: "ProjectsPage.qml",  hint: "Build and run your apps" },
  { key: "logs",      label: "Logs",      glyph: Glyph.logs,     service: "logs",      file: "LogsPage.qml",      hint: "Device log, crashes and build logs" },
  { key: "toolchain", label: "Toolchain", glyph: Glyph.toolbox,  service: "toolchain", file: "ToolchainPage.qml", hint: "Versions, SDKs and disk usage" },
  { key: "settings",  label: "Settings",  glyph: Glyph.gear,     service: "",          file: "SettingsPage.qml",  hint: "Preferences" }
]

function pageIndex(key) {
  for (var i = 0; i < PAGES.length; i++) if (PAGES[i].key === key) return i
  return -1
}

function pageByKey(key) {
  var i = pageIndex(key)
  return i >= 0 ? PAGES[i] : null
}

// Aliases accepted by nav() / the `ios-studio <page>` deep link.
var PAGE_ALIASES = {
  overview: "home", onboarding: "setup", wizard: "setup",
  appleid: "account", "apple-id": "account", team: "account", signing: "account",
  iphone: "device", phone: "device", devices: "device",
  project: "projects", builds: "projects", build: "projects",
  log: "logs", crashes: "logs", syslog: "logs",
  sdk: "toolchain", tools: "toolchain",
  preferences: "settings", prefs: "settings"
}

// Aliases that also pick a tab on the target page (`ios-studio crashes`).
var ALIAS_PARAMS = {
  crashes: { tab: "crashes" }, syslog: { tab: "device" }
}

// "logs:crashes", "logs/crashes", "device:screen:mirror" (page:tab:action) or an alias → { page, params }.
function parseDeepLink(link) {
  var s = String(link || "").trim()
  var m = s.match(/^([^:\/\s]+)\s*[:\/]\s*(.+)$/)
  var name = m ? m[1] : s
  var page = resolvePage(name)
  var params = {}
  var implied = ALIAS_PARAMS[name.toLowerCase()]
  if (implied) for (var k in implied) params[k] = implied[k]
  if (m) {
    var rest = m[2].trim().toLowerCase().split(/[:\/]/)   // "device:screen:mirror" → tab + action
    params.tab = rest[0]
    if (rest[1]) params.action = rest[1]
  }
  return { page: page, params: params }
}

function resolvePage(name) {
  var key = String(name || "").trim().toLowerCase()
  if (pageIndex(key) >= 0) return key
  if (PAGE_ALIASES[key]) return PAGE_ALIASES[key]
  return ""
}

// Single source for the `?` sheet (and README).
var SHORTCUTS = [
  { group: "Navigate", keys: "1 – 8", text: "Jump to a page" },
  { group: "Navigate", keys: "Ctrl Tab", text: "Next / previous page" },
  { group: "Navigate", keys: "j k  ↑ ↓", text: "Move the cursor" },
  { group: "Navigate", keys: "Enter", text: "Open or run the highlighted item" },
  { group: "Navigate", keys: "Esc", text: "Close the top sheet, clear search, or go back" },
  { group: "Actions", keys: "r", text: "Refresh this page" },
  { group: "Actions", keys: "/", text: "Search on this page" },
  { group: "Actions", keys: "x", text: "Remove the highlighted item (asks first)" },
  { group: "Actions", keys: "c", text: "Copy the highlighted value" },
  { group: "Help", keys: "?", text: "Show or hide this sheet" }
]

function shortcutGroups() {
  var out = [], idx = {}
  for (var i = 0; i < SHORTCUTS.length; i++) {
    var s = SHORTCUTS[i]
    if (idx[s.group] === undefined) { idx[s.group] = out.length; out.push({ title: s.group, entries: [] }) }
    out[idx[s.group]].entries.push(s)
  }
  return out
}

// ------------------------------------------------------------ formatting

function formatBytes(n, digits) {
  var v = Number(n)
  if (!isFinite(v) || v < 0) return "—"
  var units = ["B", "KB", "MB", "GB", "TB"]
  var i = 0
  while (v >= 1000 && i < units.length - 1) { v /= 1000; i++ }
  var d = digits === undefined ? (v >= 100 || i === 0 ? 0 : 1) : digits
  return v.toFixed(d) + " " + units[i]
}

function _toMs(t) {
  if (t === null || t === undefined || t === "") return NaN
  if (typeof t === "number") return t < 1e12 ? t * 1000 : t   // seconds or ms
  var ms = Date.parse(String(t))
  return ms
}

function plural(n, word) { return n + " " + word + (n === 1 ? "" : "s") }

// "just now", "5 minutes ago", "in 3 days", "yesterday"
function relativeTime(t, nowMs) {
  var ms = _toMs(t)
  if (!isFinite(ms)) return ""
  var now = nowMs === undefined ? Date.now() : nowMs
  var diff = Math.round((ms - now) / 1000)
  var future = diff > 0
  var s = Math.abs(diff)
  var text
  if (s < 45) return future ? "in a moment" : "just now"
  if (s < 90) text = "a minute"
  else if (s < 3600) text = plural(Math.round(s / 60), "minute")
  else if (s < 5400) text = "an hour"
  else if (s < 86400) text = plural(Math.round(s / 3600), "hour")
  else if (s < 172800) return future ? "tomorrow" : "yesterday"
  else if (s < 86400 * 30) text = plural(Math.round(s / 86400), "day")
  else if (s < 86400 * 365) text = plural(Math.round(s / (86400 * 30)), "month")
  else text = plural(Math.round(s / (86400 * 365)), "year")
  return future ? "in " + text : text + " ago"
}

// "1:05", "12 min", "2 h 5 min" — for durations in seconds.
function formatDuration(sec) {
  var s = Math.max(0, Math.round(Number(sec) || 0))
  if (s < 60) return s + " s"
  if (s < 3600) return Math.round(s / 60) + " min"
  var h = Math.floor(s / 3600), m = Math.round((s % 3600) / 60)
  return h + " h" + (m ? " " + m + " min" : "")
}

// Signing expiry: free-team profiles expire every 7 days.
// -> { level: "ok"|"warn"|"error"|"info", text: "expires in 3 days", days }
function expiryStatus(dateIso, nowMs) {
  var ms = _toMs(dateIso)
  if (!isFinite(ms)) return { level: "info", text: "no expiry date", days: NaN }
  var now = nowMs === undefined ? Date.now() : nowMs
  var hours = (ms - now) / 3600000
  var days = Math.floor(hours / 24)
  var text, level
  if (hours <= 0) {
    var ago = Math.ceil(-hours / 24)
    text = ago <= 1 ? "expired" : "expired " + plural(ago, "day") + " ago"
    level = "error"
  } else if (hours < 24) {
    text = hours < 2 ? "expires within the hour" : "expires in " + plural(Math.round(hours), "hour")
    level = "error"
  } else if (hours < 48) {
    text = "expires tomorrow"; level = "warn"
  } else {
    text = "expires in " + plural(days, "day")
    level = days <= 3 ? "warn" : "ok"
  }
  return { level: level, text: text, days: days }
}

// Normalise anything into one of the four levels the UI paints.
function level(v) {
  var s = String(v || "").toLowerCase()
  if (s === "ok" || s === "ready" || s === "success" || s === "done") return "ok"
  if (s === "warn" || s === "warning" || s === "needs-setup") return "warn"
  if (s === "error" || s === "failed" || s === "fail") return "error"
  if (s === "loading" || s === "running" || s === "busy") return "busy"
  return "info"
}

function levelGlyph(lv) {
  switch (level(lv)) {
  case "ok": return Glyph.checkCircle
  case "warn": return Glyph.warn
  case "error": return Glyph.error
  case "busy": return Glyph.sync
  default: return Glyph.info
  }
}

// Plain-language fallback when a helper failed without a usable message.
function errorMessage(err) {
  if (!err) return "Something went wrong."
  if (typeof err === "string") return err
  return String(err.message || err.code || "Something went wrong.")
}

function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)) }

function matches(haystack, query) {
  var q = String(query || "").trim().toLowerCase()
  if (!q) return true
  return String(haystack || "").toLowerCase().indexOf(q) >= 0
}

function greeting(nowMs) {
  var h = new Date(nowMs === undefined ? Date.now() : nowMs).getHours()
  if (h < 5) return "Up late"
  if (h < 12) return "Good morning"
  if (h < 18) return "Good afternoon"
  return "Good evening"
}

// Map a Qt key event to a cursor action for page handleKey() implementations.
// Uses raw key codes (Qt.Key_Down = 0x01000015 …) so this file stays Qt-free.
function keyAction(event) {
  var k = event.key, t = event.text
  if (k === 0x01000015 || t === "j") return "down"
  if (k === 0x01000013 || t === "k") return "up"
  if (k === 0x01000012 || t === "h") return "left"
  if (k === 0x01000014 || t === "l") return "right"
  if (k === 0x01000004 || k === 0x01000005 || k === 0x20) return "activate"   // Return, Enter, Space
  if (t === "x" || k === 0x01000007) return "delete"                            // x, Delete
  if (t === "c") return "copy"
  if (k === 0x01000010 || t === "g") return "home"
  if (k === 0x01000011 || t === "G") return "end"
  if (k === 0x01000016) return "pageUp"
  if (k === 0x01000017) return "pageDown"
  return ""
}

// ---------------------------------------------------------------- run presets (helper/run_presets.py)
// Saved launch arguments, environment and language per project. These mirror the helper's rules so
// the editor can preview and validate as you type; the helper still checks everything on save.
var PRESET_LANGUAGES = [
  { value: "en", label: "English", region: "en_US" },
  { value: "de", label: "German", region: "de_DE" },
  { value: "fr", label: "French", region: "fr_FR" },
  { value: "es", label: "Spanish", region: "es_ES" },
  { value: "it", label: "Italian", region: "it_IT" },
  { value: "pt-BR", label: "Portuguese (Brazil)", region: "pt_BR" },
  { value: "nl", label: "Dutch", region: "nl_NL" },
  { value: "ru", label: "Russian", region: "ru_RU" },
  { value: "ja", label: "Japanese", region: "ja_JP" },
  { value: "ko", label: "Korean", region: "ko_KR" },
  { value: "zh-Hans", label: "Chinese (Simplified)", region: "zh_CN" },
  { value: "ar", label: "Arabic (right to left)", region: "ar_SA" },
  { value: "he", label: "Hebrew (right to left)", region: "he_IL" }
]
var PRESET_MAX_NAME = 40
var PRESET_LANGUAGE_RE = /^[a-z]{2,3}(-[A-Z][a-z]{3})?(-([A-Z]{2}|[0-9]{3}))?$/
var PRESET_REGION_RE = /^[a-z]{2,3}(_[A-Z][a-z]{3})?(_([A-Z]{2}|[0-9]{3}))?$/
var ENV_NAME_RE = /^[A-Za-z_][A-Za-z0-9_]*$/

// "German" for "de", the code itself for anything not in the short list.
function languageLabel(code) {
  for (var i = 0; i < PRESET_LANGUAGES.length; i++) if (PRESET_LANGUAGES[i].value === code) return PRESET_LANGUAGES[i].label.replace(/ \(right to left\)$/, "")
  return String(code || "")
}

// The helper's spelling fix-ups: "de-de" -> "de-DE" (sep "-") or "de_DE" (sep "_"); "" when empty.
function presetTag(value, sep) {
  var s = String(value || "").trim()
  if (s === "") return ""
  var parts = s.split(/[-_]/)
  var out = [parts[0].toLowerCase()]
  for (var i = 1; i < parts.length; i++) {
    var p = parts[i]
    if (p.length === 4 && /^[A-Za-z]+$/.test(p)) out.push(p.charAt(0).toUpperCase() + p.slice(1).toLowerCase())
    else if (/^[A-Za-z]+$/.test(p)) out.push(p.toUpperCase())
    else out.push(p)
  }
  return out.join(sep)
}

// A preset -> { args, env } as app.launch gets it: its own args, then the generated ones.
function presetResolve(p) {
  p = p || {}
  var args = (p.args || []).slice()
  var lang = presetTag(p.language, "-"), region = presetTag(p.region, "_")
  if (lang) args.push("-AppleLanguages", "(" + lang + ")")
  if (region) args.push("-AppleLocale", region)
  if (p.uiTestMode) args.push("-UITestMode", "YES")
  return { args: args, env: Object.assign({}, p.env || {}) }
}

// Short description for menus and rows: "German · de_DE · 2 arguments · UI test mode".
function presetSummary(p) {
  if (!p) return ""
  var parts = []
  if (p.language) parts.push(languageLabel(p.language))
  if (p.region) parts.push(p.region)
  var n = (p.args || []).length, e = Object.keys(p.env || {}).length
  if (n) parts.push(plural(n, "argument"))
  if (e) parts.push(plural(e, "variable"))
  if (p.uiTestMode) parts.push("UI test mode")
  return parts.length ? parts.join(" · ") : "No changes from a plain launch"
}

// A project's preset names (projects list items carry runPresets as names or objects).
function presetNames(project) {
  var list = (project && project.runPresets) || []
  return list.map(function(x) { return typeof x === "string" ? x : String((x && x.name) || "") }).filter(function(n) { return n !== "" })
}
