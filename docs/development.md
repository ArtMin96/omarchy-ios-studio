# Development

How iOS Studio is put together, for people who want to change it.

## Overview

iOS Studio is a standalone [Quickshell](https://quickshell.org/) config with a Python backend.

- UI: QML, started with `qs -n -p <repo>`. `shell.qml` declares `//@ pragma AppId ios-studio` (that's also the Hyprland window class), a `ShellRoot` and one `FloatingWindow`. Closing the window quits the app.
- Helper (`bin/iosstudio-helper`): stdlib-only Python under `/usr/bin/python3 -I` (compiled modules cached in `~/.cache/ios-studio/pycache`). One call per action, or a stream of JSON lines for long jobs.
- Device broker (`bin/iosstudio-deviced`, `helper/iosstudio/device/device_broker.py`): one long-running Python process per user in the pymobiledevice3 venv. It owns the only connection to the iPhone and listens on a Unix socket, so the app, the MCP server and background jobs share it. It exits two minutes after its last client disconnects.
- MCP server (`bin/iosstudio-mcp`, `mcp/`): a stdio server for AI coding agents. It calls the same helper as the app and reaches the iPhone through the broker with `helper/iosstudio/device/device_client.py`. See [mcp.md](mcp.md).
- Tools it drives: xtool (build, sign, install, Apple account), the Swift toolchain, omarchy-apple-dev (SDK preparation), pymobiledevice3 (device access), rcodesign and ipsw.

## File layout

```
shell.qml            window, sidebar, page loader, global keys, IPC handler, overlays
AppState.qml         shared state: current page, nav(), toast(), confirm(), settings
core/                non-visual plumbing: Cli.qml, Stream.qml (one-shot and streaming helper calls),
                     Resource.qml (one piece of backend data + its state), ThemeWatcher.qml (follows the
                     Omarchy theme), Host.qml (overlay stack, singleton), CommandCheck.qml (`command -v`, singleton)
lib/Model.js         pure helpers: glyphs, PAGES, SHORTCUTS, formatting, run preset rules
services/            one QML service per area: Setup, Toolchain, Account, Device, Project, Log, Watch
pages/               one page per sidebar entry
components/          UI building blocks (see components.md), one folder per area:
  common/            shared widgets used by several areas (Card, ListRow, Sheet, StateView, Theme, LogView, …)
  device/            the iPhone page: hero, tabs (Device*View), app data, debug, perf chart, screenshot compare
  projects/          the Projects page: cards, build panel, sheets (new, packages, format, presets, build settings)
  logs/              the Logs page: device/console/build log tabs, crash browser
  account/           the Apple ID page: sign-in sheet, revoke warning
  settings/          Settings parts: AI agents, editor setup
helper/
  run.py             starts a module of the package as __main__ (`python3 -I helper/run.py iosstudio …`); the bin/ scripts use it
  root/              scripts that run as root through pkexec
  iosstudio/         the helper and the device daemon: one stdlib-only package (the real daemon adds pymobiledevice3)
    __main__.py, cli.py   `python -m iosstudio <group> <action>`: group name -> features/ module (imported lazily)
    core/            shared by everything; each module is used qualified (`output.ok(…)`, `appdirs.STATE_DIR`) so tests can patch it
      output.py      the output protocol: ok()/fail()/event()/step()/done()/prompt(), HelperError, fix()
      subproc.py     run_cmd(), stream_cmd(), run_group(), kill_proc(), wait_or_kill(), which(), pymd3_python(), demo_delay()
      fileio.py      atomic_write(), load_json()/save_json(), tail_text(), dir_size(), prune()
      appdirs.py     XDG folders read at call time (config_home()/state_dir()/…; CONFIG_DIR/STATE_DIR/… are import-time snapshots), sdk_bundle(), tilde(), REPO, settings(), project roots, cache_get()/cache_put()
      flags.py       parse_flags(): --key value / --flag -> dict
    features/        one module per helper group, plus the pure logic they and the daemon features share
      setup.py, account.py, projects.py, appconfig.py   the setup/toolchain, account, projects/build and app groups
      build_settings.py  per-project build settings (prebuild command, env, open-file limit, allow generated), used by projects.py
      format.py      format and lint (`projects format`, `projects format-lint`, or just `format` / `format-lint`): swift-format dry run/apply with a bounded diff, swift-format and SwiftLint findings as style diagnostics, the stored lint result
      packages.py    Swift package dependencies (`packages` group): list (dump-package, cached, + Package.resolved), add/add-product, update/resolve as streams with step/progress events and cancel, remove with a checked dry run
      package_manifest.py  Package.resolved v1–v3, version rules, and the remove edit on Package.swift through a small Swift lexer
      project_check.py   the project check: config mistakes found before building (xtool.yml keys, usage descriptions, icon, entitlements), used by projects.py
      run_presets.py     run presets: saved launch arguments, environment, language/region and UI test mode per project, and the launch args they resolve to, used by projects.py
      watch.py       compile on save (`watch` group): inotify (ctypes) or polling, debounce/supersede scheduler, one detached watcher per user found through watch.lock / watch.json in the runtime dir, `build compile` per save
      editor_setup.py    editor setup (`editor` group): Swift completion in Zed, Neovim and VS Code once per machine, .bsp/xtool.json (and .vscode/ for VS Code) per project after builds and on create/convert, a JSONC merge that only inserts
      screenshots.py     screenshot baselines and visual diffs (`screenshots` group): pure-Python PNG codec or Pillow, status-bar mask, regions, diff image, matrix sets
      ui_inspect.py  UI inspection without a device: accessibility focus events -> elements, label matching, the change summary of a press, audit issues (used by device/features/ui.py and its demo twin)
      diagnostics.py     build output -> diagnostics (compiler, linker, package, signing, device, ...), used by projects.py
      crash_symbols.py   keeps each build's binary + dSYM by UUID, resolves crash frames to file:line
      app_data.py    App data shared by both daemons: plist <-> JSON with tagged types, typed values, store summaries and bounded rows (read-only sqlite), the store cache, reset plans, the last build of a bundle id
      xip_version.py     reads the Xcode version out of a .xip without unpacking it (used by setup.py)
      agents.py      Settings › AI agents: connect the MCP server to coding agents
      debugger.py    iPhone › Debug: backtraces and breakpoint sessions (group `debug`)
      lldb_driver.py     runs inside lldb's Python (python3.12) and drives lldb for debugger.py
    device/          the device daemon, its broker and client
      deviced.py     device daemon core: session, tunnel, device list, dispatch (pymobiledevice3)
      deviced_core.py    what feature modules import: emit, dlog, err, classify, paths, PortForward
      deviced_kit.py     shared by both daemons: feature registry/loader, Resources, CursorRing
      features/      one module per feature group of the real daemon: appdata, apps, cameratest, conditions,
                     console, crashes, ddi, debug, files, install, location, metrics, mirror, pairing,
                     pcap, profiles, screen, syslog, ui
      device_broker.py   shared daemon: socket, clients, events, syslog fan-out, idle exit
      device_client.py   asyncio client for the broker (stdlib only, no package imports: the MCP server loads it by path)
      device_link.py, device_crash.py, device_models.py   device helpers
      device_network.py  Network tab helpers shared by both daemons: condition rules, capture summary, pcapng
      device_files.py    Files feature helpers shared by both daemons (transfers, paths, conversions, Photos.sqlite)
      metrics_core.py    Performance sessions shared by both daemons: commands, sample rings, events, CSV
      metrics_stats.py   the Performance report: averages, percentiles, leaks, spikes, hitches, anomalies
      app_console.py     App console shared by both daemons: an app's stdout/stderr split into lines, buffered, console.*
      deviced_demo.py, deviced_demo_core.py, demo_features/   the demo iPhone: daemon, data and feature modules
    demo/            the helper's demo mode
      demo.py, demo_art.py   made-up helper answers and art (demo_art is also used by the demo daemon)
      debugger_demo.py   the `debug` group in demo mode: a made-up backtrace and session, no lldb
mcp/
  iosstudio_mcp/     the MCP server: server.py, tools/*.py (98 tools), prompts, resources
  pyproject.toml     its dependencies (installed into a private venv on first run)
  evaluation.xml, EVALUATION.md   demo-mode evaluation questions and results
bin/                 ios-studio launcher, iosstudio-helper, iosstudio-deviced, iosstudio-mcp
pyproject.toml       dev tools only (pytest and ruff config, the dev dependency group); not a package
tests/               helper/ (unit), cli/ (the helper as a subprocess), mcp/ (MCP tools), e2e/ (real Swift, network), support/ (shared fakes and helpers), fixtures/ (see Tests below)
templates/           Swift app templates used by "New app"
install.sh           launcher integration (and --uninstall)
.github/             CI and release workflows, release-note categories, scripts/check-version.sh (see CI and Releases below)
```

QML imports between these folders are relative, so qmllint can follow them: a page imports
`"../components/common"` and its own area folder, a component in `components/device/` imports `"../common"`
(and `"../../core"` if it uses Stream, Resource, Host or CommandCheck), and everything imports
`"<up>/lib/Model.js" as Model`. Files in the same folder see each other without an import. Quickshell only
resolves folders it found by following imports from `shell.qml`; pages load by URL, so `shell.qml` imports
`"pages"` to make Quickshell scan them (and through them every `components/` folder). A new folder must be
imported from a file that is already reached, or its types are "not a type" at runtime.

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

Groups: `setup`, `toolchain` (setup.py), `account` (account.py), `projects`, `build` (projects.py; `projects check` in project_check.py, see [features/project-check.md](features/project-check.md); `projects presets` and `build run --preset` in run_presets.py, see [features/run-presets.md](features/run-presets.md); per-project build settings in build_settings.py, see [features/project-build-settings.md](features/project-build-settings.md)), `app` (appconfig.py), `agents` (agents.py: connect the MCP server to AI tools, see [mcp.md](mcp.md)), `debug` (debugger.py: LLDB on your own apps, see [deviced-protocol.md](deviced-protocol.md#debugging-debug-used-by-iphone--debug-and-the-debug-helper-group)), `screenshots` (screenshots.py: baselines and visual diffs, see [features/screenshot-compare.md](features/screenshot-compare.md)), `editor` (editor_setup.py: `editor status`, `editor setup --editor E`, `editor status --project P [--fix]`, see [features/editor-setup.md](features/editor-setup.md)), `watch` (watch.py: `watch run --project P [--auto-run] [--debounce-ms N]` streams the watcher's snapshots, `--detach [--owner PID]` starts or joins it and returns; `watch status [--project P] [--wait-seconds N]`, `watch stop [--project P]`; see [features/watch-mode.md](features/watch-mode.md)), `packages` (packages.py: `packages list|add|add-product|update|resolve|remove --project P …`, see [features/packages.md](features/packages.md)). `iosstudio/cli.py` imports modules lazily, so one broken module doesn't take the others down.

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

`services/DeviceService.qml` starts `bin/iosstudio-deviced` when the app opens. That process is only a stdio bridge. The daemon itself is a shared broker on `$XDG_RUNTIME_DIR/ios-studio/deviced.sock` (`helper/iosstudio/device/device_broker.py`). A process started without `XDG_RUNTIME_DIR` (the MCP SDK's `stdio_client` drops it) uses `/run/user/<uid>` when that folder exists and is yours, so it still finds the app's daemon; only without it does everything fall back to `/tmp/ios-studio-<uid>` (`device_client.runtime_base()`, also used by watch mode). The bridge starts it if it isn't running, and it exits 120 s after its last client leaves, so the app and the MCP server (`helper/iosstudio/device/device_client.py`) share one tunnel to the phone. Demo mode uses its own socket under `$XDG_RUNTIME_DIR/ios-studio-demo/`. Diagnostics go to `deviced.log` next to the socket.

Requests and replies are JSON lines with an `id`. The daemon also pushes events such as `devices`, `connection` and `syslog`. Pages call it through `app.device.request(cmd, args, callback)` and listen on `app.device.daemonEvent(name, data)`. The Logs page uses the same daemon for the live log and crash reports.

The daemon also has a `--once <cmd> '<json>'` mode for background jobs (the daily auto-renew timer uses it to install), and it writes the last known device state to `~/.local/state/ios-studio/device.json`.

Every command and event is documented in [deviced-protocol.md](deviced-protocol.md).

### Device daemon features

`deviced.py` is only the core. Each feature group is a module in `helper/iosstudio/device/features/`, with a demo twin of the same name in `helper/iosstudio/device/demo_features/`. At start the daemon imports every module there (files starting with `_` are skipped) and calls its `register(reg)`. When a module fails to import or register, the daemon logs the traceback to `deviced.log`, skips the module and keeps serving the rest. `daemon.features` lists what loaded, what failed and which module owns each command. A command name that is already taken makes the second module fail, so two features can't silently shadow each other.

A feature module:

```python
# helper/iosstudio/device/features/perf.py
from iosstudio.device.deviced_core import emit, err   # never import deviced (it runs as __main__)
from iosstudio.device.deviced_kit import CursorRing

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
python3 helper/iosstudio/device/device_client.py daemon.features     # starts this checkout's demo daemon on demand
python3 helper/iosstudio/device/device_client.py device.info
python3 helper/iosstudio/device/device_client.py daemon.shutdown '{"all": true}'
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

IPC functions: `nav(page)` (also `page:tab`), `navTab(page, tab)`, `refresh()`, `status()`, `applyTheme()`, `toast(text, level)`. `ios-studio <page>` uses `nav` to deep-link. Page names and aliases are in `lib/Model.js`.

Demo mode, `ios-studio --demo[=ready|expiring|fresh] [page]`, runs the whole app on made-up data: a fake account, iPhone and projects. It never touches your Apple account, phone, Hyprland config or real settings. Use it for screenshots and for UI work without a phone. A demo window and a real one can't be open at the same time. The MCP server has its own demo mode (`IOSSTUDIO_DEMO=1`, see [mcp.md](mcp.md#demo-mode)).

`setup install` accepts `--dry-run`. `IOSSTUDIO_DEMO_FAIL=linker` or `=package` makes demo builds fail with parsed linker or package errors, without a toolchain. `IOSSTUDIO_DEMO_DELAY` multiplies the demo's pretend-work pauses (build steps, installs, the demo iPhone's UI and debugger); `0` makes them instant, which the tests use.

## Tests

pytest runs everything (the tests themselves are plain `unittest` classes). Make the dev venv once with `uv sync --group dev` (pytest, ruff, and the MCP server's dependencies in `.venv/`, from the root `pyproject.toml`).

| Area | Folder | Command |
|---|---|---|
| Helper modules, in process (stdlib only) | `tests/helper/` | `python3 -m pytest tests/helper` |
| Helper CLI: `bin/iosstudio-helper` as a subprocess with fake xtool, swift, swift-format (marked `slow`) | `tests/cli/` | `python3 -m pytest tests/cli` |
| MCP server tools (need mcp, pydantic, pillow; skipped without them) | `tests/mcp/` | `.venv/bin/pytest tests/mcp` |
| Real Swift toolchain, network (marked `slow`; network tests also `network`) | `tests/e2e/` | `.venv/bin/pytest tests/e2e -m ""` |
| Everything CI runs | `tests/` | `.venv/bin/pytest --durations=10` |

- `pytest` deselects `network` tests by default (`addopts` in `pyproject.toml`); `-m ""` runs everything, `-m "not slow"` skips the subprocess and toolchain tests.
- `tests/helper/` and `tests/cli/` need nothing but the standard library and pytest: CI runs them with a bare system Python to keep the helper stdlib-only.
- `tests/conftest.py` points every `XDG_*` folder and `IOSSTUDIO_RUNTIME_DIR` at a temp folder before any test imports the helper, so no test touches your settings, state or the running device daemon, and sets `IOSSTUDIO_DEMO_DELAY=0`.
- Shared pieces live in `tests/support/` (`import support`): repo paths, `isolated_env()` for helper subprocesses, `helper_json()`, fake xtool/swift/swift-format scripts, throwaway projects, a crash report, and `FakeMCP`/`size_of` for MCP tool tests. Test files never import each other.
- Helper and MCP modules are imported from their packages (`from iosstudio.features import format as F`, `from iosstudio_mcp import budget`): `pyproject.toml` puts `helper/`, `mcp/` and `tests/` on the path. Patch a shared folder or output where the code reads it (`mock.patch.object(appdirs, "STATE_DIR", …)`, `output._write = …`).
- Fixtures (captured tool output, small projects, PNGs, stores) are in `tests/fixtures/<area>/`; each test file's docstring says what it covers.
- No test needs xtool, pymobiledevice3, an iPhone or your home folder. Tests that use Swift, `swift-format` or `llvm-symbolizer` skip without them.

Everything else has no automated tests: check that QML loads without warnings in `qs log` and that the helper commands you touched return valid JSON.

## CI

`.github/workflows/ci.yml` runs on every pull request, on pushes to `main` and by hand (Actions › CI › Run workflow). A newer push to the same branch cancels the older run. It has three jobs:

- Python, on 3.11 (the oldest the helper runs on, because of `tomllib`), 3.14 (what Omarchy ships) and 3.15. It compiles every file in `helper/`, `mcp/` and `tests/`, runs `tests/helper` and `tests/cli` with that Python and only pytest installed (the helper must stay stdlib-only), makes the dev venv with `uv sync --group dev` and runs every test but the `network` ones in it with `--durations=10`, then checks that `bin/iosstudio-mcp --prepare` (the same uv path users get) still builds the MCP venv. No test needs xtool, pymobiledevice3, an iPhone or your home folder: they use fake tools, fixtures and temp folders.
- Lint: ruff 0.17.4 with the rules in `pyproject.toml`: pyflakes, pycodestyle's real errors (unused imports and variables, undefined names, syntax) and import order (`I`, fixable with `ruff check --fix`), no style rules. `E731` (a lambda assigned to a name) and `E741` (names like `l`) are off. Then shellcheck at warning level on `install.sh`, the shell scripts in `bin/`, `helper/root/`, `.github/scripts/` and the dSYM fixture's `build.sh`.
- QML: qmllint on every `.qml` file in an `archlinux:latest` container with `qt6-declarative` and `quickshell` from Arch's repos. `qs.Commons` and `qs.Ui` are symlinks into Omarchy's shell that only `install.sh` makes, so their imports don't resolve there. qmllint's default levels only fail on syntax errors; the thousands of unresolved-import, unqualified and missing-property warnings that follow from that are counted in the log but don't fail the job.

To run the same checks locally:

```bash
python3 -m compileall -q helper mcp tests        # writes __pycache__ folders (gitignored)
python3 -m pytest tests/helper tests/cli           # stdlib + pytest only
uv sync --group dev && .venv/bin/pytest -m "not network" --durations=10
.venv/bin/ruff check                               # or: uvx ruff@0.17.4 check
shellcheck --severity=warning install.sh bin/ios-studio bin/iosstudio-helper bin/iosstudio-deviced bin/iosstudio-mcp helper/root/*.sh tests/fixtures/crash-symbolication/build.sh .github/scripts/*.sh
/usr/lib/qt6/bin/qmllint $(git ls-files '*.qml') >/dev/null; echo $?   # 0 unless there's a syntax error
actionlint                                         # the workflow files themselves
```

## Releases

Pushing a tag `vX.Y.Z` runs `.github/workflows/release.yml`. It checks that the tag matches the version files (`.github/scripts/check-version.sh`), runs the CI jobs above, then creates a GitHub Release with notes generated from the merged pull requests, grouped by label as set in `.github/release.yml` (enhancement, bug, mcp, ui/accessibility, documentation, ci, then everything else). It attaches `ios-studio-X.Y.Z.tar.gz` (a `git archive` of the tag) and `ios-studio-X.Y.Z.tar.gz.sha256`. A tag with a suffix, like `v0.2.0-rc1`, is published as a pre-release and checked against its base version (0.2.0). Only the last job can write to the repo (`contents: write`).

To make a release:

1. Bump the version in both files to the same `X.Y.Z`: `readonly property string version` in `AppState.qml` (Settings › About shows it) and `version` in `mcp/pyproject.toml`. Changing `pyproject.toml` makes each user's MCP venv rebuild once on its next start.
2. Merge that through a pull request, then tag the merge commit on `main` and push the tag:

   ```bash
   git switch main && git pull
   .github/scripts/check-version.sh v0.2.0    # prints 0.2.0, or says which file is still behind
   git tag -a v0.2.0 -m "iOS Studio 0.2.0"
   git push origin v0.2.0
   ```

3. Watch Actions › Release. If the version check fails, the job names the file that differs: delete the tag (`git push origin :v0.2.0 && git tag -d v0.2.0`), fix it and tag again.

Users install a release by cloning its tag or from the tarball, see the README's Install section.

## Where things live

| What | Path |
|---|---|
| Settings | `~/.config/ios-studio/settings.json` |
| State (build history, renewals, device, setup progress; projects.json has each project's build settings and run presets; editor-auto.json what editor setup wrote into each project) | `~/.local/state/ios-studio/` |
| Latest lint result per project (format and lint) | `~/.local/state/ios-studio/lint/` |
| Full diffs of format dry runs that didn't fit (the newest 10 are kept) | `~/.cache/ios-studio/format/` |
| Swift packages: manifest descriptions, product lists and SwiftPM scratch folders (the newest 100 of each), full `Package.swift` diffs that didn't fit (the newest 10) | `~/.cache/ios-studio/packages/` |
| One lock per project so two package changes never run at once | `~/.local/state/ios-studio/packages/` |
| Build logs (the newest 300 builds' logs are kept) | `~/.local/state/ios-studio/build-logs/` |
| Cache (screenshots, crash files, account lists) | `~/.cache/ios-studio/` |
| Kept builds for crash symbolication (binary + dSYM per UUID; last 10 per project, 1 GB in total) | `~/.cache/ios-studio/symbols/` |
| Screenshots, screen recordings, packet captures, metrics CSVs and memgraphs, camera test results (the oldest are deleted past a count and size cap, see `fileio.prune`) | `~/.cache/ios-studio/screens/`, `recordings/`, `captures/`, `metrics/`, `cameratest/` |
| App executables unpacked from `.ipa` files for the debugger (the newest 2 per project) | `~/.cache/ios-studio/debug-builds/` |
| Watch mode (lock, status, events, owners, log; demo mode uses `ios-studio-demo/`) | `$XDG_RUNTIME_DIR/ios-studio/watch*` (`$IOSSTUDIO_RUNTIME_DIR` overrides the base; without `XDG_RUNTIME_DIR`, `/run/user/<uid>` when it's yours, else `/tmp/ios-studio-<uid>`: `device_client.runtime_base()`, shared with the device daemon) |
| Compiled Python modules of the helper, daemon and MCP server | `~/.cache/ios-studio/pycache/` |
| Debug symbols, per iOS version | `~/.cache/ios-studio/DeviceSupport/` |
| App data: copies of an app's SwiftData / Core Data stores (pulled again when they change; the 5 most recently used apps) its preferences file before each change (the newest 10 per app), and its recent changes for the `reverted` check (`writes.json`) | `~/.cache/ios-studio/appdata/<bundle id>/` |
| Imported photos and videos (default) | `~/Pictures/iPhone` |
| pymobiledevice3 venv | `~/.local/share/ios-studio/venv` |
| MCP server venv | `~/.local/share/ios-studio/mcp-venv` |
| Config backups made by Settings › AI agents | `~/.local/state/ios-studio/backups/` |
| App checkout | anywhere; the README clones into `~/.local/share/omarchy-ios-studio`. `install.sh` and `bin/ios-studio` find it from their own path |
| Launcher command | `~/.local/bin/ios-studio` (symlink into the checkout) |
| Desktop entry, icon | `~/.local/share/applications/ios-studio.desktop`, `~/.local/share/icons/hicolor/scalable/apps/ios-studio.svg` |
| New apps (default) | `~/Developer`; scanned roots default to whichever of `~/Developer`, `~/Projects`, `~/Code`, `~/src`, … exist (`appdirs.default_project_roots()`) |
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
