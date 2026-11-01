# Device daemon protocol (`bin/iosstudio-deviced`)

Clients: `services/DeviceService.qml` (iPhone page), `services/LogService.qml` (Logs page, through
`app.device.request()` / `app.device.daemonEvent`) and the MCP server (`mcp/`, through
`helper/device_client.py`).

The daemon is one long-running Python process per user (pymobiledevice3 venv, see
`common.pymd3_python()`). It owns the only tunnel to the iPhone and is shared: the GUI, the MCP
server and `--once` jobs all talk to the same process over a Unix socket (see [Broker](#broker)).
`bin/iosstudio-deviced` with no arguments is a stdio bridge to it, so a client can talk JSON
lines over stdin/stdout without knowing about the socket. stderr is free-form debug text (never parse it).

## Broker

```
$XDG_RUNTIME_DIR/ios-studio/deviced.sock         real phone   (mode 0600, directory 0700)
$XDG_RUNTIME_DIR/ios-studio-demo/deviced.sock    demo mode    (IOSSTUDIO_DEMO set; fake iPhone)
  deviced.lock   flock held by the live daemon (single instance)
  deviced.log    the daemon's stderr (truncated at each start)
```

`IOSSTUDIO_RUNTIME_DIR=<dir>` replaces `$XDG_RUNTIME_DIR` in both paths, for the bridge, the
daemons and `device_client.py`. It gives a checkout or test run a private daemon. A real daemon
under a custom directory answers every request with `busy` while the default one is live, because
a second tunnel would break the first one's link.

| invocation | what it does |
|---|---|
| `iosstudio-deviced` | connects to the socket; if nothing answers it starts `iosstudio-deviced --serve` detached (own session) and waits for the socket (≤30 s). Then it bridges stdin↔socket until either side closes. stdlib python, starts in ~0.1 s. |
| `iosstudio-deviced --serve` | becomes the daemon: takes the lock (exits 0 if another daemon is live), removes a stale socket, listens, then loads pymobiledevice3. Normally only started by the bridge or `device_client.py`. |
| `iosstudio-deviced --once <cmd> '<json>'` | one request through the live daemon if there is one (no second tunnel), otherwise the standalone one-shot mode below. |

Implementation: `helper/device_broker.py` (stdlib), shared by `helper/deviced.py` and
`helper/deviced_demo.py`. Each daemon is a small core (session, tunnel, devices, dispatch) plus
one feature module per command group in `helper/deviced_features/` (demo:
`helper/deviced_demo_features/`). A module that fails to load is skipped and logged, and its
commands answer `bad-args` ("Unknown command"); `daemon.features` reports it. How to add one:
[development.md](development.md#device-daemon-features).

**Per connection.** Each connection is a client with the framing below. Request `id`s are scoped
to the connection (two clients may both use `id: 1`; each reply goes back to the connection that
asked). On connect the daemon sends a snapshot: `ready`, `devices`, and when known `connection`,
`deviceInfo` and `mirror`, so a client attaching to a daemon that is already connected sees the
current state at once.

**Lifetime.** The daemon runs while any client is connected. With no clients it exits after an
idle grace period (`IOSSTUDIO_DEVICED_IDLE` seconds, default 120), closing the tunnel, the syslog
stream, the mirror and the DVT channels, and removing the socket. Quitting the GUI therefore never
stops the daemon while the MCP server is connected, and vice versa; reopening the GUI within the
grace period reattaches to the open tunnel instantly. SIGTERM/SIGINT/SIGHUP shut it down cleanly.
The bridge exits when the daemon goes away (the GUI's Stream then restarts it, which starts a new
daemon).

`daemon.shutdown` means "this client is done": the daemon replies `{}` and closes that
connection; other clients are unaffected. `{"all": true}` stops the daemon for everyone.

### Broker commands

| cmd | args | reply data |
|---|---|---|
| `hello` | `{name?, events?, version?}` | `{version, pid, clientId, name, clients, demo, socket, idle, events}`; names the client (logs, `daemon.clients`) and optionally sets the subscription |
| `subscribe` | `{events: [name…] \| null}` | same as `hello`; `null`, `"*"` or omitting `events` = all events |
| `daemon.clients` | – | `[{id, name, since, events, syslog: Filters \| null, mirror: bool}]` |
| `daemon.shutdown` | `{all?: bool}` | `{}`; detaches this client (or stops the daemon with `all`) |

**Events.** Device-wide events (`devices`, `connection`, `deviceInfo`, `frame`, `mirror`, `pair`,
`appIcon`, `appSizes`, `installProgress`, …) are broadcast to every client subscribed to them.
The default subscription is all events, which is what the GUI relies on. `syslog` and
`syslogState` are per client: a client only gets entries for its own `syslog.start`, filtered by
its own filters, whatever its subscription.

**Shared (global) state.** These affect every client; the last writer wins:
- the active device (`device.select`, `device.reconnect`);
- the simulated location (`location.set` / `location.clear`; it stays until cleared or until the
  daemon exits, not when the client that set it disconnects);
- pairing (`pair.*`; one flow at a time, progress is broadcast);
- the mirror frame rate (`mirror.start {fps}` from any client changes it for all).

**Syslog.** There is one log stream to the phone. Each client's `syslog.start` / `syslog.filter`
/ `syslog.stop` only changes its own subscription; filtering happens in the daemon per client, with
the same batching and limits per client (100 ms, ≤200 entries per batch, 1000-entry buffer,
`dropped` counted per client). The phone-side filter is a single `pid` when every subscriber
filters on exactly that one pid; otherwise the phone sends everything and entries below every
subscriber's `minLevel` are skipped before they're built. The phone stream starts with the first
subscriber and stops when the last one stops or disconnects.

**Held resources.** Features that keep something running on the phone for a client (the mirror;
network conditions, packet captures, port forwards) hold it as a reference-counted resource. It is
released when the last client holding it stops it or disconnects, after an optional timeout, when
the active device changes, at shutdown, and for tunnel-bound resources when the tunnel drops.

**Mirror.** Reference counted: `mirror.start` adds the client, `mirror.stop` removes it and the
frames stop only when no client holds it (the reply then says `{running: false, sharedWith: n}`
while others still do). A client disconnecting releases its reference. `frame` events go to every
subscribed client.

### Python client (`helper/device_client.py`)

Stdlib asyncio; import it from the repo or vendor the single file. Its module docstring is the
full API.

```python
import sys; sys.path.insert(0, "<repo>/helper")
from device_client import DeviceClient, DeviceError

async with DeviceClient(name="mcp") as dev:          # spawns the daemon if needed
    info = await dev.request("device.info", {}, timeout=40)
    async with dev.events(["syslog"]) as stream:
        await dev.request("syslog.start", {"filters": {"bundleId": "com.example.app", "minLevel": "info"}})
        async for name, entries in stream:
            ...
```

`request()` raises `DeviceError(code, message, fix, detail)` for error replies, plus `timeout`,
`disconnected` and `daemon-unavailable`. Demo mode follows `IOSSTUDIO_DEMO` (or `demo=True`).
After a daemon restart the client reconnects by itself and emits a local `reconnected` event;
syslog subscriptions and mirror references must be re-requested then.

`python3 helper/device_client.py device.info '{}'` sends one request from a shell (debugging).

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
| `needs-usb` | The feature only works over a USB cable (packet capture). |
| `unsafe-over-wifi` | That network condition would cut the Wi-Fi link to this computer; USB only. |
| `wifi-risk` | That network condition also slows the Wi-Fi link; repeat with `acceptWifiRisk: true` after the user agreed. |
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
| `condition` | Condition + `reason` (see [Network conditions](#network-conditions)) | a condition was set or switched off |
| `pcap` | Capture (see [Packet capture](#packet-capture)) | about once a second while capturing, and when it stops |
| `metrics.sample` | `{sessionId, …Sample}` | about once a second per running metrics session |
| `metrics.event` | `{sessionId, …MetricsEvent}` | hang, jetsam, crash, watchdog, thermal, hitch, exit, launch, note |
| `metrics.state` | MetricsSession | a metrics session started, changed status or stopped (also in the welcome) |
| `transfer` | Transfer (see [Files](#files)) | during `media.pull`, `container.pull/push` and inside `cameratest.run`, at most 4 a second, plus one final `done` / `failed` / `cancelled` |
| `cameratest` | `{testId, step, state, title, detail}` | each step of `cameratest.run` (`running`, `done`, `failed`) |
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
| `daemon.shutdown` | `{all?}` | `{}`; closes this client's connection (see [Broker](#broker)) |
| `daemon.features` | – | `{loaded: [module…], failed: {module: error}, commands: {module: [cmd…]}, resources: [{key, scope, holders, since, timeout}]}` |
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
| `app.install` | `{path, udid?}` (signed `.ipa` or `.app` folder) | `{path, bundleId, packageBundleId, seconds}`; `bundleId` is the id as installed (free teams: `XTL-<TEAM>.<id>`) |
| `processes.list` | `{udid?, appsOnly?: false}` | `[{pid, name, path, isApp, bundleId?}]` |

App:

```json
{"bundleId": "XTL-ABCDE12345.com.example.myapp", "name": "MyApp", "version": "0.1.0",
 "build": "1", "developer": true, "removable": true, "hidden": false, "system": false,
 "executable": "MyApp", "path": "/private/var/containers/Bundle/Application/…/MyApp.app",
 "icon": "<home>/.cache/ios-studio/icons/<udid>/<bundleId>.png", "running": false, "pid": null,
 "fileSharing": false, "debuggable": true}
```

`fileSharing` = the app sets `UIFileSharingEnabled` (its Documents folder can be opened, see
[Files](#files)); `debuggable` = signed with `get-task-allow` (a development build: its whole container
can be opened).

`size` (bytes, app + data) is `null` unless `sizes: true` was passed (which makes the reply ~9 s
slower); with `sizes` omitted the daemon computes them in the background and sends `appSizes`.
`sizes: false` skips that. `icon` is `null` when not cached yet; with `icons: true` the daemon fetches missing icons in the
background (CoreDevice icon service) and sends one `appIcon` event per icon.
`developer` = signed with a development profile (e.g. built with xtool).

### Screen

| cmd | args | reply data |
|---|---|---|
| `screenshot` | `{udid?, save?: bool}` | `{path, width, height, bytes, ts}` |
| `mirror.start` | `{udid?, fps?: 1.0}` (0.2–4) | `{running: true, fps}`; takes a mirror reference |
| `mirror.stop` | – | `{running: false, sharedWith?}`; releases it (frames stop when nobody holds one) |

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

### Network conditions

| cmd | args | reply data |
|---|---|---|
| `condition.list` | `{udid?}` | `{groups: [Group…], transport, active: Condition, phoneActive: [{group, profile}], defaultTimeoutS: 300, maxTimeoutS: 1800}` |
| `condition.set` | `{profile, timeoutS?: 300, acceptWifiRisk?: bool, udid?}` | Condition; replaces the active one |
| `condition.clear` | `{udid?}` | `{active: false, cleared: bool}`; `cleared` is false when nothing was on |
| `condition.status` | `{verify?: bool}` | Condition; `verify` re-reads the phone and adds `phoneActive` |

The phone's DVT condition inducer offers three groups on iOS 27: Network Link
(`SlowNetworkCondition`: 100% loss, very bad network, Edge, 2G, 3G, LTE, Wi-Fi, DSL, high-latency
DNS), Thermal State (`ThermalCondition`: Fair, Serious, Critical) and GPU Performance State
(`GPUPerformanceState`: Min, Mid, Max). Internal groups are left out.

Group:

```json
{"id": "SlowNetworkCondition", "name": "Network Link", "kind": "network", "phoneActive": false, "activeProfile": "",
 "destructive": false,
 "profiles": [{"id": "SlowNetwork3G", "name": "3G Network - best",
               "summary": "0.78 Mbps down · 0.33 Mbps up · 100 ms latency",
               "details": {"Downlink Bandwidth": "0.78 Mbps", "Downlink Latency": "100 ms", "…": "…"},
               "description": "", "wifi": "warn", "allowed": true, "note": "Your iPhone is connected over Wi-Fi, …"}]}
```

`kind` ∈ `network`, `thermal`, `gpu`, `other`. `details` is the phone's description split into
fields; when it has no fields, the raw text is in `description`. `wifi` is the profile's rule over
Wi-Fi: `ok`, `warn` (a network profile: it also slows the tunnel to this computer) or `blocked`
(`SlowNetwork100PctLoss` and `SlowNetworkVeryBadNetwork`, which would cut the tunnel so the reset
could never be sent). `allowed` and `note` apply that rule to the current transport.

Condition (also the `condition` event and `device.info`'s `condition` field, `null` when off):

```json
{"active": true, "group": "SlowNetworkCondition", "groupName": "Network Link", "kind": "network",
 "profile": "SlowNetwork3G", "name": "3G Network - best", "summary": "0.78 Mbps down · …",
 "setAt": 1793536000.0, "expiresAt": 1793536300.0, "timeoutS": 300, "remainingS": 284,
 "setBy": "gui-1", "transport": "usb"}
```

When nothing is on, the reply is `{"active": false}`.

Rules:
- One condition at a time. `condition.set` switches the old one off first (no `cleared` event for
  it) and sends `condition {reason: "set"}` for the new one.
- Over Wi-Fi, a `blocked` profile fails with `unsafe-over-wifi`, and a `warn` profile fails with
  `wifi-risk` unless `acceptWifiRisk` is true. Thermal and GPU profiles work on either transport.
- The condition is a [held resource](#broker) of the client that set it, scope `tunnel`, always
  with a timeout (`timeoutS`, 10 s to 30 min, default 5 min). It switches off on `condition.clear`
  from any client, at the timeout, when the setting client disconnects, when the tunnel drops or the
  active device changes, and at daemon shutdown. It runs on a DVT connection of its own; the phone
  resets the condition when that connection closes.
- Switching off sends `disableActiveCondition` (the phone doesn't answer it), then re-lists to check
  and closes the connection. After a tunnel drop the phone can't be reached, so once the daemon has
  reconnected it lists the conditions again and clears anything still on.
- `condition.clear` with nothing held still clears a condition the phone reports (left over from
  another tool or a crash).

Event `condition`: the Condition plus `reason` ∈ `set`, `cleared`, `timeout`, `client-gone`,
`tunnel-lost`, `shutdown`. When it ends, `active` is false and the other fields still name the
condition that was on. A new client's snapshot includes `condition {…, reason: "set"}` while one is
on.

### Packet capture

| cmd | args | reply data |
|---|---|---|
| `pcap.start` | `{bundleId? \| pid?, maxSeconds?: 30, maxMB?: 50, udid?}` | Capture (running) |
| `pcap.stop` | – | Capture (finished, with `summary`) |
| `pcap.status` | – | Capture: the running one, else the last finished one, else `{running: false}` |

USB only: pcapd refuses the Wi-Fi tunnel (`ServiceProhibited`), so over Wi-Fi `pcap.start` fails
with `needs-usb`, "Packet capture needs a USB cable. Connect your iPhone with a cable and try
again." Over USB the daemon opens pcapd over the tunnel's RSD, or over usbmux lockdown if the
phone refuses that. One capture at a time (`busy` otherwise).

`bundleId` (plain or `XTL-<TEAM>.` form) keeps the packets of that app's executable, matched on
the packet's process or effective process (so a request `nsurlsessiond` makes for the app counts).
`pid` keeps one process. Neither = the whole phone. Packets of the daemon's own tunnel (`remoted`,
`remotepairingd`, and the interface they use) are always left out. `maxSeconds` is 3 to 300,
`maxMB` 1 to 500.

The capture writes `~/.cache/ios-studio/captures/cap-<YYYYmmdd-HHMMSS>.pcapng` (standard pcapng,
Ethernet link type; each packet has a comment `PID: …, ProcName: …, EPID: …, EProcName: …`). The
newest 20 files are kept.

Capture:

```json
{"running": false, "file": "<home>/.cache/ios-studio/captures/cap-20261101-142233.pcapng", "fileBytes": 41312,
 "startedAt": 1793536000.0, "endedAt": 1793536030.2, "seconds": 30.2, "maxSeconds": 30, "maxMB": 50,
 "filter": {"process": "MyApp", "bundleId": "XTL-ABCDE12345.com.example.myapp"}, "startedBy": "gui-1",
 "packets": 312, "bytes": 180214, "excluded": 40,
 "byProcess": [{"process": "MyApp", "packets": 300, "bytes": 170000}],
 "stopReason": "time", "error": null, "summary": Summary}
```

`stopReason` ∈ `time`, `size`, `stopped`, `client-gone`, `tunnel-lost`, `shutdown`, `error` (then
`error` holds the error object). `summary` is computed when the capture stops, from pcapd's
per-packet header and the IP, TCP and UDP headers only, never the payload:

```json
{"packets": 312, "bytes": 180214, "seconds": 30.2, "sentBytes": 40000, "receivedBytes": 140214,
 "byProcess": [{"process": "MyApp", "packets": 300, "bytes": 170000}], "processCount": 2,
 "topHosts": [{"host": "203.0.113.24", "port": 443, "protocol": "TCP", "service": "HTTPS", "packets": 200, "bytes": 120000}],
 "hostCount": 6, "protocols": {"TCP": 250, "UDP": 62}, "services": {"HTTPS": 250, "QUIC": 50, "DNS": 12},
 "ipVersions": {"IPv4": 280, "IPv6": 32}, "interfaces": {"en0": 312},
 "excludedTunnelPackets": 40, "otherProcessPackets": 1200}
```

The remote end of a flow is the side with the well-known port. `services` names common ports
(HTTPS, QUIC, DNS, HTTP, mDNS, NTP, Apple Push, …) and shows others as `TCP <port>`. A process
that sent on another's behalf is listed as `App (via nsurlsessiond)`.

The capture is a held resource of the client that started it (scope `tunnel`): it stops at its
time or size limit, on `pcap.stop` from any client, when that client disconnects, when the tunnel
drops and at shutdown. Event `pcap`: the running Capture about once a second (`byProcess` = top
5), then once more with the finished Capture and its `summary`.

### Demo mode

The demo daemon has the same commands with made-up data. Its iPhone starts on Wi-Fi, so capture
answers `needs-usb`. `demo.transport {transport: "usb" | "wifi"}` (demo only) plugs the made-up
cable in or out, sends `devices`, `connection` and `deviceInfo`, and like a real transport change
releases what the tunnel held. Demo captures are small valid pcapng files of invented headers from
documentation address ranges.

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

### Syslog (live device log), used by the Logs page

| cmd | args | reply data |
|---|---|---|
| `syslog.start` | `{udid?, filters?: Filters}` | `{running: true, filters}`; replaces this client's subscription |
| `syslog.filter` | `{filters: Filters}` | `{filters}`; changes this client's filters without reconnecting |
| `syslog.stop` | – | `{running: false}`; ends this client's subscription only |

All three are per client (see [Broker](#broker)): two clients can stream with different filters at
the same time over one phone stream.

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

`syslogState` (per client): `{running, udid, filters, dropped, reconnecting, error?}`. When the tunnel drops,
the daemon sets `reconnecting: true` and resumes the stream automatically after reconnecting
(entries during the gap are lost).

### Crash reports, used by the Logs page

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

### Performance metrics, used by the Performance tab and the MCP metrics tools

A metrics session follows one app and keeps one compact row per interval (about an hour at 1 Hz) plus
its events. The daemon opens one tap per metric set on its own tunnel (each long-held instrument on a
DVT connection of its own) and shares it between every session that wants it; a tap closes with its last
session. A session is held by the client that started it: it stops when that client disconnects, when
nobody reads it (`metrics.read` or `metrics.snapshot`) for 2 hours, or when the active iPhone changes.
Stopped sessions stay readable (the newest 6). At most 4 run at once.

| cmd | args | reply data |
|---|---|---|
| `metrics.start` | `{bundleId` or `pid, name?, sets?: [str], intervalMs?: 1000, udid?}` | MetricsSession. The same client starting the same app again gets the running session back |
| `metrics.stop` | `{sessionId}` | MetricsSession + `summary` (Stats) |
| `metrics.list` | `{}` | `{sessions: [MetricsSession…]}` |
| `metrics.read` | `{sessionId, kind?: "samples"` or `"events", cursor?, limit?: 600, wait?: s ≤ 20}` | `{entries, cursor, nextCursor, hasMore, missed, bufferStart, total, kind, session}` |
| `metrics.snapshot` | `{sessionId?, recentEvents?: bool}` | `{session, stats: Stats}`; without `sessionId` and no running session: `{sessions}` |
| `metrics.series` | `{sessionId, metric, points?: 60}` | `{metric, label, unit, buckets: [{t0, t1, min, avg, max, n}], stats}` |
| `metrics.export` | `{sessionId}` | `{path, eventsPath, rows, events, bytes, columns}`: CSV files under `~/.cache/ios-studio/metrics/` |
| `metrics.hitches` | `{sessionId, seconds?: 60, stop?: bool}` | MetricsSession; frame hitch tracking for at most 60 s |
| `metrics.launchProfile` | `{bundleId, udid?}` | LaunchProfile. Quits and relaunches the app: confirm with the user first |
| `metrics.leaks` | `{bundleId, udid?}` | LeakReport. Pauses the app while its heap is scanned: confirm first |

`sets` (default: all but `hitches`):

| set | source | fields in a sample |
|---|---|---|
| `process` | Sysmontap at 1 Hz. Rows are deltas, so the last values per pid are kept, and counters become rates | `cpu` (% of one core), `mem` (footprint MB), `rss` (MB), `threads`, `wakeups` (/s), `faults` (/s), `diskR`, `diskW` (KB/s), `sysCpu` (% of the whole phone) |
| `energy` | EnergyMonitor for the app's pid (the first, cumulative sample is dropped) | `energy`, `eCpu`, `eGpu`, `eNet`, `eDisplay`, `eLoc`, `eOverhead`, `thermal` (thermal-state cost) |
| `graphics` | the Graphics instrument | `fps` (Core Animation frames per second of the whole display, not the app), `gpu` (%), `gpuMem` (MB) |
| `network` | NetworkStatistics for the app's pid | `netRx`, `netTx` (KB/s) |
| `battery` | IOPMPowerSource every 5 s | `batt` (%), `battMw`, `charging` (0/1) |
| `events` | the daemon's own syslog stream (one more subscriber, level `default` and up) and, after the app quits, the crash report list | events only |
| `hitches` | ActivityTraceTap signposts (`FrameUpdate`, `Commit`, `UpdateSequence`), filtered by pid here. Very heavy (2,000 to 3,500 rows a second for the whole phone), so it's opt-in and capped at 60 s | `hitchMs`, plus `hitch` events |

Every sample also has `ts` and `pid`. A field is missing when its tap has no fresh value.

MetricsSession:

```json
{"sessionId": "m1", "bundleId": "XTL-A1B2C3D4E5.com.example.app", "name": "MyApp", "pid": 812,
 "follow": true, "sets": ["process", "energy", "graphics", "network", "battery", "events"],
 "intervalMs": 1000, "started": 1793549796.99, "stopped": null, "running": true,
 "status": "running", "samples": 295, "events": 9,
 "hitchTracking": {"running": false, "until": null}, "owner": "ios-studio-3"}
```

`status` is `starting`, `running`, `waiting-app` (the app isn't running; sampling starts when it opens),
`reconnecting` (every tap is failing; `error` says why), `app-exited` (a bare `pid` session whose process
quit) or `stopped`. A session that didn't stop on request has `error.code` `client`, `timeout`, `session` or
`shutdown`. A `bundleId` session follows relaunches: it picks up the new pid within about 2 s and records
`exit` and `launch` events.

MetricsEvent: `{ts, kind, text, durationMs?, source?, report?}`. `kind` is one of `hang` (HangTracer; needs
Hang Detection on, iPhone Settings › Developer), `jetsam` (a memorystatus kill that names the app, or a
JetsamEvent report written when it quit), `crash` (a new report for the app's executable after it quit),
`watchdog`, `thermal` (at most one per 30 s), `hitch` (`durationMs` of the slow frame), `exit`, `launch`,
`note`.

Stats (also what `metrics.stop` returns as `summary`):

```json
{"window": {"samples": 300, "start": 1793549531, "end": 1793549830, "seconds": 299},
 "series": {"cpu": {"min": 2, "avg": 21.3, "p95": 33.3, "max": 96.4, "last": 18, "n": 300}},
 "memory": {"slopeMbPerMin": 3.26, "r2": 0.87, "seconds": 258, "growthMb": 14, "from": 138.4, "to": 152.2,
            "likelyLeak": true},
 "cpuSpikes": {"count": 3, "longest": 5, "items": [{"ts": 1793549651, "seconds": 5, "peak": 96}]},
 "fpsDrops": {"count": 3, "items": [{"ts": 1793549636, "seconds": 5, "low": 44}]},
 "hitches": {"count": 9, "totalMs": 853, "worstMs": 128},
 "battery": {"charging": false, "from": 78, "to": 76, "drainPctPerHour": 16.5, "mWh": 120.9, "avgMw": 1437},
 "energy": {"avg": 5.75, "max": 11.96, "byComponent": {"Display": {"avg": 2.15, "share": 37}}},
 "io": {"diskR": {"avg": 37.9, "max": 428, "unit": "KB/s"}},
 "eventCounts": {"jetsam": 1, "hang": 1, "hitch": 9},
 "anomalies": [{"kind": "leak", "level": "warn",
                "text": "Memory grew 3.26 MB/min for 4 minutes (138.4 → 152.2 MB): a possible leak. Run Check for leaks."}]}
```

The rules live in `helper/metrics_stats.py`. The memory trend is a linear fit of the footprint since the
app's last launch, over the whole run and the last 10 and 5 minutes; the most leak-like window is
reported, and it counts as a likely leak at 1 MB/min or more with R² ≥ 0.6 over at least 2 minutes. A CPU
spike is above 80% for 3 s or more. An FPS drop is 10 to 50 fps for 2 s or more (below 10 the screen is
mostly idle rather than dropping frames). Battery drain is a linear fit of the level over at least 2
minutes, and mWh integrates `battMw`.

LaunchProfile:

```json
{"bundleId": "XTL-A1B2C3D4E5.com.example.app", "name": "MyApp", "pid": 812, "seconds": 4.2,
 "phases": [{"name": "Loading libraries (dyld)", "ms": 182.4}, {"name": "Static initializers", "ms": 41.7},
            {"name": "First frame (timed on this computer)", "ms": 690}],
 "preMainMs": 224.1, "firstFrameMs": 690, "timing": {"spawn": 0.0, "init": 0.18, "main": 0.22},
 "counts": {"images": 312, "dlopen": 4},
 "verdict": "OK launch (690 ms). Apple suggests the first frame within 400 ms."}
```

The daemon launches the app suspended (killing the running copy), lets DyldMetrics capture the loader
timing at `main`, resumes it, and stops the first-frame timer at the app's first `FrameUpdate`, `Commit` or
`UpdateSequence` signpost. That timer runs on this computer, so expect about ± 0.2 s. DyldMetrics only
works for development-signed apps; for other apps `dyldError` and `note` say so and only the first frame
is timed.

LeakReport: `{bundleId, name, pid, leakCount, leakedBytes, seconds, memgraphPath?, memgraphBytes?,
verdict}`. `leakedBytes` is null because the phone doesn't report sizes. The app has to be running and
debuggable (errors `not-running`, `not-debuggable`). The leaked-only memory graph is saved next to the CSV
exports, for `leaks` or Xcode on a Mac.
### Files

The camera roll (read-only) and app containers, used by iPhone › Files and the MCP file tools.
Implementation: `helper/deviced_features/files.py` and `cameratest.py`, shared helpers in
`helper/device_files.py`.

| cmd | args | reply data |
|---|---|---|
| `media.list` | `{offset?: 0, limit?: 200 (≤1000), kind?: "photo"\|"video", refresh?: bool, udid?}` | `{total, offset, items: [MediaItem…], source, photos, videos}` newest first |
| `media.thumb` | `{id \| path, udid?}` | `{id, path, source}`: a local JPEG; `source` ∈ `cache`, `phone`, `generated` |
| `media.pull` | `{ids \| paths, dest?: "~/Pictures/iPhone", convertHeic?: false, transferId?, udid?}` | `{transferId, dest, files, skipped, failed: [{path, message}], bytes, converted, notConverted, seconds}` |
| `container.list` | `{bundleId, path?, documentsOnly?: false, udid?}` | `{bundleId, name, path, root, mode, developer, fileSharing, writable, entries: [Entry…]}` |
| `container.pull` | `{bundleId, paths, dest?: "~/Downloads", transferId?, udid?}` | `{transferId, dest, files, bytes, seconds}`; folders are copied whole |
| `container.push` | `{bundleId, srcs \| src, path?, overwrite?: false, transferId?, udid?}` | `{transferId, path, files, bytes, seconds}`; `path` (a phone folder) is created if missing |
| `container.mkdir` | `{bundleId, path, udid?}` | `{path}` |
| `container.delete` | `{bundleId, paths, udid?}` | `{deleted, failed}`; recursive (UI must confirm first) |
| `transfer.cancel` | `{transferId}` | `{transferId, cancelled: bool}`; the transfer's request then fails with `cancelled` |
| `transfer.list` | – | `[{id, kind}]` running transfers |
| `cameratest.run` | `{bundleId, inputs: [local path…], args?: [str] \| str, env?: {} \| "K=V, …", timeout?: 60, testId?, minLevel?: "info", udid?}` | CameraTest (below) |

MediaItem:

```json
{"id": "DCIM/100APPLE/IMG_0001.HEIC", "path": "DCIM/100APPLE/IMG_0001.HEIC", "name": "IMG_0001.HEIC",
 "kind": "photo", "date": "2026-10-30T18:12:05", "ts": 1793383925.0, "width": 4032, "height": 3024,
 "duration": null, "size": 2310044, "thumb": "<home>/.cache/ios-studio/thumbs/<udid>/DCIM_100APPLE_IMG_0001.HEIC.jpg"}
```

Camera roll:
- `media.list` lists `DCIM` over AFC (about 0.1 s for 1,400 files) and joins it with a copy of
  `PhotoData/Photos.sqlite` (plus `-wal`/`-shm`) for dates, sizes, dimensions and video length
  (`source: "photos-db"`). The copy lives in `~/.cache/ios-studio/media/<udid>/` and is pulled
  again only when the database's size or mtime changes. Assets in Recently Deleted or Hidden, iCloud-only
  assets (not in DCIM) and Live Photo companion videos are left out. When the database's columns
  aren't the expected ones (they change between iOS releases), the plain DCIM listing is used
  (`source: "dcim"`; `date`, `size` and dimensions are `null`). Listings are reused for 15 s;
  `refresh` forces a new one.
- `media.thumb` writes `~/.cache/ios-studio/thumbs/<udid>/<path>.jpg`: the phone's own thumbnail
  (`PhotoData/Thumbnails/V2/<path>/5005.JPG`, or a video key frame) when there is one, else it pulls the
  original (up to 80 MB) and converts it with vips, ImageMagick or ffmpeg (Qt has no HEIF plugin, so
  HEIC never reaches the UI as is). At most 4 thumbnails are made at a time. `no-converter` (fix →
  Setup › System packages) when no tool could read the file, `no-thumbnail` for a huge original.
- `media.pull` copies originals unchanged; files already imported (same name and size) are skipped,
  other name clashes become `name (2).ext`. With `convertHeic`, HEIC/HEIF photos are saved as JPEG
  (heif-convert, vips, ImageMagick or ffmpeg); one that can't be converted stays HEIC and counts in
  `notConverted`.
- Nothing ever writes to `DCIM` or `PhotoData`.

Containers (house_arrest):
- Your development builds (`get-task-allow`) vend the whole container (`mode: "container"`, `root: "/"`:
  `Documents`, `Library`, `tmp`). Entries the phone refuses (such as `SystemData`) are skipped.
- App Store apps with `UIFileSharingEnabled` vend `Documents` only (`mode: "documents"`, `root:
  "/Documents"`); `documentsOnly` gives the same view of your own app.
- Any other app fails with `no-file-sharing` ("This app doesn't share files…").
- Paths are POSIX paths inside `root`; `..` is refused (`bad-args`). `container.push` first checks
  every target and fails with `exists` (the clashing paths in `detail`, one per line) unless
  `overwrite`; nothing is copied then. Pulls never overwrite local files (`name (2).ext`).
- Error codes besides the common ones: `no-file-sharing`, `denied` (the phone refused this path),
  `exists`, `no-space`, `not-found`.

Transfer event (`transfer`):

```json
{"id": "m4211-3", "kind": "media.pull", "state": "running", "bytes": 10485760, "total": 48211322,
 "files": 2, "count": 9, "file": "IMG_0003.HEIC", "seconds": 1.9, "dest": "/home/me/Pictures/iPhone"}
```

`kind` ∈ `media.pull`, `container.pull`, `container.push`, `cameratest.push`, `cameratest.pull`; `state`
∈ `running`, `done`, `failed` (with `error`), `cancelled`. Pass your own `transferId` to match events
to a request before its reply arrives. A client that disconnects cancels its transfers.

#### Camera test (`cameratest.run`)

One job for your own app (others fail with `not-dev-app`): clear `Documents/CameraTestOutput` and
`Documents/CameraTestInput`, copy `inputs` to `CameraTestInput`, stop and launch the app with `args`
(default `["-cameraTestMode", "1"]`) and `env` while recording its log (bundleId filter, `minLevel`),
wait for `CameraTestOutput/done.json`, the app exiting (checked every 3 s) or `timeout` seconds,
copy `CameraTestOutput` to `~/.cache/ios-studio/cameratest/<testId>/pulled/`, and look for new crash
reports. `cameratest` events report each step; `transfer.cancel {transferId: testId}` stops it. The
reply comes when the run ends, so give the request `timeout + ~60 s`. The app-side hook is in
[camera-testing.md](camera-testing.md).

```json
{"testId": "ct4211-7", "bundleId": "XTL-ABCDE12345.com.example.myapp", "ok": true, "timedOut": false,
 "exited": false, "done": {"results": [{"file": "cat.jpg", "frame": 0, "label": "cat"}]}, "doneRaw": null,
 "outputDir": "<home>/.cache/ios-studio/cameratest/ct4211-7/pulled/CameraTestOutput",
 "outputs": ["<…>/CameraTestOutput/done.json", "<…>/CameraTestOutput/cat-annotated.png"],
 "logLines": ["14:22:31 info    MyApp: CameraTest: 1 input file(s)", "…"], "logCount": 37,
 "logFile": "<…>/ct4211-7/syslog.txt", "newCrashes": [CrashItem…], "pid": 812, "seconds": 6.4,
 "args": ["-cameraTestMode", "1"], "env": {}, "timeout": 60,
 "steps": [{"id": "check", "title": "Check the app", "state": "done", "detail": "MyApp"}, "…"]}
```

`ok` = `done.json` parsed and no new crash. `doneRaw` holds the text (≤20 KB) when `done.json` isn't
valid JSON. `logLines` keeps the last 400 lines; `syslog.txt` has all of them. Step ids: `check`,
`clear`, `push`, `launch`, `wait`, `pull`, `crashes`.

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

## One-shot mode

```
bin/iosstudio-deviced --once <cmd> '<json args>'
```

When a daemon is live, the request goes through it (no second tunnel; `args.udid`, if given, must
be the active device) and the reply is printed in the same format. Otherwise it runs standalone: it opens the tunnel to `args.udid` (or the default device: settings `defaultDevice`, last used, first
USB, first Wi-Fi pair record), runs one command, prints **one** JSON object
`{"ok": true, "data": …}` / `{"ok": false, "error": {…}}` on stdout and exits (0 ok, 1 error).
Events and diagnostics go to stderr only. Any command works, e.g.
`--once app.install '{"path": "/…/App.ipa"}'` (background renewals: helper/projects.py
`renew-due`), `--once device.info '{}'`. The tunnel wait is 25 s, so an unreachable phone answers
`unreachable` in ~25–35 s.

## State file for other parts of the app

Whenever the device's info or connection changes, the daemon writes (atomically)
`~/.local/state/ios-studio/device.json`:

```json
{"udid": "…", "name": "My iPhone", "model": "iPhone 17 Pro", "productType": "iPhone18,1",
 "iosVersion": "27.2", "build": "24B115", "developerMode": true, "ddiMounted": true,
 "connection": "connected", "transport": "wifi", "wifiPaired": true, "usbPaired": true,
 "battery": 36, "updatedAt": 1793536047.07}
```

Unknown values are `null`; values from an earlier connection of the same device are kept.
`connection` is the last connection state the daemon saw (stale once the daemon has exited; check
`updatedAt`).
