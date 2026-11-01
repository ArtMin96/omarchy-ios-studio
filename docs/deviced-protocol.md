# Device daemon protocol (`bin/iosstudio-deviced`)

Owner: device slice. Consumers: `services/DeviceService.qml` (device slice) and
`services/LogService.qml` (logs slice, through `app.device.request()` / `app.device.daemonEvent`).

The daemon is a long-running Python process (pymobiledevice3 venv, see `common.pymd3_python()`)
started by DeviceService while the app is open. It talks **JSON lines** over stdin/stdout. stderr
is free-form debug text (never parse it). It exits when stdin closes (the app quit) or on
`daemon.shutdown`.

## Framing

Request (UI → daemon), one per line:

```json
{"id": 7, "cmd": "apps.list", "args": {"udid": "00008150-…"}}
```

Reply (daemon → UI), exactly one per request, possibly out of order (requests run concurrently):

```json
{"id": 7, "ok": true, "data": [ ... ]}
{"id": 7, "ok": false, "error": {"code": "locked", "message": "Your iPhone is locked. Unlock it and try again.", "fix": {"label": "Retry", "page": "device"}, "detail": "raw exception text"}}
```

Event (daemon → UI, unsolicited):

```json
{"event": "devices", "data": [ ... ]}
```

Rules:
- `id` is any JSON scalar chosen by the UI; it's echoed back verbatim.
- `args.udid` is optional everywhere. Omitted = the **active device** (see `device.select`).
- Every `error` has the helper's error shape (see [development.md](development.md#helper-protocol)): `code`, plain-language `message`, optional `fix`
  (`{label, page, step?, action?}`) and optional `detail`.
- Times: `ts` fields are Unix epoch seconds (float); `date` fields are ISO-8601 strings.
- Paths returned are absolute local paths under `~/.cache/ios-studio/`.
- Slow commands: a request never blocks events or other requests. A command that needs the
  tunnel waits up to 25 s for it to come up, then fails with `unreachable`.

### Common error codes

| code | meaning (message is already user-facing) |
|---|---|
| `no-device` | No iPhone is known or selected. fix → `{page:"device", action:"pair"}` |
| `unreachable` | The phone didn't answer (asleep, other network, unplugged). |
| `locked` | The phone is locked; unlock it. |
| `not-trusted` | "Trust This Computer" wasn't accepted / pairing record invalid. fix → pair |
| `not-paired` | No pairing record for Wi-Fi. fix → `{page:"device", action:"pair"}` |
| `developer-mode-off` | Developer Mode is off. fix → `{page:"device", action:"devmode"}` |
| `ddi` | The developer disk image couldn't be mounted (needs internet). |
| `not-found` | App / crash report / process not found. |
| `bad-args` | Missing or invalid argument. |
| `busy` | Another exclusive operation (pairing) is running. |
| `pmd3-api` | The installed pymobiledevice3 lacks an API. fix → `{page:"setup", step:"pymobiledevice3"}` |
| `failed` | Anything else (see `detail`). |

## Device object

Used by `devices` event, `devices.list`, `device.select`.

```json
{
  "udid": "00008150-000000000000001C",
  "name": "My iPhone",            // "" until first connect (cached afterwards)
  "productType": "iPhone18,1",
  "model": "iPhone 17 Pro",             // marketing name ("" if unknown)
  "ios": "27.2",
  "build": "24B115",
  "transport": "wifi",                  // "usb" | "wifi" | null (not connected right now)
  "usb": false,                         // seen on usbmux right now
  "wifiPaired": true,                   // ~/.local/share/pymobiledevice3/remote_<udid>.plist exists
  "usbPaired": true,                    // /var/lib/lockdown/<udid>.plist exists
  "state": "connected",                 // see connection states
  "active": true                        // the device commands default to
}
```

Device name/model/iOS are cached in `~/.cache/ios-studio/devices.json` so the UI shows them before
the tunnel is up.

## Events

| event | data | when |
|---|---|---|
| `ready` | `{version, pymobiledevice3, python, pid}` | once, at start |
| `devices` | `[Device…]` | at start and whenever the list or any field changes |
| `connection` | Connection (below) | on every connection state change of the active device |
| `deviceInfo` | `{udid, …partial DeviceInfo}` | after connect and every 60 s (battery etc.) |
| `frame` | `{path, seq, ts, width, height}` | mirror running; one per screenshot |
| `mirror` | `{running, fps, error?}` | mirror started/stopped/failed |
| `syslog` | `[SyslogEntry…]` (array!) | batched, see Syslog |
| `syslogState` | `{running, udid, filters, dropped, reconnecting, error?}` | stream state change |
| `pair` | Pair (below) | pairing flow progress |
| `appIcon` | `{udid, bundleId, path}` | icon fetched in the background after `apps.list` |
| `installProgress` | `{path, percent, status}` | during `app.install`; `status` ∈ `uploading` (percent 0), `installing`, `done` (100) |
| `appSizes` | `{udid, sizes: {bundleId: bytes}}` | ~9 s after `apps.list` (sizes are slow to compute) |
| `log` | `{level, text}` | daemon diagnostics (optional to show) |

### Connection

```json
{"udid": "…", "state": "connected", "transport": "wifi",
 "message": "Connected over Wi-Fi", "detail": "", "retryIn": 0, "since": 1730459000.1}
```

`state` ∈

| state | message examples |
|---|---|
| `idle` | "No iPhone selected" |
| `searching` | "Looking for your iPhone…" |
| `connecting` | "Connecting over Wi-Fi…" / "Connecting over USB…" |
| `connected` | "Connected over Wi-Fi" / "Connected over USB" |
| `locked` | "Locked — unlock your iPhone" (connected, but the phone is locked) |
| `untrusted` | "Not trusted — tap Trust on your iPhone" |
| `not-paired` | "Not paired for Wi-Fi — pair it once" |
| `unreachable` | "Can't reach your iPhone — is it awake and on the same Wi-Fi?" (`retryIn` s until next try) |
| `error` | plain message + `detail` |

The daemon keeps one tunnel open to the active device and reconnects with backoff
(3, 5, 10, 20, 30 s, then every 30 s). USB is preferred over Wi-Fi when both are available;
plugging in a cable switches transport on the next reconnect.

## Commands

### Daemon / devices

| cmd | args | reply data |
|---|---|---|
| `daemon.ping` | – | `{pong: true, ts}` |
| `daemon.shutdown` | – | `{}` then the process exits |
| `devices.list` | – | `[Device…]` |
| `device.select` | `{udid}` | `Device`; makes it active, (re)opens its tunnel |
| `device.reconnect` | `{udid?}` | `{}`; drops the tunnel and reconnects now (resets backoff) |
| `device.info` | `{udid?, fresh?: bool}` | DeviceInfo (below). `fresh` re-reads battery/storage/DDI |

DeviceInfo:

```json
{
  "udid": "…", "name": "My iPhone", "productType": "iPhone18,1", "model": "iPhone 17 Pro",
  "hardwareModel": "V53AP", "ios": "27.2", "build": "24B115", "cpu": "arm64e",
  "deviceClass": "iPhone", "serial": "", "wifiAddress": "aa:bb:…", "timeZone": "Asia/Yerevan",
  "transport": "wifi", "address": "fe80::…%wlp2s0",
  "battery": {"level": 84, "charging": false, "external": false, "cycleCount": 322, "health": 91},
  "storage": {"total": 256000000000, "free": 120000000000, "used": 136000000000},
  "developerMode": true, "ddiMounted": true, "locked": false, "passcodeSet": true,
  "wifiPaired": true, "usbPaired": true, "pymobiledevice3": "11.49.0"
}
```

Any field may be `null` when unknown (e.g. `battery` if the diagnostics service failed).

### Apps and processes

| cmd | args | reply data |
|---|---|---|
| `apps.list` | `{udid?, system?: false, icons?: true, sizes?: bool}` | `[App…]` sorted by name (~0.7 s over Wi-Fi) |
| `app.launch` | `{bundleId, args?: [str], env?: {}, udid?}` | `{bundleId, pid}` (mounts DDI first) |
| `app.kill` | `{bundleId, udid?}` | `{bundleId, killed: [pid…]}` |
| `app.uninstall` | `{bundleId, udid?}` | `{bundleId}` (UI must confirm first) |
| `app.install` | `{path, udid?}` (signed `.ipa` or `.app` folder) | `{path, bundleId, packageBundleId, seconds}` — `bundleId` is the id as installed (free teams: `XTL-<TEAM>.<id>`) |
| `processes.list` | `{udid?, appsOnly?: false}` | `[{pid, name, path, isApp, bundleId?}]` |

App:

```json
{"bundleId": "XTL-ABCDE12345.com.example.myapp", "name": "MyApp", "version": "0.1.0",
 "build": "1", "developer": true, "removable": true, "hidden": false, "system": false,
 "executable": "MyApp", "path": "/private/var/containers/Bundle/Application/…/MyApp.app",
 "icon": "<home>/.cache/ios-studio/icons/<udid>/<bundleId>.png", "running": false, "pid": null}
```

`size` (bytes, app + data) is `null` unless `sizes: true` was passed (which makes the reply ~9 s
slower); with `sizes` omitted the daemon computes them in the background and sends `appSizes`.
`sizes: false` skips that. `icon` is `null` when not cached yet; with `icons: true` the daemon fetches missing icons in the
background (CoreDevice icon service) and sends one `appIcon` event per icon.
`developer` = signed with a development profile (e.g. built with xtool).

### Screen

| cmd | args | reply data |
|---|---|---|
| `screenshot` | `{udid?, save?: bool}` | `{path, width, height, bytes, ts}` |
| `mirror.start` | `{udid?, fps?: 1.0}` (0.2–4) | `{running: true, fps}` |
| `mirror.stop` | – | `{running: false}` |

`screenshot` writes `~/.cache/ios-studio/screens/shot-<YYYYmmdd-HHMMSS>.png`. Mirror frames go to
two rotating files `~/.cache/ios-studio/screens/mirror-<udid>-{0,1}.png` (the UI must bust its
image cache, e.g. `source = "file://" + path + "?" + seq`). Frames are only produced while the
screen is reachable; a failure emits `mirror {running:false, error}`.

### Location

| cmd | args | reply data |
|---|---|---|
| `location.set` | `{lat, lon, udid?}` | `{lat, lon}` (DVT simulate-location; mounts DDI) |
| `location.clear` | `{udid?}` | `{}` |

The simulated location stays active until `location.clear`, a phone reboot, or the daemon's DVT
connection closing (iOS restores the real location when the instrument connection ends). The
daemon keeps the connection open while a location is set.

### Developer Mode and DDI

| cmd | args | reply data |
|---|---|---|
| `ddi.status` | `{udid?}` | `{mounted}` |
| `ddi.mount` | `{udid?}` | `{mounted: true}` (downloads the personalized image; needs internet) |
| `devmode.status` | `{udid?}` | `{enabled}` |
| `devmode.reveal` | `{udid?}` | `{}` (makes the Developer Mode switch appear in Settings; USB lockdown only) |

### Profiles

| cmd | args | reply data |
|---|---|---|
| `profiles.list` | `{udid?}` | `[{name, uuid, appId, team, teamName, created, expires, expired, devices, isXcodeManaged}]` newest expiry first; `created/expires` are epoch seconds |

### Pairing

| cmd | args | reply data |
|---|---|---|
| `pair.status` | – | `{records: [{udid, wifi: bool, usb: bool, path}], usb: [udid…]}` |
| `pair.start` | `{method: "usb" \| "wifi", udid?}` | `{started: true}`; progress comes as `pair` events |
| `pair.code` | `{code}` | `{}`; only when a `pair` event asked for `need-code` |
| `pair.cancel` | – | `{}` |

- `usb`: needs the phone on a cable. Lockdown pairing (the phone shows **Trust This Computer**;
  the user taps Trust and enters the passcode), then the Wi-Fi RemotePairing record is created
  over the same cable with no further tap.
- `wifi` (iOS 27+, no cable): the daemon advertises this computer as a pairable host and shows a
  6-digit code (`pair` event `phase: "show-code"`). The user opens Settings > Developer > Paired
  Macs on the phone, taps this computer under "Other Devices" and types the code **on the phone**.

Pair event:

```json
{"phase": "waiting", "method": "wifi", "message": "On your iPhone, open Settings › Developer › Paired Macs", "code": null, "udid": null}
```

Note: in the iOS 27 Wi-Fi flow the code is shown by **this computer** and typed on the phone, so
`pair.code` is normally not used; it exists for PIN-entry flows (`need-code`), which no current
iPhone flow triggers.

`phase` ∈ `starting`, `waiting` (instructions in `message`), `trust` (tap Trust on the phone),
`show-code` (`code` = 6 digits to type on the phone), `need-code` (type the code shown on the phone
into the app → `pair.code`), `done` (`udid`, `name`), `failed` (`message`, `detail`), `cancelled`.

### Syslog (live device log) — used by the logs slice

| cmd | args | reply data |
|---|---|---|
| `syslog.start` | `{udid?, filters?: Filters}` | `{running: true, filters}`; replaces a running stream |
| `syslog.filter` | `{filters: Filters}` | `{filters}`; changes filters without reconnecting |
| `syslog.stop` | – | `{running: false}` |

Filters (all optional, combined with AND; empty/omitted = everything):

```json
{
  "processes": ["MyApp", "SpringBoard"],  // exact process (executable basename) match, any of
  "pids": [812],                               // any of
  "minLevel": "default",                       // "debug" < "info" < "default" < "error" < "fault"
  "text": "camera",                            // case-insensitive substring of message, process or subsystem
  "subsystem": "com.apple.camera",             // prefix match
  "bundleId": "app.myapp.mobile.dev"       // convenience: resolves to that app's executable (+ lines naming it)
}
```

Events: `{"event": "syslog", "data": [SyslogEntry, …]}`, flushed **every 100 ms** when there is
anything to send, **at most 200 entries per batch** (so at most 2000 entries/s reach the UI). The
pending buffer holds at most 1000 entries; on overflow the oldest are dropped and counted in
`syslogState.dropped` (emitted at most once per second while dropping). Messages longer than 8 KB
are truncated with `…`.

Measured on the iPhone 17 Pro over Wi-Fi: an **unfiltered** stream is ~1500–2000 entries/s (the
phone is chatty), so the UI should keep a ring buffer (e.g. 5000 rows) and ideally pass at least a
`minLevel`, `processes` or `bundleId` filter. A single `pids` entry is filtered on the phone itself
(cheapest); everything else is filtered in the daemon.

SyslogEntry:

```json
{"ts": 1730459123.456789, "process": "MyApp", "pid": 812, "level": "error",
 "subsystem": "com.apple.camera", "category": "capture", "message": "text\nmay be multi-line",
 "image": "AVFCapture"}
```

- `level` ∈ `debug`, `info`, `default`, `error`, `fault` (`default` = os_log "notice"/default; the
  rare "user action" level is reported as `default`).
- `subsystem`, `category` are `""` when the entry has no os_log label.
- `image` is the basename of the sending library/binary (`""` if none).

`syslogState`: `{running, udid, filters, dropped, reconnecting, error?}`. When the tunnel drops,
the daemon sets `reconnecting: true` and resumes the stream automatically after reconnecting
(entries during the gap are lost).

### Crash reports — used by the logs slice

| cmd | args | reply data |
|---|---|---|
| `crash.list` | `{udid?, process?: str, flush?: true}` | `[CrashItem…]` newest first |
| `crash.pull` | `{name, udid?}` (`name` = `CrashItem.name`, which is the device path) | `{path, summary: CrashSummary}` |

CrashItem:

```json
{"name": "/MyApp-2026-11-01-142233.ips", "file": "MyApp-2026-11-01-142233.ips",
 "process": "MyApp", "date": "2026-11-01T14:22:33", "ts": 1730456553.0,
 "kind": "crash", "retired": false}
```

`kind` ∈ `crash`, `resource` (cpu/wakeups/disk writes), `jetsam`, `hang`, `other`; derived from the
file name. `flush` (default true) first moves reports still pending on the phone into the list.

`crash.pull` copies the file to `~/.cache/ios-studio/crashes/<udid>/<file>` (re-used when already
pulled) and parses the `.ips` header + body:

```json
{"process": "MyApp", "bundleId": "app.myapp.mobile.dev", "appVersion": "0.1.0 (1)",
 "osVersion": "iPhone OS 27.2 (24B115)", "date": "2026-11-01 14:22:33.00 +0400",
 "exceptionType": "EXC_CRASH", "signal": "SIGABRT", "terminationReason": "…",
 "crashedThread": 0, "frames": ["0 libsystem_kernel.dylib __pthread_kill + 8", "…"],
 "bugType": "309", "incident": "UUID"}
```

Any field may be `null` (resource reports have no exception). `frames` = the first 15 frames of
the crashed thread, unsymbolicated app frames are `"<image> + 0xOFFSET"`.

### Installing apps (`app.install`)

Works over the active tunnel, Wi-Fi or USB (installation_proxy over RSD, the same path the transport in
helper/device_link.py uses; ~7 s for a 45 MB app over Wi-Fi). The reply comes after the phone reports
"Complete". Error codes besides the common ones:

| code | message |
|---|---|
| `not-found` / `bad-args` / `bad-package` | the file is missing, not an .ipa/.app, or damaged |
| `app-limit` | "Free Apple IDs can only have 3 of your own apps on an iPhone at a time…" (fix → iPhone page) |
| `device-not-registered` | "This iPhone isn't registered with your Apple ID yet. Run once over USB." |
| `no-space` | not enough free storage on the iPhone |
| `locked` | the iPhone is locked |
| `incompatible` / `bad-signature` | iOS version/architecture mismatch, invalid signature |
| `install-failed` | anything else (`detail` holds the installer's text) |

## One-shot mode (no app running)

```
bin/iosstudio-deviced --once <cmd> '<json args>'
```

Opens the tunnel to `args.udid` (or the default device: settings `defaultDevice`, last used, first
USB, first Wi-Fi pair record), runs one command, prints **one** JSON object
`{"ok": true, "data": …}` / `{"ok": false, "error": {…}}` on stdout and exits (0 ok, 1 error).
Events and diagnostics go to stderr only. Any command works, e.g.
`--once app.install '{"path": "/…/App.ipa"}'` (background renewals: helper/projects.py
`renew-due`), `--once device.info '{}'`. The tunnel wait is 25 s, so an unreachable phone answers
`unreachable` in ~25–35 s.

## State file for other slices

Whenever the device's info or connection changes, the daemon writes (atomically)
`~/.local/state/ios-studio/device.json`:

```json
{"udid": "…", "name": "My iPhone", "model": "iPhone 17 Pro", "productType": "iPhone18,1",
 "iosVersion": "27.2", "build": "24B115", "developerMode": true, "ddiMounted": true,
 "connection": "connected", "transport": "wifi", "wifiPaired": true, "usbPaired": true,
 "battery": 36, "updatedAt": 1793536047.07}
```

Unknown values are `null`; values from an earlier connection of the same device are kept.
`connection` is the last connection state the daemon saw (stale once the daemon has exited — check
`updatedAt`).
