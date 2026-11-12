.pragma library

// Shared pure helpers for iOS Studio. No Qt types in here, so everything is
// trivially testable and safe to import from any page, service or component:
//   import "lib/Model.js" as Model           (from the repo root)
//   import "../lib/Model.js" as Model        (from pages/, services/, core/)
//   import "../../lib/Model.js" as Model     (from components/<folder>/)

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
  tune: g(0xF062E),
  // UI inspector (DeviceInspectView): element roles and audit issue types
  inspect: g(0xF1276),
  accessibility: g(0xF02E6),
  tap: g(0xF0741),
  text: g(0xF0284),
  textbox: g(0xF060E),
  toggle: g(0xF0521),
  slider: g(0xF1542),
  header: g(0xF0274),
  tab: g(0xF04E9),
  list: g(0xF0572),
  element: g(0xF0763),
  linkVariant: g(0xF0339),
  contrast: g(0xF0197),
  textSize: g(0xF027F),
  clipped: g(0xF019F),
  hitArea: g(0xF0CFD),
  label: g(0xF0316),
  // screenshot compare (ScreenshotCompareView)
  compare: g(0xF018A),
  imagePlus: g(0xF087C),
  // editor setup (Settings › Code editor)
  editorSetup: g(0xF107B),
  fileCode: g(0xF022E),
  // format and lint (ProjectFormatSheet, StyleWarningsSection)
  format: g(0xF0068),       // auto-fix: Format project…
  lint: g(0xF0694),         // code-tags-check: Lint project, style warnings
  // Swift packages (ProjectPackagesSheet)
  packages: g(0xF0431),     // puzzle: Packages…
  checkboxOn: g(0xF0132),
  checkboxOff: g(0xF0131),
  // app data (DeviceAppDataView)
  database: g(0xF01BC),
  table: g(0xF04EB),
  eraser: g(0xF01FE),       // Wipe data…
  reinstall: g(0xF099B),    // restore: Clean reinstall…
  json: g(0xF0626),         // code-json: Copy as JSON
  arrowLeft: g(0xF004D),    // back from an app's details
  // hot reload (HotReloadBar)
  hotReload: g(0xF0241)     // flash
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
  { group: "Projects", keys: "n  a", text: "New app / add a project you have" },
  { group: "Projects", keys: "B", text: "Check build (compile only)" },
  { group: "Projects", keys: "w", text: "Compile on save, on or off" },
  { group: "Projects", keys: "F  L", text: "Format / lint the project" },
  { group: "Projects", keys: "P", text: "The project's Swift packages" },
  { group: "Projects", keys: "D", text: "Debug in editor, start or stop" },
  { group: "Projects", keys: "H", text: "Hot reload, on or off" },
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

// A build history entry made by watch mode (compile on save, or its auto-run): `trigger: "watch"`
// (older entries: a buildId "w-<n>").
function isWatchBuild(e) {
  if (!e) return false
  return e.trigger === "watch" || (!e.trigger && /^w-/.test(String(e.buildId || "")))
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

// A countdown or stopwatch: "0:07", "4:59", "1:02:03" (seconds, floored). The one implementation for
// timers in the UI; elapsed time since a start is clock(elapsedSeconds(startedAt)).
function clock(sec) {
  var s = Math.max(0, Math.floor(Number(sec) || 0))
  var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60
  var mm = h > 0 ? (m < 10 ? "0" : "") + m : String(m)
  return (h > 0 ? h + ":" : "") + mm + ":" + (r < 10 ? "0" : "") + r
}

// Seconds since `t` (a Date, ms or s epoch, or an ISO string), never negative; NaN for no time.
function elapsedSeconds(t, nowMs) {
  var ms = t instanceof Date ? t.getTime() : _toMs(t)
  if (!isFinite(ms)) return NaN
  var now = nowMs === undefined ? Date.now() : nowMs
  return Math.max(0, (now - ms) / 1000)
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

// QML files can use Util.clamp from qs.Commons (also maps non-numbers to lo); this copy is for JS-only code.
function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)) }

function matches(haystack, query) {
  var q = String(query || "").trim().toLowerCase()
  if (!q) return true
  return String(haystack || "").toLowerCase().indexOf(q) >= 0
}

// ---- projects (ProjectsPage)

// The names more than one listed project has, as {name: true}: there the folder tells the apps apart
// (a copy of an app in another folder keeps its name).
//   sharedNames([{name: "A"}, {name: "A"}, {name: "B"}]) -> {A: true}
function sharedNames(projects) {
  var seen = Object.create(null)
  var out = {}
  ;(projects || []).forEach(function(p) {
    var n = p && p.name ? String(p.name) : ""
    if (!n) return
    if (seen[n]) out[n] = true
    seen[n] = true
  })
  return out
}

// A path with the home folder shown as "~":  tildePath("/home/me/Projects/A", "/home/me") -> "~/Projects/A"
function tildePath(path, home) {
  var p = String(path || "")
  var h = String(home || "").replace(/\/+$/, "")
  return h && (p === h || p.indexOf(h + "/") === 0) ? "~" + p.substring(h.length) : p
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

// ---------------------------------------------------------------- run presets (helper/iosstudio/features/run_presets.py)
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

// ---- iPhone cold open (services/DeviceService.qml)

// The daemon's state file (~/.local/state/ios-studio/device.json, deviced-protocol.md "State file", as
// text) -> a Device object standing in for `current` until discovery has finished, or null when there's
// no paired iPhone in it.
function knownDevice(stateText) {
  var d = null
  try { d = JSON.parse(stateText || "null") } catch (e) { d = null }
  if (!d || typeof d !== "object" || !d.udid || !(d.wifiPaired || d.usbPaired)) return null
  return { udid: d.udid, name: d.name || "", model: d.model || "", productType: d.productType || "",
           ios: d.iosVersion || "", build: d.build || "", transport: null, usb: false,
           wifiPaired: !!d.wifiPaired, usbPaired: !!d.usbPaired, state: "connecting", active: true, known: true }
}

// `known` with the non-blank fields of the daemon's cached copy of the same iPhone (from `devices`).
function mergeKnownDevice(known, devices) {
  var cur = Object.assign({}, known)
  for (var i = 0; i < (devices || []).length; i++) {
    var d = devices[i]
    if (!d || d.udid !== cur.udid) continue
    for (var k in d) if (k !== "state" && k !== "active" && d[k] !== "" && d[k] !== null && d[k] !== undefined) cur[k] = d[k]
  }
  return cur
}

// An appsRunning event's {bundleId: pid|null} applied to an app list: a new list in which only the apps
// whose running state changed are copies (the rest are the same objects), or null when nothing changed.
function patchRunning(list, running) {
  var copy = null
  for (var i = 0; i < (list || []).length; i++) {
    var bid = list[i].bundleId
    if (!running || !Object.prototype.hasOwnProperty.call(running, bid)) continue
    var pid = running[bid], on = pid !== null && pid !== undefined && pid !== false
    if (!!list[i].running === on && (!on || list[i].pid === pid)) continue
    if (!copy) copy = list.slice()
    copy[i] = Object.assign({}, list[i], { running: on, pid: on ? pid : null })
  }
  return copy
}

// ---- app folders (AppState.qml; the helper's rule in helper/iosstudio/core/appdirs.py)

// An XDG base folder: the variable's value, or home + "/" + defaultRel when it is unset or empty.
//   xdgHome(env("XDG_STATE_HOME"), "/home/me", ".local/state") -> "/home/me/.local/state"
function xdgHome(value, home, defaultRel) {
  return value ? String(value) : String(home || "") + "/" + defaultRel
}

// ---- app icon (ProjectIconSheet; the helper's `projects icon --background`)

// A typed background color as the helper wants it ("#rrggbb", lower case), or "" when it isn't one.
// Accepts #rgb and a missing "#":  hexColor("1E90ff") -> "#1e90ff", hexColor("#fff") -> "#ffffff"
function hexColor(text) {
  var s = String(text === undefined || text === null ? "" : text).trim().replace(/^#/, "")
  if (/^[0-9a-fA-F]{3}$/.test(s)) s = s[0] + s[0] + s[1] + s[1] + s[2] + s[2]
  return /^[0-9a-fA-F]{6}$/.test(s) ? "#" + s.toLowerCase() : ""
}

// ---- debug in editor (services/EditorDebugService.qml; the helper's `debug editor` stream)

var EDITOR_NAMES = { vscode: "VS Code", zed: "Zed", neovim: "Neovim" }

// A new bridge entry while `debug editor` starts. state: starting | ready | attached | detached | stopping |
// ended | error. owned: false for a bridge found by `debug editor-status` (another iOS Studio window, or one
// left running from before a restart); it's followed by asking again, and stopped by its helper pid.
function editorDebugEntry(path, name) {
  return { path: String(path || ""), name: String(name || ""), state: "starting", step: "", owned: true,
           bundleId: "", port: 0, pid: 0, launched: false, configName: "", editors: [], notices: [],
           error: null, ended: "", attached: false, killed: false, helperPid: 0, at: Date.now() }
}

// The entry after one stream event (a new object; the old one is unchanged). Events: step, notice, ready,
// editor {state}, forward {port}, done {ok, data | error}.
function editorDebugEvent(entry, ev) {
  if (!entry || !ev || typeof ev !== "object") return entry
  var e = Object.assign({}, entry)
  var kind = ev.event
  if (kind === "step") {
    if (ev.status === "running" && e.state === "starting") e.step = String(ev.title || "")
  } else if (kind === "notice") {
    // "waiting" repeats what the ready line says; the rest are kept, one per id, newest last, at most 6.
    if (ev.id === "waiting") return entry
    var list = (e.notices || []).filter(function(n) { return n.id !== ev.id })
    list.push({ id: String(ev.id || ""), level: String(ev.level || "info"), message: String(ev.message || "") })
    e.notices = list.slice(-6)
  } else if (kind === "ready") {
    e.state = "ready"
    e.step = ""
    e.bundleId = String(ev.bundleId || e.bundleId || "")
    if (ev.appName) e.name = String(ev.appName)
    e.port = Number(ev.port || 0)
    e.pid = Number(ev.pid || 0)
    e.launched = ev.launched === true
    e.configName = String(ev.configName || "")
    e.editors = (ev.editors || []).slice()
  } else if (kind === "editor") {
    if (e.state === "stopping") return entry
    var st = String(ev.state || "")
    if (st === "attached") { e.state = "attached"; e.attached = true }
    else if (st === "detached") e.state = "detached"
    else if (st === "waiting") e.state = "ready"
  } else if (kind === "forward") {
    if (ev.port) e.port = Number(ev.port)
  } else if (kind === "done") {
    e.at = Date.now()
    if (ev.ok === true) {
      var d = ev.data || {}
      e.state = "ended"
      e.ended = String(d.ended || "cancelled")
      e.attached = d.attached === true || e.attached
      e.killed = d.killed === true
    } else {
      var err = ev.error || {}
      // Stopped before it was ready: a calm ending, not an error.
      if (err.code === "cancelled") { e.state = "ended"; e.ended = "cancelled" }
      else { e.state = "error"; e.error = typeof err === "object" ? err : { code: "failed", message: String(err) } }
    }
  }
  return e
}

// An entry for a bridge `debug editor-status` reports ({bundleId, appName, project, port, pid, launched,
// editor: waiting|attached|detached, helperPid}).
function editorDebugFromStatus(b, name) {
  var e = editorDebugEntry(b.project || "", b.appName || name || "")
  var ed = String(b.editor || "waiting")
  e.state = ed === "attached" ? "attached" : ed === "detached" ? "detached" : "ready"
  e.owned = false
  e.bundleId = String(b.installedBundleId || b.bundleId || "")
  e.port = Number(b.port || 0)
  e.pid = Number(b.pid || 0)
  e.launched = b.launched === true
  e.attached = ed !== "waiting"
  e.helperPid = Number(b.helperPid || 0)
  e.configName = "Attach to " + e.name + " on iPhone (iOS Studio)"
  return e
}

// Who the bar talks about: "VS Code", "Zed", or "your editor" (none or several configured).
function editorDebugWho(editors) {
  var l = editors || []
  return l.length === 1 && EDITOR_NAMES[l[0]] ? EDITOR_NAMES[l[0]] : "your editor"
}

// What to do in the editor: F5 in VS Code, the debug panel in Zed.
function editorDebugHow(editors, configName) {
  var l = editors || []
  var cfg = configName ? "“" + configName + "”" : "the iOS Studio configuration"
  if (l.length === 1 && l[0] === "zed") return "start " + cfg + " from the debug panel"
  return "press F5 on " + cfg
}

// The bar's text for an entry: { level (ok|warn|error|info|busy), text, detail, tip, live (a bridge runs) }.
function editorDebugLabel(e) {
  if (!e) return null
  var who = editorDebugWho(e.editors)
  var Who = who.charAt(0).toUpperCase() + who.substring(1)
  var app = e.name || "The app"
  var tip = "Debug in editor: iOS Studio holds the connection to the debugger on your iPhone open; your editor's "
            + "debugger (lldb-dap) attaches to it. Stop ends it."
  switch (e.state) {
  case "starting":
    return { level: "busy", text: (e.step || "Starting the debugger") + "…", detail: "", tip: tip, live: true }
  case "ready":
    return { level: "info", text: "Waiting for " + who + " · " + editorDebugHow(e.editors, e.configName), tip: tip, live: true,
             detail: e.launched ? app + " started paused and waits for the debugger." : "" }
  case "attached":
    return { level: "ok", text: Who + " is attached · the app pauses at breakpoints", detail: "", tip: tip, live: true }
  case "detached":
    return { level: "info", text: Who + " detached · " + editorDebugHow(e.editors, e.configName) + " to attach again",
             detail: "", tip: tip, live: true }
  case "stopping":
    return { level: "busy", text: "Stopping…", detail: "", tip: tip, live: true }
  case "ended":
    var why = e.ended === "app-exited" ? app + " exited, so debugging in the editor ended"
            : e.ended === "timeout" ? "Debugging in the editor ended after 2 hours"
            : "Stopped debugging in the editor"
    var more = e.killed ? " " + app + " was closed, since no editor attached." : ""
    return { level: "info", text: why, detail: more.trim(), tip: "", live: false }
  case "error":
    return { level: "error", text: errorMessage(e.error), detail: "", tip: "", live: false }
  }
  return null
}

// ---- hot reload (services/HotReloadService.qml; the helper's `hotreload run` stream and `hotreload status`)

// Session states that mean a session runs (`stopped` is the only other one).
var HOT_RELOAD_LIVE = ["starting", "building", "installing", "launching", "watching", "paused", "reloading",
                       "reloaded", "failed"]

// A project's hot reload entry. session: the latest snapshot (the `hotreload` event / `hotreload status`), or
// null before the first one. action: what this window is doing to it right now: "" | enabling | disabling |
// starting | stopping | pausing | resuming. error: a failure that has no snapshot to carry it (enable, disable,
// pause failed, or `run` ended before it published one, e.g. busy), errorAction: which one (enable | disable |
// start | pause | resume | stop). owned: false for a session another process
// runs (found by `hotreload status`).
function hotReloadEntry(path, name) {
  return { path: String(path || ""), name: String(name || ""), owned: true, action: "", error: null, errorAction: "",
           session: null, at: Date.now() }
}

function _hrSnap(ev) {
  var s = Object.assign({}, ev)
  delete s.event
  delete s.history
  return s
}

// The entry after one stream event (a new object; the old one is unchanged). Events: hotreload (a snapshot),
// done {ok, data: the final snapshot | error}; others (log) leave it as it is.
function hotReloadEvent(entry, ev) {
  if (!entry || !ev || typeof ev !== "object") return entry
  var e = Object.assign({}, entry)
  var old = entry.session
  if (ev.event === "hotreload") {
    // Snapshots of one session come in order; an older one (a late status reply) is dropped.
    if (old && old.pid === ev.pid && Number(ev.seq || 0) <= Number(old.seq || 0)) return entry
    var s = _hrSnap(ev)
    e.session = s
    e.error = null
    e.errorAction = ""
    e.at = Date.now()
    if (e.action === "starting") e.action = ""
    else if (e.action === "pausing" && s.paused === true) e.action = ""
    else if (e.action === "resuming" && s.paused !== true) e.action = ""
    else if (e.action === "stopping" && s.state === "stopped") e.action = ""
    return e
  }
  if (ev.event === "done") {
    var d = ev.data && typeof ev.data === "object" ? _hrSnap(ev.data) : {}
    var had = !!old || Object.keys(d).length > 0
    var f = Object.assign({}, old || {}, d)
    f.state = "stopped"
    f.running = false
    f.progress = null
    if (ev.ok !== true) {
      var err = ev.error && typeof ev.error === "object" ? ev.error
              : { code: "failed", message: String(ev.error || "Hot reload stopped.") }
      if (!f.stopReason || f.stopReason === "stopped") f.stopReason = err.code === "cancelled" ? "stopped" : "error"
      if (!f.lastError || f.lastError.code !== err.code) f.lastError = err
      if (!had && err.code !== "cancelled") { e.error = err; e.errorAction = "start" }
    }
    if (!f.stopReason) f.stopReason = "stopped"
    e.session = had ? f : null
    if (e.action !== "disabling") e.action = ""
    e.at = Date.now()
    return e
  }
  return entry
}

// An entry for a session `hotreload status` reports (someone else's: another window, the MCP server).
function hotReloadFromStatus(snap, name) {
  var e = hotReloadEntry(snap && snap.path, (snap && snap.project) || name || "")
  e.owned = false
  e.session = snap ? _hrSnap(snap) : null
  return e
}

// A session runs (its state is a live one, and status didn't find it dead).
function hotReloadLive(entry) {
  var s = entry && entry.session
  return !!s && HOT_RELOAD_LIVE.indexOf(String(s.state)) >= 0 && s.running !== false
}

// "1.6 s", "0.4 s", "12 s" for a reload's milliseconds.
function hotReloadMs(ms) {
  var v = Number(ms)
  if (!isFinite(v) || v < 0) return ""
  return v < 10000 ? (v / 1000).toFixed(1) + " s" : Math.round(v / 1000) + " s"
}

// "12:04:31" (local time) for unix seconds or ms.
function hotReloadClock(t) {
  var ms = _toMs(t)
  if (!isFinite(ms)) return ""
  var d = new Date(ms)
  function p(n) { return (n < 10 ? "0" : "") + n }
  return p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds())
}

// "just now", "12 s ago", "3 min ago", then relativeTime: the time since a reload, updated every second.
function hotReloadAgo(t, nowMs) {
  var s = elapsedSeconds(t, nowMs)
  if (!isFinite(s)) return ""
  if (s < 5) return "just now"
  if (s < 60) return Math.floor(s) + " s ago"
  if (s < 3600) return Math.floor(s / 60) + " min ago"
  return relativeTime(t, nowMs)
}

function _hrName(rel) { var r = String(rel || ""); return r.substring(r.lastIndexOf("/") + 1) }

// "ContentView.swift", "ContentView.swift and 2 more".
function hotReloadFiles(files, count) {
  var l = files || []
  var n = Math.max(Number(count || 0), l.length)
  if (!l.length) return n ? plural(n, "file") : "your changes"
  return _hrName(l[0]) + (n > 1 ? " and " + (n - 1) + " more" : "")
}

// "Reloaded ContentView.swift · 1.6 s · 12:04:31 · 5 s ago" (ago left out when nowMs is null).
function hotReloadReloadText(r, nowMs) {
  if (!r) return ""
  var parts = ["Reloaded " + hotReloadFiles(r.files, r.fileCount)]
  var ms = hotReloadMs(r.ms)
  if (ms) parts.push(ms)
  var at = hotReloadClock(r.time)
  if (at) parts.push(at)
  if (nowMs !== null && at) parts.push(hotReloadAgo(r.time, nowMs))
  return parts.join(" · ")
}

// A blocker {kind, type?, name?, file, line?, message} as {where: "ContentView.swift:12", file, line, text}.
function hotReloadBlocker(b) {
  var f = String((b && b.file) || "")
  var line = b && b.line ? Number(b.line) : 0
  return { where: f ? _hrName(f) + (line ? ":" + line : "") : "", file: f, line: line,
           text: String((b && b.message) || "") }
}

// The first line of a reload error's first diagnostic (an error first), with its place: "ContentView.swift:14:
// cannot find 'foo' in scope". "" when there is none.
function hotReloadDiagnostic(err) {
  var l = (err && err.diagnostics) || []
  if (!l.length) return ""
  var d = l[0]
  for (var i = 0; i < l.length; i++) if (l[i] && l[i].severity === "error") { d = l[i]; break }
  var msg = String((d && d.message) || "").split("\n")[0]
  var where = d && d.file ? _hrName(d.file) + (d.line ? ":" + d.line : "") : ""
  return where ? where + ": " + msg : msg
}

// "ContentView.swift, ForecastView.swift and 2 more": the file names of paths (the first three).
function hotReloadFileNames(files) {
  var l = (files || []).map(_hrName)
  if (l.length <= 3) return l.join(", ")
  return l.slice(0, 3).join(", ") + " and " + (l.length - 3) + " more"
}

// The project switches this window flipped, after a `hotreload status` reply. local: path → {enabled, mode,
// importedBy?, after}: `after` is the number of the last status fetch that started before the change was done.
// A reply to a fetch that started later (replySeq > after) read the project after the change, so it is the
// truth, whatever it says (an agent or a hand edit may have changed it since); an older reply only clears a
// switch it agrees with. → the same object when nothing changes, else a new one.
function hotReloadReconcile(local, projects, replySeq) {
  var l = local || {}, out = null
  for (var k in l) {
    var st = projects ? projects[k] : null
    var agrees = !!st && !!st.enabled === !!l[k].enabled && (l[k].mode === undefined || l[k].mode === st.mode)
    if (Number(replySeq) > Number(l[k].after) || agrees) {
      if (!out) out = Object.assign({}, l)
      delete out[k]
    }
  }
  return out || l
}

var _HR_BUSY = { starting: "Starting", building: "Building", installing: "Installing", launching: "Opening",
                 reloading: "Reloading" }

// What the bar shows for an entry. setting: the project's {enabled, mode} (or null). nowMs: for "5 s ago".
// → { state, level (ok|warn|error|info|busy), pill, pillGlyph ("" = the level's), text, detail, live, busy,
//     progress (0..1, or -1 without a bar), indeterminate, pending [names], blockers [hotReloadBlocker], moreBlockers,
//     diagnostic, error (the {code, message, fix} its fix button uses, or null), note {level, text, fix} | null,
//     canPause, canResume, pauseTip }
// null when there is nothing to show (no entry and hot reload is off).
function hotReloadLabel(entry, setting, nowMs) {
  var on = !!(setting && setting.enabled)
  var s = entry ? entry.session : null
  var live = hotReloadLive(entry)
  var action = entry ? entry.action : ""
  var out = { state: "", level: "info", pill: "", pillGlyph: "", text: "", detail: "", live: live, busy: false,
              progress: -1, indeterminate: false, pending: [], blockers: [], moreBlockers: 0, diagnostic: "",
              error: null, note: null, canPause: false, canResume: false,
              pauseTip: "Pause works once the app runs and hot reload watches for saves." }
  function busy(state, pill, text, detail) {
    out.state = state; out.level = "busy"; out.busy = true; out.pill = pill; out.text = text; out.detail = detail || ""
    return out
  }
  if (action === "enabling") return busy("enabling", "Turning on", "Adding hot reload to Package.swift…")
  if (action === "disabling") return busy("disabling", "Turning off", "Stopping hot reload and taking it out of Package.swift…")
  // Stop before the session's first snapshot (it was still starting): nothing else to show yet.
  if (action === "stopping" && !s) return busy("stopping", "Stopping", "Stopping hot reload…")
  // Off (known, not just unknown): only an error of this window's own toggle still shows.
  if (setting && setting.enabled === false && !live && action === "" && !(entry && entry.error)) return null
  if (!entry || (!s && !entry.error)) {
    if (!on) return null
    if (action === "starting") return busy("starting", "Starting", "Starting hot reload…")
    out.state = "idle"; out.pill = "Not running"; out.pillGlyph = Glyph.pause
    out.text = "Hot reload is on, but not running"
    out.detail = "Start builds and opens the app once, then each save reloads on your iPhone."
    return out
  }
  if (!s || (entry.error && !live)) {
    out.state = "error"; out.level = "error"
    out.pill = { enable: "Couldn't turn on", disable: "Couldn't turn off" }[entry.errorAction] || "Didn't start"
    out.text = errorMessage(entry.error); out.error = entry.error
    return out
  }
  var st = String(s.state || "")
  var err = s.lastError || null
  var notice = s.notice && s.notice.message ? s.notice : null
  if (live && notice) {
    var age = elapsedSeconds(notice.time, nowMs)
    if (level(notice.level) === "warn" || !isFinite(age) || age < 300)
      out.note = { level: level(notice.level) === "warn" ? "warn" : "info", text: String(notice.message), fix: notice.fix || null }
  }
  // A start problem the session lives with (the app didn't open; it has no reload seq): a warning line, not the
  // failed state. A failed reload's error stays in the snapshot until the next one works; it isn't repeated.
  if (live && err && err.seq === undefined && st !== "failed") out.note = { level: "warn", text: errorMessage(err), fix: err.fix || null }
  var started = ["watching", "paused", "reloading", "reloaded", "failed"].indexOf(st) >= 0
  if (live && started) {
    out.canPause = s.paused !== true
    out.canResume = s.paused === true
    out.pauseTip = ""
  }
  if (action === "stopping" && live) return busy("stopping", "Stopping", "Stopping hot reload…")
  if (action === "pausing" && live) { out.canPause = false; out.pauseTip = "Pausing…" }
  if (action === "resuming" && live) { out.canResume = false; out.pauseTip = "Resuming…" }
  var pr = s.progress || null
  switch (st) {
  case "building":
    var frac = pr && pr.fraction !== undefined && pr.fraction !== null ? Number(pr.fraction) : NaN
    busy("building", "Building" + (frac > 0 ? " " + Math.round(frac * 100) + "%" : ""), String(s.message || "Building the app"),
         pr && pr.detail ? String(pr.detail) : "")
    out.progress = isFinite(frac) ? clamp(frac, 0, 1) : 0
    out.indeterminate = !(frac > 0)
    return out
  case "starting":
  case "installing":
  case "launching":
    return busy(st, _HR_BUSY[st], String(s.message || _HR_BUSY[st] + "…"))
  case "reloading":
    busy("reloading", "Reloading", String(s.message || "Reloading " + hotReloadFiles(s.pending, 0)),
         pr && pr.detail ? String(pr.detail) + "…" : "")
    if (s.paused === true) out.detail = "Paused: saves after this reload wait until you resume"
    return out
  case "reloaded":
    out.state = "reloaded"; out.level = "ok"; out.pill = "Reloaded"
    out.text = s.lastReload ? hotReloadReloadText(s.lastReload, nowMs) : String(s.message || "Reloaded")
    return out
  case "watching":
    out.state = "watching"; out.level = "ok"; out.pill = "Watching"; out.pillGlyph = Glyph.eye
    var msg = String(s.message || "")
    if (/^Back to /.test(msg) || /^Resumed/.test(msg) || !s.lastReload) {
      out.text = msg || "Watching for saves"
      if (!s.lastReload) out.detail = "Save a Swift file and the change shows on your iPhone in a few seconds."
    } else out.text = hotReloadReloadText(s.lastReload, nowMs)
    return out
  case "paused":
    out.state = "paused"; out.level = "info"; out.pill = "Paused"; out.pillGlyph = Glyph.pause
    var n = Number(s.pendingCount || (s.pending || []).length || 0)
    out.text = n ? plural(n, "saved file") + " waiting" : "Saves wait until you resume"
    out.pending = (s.pending || []).map(_hrName)
    out.detail = n ? out.pending.join(", ") + (n > out.pending.length ? " and " + (n - out.pending.length) + " more" : "")
                   + " reload together when you resume." : "Your app keeps running the last reload."
    return out
  case "failed":
    out.state = "failed"; out.level = "error"; out.pill = "Reload failed"
    out.text = errorMessage(err || { message: s.message })
    var bl = (err && err.blockers) || []
    out.blockers = bl.slice(0, 3).map(hotReloadBlocker)
    // The reply carries up to 10; blockersTotal counts them all.
    var total = err && Number(err.blockersTotal) > bl.length ? Number(err.blockersTotal) : bl.length
    out.moreBlockers = Math.max(0, total - out.blockers.length)
    // One blocker repeats the message: show its place only.
    if (out.blockers.length === 1 && out.text.indexOf(out.blockers[0].text) === 0) out.blockers[0].text = ""
    out.diagnostic = hotReloadDiagnostic(err)
    if (out.diagnostic && out.text.indexOf(out.diagnostic.replace(/^[^:]+:\d+: /, "")) >= 0) out.diagnostic = ""
    out.error = err
    out.detail = "Still watching: fix it and save again."
    return out
  case "stopped":
    out.state = "stopped"; out.pill = "Stopped"; out.pillGlyph = Glyph.stop
    var reason = String(s.stopReason || "stopped")
    out.text = String(s.message || "Stopped")
    if (err && (reason === "error" || reason === "build-failed" || reason === "session-died")) {
      out.level = "error"; out.pill = reason === "build-failed" ? "Build failed" : "Stopped"; out.pillGlyph = ""
      out.text = errorMessage(err); out.error = err
      out.diagnostic = hotReloadDiagnostic(err)
    } else if (reason === "app-exited") {
      out.detail = "Start runs the app again with hot reload."
    }
    return out
  }
  out.state = st; out.pill = st; out.text = String(s.message || "")
  return out
}
