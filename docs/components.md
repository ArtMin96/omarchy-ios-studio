# iOS Studio components

Shared building blocks in `components/` on top of Omarchy's `qs.Ui` kit. Build every page
from these, so all pages look the same and follow the live theme.

```qml
import QtQuick
import QtQuick.Layouts
import qs.Commons                  // Style, Util, Border
import qs.Commons as Commons       // Commons.Color.*
import qs.Ui                       // Button, TextField, ToggleSwitch, Dropdown, …
import qs.components               // everything below
import "../Model.js" as Model      // Glyph, formatters (from pages/ or services/)
```

Rules: sizes come only from `Style.font.*`, spacing from `Style.spacing.*` / `Style.space(px)`, radius from
`Style.cornerRadius`, and colours from `Commons.Color.*` or `Theme.*`. No hard-coded px or hex values.
Quickshell picks up new files in `components/` automatically (no qmldir). Add new shared pieces as **new
files** there.

> Note on names: several names are taken by QtQuick / QtQuick.Controls, and a page that imports
> QtQuick.Controls would silently get the Qt type instead. So the palette singleton is **`Theme`** (not
> Palette), the filter box is **`FilterField`** (not SearchField), the progress bar is **`ProgressTrack`**
> (not ProgressBar), and the in-page tabs are **`PageTabs`** (not TabBar). Check the name against
> `/usr/lib/qt6/qml/QtQuick/Controls/Basic/` before you add a new component.

---

## Page contract (how shell.qml talks to your page)

```qml
PageFrame {
  id: page
  required property var app              // AppState: services, nav, toast, confirm, settings
  title: "iPhone"; subtitle: "…"

  // all optional hooks, called by shell.qml when they exist:
  function activated(params) {}          // on every nav() to this page (params = app.pageParams), incl. deep links
  function handleKey(event) { return false }   // page cursor model; return true if consumed
  function focusSearch() {}              // global "/"
  function refresh() {}                  // global "r" (default: the page's service.refresh())
  function goBack() {}                   // Esc when no overlay or text field has focus (`escape` is an illegal QML method name)
}
```

- Pages are loaded lazily on first visit and **kept alive**, so page-local state survives switching.
  Shared state belongs in the service (`app.device`, …).
- Global keys that the shell handles first: `1`–`8` pages, `?` shortcut sheet, `r` refresh, `/` search,
  `Ctrl+Tab` next page, and `Esc` (closes the top overlay, else leaves a text field, else calls `page.goBack()`).
  The shell calls `handleKey(event)` **before** the global keys, so return `true` only for keys you use
  (typically `j`/`k`/arrows, `Enter`, `x`, `c`). Use `Model.keyAction(event)` to map an event to
  `"up"|"down"|"left"|"right"|"activate"|"delete"|"copy"|"home"|"end"|"pageUp"|"pageDown"|""`.
- One cursor per page: keep `property int cursor` on the page, bind `hasCursor: page.cursor === index`
  on rows, and set `page.cursor = index` from the rows' `hovered(true)`.

## AppState (`app`)

| Member | |
|---|---|
| `app.nav(page, params?)` | Switch page. Accepts aliases (`"iphone"`, `"sdk"`). Params land in `app.pageParams` and `activated(params)`. |
| `app.openSetupStep(id)` | `nav("setup", {step: id})` |
| `app.toast(text, level?)` | level: `info` (default), `ok`, `warn`, `error` |
| `app.confirm({title, message, confirmText, cancelText, destructive, glyph, onConfirm, onCancel})` | Cancel is the default button. Use it for every destructive action. Returns a handle. A dialog asked for while another is open is queued and shown after it, so no question is lost. |
| `app.closeConfirm(handle)` | Close that dialog (or drop it from the queue) unanswered: neither callback runs. Use it when whatever asked has gone away (e.g. the job that wanted an answer ended). |
| `app.notify(title, body, glyph)` | Desktop notification, only when the window isn't focused and notifications are on. |
| `app.cli` | Shared `Cli` instance (see below). |
| `app.settings`, `app.setting(key, fallback)`, `app.setSetting(key, value)` | `~/.config/ios-studio/settings.json`. Keys: `projectRoots` (array; empty = the detected defaults, see `common.default_project_roots()`), `editorCommand` ("" = Omarchy editor), `editorSetup` (vscode|zed|neovim: your editor for Swift, set by Settings › Code editor and by `editor setup`), `defaultDevice` (UDID), `notifications`, `autoRenew`, `autoRenewDays`, `firstRunDone`, plus `opaqueWindow`, `agentsDemo`, the Logs page's `logsLevel`, `logsProcess`, `logsKeepStreaming` and Screen › Compare's `compareProject`. Pages may add keys. |
| `app.setup`, `app.toolchain`, `app.account`, `app.device`, `app.projects`, `app.logs`, `app.watch` | Services (`app.watch`: watch mode, see WatchService below) |
| `app.navLink(link, tab?)`, `app.cyclePage(delta)` | Deep link (see below); next/previous page (`Ctrl+Tab`). |
| `app.page`, `app.pageParams`, `app.windowFocused`, `app.shortcutsOpen`, `app.demo` (the demo variant, "" when off) | State |
| `app.home`, `app.helperPath`, `app.devicedPath`, `app.repoPath`, `app.configDir`, `app.stateDir`, `app.cacheDir`, `app.demoDir` | Paths |
| `app.refreshAll()`, `app.refreshCurrent()` | |

## Cli (one-shot helper calls), `Cli.qml`

```qml
app.cli.call(["toolchain", "status"], "",
  function(data) { root.info = data; root.status = "ready" },
  function(err)  { root.error = err;  root.status = "error" })   // err = {code, message, fix?, detail?}

app.cli.call(["account", "login-check"], JSON.stringify({ password: pw }), ok, fail)   // secrets via stdin only
app.cli.call(["account", "ds-list", "--cached"], "", ok, fail, { timeout: 30000 })
```
Every call spawns its own process, so calls run concurrently. `onFail` always gets an error object, even
on a crash, garbage output or timeout (default 120 s). Services can also create their own `Cli {}` (import
`".."` from `services/`). `cli.busy` is the number of calls in flight.

## Stream (JSON-lines processes), `Stream.qml`

```qml
Stream {
  id: install
  onEvent: function(ev) {
    if (ev.event === "step") …; else if (ev.event === "progress") …; else if (ev.event === "log") log.append(ev.text)
    else if (ev.event === "prompt") promptSheet.ask(ev)        // answer: install.write({prompt: ev.id, value: v})
    else if (ev.event === "notice") app.toast(ev.message, ev.level)
  }
  onFinished: function(ok, data, error) { … }                   // from {"event":"done"}, or an unexpected exit
}
install.start(["setup", "install", "--step", "swift"])
install.stop()
```
Props: `program` (argv prefix, default the helper; set `[app.devicedPath]` for the daemon), `args`,
`running`, `autoRestart` (1 s→30 s backoff), `restarts`, `quitMessage` (written before terminating on
`stop()`), `environment`, `lastDone`, `exitCode`, `maxLineLength`; `write(obj)` sends one JSON line to
stdin, `restart()`; signals `event(obj)`, `finished(ok, data, error)`, `exited(code)`. Non-JSON stdout lines and all stderr lines arrive as
`{event: "log", text, stream}`. Lines are capped at 512 KiB. Every other event (`notice` included)
passes through unchanged.

## Model.js

`Model.Glyph.*` (Nerd Font glyphs): home rocket apple account phone hammer build play pause stop logs
console crash gear sdk box toolbox wrench folder folderOpen folderPlus file check checkCircle circle dot warn
error info help refresh sync download upload trash link linkOff wifi wifiOff usb battery batteryCharging
lock unlock key cert shield shieldCheck clock timer location camera image video search keyboard close plus minus
chevronRight/Left/Down/Up arrowRight arrowUp openExternal copy save export edit code swift disk memory
cloudDownload broom bell bellOff eye eyeOff filter star dots window apps package signature developer heart
sparkles terminal layers briefcase idCard xml network speed thermometer chip record setup phoneLink bug settings
robot testTube asterisk hexagon consoleLine cursorArrow starFour lightning speedometer chart translate tune, and for the UI inspector inspect accessibility tap text textbox toggle slider
header tab list element linkVariant (element roles) and contrast textSize clipped hitArea label (audit issue types), and compare imagePlus
(screenshot compare), and editorSetup fileCode (Settings › Code editor).

Functions: `formatBytes(n)` → "1.2 GB"; `relativeTime(isoOrEpoch)` → "5 minutes ago" / "in 3 days";
`formatDuration(sec)` → "12 min"; `expiryStatus(iso)` → `{level, text: "expires in 3 days", days}`;
`level(x)` normalises to `ok|warn|error|info|busy`; `levelGlyph(level)`; `errorMessage(err)`;
`plural(n, word)`; `isWatchBuild(entry)` (a build history entry from watch mode: `trigger: "watch"`, or a `w-` buildId); `clamp(v, lo, hi)`; `matches(text, query)`; `greeting()`; `keyAction(event)`; `PAGES`,
`pageIndex(key)`, `pageByKey(key)`, `resolvePage(name)`, `parseDeepLink(link)`, `SHORTCUTS`, `shortcutGroups()`.

Run presets (mirroring `helper/run_presets.py`, so the editor can preview and validate as you type; the helper
still checks on save): `PRESET_LANGUAGES` (`{value, label, region}` for the language list: en, de, fr, es, it,
pt-BR, nl, ru, ja, ko, zh-Hans, ar, he), `PRESET_MAX_NAME` (40), `PRESET_LANGUAGE_RE`, `PRESET_REGION_RE`,
`ENV_NAME_RE`; `languageLabel(code)` → "German" (the code itself when it isn't in the list);
`presetTag(value, sep)` → the helper's spelling (`"de-de"` → `"de-DE"` with `"-"`, `"de_DE"` with `"_"`);
`presetResolve(p)` → `{args, env}` as the launch gets them (own args, then `-AppleLanguages (…)`, `-AppleLocale …`,
`-UITestMode YES`); `presetSummary(p)` → "German · de_DE · 2 arguments · UI test mode"; `presetNames(project)`.

---

## Theme (singleton), semantic colours

| | |
|---|---|
| `Theme.success`, `Theme.warn` | Picked by hue from the theme's colors.toml, synthesised when the theme has no fitting colour. |
| `Theme.error` (= urgent), `Theme.info` (= accent) | |
| `Theme.dim`, `Theme.faint` | Secondary and tertiary text |
| `Theme.hairline` | Separators and quiet borders |
| `Theme.surface`, `Theme.surfaceRaised` | Card fills (resting / hover) |
| `Theme.scrim`, `Theme.isDark` | |
| `Theme.levelColor(level)`, `Theme.levelFill(level)` | `ok/warn/error/info/busy` → colour / tinted fill |

```qml
Rectangle { color: Theme.levelFill("warn"); border.color: Theme.levelColor("warn") }
```

## AppText

Themed text. `size`: `caption | small | body | subtitle | title | heading | display | displayLarge`.
`tone`: `primary | dim | faint | accent | ok | warn | error | info | inherit` (inherit keeps your `color`).
`strong` sets bold, `mono` the monospace font.
```qml
AppText { text: "Signed in as you@…"; tone: "dim"; size: "small"; elide: Text.ElideRight; Layout.fillWidth: true }
```

## Icon

```qml
Icon { glyph: Model.Glyph.phone; size: Style.font.iconLarge; color: Theme.dim; spinning: false }
```

## ActionButton

`text`, `glyph`, `kind: "primary" | "secondary" (default) | "ghost" | "danger"`, `busy` (spinner, ignores
clicks), `enabled`, `disabled`, `compact`, `tooltip`, `hasCursor`; read-only `interactive`; signals `clicked()`,
`hovered(bool)`. Use one `primary` per view, for the main call to action.

`enabled: false` dims the button and blocks clicks **and** hover, so its tooltip never shows. `disabled: true`
dims it and blocks clicks but keeps hover and the tooltip: use it whenever the tooltip explains why the button
can't be used yet.

A button without a `glyph` is as tall as one with a glyph, so a text-only Cancel beside an icon button in a
Row (a Sheet footer) lines up on the same text baseline.
```qml
ActionButton { text: "Run on iPhone"; glyph: Model.Glyph.play; kind: "primary"; busy: svc.building; onClicked: svc.run(p) }
ActionButton { text: "Install"; disabled: !app.device.connected; tooltip: disabled ? "Connect your iPhone first" : "" }
```

## IconButton

Glyph-only toolbar or row button: `glyph`, `tooltip`, `danger` (turns urgent on hover), `busy`, `enabled`.
```qml
IconButton { glyph: Model.Glyph.trash; tooltip: "Uninstall"; danger: true; onClicked: page.uninstall(app) }
```

## Card

Quiet raised surface with an optional header. Props: `title`, `subtitle`, `glyph`, `glyphColor`,
`actions` (list, top right), `clickable` + `clicked()`, `hasCursor`, `accentEdge`, `edgeColor`,
`contentSpacing`. Children go into a ColumnLayout, so use `Layout.fillWidth: true` on them.
```qml
Card {
  Layout.fillWidth: true
  title: "Certificates"; glyph: Model.Glyph.cert; subtitle: "2 active"
  actions: [ IconButton { glyph: Model.Glyph.refresh; tooltip: "Refresh"; onClicked: svc.refresh() } ]
  KeyValueList { Layout.fillWidth: true; rows: [...] }
}
```

## PageFrame

Standard page: margins, `PageHeader`, and a scrolling, width-capped ColumnLayout. Props: `title`,
`subtitle`, `glyph`, `actions`, `status`, `scrollable` (false = content fills the remaining height, for
LogView or big ListViews), `maxContentWidth`, `spacing`; `scrollToItem(item)`; `flickable`.
```qml
PageFrame {
  required property var app
  title: "Logs"; scrollable: false
  LogView { Layout.fillWidth: true; Layout.fillHeight: true }
}
```

## PageHeader

`title`, `subtitle`, `glyph`, `status` (an item next to the title, e.g. a StatusPill), `actions` (top right).
PageFrame already includes one.

## SectionHeader

Small uppercase group label with a hairline, an optional `detail` (e.g. a count) and `trailing` items.
```qml
SectionHeader { Layout.fillWidth: true; text: "Installed apps"; detail: "12"; trailing: [ IconButton { glyph: Model.Glyph.refresh } ] }
```

## StatusPill

`level` (`ok|warn|error|info|busy`, aliases normalised), `text`, `glyph` (override), `showGlyph`,
`quiet` (no fill, for dense rows). `busy` spins.
```qml
StatusPill { level: exp.level; text: exp.text }   // exp = Model.expiryStatus(profile.expires)
```

## Badge

A dot (no `text`) or a small count chip. `level`, `text`.
```qml
Badge { level: "warn"; text: "3" }
```

## InfoRow / KeyValueList

`InfoRow`: `label`, `value`, `glyph`, `level` (tints the value and adds a glyph), `copyable` (copies with
wl-copy, emits `copied(value)`), `labelWidth`, `trailing`.
`KeyValueList`: `rows: [{label, value, level?, glyph?, copy?}]`, `labelWidth`, `separators`; `copied(value)`.
```qml
KeyValueList {
  Layout.fillWidth: true
  rows: [ { label: "Model", value: d.model }, { label: "UDID", value: d.udid, copy: true },
          { label: "Developer Mode", value: d.devMode ? "On" : "Off", level: d.devMode ? "ok" : "warn" } ]
  onCopied: function(v) { app.toast("Copied") }
}
```

## ListRow

Selectable row on the shared cursor model (CursorSurface). Props: `glyph`, `glyphColor`, `title`,
`subtitle`, `meta`, `badgeLevel`, `hasCursor`, `current`, `trailing`, `leading`; signals `clicked()`,
`doubleClicked()`, `hovered(bool)`.
```qml
Repeater {
  model: svc.apps
  delegate: ListRow {
    required property var modelData; required property int index
    Layout.fillWidth: true
    glyph: Model.Glyph.apps; title: modelData.name; subtitle: modelData.bundleId; meta: modelData.version
    hasCursor: page.cursor === index
    onHovered: function(h) { if (h) page.cursor = index }
    trailing: [ IconButton { glyph: Model.Glyph.play; tooltip: "Launch"; onClicked: svc.launch(modelData) } ]
  }
}
```

## PageTabs

In-page tabs with an accent underline: `tabs: [{key, label, glyph?, count?, level?}]`, `current`,
`cursorIndex`; `selected(key)`.
```qml
PageTabs { Layout.fillWidth: true; tabs: [{key: "live", label: "Live"}, {key: "crashes", label: "Crashes", count: 2, level: "warn"}]; current: page.tab; onSelected: function(k) { page.tab = k } }
```

## ButtonGroupLite

Compact segmented filter: `options: [{value, label}]`, `value`; `picked(value)`. (For forms, `qs.Ui`
`ButtonGroup` also works.)

## FilterField

`text`, `placeholder`, `field` (the TextField), `editing`; `focusField()`, `clear()`; signals
`accepted(text)`, `cleared()`. Esc clears, and a second Esc hands focus back to the page.
```qml
FilterField { id: search; placeholder: "Filter apps" }
function focusSearch() { search.focusField() }
```

## EmptyState

`glyph`, `title`, `text`, `actionText` + `actionGlyph` (+ `actionKind`, default `primary`) → `action()`, `secondaryText` +
`secondaryGlyph` → `secondary()`, `compact`, `level` (tints the glyph disc).
```qml
EmptyState { Layout.fillWidth: true; glyph: Model.Glyph.phone; title: "No iPhone connected"
  text: "Plug it in with a USB cable and tap Trust on the phone."; actionText: "Pairing help"; onAction: app.openSetupStep("pair") }
```

## ErrorState

Plain-language error with a one-click fix. `app` (needed for the fix button), `error` (the helper error
object `{code, message, fix?: {label, page, step?, action?, url?}, detail?}`), or `message`; `title`,
`compact`, `banner` (slim inline strip), `showRetry`, `retryText`; signals `retry()`, `fixed(fix)`.
The fix button opens `fix.url` externally when present, else calls `app.nav(fix.page, fix)`.
"Show details" reveals `detail`, selectable.
```qml
ErrorState { Layout.fillWidth: true; app: page.app; error: svc.error; visible: svc.status === "error"; onRetry: svc.refresh() }
ErrorState { Layout.fillWidth: true; app: page.app; error: svc.error; banner: true }
```

## LoadingState / Skeleton / Spinner

```qml
LoadingState { Layout.fillWidth: true; text: "Asking Apple for your certificates…"; detail: "This can take about 10 seconds." }
Skeleton { Layout.fillWidth: true; lines: 4; rowHeight: Style.space(36); gap: Style.spacing.md }   // list placeholder
Spinner { size: Style.font.heading; running: true; color: Commons.Color.accent }
```
`Skeleton` also has `running` (the shimmer) and `Spinner` a `lineWidth`. Prefer `Skeleton` for lists and cards, so the layout doesn't jump when the data arrives.

## ProgressTrack

`value` (0…1), `indeterminate`, `level` ("" = accent, ok, warn, error), `label` (left caption),
`trailing` (right caption), `thickness`.
```qml
ProgressTrack { Layout.fillWidth: true; value: ev.fraction; label: ev.detail; trailing: Model.formatDuration(ev.eta) + " left" }
```

## StepRow

A wizard step. Props: `number`, `title`, `description`, `stepState`
(`done|missing|running|failed|pending|optional|skipped`), `meta` ("≈ 900 MB · 3 min"), `actionText`,
`actionGlyph`, `actionKind` (auto: primary when missing or failed), `actionEnabled`, `actionBusy`
(default `stepState === "running"`), `progress` (-1 hides it), `progressIndeterminate`, `progressText`,
`progressTrailing`, `errorText`, `expanded`, `hasCursor`, `extra` (more controls under the description);
read-only `logCount`; signals `action()`, `hovered(bool)`, `toggled(expanded)`.

The log is fed imperatively (it keeps the last 400 lines): `setLog(lines)` replaces it, `appendLog(text)` adds
a line, `clearLog()` empties it. With `app.setup`, load `svc.logLines(id)` once, then follow its
`stepLog(id, text)` and `stepLogCleared(id)` signals (`""` = every step), as `SetupPage.qml` does.
```qml
StepRow {
  Layout.fillWidth: true
  number: index + 1; title: s.title; description: s.description; stepState: s.state; meta: s.meta
  actionText: s.state === "done" ? "Repair" : "Install"; actionGlyph: Model.Glyph.download
  progress: s.progress === undefined ? -1 : s.progress; progressText: s.detail
  errorText: s.error ? s.error.message : ""
  Component.onCompleted: setLog(svc.logLines(s.id))
  onAction: svc.install(s.id)
}
```

## LogView

Fast, bounded, monospace log list. Feed it with `lines: [...]` (declarative) or `append(text, level?, meta?)`
/ `appendMany(arr)` / `clear()` (streams; batched every 80 ms, keeps the last `maxLines`, default 5000).
Props: `follow`, `paused`, `wrap`, `showToolbar` (filter field, All/Warnings/Errors, pause, follow, copy,
clear), `toolbarExtra` (your own toolbar items), `showMeta` (left column from `meta`, e.g. the process
name), `filter`, `levelFilter` (`""|"info"|"warn"|"error"` = minimum level), `emptyText`; read-only
`count`, `shownCount`; `text()`, `copyAll()`, `scrollToEnd()`; signals `lineCopied(text)` (on a
double-click), `copiedAll(n)`. The level is guessed from the text when omitted.
```qml
LogView { id: log; Layout.fillWidth: true; Layout.fillHeight: true; onCopiedAll: function(n) { app.toast("Copied " + n + " lines") } }
Connections { target: app.device; function onDaemonEvent(name, data) { if (name === "syslog") log.append(data.message, data.level, data.process) } }
```

## Sheet

In-window modal for forms (sign-in, 2FA, pickers). Declare it anywhere in your page; it lifts itself
into the window overlay, so the scrim covers the whole app. Props: `title`, `subtitle`, `glyph`, `opened`,
`dismissible`, `busy` (progress strip, blocks dismissal), `preferredWidth`, `footer` (buttons, right
aligned). Body children go into a ColumnLayout. `open()`, `close()`; signals `shown()`, `closed()`.
Esc, the close button or a scrim click dismiss it.
```qml
Sheet {
  id: signIn
  title: "Sign in to Apple ID"; subtitle: "Your password is sent only to Apple."; glyph: Model.Glyph.apple
  busy: svc.signingIn
  TextField { id: email; Layout.fillWidth: true; placeholderText: "Apple ID (email)" }
  TextField { id: pw; Layout.fillWidth: true; placeholderText: "Password"; password: true; onAccepted: go() }
  footer: [ ActionButton { text: "Cancel"; kind: "ghost"; onClicked: signIn.close() },
            ActionButton { text: "Sign in"; kind: "primary"; onClicked: go() } ]
  onShown: email.forceActiveFocus()
}
```

## ConfirmHost / ToastHost / ShortcutSheet / Sidebar / Host

Hosted by `shell.qml`. Pages use them only through `app.confirm(...)`, `app.toast(...)` and
`app.shortcutsOpen`. `Host` (singleton) is the overlay registry: `Host.depth > 0` means an overlay owns
the keyboard. `Host.covers(item, Host.top)` is true while an overlay is open and `item` isn't inside it;
`ActionButton`, `IconButton` and `RunSplitButton` use it to hide their tooltips under a confirm dialog or sheet
(the pointer hasn't moved, so their hover would otherwise keep the tooltip up over the dim layer). Do the same for a
hand-made `PanelToolTip`. A toast's level glyph sits on its first text line, so a two-line toast keeps it beside
the first line rather than centred on the block.

## Also available from qs.Ui

`Button`, `ButtonGroup`, `TextField` (`password: true`), `NumberField`, `Toggle` (labelled row),
`ToggleSwitch`, `Dropdown`, `SearchableDropdown`, `MultiSelect`, `PanelSeparator`, `PanelToolTip`,
`CursorSurface`, `BorderSurface`. See `/usr/share/omarchy/shell/Ui/*.qml`. Don't use `KeyboardPanel`,
`PopupCard`, `Panel` or the bar widgets (they are layer-shell only).

---

## More components and app-wide features

- **QuietNote**: slim, left-aligned "nothing here yet" line for small dashboard sections where a full
  `EmptyState` would outweigh the lists beside it. Props: `glyph`, `title`, `text`, `level` (tints the glyph).
  The glyph is centred on the title's first line.
  ```qml
  QuietNote { Layout.fillWidth: true; glyph: Model.Glyph.hammer; title: "No builds yet"; text: "Builds you run show up here." }
  ```
- **ListRow**: read-only `narrow` (width < 560 px). Narrow rows move `meta` under the subtitle so the title
  keeps its room; wide rows elide `meta` at 35 % of the row. Hide optional trailing items with `!row.narrow`.
- **StepRow**: read-only `narrow` (width < 560 px). The action button and `meta` drop under the description
  instead of squeezing it.
- **LogView**: the toolbar wraps onto two rows below ~640 px (the line count hides). `copyAll()` and the
  double-click copy pipe text to `wl-copy` over stdin (no 128 KB argv limit).
- **Sidebar**: `compact` (icons only, tooltips, status dots). shell.qml turns it on below 920 px of window width.
  While `app.watch` watches a project, the Projects entry also shows a small eye (`Style.font.bodySmall`, after the
  label; in compact mode a caption-sized one in the icon's bottom corner): `Theme.faint` while it waits, accent while
  it starts or compiles, `urgent` after a failed compile. Its tooltip names the project and the state. While it
  shows, the entry's "Last build failed" dot or badge is left out (the red eye already says so).
- **Deep links**: `app.navLink(link, tab?)` accepts `"logs"`, `"logs:crashes"`, `"logs/crashes"` or an alias
  that implies a tab (`"crashes"`). IPC: `nav <page[:tab]>` and `navTab <page> <tab>`; the launcher passes its
  argument through, so `ios-studio crashes` opens Logs → Crashes. Pages read the tab from `params.tab`.
  A third segment is the action: `device:screen:mirror` opens the Screen tab and starts the live view.
- **Demo mode** (`ios-studio --demo[=ready|expiring|fresh]`, env `IOSSTUDIO_DEMO`): the helper answers from
  `helper/demo.py` and the daemon is `helper/deviced_demo.py` (a made-up iPhone, account and projects; no side
  effects). AppState then uses `$XDG_RUNTIME_DIR/ios-studio-demo/` for settings, state and cache. Use it for
  screenshots so no personal data shows.
- **PerfChart**: one metric over time (Performance tab). `title`, `unit`, `note`, `series: [{key, label, color?,
  dashed?}]`, `stacked` (areas in categorical colours from `categorical(i)`: fixed OKLCH hues at a lightness for the
  theme's mode), `samples` + `revision` (bump it to repaint), `t0`/`t1` (unix s), `events` (markers), `hoverTs` and
  `hoverAt(ts)` for a crosshair shared across charts, `yFloor`, `yFixed`, `zeroBase` (false: the axis starts near the
  lowest value), `decimals`, `gapS` (longer gaps break the line), `plotHeight`. One y-axis, and a legend for two or more series.
  ```qml
  PerfChart { title: "Network"; unit: "KB/s"; series: [{ key: "netRx", label: "In" }, { key: "netTx", label: "Out", dashed: true }]
    samples: view.samples; revision: view.revision; t0: view.t0; t1: view.t1; hoverTs: view.hoverTs; onHoverAt: function(ts) { view.hoverTs = ts } }
  ```
- **DevicePerformanceView**: the iPhone page's Performance tab (`app`). It keeps its own state (the `metrics.*`
  session, samples, events and stats) and reattaches to a running session, including one an AI agent started.
- **DevicePhoneFrame**: a drawn iPhone that shows a screenshot or mirror frame. Props: `source` (file URL),
  `live` with `liveLabel` (the corner marker, e.g. `"REC 0:12"`), `dimmed`, `rotationSteps`, `glyph`,
  `placeholder`, `screenAspect` (default the iPhone 17 Pro's), `clickable` + `clicked()`, `mirrorService`
(set it to `app.device`: the frame then counts as a mirror viewer while visible, and DeviceService pauses the
live mirror when no frame can be seen). Frames are double-buffered: the next one decodes in a hidden `Image` and they swap
  once it's ready, and a frame that arrives while one is still loading replaces the queued one. The rounded
  mask is one layer effect on the container. `frameShown(url)` fires for every frame shown; pass it to
  `app.device.ackFrame(url)` so the daemon doesn't send faster than the window can draw. `highlight`
  (`{x, y, w, h}`) outlines part of the screen in an accent box, and `highlightSpace` (`{w, h}`) is the whole
  screen; both are in the iPhone's points. The box is mapped onto the image as shown (cropped to fill the glass)
  and turns with it. Nothing is drawn without an image, a highlight or a space, so a missing size never gives a
  misplaced outline.
  ```qml
  DevicePhoneFrame { source: app.device.screenSource; live: app.device.mirroring; mirrorService: app.device
                     highlight: inspector.highlight; highlightSpace: inspector.space
                     onFrameShown: function(url) { app.device.ackFrame(url) } }
  ```
- **DeviceScreenView**: the iPhone › Screen tab: the live view (Smooth, Screenshots or AirPlay through UxPlay),
  the frame rate it reaches, recordings and stills. State and actions are on `app.device`: `mirrorMode`,
  `mirrorActive`, `measuredFps`, `mirrorNotice`, `recording`, `setMirrorMode()`, `startRecording(maxSeconds)`,
  `stopRecording()`, `dismissRecording()`. The settings keys `mirrorMode` and `mirrorView` remember the choice.
  Beside the phone, `PageTabs` switch `pane` between `"view"` (Live view: all of the above), `"elements"` and
  `"audit"` (a `DeviceInspectView`, with counts on the tabs) and `"compare"` (a `ScreenshotCompareView`; the deep
  link `device:screen:compare` opens it). While inspecting, the phone outlines the inspector's
  cursor row. `handleKey(action)` and `focusFilter()` are DevicePage's hooks into the inspector (it calls them on the
  Screen tab: `j`/`k`, Enter, `/`), and `devModeHelp()` asks the page to open the Developer Mode sheet.
- **DeviceInspectView**: Screen › Elements and Audit (`app`, `section`: `"elements"` or `"audit"`,
  `availableHeight`: the lists scroll inside it so the phone stays beside them). Elements calls `ui.tree`, filters
  by label, value, role, identifier or hint, and presses the cursor row with `ui.press` (Press button or Enter),
  then shows what changed and reads the screen again. Audit calls `ui.audit` and groups the issues by type, most
  frequent first; an issue whose `details.elementId` is in the last read links to that row (`revealElement(id)`
  and the `showElements()` signal switch the pane). `highlight` is the cursor row's frame (an audit issue's own
  frame, else its element's) and `space` the screen size in points: ui.tree's `screen`, else the size of the
  screenshot each read takes, divided by the iPhone's scale. `no-app-in-front` shows as an empty state, Developer
  Mode off as a blocked state with `devModeHelp()`. When no element has a frame (iOS 27.3), the footer says rows
  can't be outlined. `handleKey(action)` (`up`, `down`, `home`, `end`, `activate`) and `focusFilter()`.
  ```qml
  DeviceInspectView { app: root.app; section: "elements"; availableHeight: phone.height - y
                      onDevModeHelp: root.devModeHelp(); onShowElements: root.pane = "elements" }
  ```
- **DeviceInspectRow**: one row of those lists, on `CursorSurface`: `glyph`, `glyphColor`, `title` (`titleMuted`
  for a placeholder such as "No label", `titleLines` for audit messages), a dim `value` after it, and `secondary`
  under it. `pressable` adds a Press button on the cursor row (its room is always kept, so the text doesn't shift;
  `pressBusy`, `pressEnabled`, `pressTooltip`, signal `pressRequested()`). `clickable` makes the row a link with a
  chevron on the cursor row (`clickTooltip`, `clicked()`). `hovered(bool)` moves the view's cursor; `hasCursor`
  draws it.
  ```qml
  DeviceInspectRow { glyph: Model.Glyph.tap; title: "Sign in"; secondary: "Button · loginButton"
                     pressable: true; onPressRequested: view.press(el) }
  ```
- **ScreenshotCompareView**: Screen › Compare (`app`). A project (Dropdown over `app.projects.items`: the
  `compareProject` setting, else the last build's project, else the first) and a screen name (TextField; the
  project's baselines are listed below it as `ListRow`s, a click picks the name, the bin deletes). Compare takes a
  screenshot with `app.device.screenshot()` and calls `screenshots compare`; Save as baseline / Replace baseline
  takes one and calls `screenshots save` (replacing asks first). The result: a verdict `StatusPill` (Same ok,
  Changed and Size mismatch warn, No baseline info) with the diff % and the threshold, a line with the number of
  changed areas and the ignored status bar, then baseline | current | diff in three equal `ScreenshotDiffPane`s,
  and Accept as new baseline (`screenshots save` with the compared `current_path`, confirms when it replaces one)
  and Delete baseline (`screenshots delete --confirm` after `app.confirm`). The list is read when the pane is
  shown and after every save or delete; nothing polls. Every image reloads after a save (`revision`).
  `setName(name)` fills the name field. Disabled buttons say why (no iPhone, no project, no name, busy).
  ```qml
  ScreenshotCompareView { app: root.app }
  ```
- **ScreenshotDiffPane**: one image of that comparison: `title`, `detail` (caption, e.g. the size), `path` (local
  file), `revision` (bump to reload a file rewritten in place; the Image has `cache: false` and a `?v=` query),
  `imageSize` (`{w, h}` pixels), `regions` (`[{x, y, w, h}]` pixels, outlined in the warn colour, at least 6 px on
  screen, grown outward by the stroke plus a small gap so it never touches the change), `maskedTop` (pixels at the top that are shaded as ignored), `placeholder` + `placeholderGlyph` (no
  `path`), `cellHeight` (set by the caller so the panes match). The image keeps its aspect ratio and is centred;
  a click opens it with `xdg-open`.
  ```qml
  ScreenshotDiffPane { title: "Current"; path: r.current_path; imageSize: { w: r.width, h: r.height }
                       regions: r.regions; maskedTop: r.masked_top; cellHeight: panes.cellH }
  ```
- **DeviceRecordingPreview**: a muted, looping player for a finished recording (`source`: file URL). It imports
  QtMultimedia, so load it through a `Loader`; without qt6-multimedia the Loader fails quietly and the view
  leaves the preview out.
- **SettingRow**: preference row with `title`, `description`, `glyph`, `note` + `noteLevel` (a coloured line
  under the description) and `separator`. Its children are the control on the right.
  ```qml
  SettingRow { title: "Notifications"; description: "Tell me when a build finishes."
    ToggleSwitch { checked: app.settings.notifications; onToggled: app.setSetting("notifications", !checked) } }
  ```
- **AccountSignInSheet**: the Apple ID sign-in sheet (email and password, 2FA code, team picker), driven by
  `app.account`'s sign-in flow. Props: `app`; `begin(email?)`; signal `signedIn(status)`. The password stays in
  the sheet's memory and goes to the helper over stdin.
- **AccountRevokeWarning**: explains why one Apple ID on two computers revokes the other's certificate.
  Props: `compact`, `expanded`.
- **ProjectCard**: one project on the Projects page. Props: `project`, `svc` (`app.projects`), `hasCursor`,
  `expanded`; read-only `narrow`. Run is the main action, the rest sit behind More. Signals `run()`, `buildIpa()`,
  `checkBuild()`, `renew()`, `openEditor()`, `openFolder()`, `removeFromList()`, `toggleAutoRenew()`,
  `openBuildSettings()`, `editPresets()` (Edit presets… in Run's menu, or More › Run presets), `showBuild()`,
  `hovered(bool)`; read-only `blocked` (a generated project its build settings don't allow yet) and `runPreset` (the
  selected preset's name, from `svc.selectedPreset(project)`). Run is a RunSplitButton; a selected preset shows as
  a faint "Runs with <name>" after the bundle ID, which elides before the bundle ID does. It asks
  `svc.checkSoon(project)` for a project check when it appears, gets the cursor or expands, and shows a
  ProjectCheckView below the details. It also asks `svc.editorStatusSoon(project)` once when it appears; read-only
  `editorHint` is the helper's `hint` while the project isn't ready for completion ("Build once to enable
  completion"), shown faint with a file glyph at the end of the folder line (on its own line under it on a narrow
  card). Nothing shows when it's ready. Editor setup itself is machine-wide (Settings › Code editor), so the card has
  no editor button and its right cluster stays as it is. Watch mode: prop `watchSvc` (`app.watch`), read-only
  `watchState` (`watchSvc.forProject(project)`) and `watchInfo` (`watchSvc.label(…)`); signal `showProblems()`. While
  the project is watched, a WatchStateLabel takes the place of the title row's last-build pill (the "Building…" pill
  still wins during a manual build) and the project name keeps up to 140 px before it elides. The toggle itself
  lives in Run's menu, so nothing is added to the right cluster. Format and lint: More has Format project… and Lint
  project (ghost compact, after Check build); signals `formatProject()` (the page opens ProjectFormatSheet) and
  `lintProject()` (the page calls `svc.showLint` and opens the drawer on Problems). Read-only `lintEntry`
  (`svc.lintFor(project)`): Lint project spins while it runs, and `lintTip` adds the last count and when.
  `F` / `L` on the page do the same for the highlighted card.
- **RunSplitButton**: a project's `[▶ Run | ▾]` split button. Props: `project`, `svc`, `disabled` (blocks only the
  Run part; the menu stays usable), `tooltip`; read-only `presets`, `selected`, `rows`, `popupOpen`; `openMenu()`,
  `closeMenu()`, `pick(i)`; signals `run()`, `editRequested()`. The chevron opens a menu right-aligned under the
  button: Default (no preset), each preset (a check on the selected one, its language or `Model.presetSummary`
  beside it) and Edit presets… (New preset… when there are none). Picking a preset calls `svc.selectPreset`.
  `j`/`k` or the arrow keys move, Enter picks, Esc closes. With `watchSvc` (`app.watch`) the menu ends, under a
  hairline, with Watch for changes (a check while on; detail "compile on save", or "instead of <project>" when
  another one is watched) and Run after a clean compile (detail `watchSvc.autoRunBlockerShort` when it can't).
  `watchBlocker` (a short reason, "" = allowed) greys out Watch for changes; greyed rows stay in the menu, faint,
  with their reason in the detail column, and don't react to a click. Rows take clicks with a MouseArea, and the
  menu's background swallows the rest, so a click never also reaches what lies under the (non-modal) menu: a
  TapHandler only grabs passively. ToastHost dismisses toasts the same way. These two rows close the menu and call
  `watchSvc.toggle(project)` / `watchSvc.setAutoRun(project, on)`; the cursor opens on the preset rows, never on them.
- **WatchStateLabel**: watch mode's state on a project card: a glyph and small text in the quiet StatusPill style
  (level colour, `Theme.dim` for info, spinner glyph for busy) that elides and is clickable (underlined on hover,
  `PanelToolTip` with the detail). Props: `info` (`{level, glyph, text, tip}` from `app.watch.label(w)`); signal
  `clicked()`.
- **WatchService** (`app.watch`, `services/WatchService.qml`): watch mode (compile on save, `helper/watch.py`). One
  watcher per user. The service follows `<runtime dir>/watch.json` with a FileView (re-read every 2 s while the
  window is focused; `watch status` every 15 s only to catch a crashed watcher), so a watch an agent started shows
  too, and runs `watch run --project P [--auto-run]` as a Stream for a watch it starts (the stream keeps the watcher
  alive; `{"cmd": "stop"}` on its stdin stops it). `current` is `null` or `{path, name, state, autoRun, owned, pid,
  seq, message, reason, changed, progress, last: {id, ok, errors, warnings, duration, time, logFile, diagnostics,
  errorMessage}, run, mode, modeReason}`; `active`, `state` ("off" when nothing is watched), `busy` (`starting` /
  `stopping`), `now` (ticks every 5 s while active and focused). `forProject(p)` (also while starting),
  `isWatching(p)`, `start(p, autoRun)`, `stop()` (an agent's watch through `watch stop`), `toggle(p)`,
  `setAutoRun(p, on)` (changes the running watcher through `watch run --detach`, no restart), `showLast()`,
  `autoRunBlocker` / `autoRunBlockerShort` (no iPhone, no Apple ID), `label(w)` → `{level, glyph, text, tip}`. Each
  new `lastBuild` goes to `app.projects.showWatchBuild(…)` with the full diagnostics from the build history (matched
  by `buildId`, else the log file; the snapshot's first five when not found). See
  [features/watch-mode.md](features/watch-mode.md).
- **RunPresetsSheet**: a project's Run presets sheet. Props: `app`, `svc`; `openFor(project)` opens the list,
  `openFor(project, name)` goes straight to that preset's editor. The list has Default (no preset) and one row per
  preset with Use for Run (or a "Used by Run" pill), edit and delete (asks first); the editor has name, language
  (Same as the iPhone, `Model.PRESET_LANGUAGES` or Other language code…), region (with a "Use <region>"
  suggestion), launch argument rows, environment variable rows, the UI test mode switch and a "What Run passes to
  the app" preview with the generated arguments tagged by their source. It validates like the helper (name, codes,
  env names) and shows the helper's error when a save is refused. Loads and saves through `svc.loadPresets` /
  `svc.savePreset`. See [features/run-presets.md](features/run-presets.md).
- **ProjectCheckView**: the project check on a ProjectCard. Props: `project`, `svc`, `showClean` (show the row for a
  clean project or a first check still running; the card passes its More state), `open` (issues shown). A summary
  row ("2 settings warnings", a spinner while the first check runs, "Couldn't check…" on an error) with Check again
  (off while this project is building), expanding to one BuildDiagnosticRow per issue (`showChip: false`; a click
  opens the file at the line) and a "Showing N of M" line when the result was cut. Reads `svc.checkFor(project)`.
  See [features/project-check.md](features/project-check.md).
- **ProjectNewSheet**: the New app sheet (name, template, location, bundle id). Props: `app`, `svc`,
  `template` (default `swiftui-app`); `open()`, `reset()`; signal `created(project)`.
- **ProjectBuildSettingsSheet**: a project's Build settings sheet (prebuild command, environment variable rows,
  open-file limit, Build generated project, and the project scripts' suggestions). Props: `app`, `svc`;
  `openFor(project)`. Loads and saves through `svc.loadBuildSettings` / `svc.saveBuildSettings`. See
  [features/project-build-settings.md](features/project-build-settings.md).
- **BuildPanel**: the Projects page's build drawer (steps, progress, Problems and raw log, result banner). It
  slides in from the right and isn't modal. Props: `app`, `svc`, `opened`, `tab` (`log` or `problems`); read-only
  `log` (the LogView); signal `closeRequested()`. It takes hover and unused wheel over its whole area, so a card's
  tooltip behind it can't show through and scrolling past the end of Problems or Log doesn't move the page. A run with a run preset names it in the subtitle ("Run
  (German)"), the launch step and the result banner. A compile on save (`build.watch`) shows like a finished
  Build: subtitle "Compiled on save" (plus "· compiling again…" while the next one runs), the result banner, the
  Problems tab (it switches there when the compile failed with errors) and its saved log (`build log --file`) in Log.
  Style warnings: a StyleWarningsSection is the Problems list's footer, for `stylePath` (`svc.lintFocus`, else the
  build's project; it asks `svc.lintLastSoon(path)` when that changes). With no build problems the list's header is a
  QuietNote "No build problems". The tab's count adds the shown style warnings (`styleCount`); its colour, the
  headline and the banner only follow the build's own `errorCount` / `warningCount`. While `lintView`
  (`svc.lintFocus` is set) the drawer shows only that project's style warnings: headline "5 style warnings"
  ("No style warnings", "Checking the style") with the lint glyph (spinner while running), subtitle
  "<name> · Code style · <path>", without the path when the whole line doesn't fit (the name lives there so it
  never elides the count), no steps, banners, tabs or
  Log, and a ghost Show build (when there is a build; it clears `lintFocus`), centred on the close button. Signal
  `formatRequested(path)` (the section's Format project…).
- **StyleWarningsSection**: the style warnings of one project (`app`, `svc`, `path`), from `svc.lintFor(path)`.
  A SectionHeader "Style" with the count ("5", "5 hidden", "checking…") and ghost compact Hide/Show (the
  `showStyleWarnings` setting) and Check again (`svc.lint`; busy while it runs); a faint line with the tools, the
  style (`config_path` or "default style") and when it ran; "Formatting fixes N of these." with Format project…
  (signal `formatRequested()`) when some findings carry the helper's "Format project …" hint (that hint is left out
  of their rows); then one BuildDiagnosticRow per warning (a click opens the file at the line). Loading, error, "No
  style warnings" (with the tools line) and hidden states are status lines on the rows' glyph column and text edge.
  Read-only `total`, `shown`, `visibleCount`. A truncated or timed-out result says so under the rows.
- **ProjectFormatSheet**: "Format project…" (`app`, `svc`; `openFor(project)` opens and runs the dry run through
  `svc.formatPreview`). `phase`: `loading` (LoadingState; Esc still closes), `ready`, `error` (ErrorState with the
  helper's hint; its fix button closes the sheet) or `applying` (the Sheet's busy strip). Ready: "N files would
  change" with files checked, style and swift-format version (glyph column and gap as the Sheet header, so the
  text starts at the title's edge), an amber box for files swift-format couldn't read (first five), the DiffView,
  and for a cut diff "Showing the start of a long diff." with the full diff's path and Open. Nothing to change:
  EmptyState "Already formatted". Footer: Cancel (Close when already formatted) and primary Apply to N files,
  `disabled` with `applyBlocker` as its tooltip while loading, after an error or with nothing to change. Apply
  calls `svc.formatApply` and closes on success; a failed apply shows a banner above the diff. Answers for an
  earlier open are dropped (`_ticket`).
- **DiffView**: a read-only unified diff (`text`, `maxHeight`, `maxLines`; read-only `rows`, `fileCount`). While it
  scrolls, a wheel past its top or bottom stops there instead of scrolling the page behind the sheet.
  `parse(text)` turns `---`/`+++` pairs into one file row (file glyph + path, bold, `surfaceRaised`), `@@` lines into
  hunk bands (accent text, the function context faint, accent tint) and the rest into add/del/context rows with old
  and new line numbers. Added lines use `Theme.success`, removed ones `Theme.error`, each on a 14 % tint with a 2 px
  bar in that colour; context is dim. Trailing spaces on changed lines show as dots. Tabs become 4 spaces. Lines
  don't wrap: the list scrolls both ways once taller than `maxHeight` or wider than the box, and the line numbers,
  +/- marks and file paths stay in place while it scrolls sideways (the font is monospace, so widths come from one
  character's advance).
- **BuildDiagnosticRow**: one build diagnostic. Props: `diag` (`{severity, message, category?, file?, line?, col?,
  hint?, details?}`, see `helper/diagnostics.py`), `projectPath`, `hasCursor`; signals `clicked()` (opens the file at
  the line; only for diagnostics with a file), `hovered(bool)`. `category` is one of `compiler`, `linker`,
  `package`, `signing`, `device`, `toolchain`, `config`, `other` (a row with a file and no category counts as
  `compiler`). Non-compiler rows show their category label, up to three `details` lines and the `hint` (what to do).
  A `config` diagnostic with a `check` id comes from the pre-build project check: instead of the category label it
  gets a "Pre-build check" chip beside the message, or at the start of the location line on narrow rows.
  `showChip` (default true) turns the chip off, as in ProjectCheckView where every row is such an issue. A
  relative `file` is shown as it is (project check results are relative to the project). A `style` diagnostic
  (`format-lint`) reads "Style · <rule> · file:line" and its glyph is `Theme.dim` (`Theme.warn` for a rule set to
  error), never the error colour. Short of room, the location line first middle-elides the folder, hiding it once
  fewer than about 6 of its characters would show, then elides the lead ("Style · line_leng… · "); the file
  name:line:col stays whole unless it alone is wider than the row.
- **ProjectService watch builds** (`app.projects`): `showWatchBuild({path, name, ok, errors, warnings, duration,
  logFile, buildId, diagnostics, errorMessage, errorCode})` puts a finished compile on save into `build` (`kind:
  "compile"`, `watch: true`, not active) and `diagnostics`, then emits `watchBuildShown(ok)`. It does nothing while a
  manual build runs (that one owns the panel). `_start` refuses a manual build of a project the watch is compiling
  right now (a toast), and a `busy` error from the helper is reworded when the watch was the other build.
- **ProjectService build diagnostics** (`app.projects`): `diagnostics` is a `ListModel` of `{n}` rows, errors
  before warnings before notes (within a severity, pre-build check rows (those with a `check` field) before the build's own output), kept outside
  `build`; `diagnostic(n)` returns the object (it never changes once
  added) and `diagnosticList()` a copy of all of them. `errorCount` and `warningCount` count up while the build
  runs, and `configCount` counts the pre-build check warnings among them (BuildPanel's banner says "N in project
  settings"); `diagnosticAdded(d)` fires per new one. Bind a Repeater/ListView to `diagnostics` and read
  `svc.diagnostic(model.n)` in the delegate.
- **ProjectService format and lint** (`app.projects`, `format` / `format-lint`, helper/format.py):
  `formatPreview(p, onDone, onFail)` (dry run) and `formatApply(p, onDone, onFail)` (writes, toasts "Formatted N
  files in <name>", lints again when that project has a lint result). `lints` maps a project path to `{state,
  result, error, at}` (`at` in ms, from the result's `at`); `lintFor(p)`, `lint(p, onDone?, onFail?)` (keeps the
  old result while it runs), `lintLastSoon(p)` (`--last`, once per path and session; an empty stored result is
  ignored). `lintFocus` is the project the drawer shows style warnings for instead of the build; `showLint(p)` sets
  it (or clears it when the drawer already shows that project's build) and lints; a build starting or a compile on
  save shown clears it. Errors arrive as the helper's error object, with `hint`.
- **ProjectService project checks** (`app.projects`): `checks` maps a project path to `{state: "loading" | "ready"
  | "error", result, error, at}`; `checkFor(p)` reads one. `check(p, onDone?, onFail?)` runs `projects check` now,
  `checkSoon(p)` queues it (one at a time, skipped while loading or checked in the last minute) and `checkAll()`
  queues every project. The Projects page calls `checkAll()` when it opens, and a finished build calls `check()`
  for its project.
- **ProjectService run presets** (`app.projects`, `projects presets`): `presetsOf(p)` (each with `resolved`) and
  `selectedPreset(p)` (the name, or `""`) read the newest answer of `projects presets` for that project, else
  `runPresets` / `runPreset` from `projects list`; a list refresh started after that answer takes over again.
  `loadPresets(p, onDone, onFail)`, `savePreset(p, preset, renameFrom, onDone, onFail)` (toasts on success),
  `deletePreset(p, name, onDone, onFail)` (asks first; says when Run goes back to a plain launch) and
  `selectPreset(p, name, onDone, onFail)` (`""` clears; shown at once and rolled back with a toast if the helper
  refuses). `onDone` gets the helper's `{path, presets, selected}`. `run(p)` passes `--preset` with the selected
  one; `build.preset` holds its name, and the launch passes the build's `launch.args` / `launch.env` to the
  daemon's `app.launch`.
- **LogDeviceTab / LogSyslogView**: the Logs page's Device log tab (`app`) and its live list.
  LogSyslogView takes SyslogEntry batches with `push(batch)` (batched every 80 ms, the last `maxLines`, default
  5000) and filters on `minLevel`, `processes` and `query` (highlighted). Props: `follow`, `paused`,
  `emptyTitle`, `emptyText`, `selectedSeq`; read-only `count`, `shownCount`, `pendingCount`, `selectedRow`;
  `clear()`, `select(row)`, `selectNext(d)`, `deselect()`, `selectedText()`, `visibleText()`, `jumpToLatest()`;
  signal `copyRequested()`.
- **LogConsoleTab**: the Logs page's Console tab (`app`): what an app launched with the console prints, in a
  LogView without its toolbar (stderr lines are `warn`/`error`, meta = time). Launch, the session picker, filter,
  stderr-only (when the streams are split), pause, copy, export and clear. Read-only `view`, `search`;
  `togglePause()`, `copySelected()`. See [features/app-console.md](features/app-console.md).
- **LogBuildsTab**: the Logs page's Build logs tab (`app`): build history, then the selected build's summary,
  problems and full log.
- **CrashBrowser / CrashReportView**: the Logs page's Crashes tab (`app`; `scope` `mine`/`all`) and one report
  (`app`, `item` = a CrashItem): a plain-language summary card, the crashed thread and the full report, with
  `save()` (to `~/Downloads`) and `copy()`. The crashed thread's frames are **CrashFrameRow**s (`text`, `info` =
  an entry of `summary.symbolication.frames` or null, `mine`; signal `clicked()`): a resolved app frame shows
  its function with file:line below, and a click opens it in the editor; under them, the symbolication status.
- **DeviceConnect**: "Connect your iPhone", shown when no paired iPhone is known: cable or Wi-Fi. Props: `app`;
  signal `pairRequested(method)`.
- **DevicePairSheet**: the pairing sheet, driven by the daemon's `pair` events (`app.device.pair`). Props: `app`;
  `begin(method)` with `"usb"` or `"wifi"`.
- **DeviceSteps**: numbered instructions for pairing and Developer Mode. `steps: [{text, done?, active?}]`.
- **DeviceHero**: the iPhone page's header: the drawn phone with the latest screen, name, battery, storage and
  readiness pills, plus fixes when something needs the user. Props: `app`; signals `openScreen()`,
  `devModeHelp()`.
- **DeviceAppsView**: installed apps (search, launch, stop, remove). Props: `app`, `cursor`, `devFirst`
  (your own builds first); read-only `search` (the FilterField); `current()`; signal `cursorMoved(index)`. The page
  owns the cursor.
- **DeviceInfoView / DeviceProfilesView / DeviceLocationView**: the iPhone page's Info tab (every detail,
  copyable), Profiles tab (read-only, expired ones behind `showExpired`) and Location tab (a simulated GPS
  location from presets or latitude/longitude). Props: `app`.
- **EditorSection**: Settings › Code editor (a Card). Props: `app` (uses `app.projects`: `editorInfo`,
  `loadEditors()`, `editorSetup(id, confirm, …)`). "Your editor" is a Dropdown (Omarchy default, each installed
  editor, Other command… with a field) that writes `editorSetup` (the helper's `preferred`) and `editorCommand`
  together through `choose(id, keepCustom)`. "Swift completion" lists one EditorSetupRow per installed editor and one
  quiet "Not installed: …" line (or "No supported editor found"), with a Skeleton while loading and an ErrorState on
  failure. Set up… dry-runs into the row; Apply runs `--confirm` and makes that editor Your editor (a custom command
  is kept). Helpers: `tidy(text)` (home → `~`), `kindLabel(kind, done)` (write/merge/skip/backup → Create/Update/Keep/Back up, or Created/…),
  `editorConfig(action)` (`scope: "user"` but not iOS Studio's settings.json, which is also user scope), `stateText`,
  `stateLevel`, `commandOf(editor)`, `openDocs()`. Deep link `nav("settings", {section: "editor"})`. See
  [features/editor-setup.md](features/editor-setup.md).
- **EditorSetupRow**: one installed editor in EditorSection, laid out like the AI tools rows (32 px tile, name and a
  quiet pill: Ready, Not set up, One step left, Set up by hand; the note below; a fixed-width button slot). Props:
  `section`, `editor` (an `editor status` entry), `plan` (`{state: loading|ready|error|running|done, result,
  error}`), `first`, `slotWidth`; signals `setupRequested()`, `applyRequested()`, `cancelRequested()`. A
  `partial` editor lists its `notes` (steps only you can do) in warn. The dry run shows under the text as a kind
  column, the file path once (mono), its `detail` and any `kept` values below it, then one warn line ("Changes your
  Neovim config for every project.", plus the backup when there is one), then Apply (Change <editor> config when it touches that config) and Cancel. Once done,
  it lists what it did and the next step. Neovim without LazyVim (`editor.lazyvim === false`) gets no Set up, only
  Snippet (opens the docs).
- **AgentsSection**: the Settings › AI agents card (helper `agents` group). Props: `app`. Public
  `refresh()` and `prepare(repair)`; SettingsPage deep-links to it with `nav("settings", {section: "agents"})`.
- **DeviceNetworkView / DeviceCaptureView**: the iPhone page's Network tab. Props: `app`. DeviceNetworkView
  lists the conditions (`condition.list`), applies one through `app.confirm` and shows the active-condition
  banner with its countdown and Reset now. It embeds DeviceCaptureView (packet capture, USB only). The
  active condition and the capture state live in `app.device` (`condition`, `conditionActive`, `capture`,
  `capturing`), so the page header pill, the tab badge and the sidebar badge follow them on every page.
- **DeviceFilesView**: the iPhone › Files tab. Props: `app`, `active` (the tab is showing; the parts load on
  first show), `section` (`media`, `apps`, `camera`; remembered in the `filesSection` setting); `show(section)`.
  Deep link: `nav("device:files:camera")`. It holds the three parts below, each with the same `app` and
  `active` props:
  - **DeviceMediaView**: the camera roll as a thumbnail grid (`media.list` in pages of 120, `media.thumb`
    four at a time), click and Shift+click selection, and import (`media.pull`) to the `mediaImportDir`
    setting (default `~/Pictures/iPhone`) with the `mediaConvertHeic` switch.
  - **DeviceContainerView**: an app's container (`container.*`): an app picker limited to apps that share
    files and your own builds (`filesApp` setting), breadcrumbs, Add files (file dialog), New folder,
    Save to… (folder dialog), Delete (through `app.confirm`), and a `DropArea` that copies files dropped from
    the desktop into the open folder. A push that would overwrite asks first.
  - **DeviceCameraTestView**: the camera test form and run (`cameratest.run`): app, test files (file dialog
    or drop), launch arguments, environment and timeout (settings `cameraTestApp`, `cameraTestInputs`,
    `cameraTestArgs`, `cameraTestEnv`, `cameraTestTimeout`), live steps, then `done.json`, output images,
    the app's log and a crash banner.
- **DeviceTransferBar**: progress of one daemon `transfer` (file, n of m, bytes) with a Cancel button.
  Props: `transfer` (the event data or null; hidden when null), `verb`; signal `cancel()`.
  ```qml
  DeviceTransferBar { Layout.fillWidth: true; transfer: root.transfer; verb: "Importing"; onCancel: svc.request("transfer.cancel", { transferId: id }, null) }
  ```
- **DeviceDebugView**: the iPhone › Debug tab. Props: `app`. It loads itself when it becomes visible (`debug check`,
  `debug sources`, `debug.symbols`) and owns the backtrace stream; the breakpoint session is a `DebugSessionPanel`.
  Its Debug symbols card follows the status `state`: a StatusPill, the reason or error, one primary fix
  (`symFix`: download, update, unpack or retry), Remove (confirmed) and the Keep the download switch.
- **DebugSessionPanel**: a breakpoint session on one app (helper `debug session` as a `Stream`; commands are
  written to its stdin). Props: `app`, `bundleId`, `appName`, `appRunning`, `sources` (`debug sources` files),
  `hasLocalBuild`, `blocked`; read-only `phase` (`idle|starting|running|stopped|ended|failed`) and `live`.
  `start()`, `send(cmd, extra)`, `end(kill)`. Its Stream's `quitMessage` detaches, so closing the window lets the app run.
- **DebugBacktraceView**: collapsible threads of a captured backtrace. Props: `app`, `backtrace` (`{threads,
  crashedThread, selectedThread}`). The crashed thread, else the selected one, starts open; clicking a frame copies it.
- **DebugFrameRow**: one stack frame (`{index, function, module, file?, line?, inApp}`): your own code in the accent
  colour, file:line, module. Props: `frame`, `selected`, `tooltip`; signal `activated()`.
