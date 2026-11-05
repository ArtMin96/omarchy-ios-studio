# Development

How iOS Studio is put together, for people who want to change it.

## Overview

iOS Studio is a standalone [Quickshell](https://quickshell.org/) config with a Python backend.

- UI: QML, started with `qs -n -p <repo>`. `shell.qml` declares `//@ pragma AppId ios-studio` (that's also the Hyprland window class), a `ShellRoot` and one `FloatingWindow`. Closing the window quits the app.
- Helper (`bin/iosstudio-helper`): stdlib-only Python under `/usr/bin/python3 -I` (compiled modules cached in `~/.cache/ios-studio/pycache`). One call per action, or a stream of JSON lines for long jobs.
- Device broker (`bin/iosstudio-deviced`, `helper/device_broker.py`): one long-running Python process per user in the pymobiledevice3 venv. It owns the only connection to the iPhone and listens on a Unix socket, so the app, the MCP server and background jobs share it. It exits two minutes after its last client disconnects.
- MCP server (`bin/iosstudio-mcp`, `mcp/`): a stdio server for AI coding agents. It calls the same helper as the app and reaches the iPhone through the broker with `helper/device_client.py`. See [mcp.md](mcp.md).
- Tools it drives: xtool (build, sign, install, Apple account), the Swift toolchain, omarchy-apple-dev (SDK preparation), pymobiledevice3 (device access), rcodesign and ipsw.

## File layout

```
shell.qml            window, sidebar, page loader, global keys, IPC handler, overlays
AppState.qml         shared state: current page, nav(), toast(), confirm(), settings
Cli.qml, Stream.qml  wrappers for one-shot and streaming helper calls
ThemeWatcher.qml     follows Omarchy theme changes
Model.js             pure helpers: glyphs, PAGES, SHORTCUTS, formatting
components/          shared UI building blocks (see components.md), including AgentsSection
services/            one QML service per area: Setup, Toolchain, Account, Device, Project, Log
pages/               one page per sidebar entry
helper/
  main.py            helper router: group name -> module
  common.py          shared helper code: ok()/fail()/event(), paths, fix links
  setup.py, account.py, projects.py, appconfig.py   one module per helper group
  diagnostics.py     build output -> diagnostics (compiler, linker, package, signing, device, ...), used by projects.py
  xip_version.py     reads the Xcode version out of a .xip without unpacking it
  agents.py          Settings › AI agents: connect the MCP server to coding agents
  debugger.py        iPhone › Debug: backtraces and breakpoint sessions (group `debug`)
  lldb_driver.py     runs inside lldb's Python (python3.12) and drives lldb for debugger.py
  debugger_demo.py   the `debug` group in demo mode: a made-up backtrace and session, no lldb
  deviced.py         device daemon core: session, tunnel, device list, dispatch (pymobiledevice3)
  deviced_core.py    what feature modules import: emit, dlog, err, classify, paths, PortForward
  deviced_kit.py     shared by both daemons: feature registry/loader, Resources, CursorRing
  deviced_features/  one module per feature group of the real daemon: apps, cameratest, conditions,
                     console, crashes, ddi, debug, files, install, location, metrics, mirror, pairing,
                     pcap, profiles, screen, syslog
  device_broker.py   shared daemon: socket, clients, events, syslog fan-out, idle exit
  device_client.py   asyncio client for the broker (used by the MCP server)
  device_link.py, device_crash.py, device_models.py   device helpers
  crash_symbols.py   keeps each build's binary + dSYM by UUID, resolves crash frames to file:line
  device_network.py  Network tab helpers shared by both daemons: condition rules, capture summary, pcapng
  device_files.py    Files feature helpers shared by both daemons (transfers, paths, conversions, Photos.sqlite)
  metrics_core.py    Performance sessions shared by both daemons: commands, sample rings, events, CSV
  app_console.py     App console shared by both daemons: an app's stdout/stderr split into lines, buffered, console.*
  metrics_stats.py   the Performance report: averages, percentiles, leaks, spikes, hitches, anomalies
  demo.py, demo_art.py, deviced_demo.py   demo mode: made-up helper answers, art and iPhone
  deviced_demo_core.py, deviced_demo_features/   the demo iPhone's data and its feature modules
  root/              scripts that run as root through pkexec
mcp/
  iosstudio_mcp/     the MCP server: server.py, tools/*.py (75 tools), prompts, resources
  pyproject.toml     its dependencies (installed into a private venv on first run)
  evaluation.xml, EVALUATION.md   demo-mode evaluation questions and results
bin/                 ios-studio launcher, iosstudio-helper, iosstudio-deviced, iosstudio-mcp
tests/               unit tests: build output parser (captured output in fixtures/), helper state files, crash report dates, crash symbolication, MCP jobs and budget
templates/           Swift app templates used by "New app"
install.sh           launcher integration (and --uninstall)
```

`shell.qml` creates one `AppState` (`app`) and every service on it: `app.setup`, `app.toolchain`, `app.account`, `app.device`, `app.projects`, `app.logs`. Each page gets `required property var app`. Every service exposes `status` (`unknown`, `loading`, `ready`, `needs-setup`, `error`), a `summary` for the Home page and sidebar badges, and `refresh()`.

## Reusing Omarchy's UI kit

The app doesn't ship its own widget library. `install.sh` creates two symlinks in the repo:

```
Commons -> $OMARCHY_PATH/shell/Commons
Ui      -> $OMARCHY_PATH/shell/Ui
```

QML then imports them as `qs.Commons` and `qs.Ui`, the same modules the Omarchy bar uses. Colours come from `Commons.Color.*`, sizes from `Style.font.*`, spacing from `Style.spacing.*`, radius from `Style.cornerRadius`. Don't hard-code pixels or colours. Both links are gitignored, so run `install.sh` after cloning.

Theme changes reach the window two ways: `ThemeWatcher.qml` watches the theme itself, and `install.sh` adds an Omarchy `theme-set` hook that calls `qs ipc -p <repo> call iosstudio applyTheme`.

## Helper protocol

```
bin/iosstudio-helper <group> <action> [--flag value ...]
```

Groups: `setup`, `toolchain` (setup.py), `account` (account.py), `projects`, `build` (projects.py), `app` (appconfig.py), `agents` (agents.py: connect the MCP server to AI tools, see [mcp.md](mcp.md)), `debug` (debugger.py: LLDB on your own apps, see [deviced-protocol.md](deviced-protocol.md#debugging-debug-used-by-iphone--debug-and-the-debug-helper-group)). `main.py` imports modules lazily, so one broken module doesn't take the others down.

A one-shot command prints exactly one JSON object and exits 0 (ok), 1 (handled error) or 2 (bad usage):

```json
{"ok": true, "data": {}}
{"ok": false, "error": {"code": "not-signed-in", "message": "You're not signed in to your Apple ID.",
  "fix": {"label": "Sign in", "page": "account", "action": "signin"}}}
```

`message` is written for end users. `fix` deep-links the UI with `page` plus an optional `step`, `action` or `url`. Secrets (password, 2FA code) only travel over stdin, never argv.

Long jobs (installs, builds, sign-in) stream JSON lines: `step`, `progress`, `log`, `notice`, `prompt` and a final `done`. The UI answers a prompt by writing `{"prompt": "<id>", "value": "..."}` to stdin. `Stream.qml` handles this for every service. The Setup-specific events are listed in [setup-steps.md](setup-steps.md).

Root work goes through one `pkexec helper/root/root-tasks.sh <flags>` per step, so the user sees one password prompt. Never `sudo`: there's no terminal. `makepkg` runs as the user.

## Device daemon

`services/DeviceService.qml` starts `bin/iosstudio-deviced` when the app opens. That process is only a stdio bridge. The daemon itself is a shared broker on `$XDG_RUNTIME_DIR/ios-studio/deviced.sock` (`helper/device_broker.py`). The bridge starts it if it isn't running, and it exits 120 s after its last client leaves, so the app and the MCP server (`helper/device_client.py`) share one tunnel to the phone. Demo mode uses its own socket under `$XDG_RUNTIME_DIR/ios-studio-demo/`. Diagnostics go to `deviced.log` next to the socket.

Requests and replies are JSON lines with an `id`. The daemon also pushes events such as `devices`, `connection` and `syslog`. Pages call it through `app.device.request(cmd, args, callback)` and listen on `app.device.daemonEvent(name, data)`. The Logs page uses the same daemon for the live log and crash reports.

The daemon also has a `--once <cmd> '<json>'` mode for background jobs (the daily auto-renew timer uses it to install), and it writes the last known device state to `~/.local/state/ios-studio/device.json`.

Every command and event is documented in [deviced-protocol.md](deviced-protocol.md).

### Device daemon features

`deviced.py` is only the core. Each feature group is a module in `helper/deviced_features/`, with a demo twin of the same name in `helper/deviced_demo_features/`. At start the daemon imports every module there (files starting with `_` are skipped) and calls its `register(reg)`. When a module fails to import or register, the daemon logs the traceback to `deviced.log`, skips the module and keeps serving the rest. `daemon.features` lists what loaded, what failed and which module owns each command. A command name that is already taken makes the second module fail, so two features can't silently shadow each other.

A feature module:

```python
# helper/deviced_features/perf.py
from deviced_core import emit, err          # never `import deviced` (it runs as __main__)
from deviced_kit import CursorRing

def register(reg):
    ring = CursorRing(max_items=5000, max_bytes=4 << 20)

    @reg.command("perf.start")
    async def start(d, a):                    # d: the Daemon, a: the request args
        s = d.need_session(a)                 # raises no-device / bad-args
        async def open_():
            ...                               # start sampling, return a handle
        async def close(handle, reason):      # reason: released, client, tunnel, session, shutdown, timeout
            ...
        await d.resources.acquire("perf", d.client(), open_, close, scope="tunnel", timeout=600)
        return {"running": True}

    @reg.command("perf.read")
    async def read(d, a):
        return await ring.wait_read(a.get("cursor"), timeout=min(float(a.get("wait", 0)), 20))

    @reg.on_tunnel_drop                       # also: on_client_gone, on_session_end, on_shutdown
    def dropped(d, session): ...

    @reg.info                                 # merged into device.info and the welcome deviceInfo
    def info(d, session): return {}
```

Handlers get `(d, args)` and raise `err(code, message, fix, detail)` for a plain-language error. Any other exception goes through `classify()`, and a connection error drops the tunnel so the session reconnects. `d.client()` is the asking client (`None` in `--once` mode).

The phone must only be reached through the session's existing tunnel. A second tunnel (another `UserspaceRsdTunnel` or `pymobiledevice3 remote ...`) breaks the daemon's link. The session (`s = d.need_session(a)`) offers:

| need | call |
|---|---|
| the RSD | `rsd = await s.get_rsd()` (waits up to 25 s for a reconnect) |
| a lockdown-over-RSD service for one call | `async with s.open_service(AfcService) as afc:` (also `PcapdService`, `DiagnosticsService`, or a coroutine such as `HouseArrestService.create`) |
| the same, kept open until the tunnel drops | `afc = await s.cached_service("afc", AfcService)`; `await s.forget_service("afc")` after it breaks |
| an app's container | `async with s.house_arrest(bundle_id, documents=False) as afc:` (VendContainer, or VendDocuments with `documents=True`) |
| a raw service connection | `conn = await s.service_connection(name, checkin=True)` (caller closes it) |
| the shared DVT connection | `dvt = await s.get_dvt()` (mounts the DDI first) |
| a DVT instrument kept open | `sm = await s.dvt_instrument("sysmon", Sysmontap)`; `s.reset_dvt()` if the channel broke |
| a DVT channel for one call | `async with s.dvt_channel(ProcessControl) as pc:` |
| a DVT connection of its own | `async with s.dedicated_dvt() as dvt:` (long-held things such as the condition inducer) |
| retry once on a broken DVT link | `await s.dvt_retry(fn)` |
| a loopback port into the phone | `fw = await s.port_forward(service="com.apple.internal.dt.remote.debugproxy")` or `port=8100`; `fw.port`, `await fw.stop()` |
| UDP for device-initiated media | `s.udp_available()`, `s.userspace_udp()`; pymobiledevice3's `screen_stream.open_media_receiver()` uses it by itself |
| USB-only services | `s.require_usb("Packet capture")`, `async with s.usb_lockdown() as lockdown:` |
| feature state | `s.data` (this device), `s.tunnel_data` (cleared when the tunnel drops) |

Import pymobiledevice3 classes the core doesn't load (`HouseArrestService`, `PcapdService`, `DisplayService`, `ConditionInducer`, ...) at the top of your feature module. If pymobiledevice3 changes, only that module fails to load.

`d.resources` (`deviced_kit.Resources`) holds things that stay open for clients: a mirror stream, a recording, a condition, a perf tap, a packet capture, a port forward. `acquire(key, client, open_fn, close_fn, scope=..., timeout=...)` counts one hold per client and runs `open_fn` once. `release(key, client)` returns how many holders are left and closes the resource at zero. The resource also closes when its last holder disconnects, after `timeout` seconds (`touch(key)` restarts the timer), on a tunnel drop (`scope="tunnel"`), on a device switch (`"tunnel"` or `"session"`) and at shutdown (all scopes). `forget(key)` drops a resource that ended by itself. Use it for anything that changes the phone, so a crashed client never leaves a network condition switched on. `mirror.py` (the live view and recordings) is a working example; it also shows the DisplayService video stream over `s.userspace_udp()`.

`deviced_kit.CursorRing` buffers streamed entries with bounds on count and bytes. `read(cursor)` returns `entries`, `nextCursor`, `hasMore`, `missed` and `bufferStart`, with the same semantics as the MCP server's `ios_logs_read`.

The demo twin registers the same command names with made-up data. Its handlers get `(ctx, args)`, where `ctx` is the demo backend (`ctx.resources`, `ctx.client()`). A plain `def` handler runs in a worker thread, an `async def` one on the event loop. Shared fake data and `event()` live in `deviced_demo_core`. Document new commands and events in [deviced-protocol.md](deviced-protocol.md).

To test in parallel checkouts without touching anyone else's daemon, give each one its own runtime directory:

```bash
export IOSSTUDIO_RUNTIME_DIR=$XDG_RUNTIME_DIR/ios-$NAME IOSSTUDIO_DEMO=1   # keep it short: socket paths max out at 107 bytes
python3 helper/device_client.py daemon.features     # starts this checkout's demo daemon on demand
python3 helper/device_client.py device.info
python3 helper/device_client.py daemon.shutdown '{"all": true}'
```

`IOSSTUDIO_RUNTIME_DIR` replaces `$XDG_RUNTIME_DIR` as the base of `ios-studio[-demo]/` (socket, lock, log, demo files) for `bin/iosstudio-deviced`, `device_client.py` and the MCP server. A real daemon started under it answers every request with `busy` while the default real daemon is running, because a second tunnel would break the first one's connection.

## Running from source

```bash
./install.sh                    # once: symlinks, launcher entry, icon, theme hook
qs -p .                         # run from the repo (Ctrl+C to stop)
qs log -p .                     # read the log of a running instance (ios-studio --log does the same)
qs ipc -p . call iosstudio nav projects   # drive it over IPC
bin/iosstudio-helper setup check          # call the helper directly
```

IPC functions: `nav(page)` (also `page:tab`), `navTab(page, tab)`, `refresh()`, `status()`, `applyTheme()`, `toast(text, level)`. `ios-studio <page>` uses `nav` to deep-link. Page names and aliases are in `Model.js`.

Demo mode, `ios-studio --demo[=ready|expiring|fresh] [page]`, runs the whole app on made-up data: a fake account, iPhone and projects. It never touches your Apple account, phone, Hyprland config or real settings. Use it for screenshots and for UI work without a phone. A demo window and a real one can't be open at the same time. The MCP server has its own demo mode (`IOSSTUDIO_DEMO=1`, see [mcp.md](mcp.md#demo-mode)).

`setup install` accepts `--dry-run`. The build output parser has unit tests with captured xtool output in `tests/fixtures/build-output/`, crash symbolication has a small arm64 dSYM fixture in `tests/fixtures/crash-symbolication/` (its `build.sh` rebuilds it), and the helper's state files and the MCP server's jobs, output budget and log reading have their own (stdlib `unittest`; the `test_mcp_*` files need the server's venv and are skipped without it). `IOSSTUDIO_DEMO_FAIL=linker` or `=package` makes demo builds fail with parsed linker or package errors, without a toolchain:

```bash
python3 -m unittest discover -s tests
~/.local/share/ios-studio/mcp-venv/bin/python -m unittest discover -s tests
```

Everything else has no automated tests: check that QML loads without warnings in `qs log` and that the helper commands you touched return valid JSON.

## Where things live

| What | Path |
|---|---|
| Settings | `~/.config/ios-studio/settings.json` |
| State (build history, renewals, device, setup progress) | `~/.local/state/ios-studio/` |
| Cache (screenshots, crash files, account lists) | `~/.cache/ios-studio/` |
| Kept builds for crash symbolication (binary + dSYM per UUID; last 10 per project, 1 GB in total) | `~/.cache/ios-studio/symbols/` |
| Screenshots, screen recordings, packet captures, metrics CSVs and memgraphs, camera test results (the oldest are deleted past a count and size cap, see `common.prune`) | `~/.cache/ios-studio/screens/`, `recordings/`, `captures/`, `metrics/`, `cameratest/` |
| Compiled Python modules of the helper, daemon and MCP server | `~/.cache/ios-studio/pycache/` |
| Debug symbols, per iOS version | `~/.cache/ios-studio/DeviceSupport/` |
| Imported photos and videos (default) | `~/Pictures/iPhone` |
| pymobiledevice3 venv | `~/.local/share/ios-studio/venv` |
| MCP server venv | `~/.local/share/ios-studio/mcp-venv` |
| Config backups made by Settings › AI agents | `~/.local/state/ios-studio/backups/` |
| App checkout | anywhere; the README clones into `~/.local/share/omarchy-ios-studio`. `install.sh` and `bin/ios-studio` find it from their own path |
| Launcher command | `~/.local/bin/ios-studio` (symlink into the checkout) |
| Desktop entry, icon | `~/.local/share/applications/ios-studio.desktop`, `~/.local/share/icons/hicolor/scalable/apps/ios-studio.svg` |
| New apps (default) | `~/Developer`; scanned roots default to whichever of `~/Developer`, `~/Projects`, `~/Code`, `~/src`, … exist (`common.default_project_roots()`) |
| Apple sign-in (owned by xtool) | `~/.config/xtool` |

## Conventions

- Every async surface has a loading, empty and error state. Errors show the plain `message` and a fix button.
- Destructive actions go through `app.confirm({...})` with Cancel as the default.
- Desktop notifications (`omarchy-notification-send`) only fire while the window isn't focused.
- Settings edits to Hyprland files only touch marker-delimited blocks, back the file up first and roll back if `hyprctl configerrors` reports a problem.

## Reference

- [components.md](components.md): the page contract and every shared component's API
- [deviced-protocol.md](deviced-protocol.md): device daemon commands and events
- [setup-steps.md](setup-steps.md): the Setup steps, what each installs, and the install events
