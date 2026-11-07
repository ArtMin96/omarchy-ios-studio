# Development reference

The detailed reference for people changing iOS Studio. Start with [ARCHITECTURE.md](../ARCHITECTURE.md) (the
map: processes, layers, patterns) and [CONTRIBUTING.md](../CONTRIBUTING.md) (setup, running in demo mode,
tests, recipes, PR rules).

## File layout

```
shell.qml              window, sidebar, page loader, global keys, IPC handler, overlays
AppState.qml           `app`: current page, nav(), toast(), confirm(), settings, every service
core/                  non-visual QML plumbing
  Cli.qml, Stream.qml    one-shot and streaming helper calls
  Resource.qml           one piece of backend data and its state
  Host.qml               overlay stack (singleton)
  CommandCheck.qml       cached `command -v` (singleton)
  ThemeWatcher.qml       follows the Omarchy theme
lib/Model.js           pure helpers: glyphs, PAGES, SHORTCUTS, formatting, run preset rules
services/              one service per area: Account, Device, Log, Project, Setup, Toolchain, Watch (+ PathResults)
pages/                 one page per sidebar entry
components/            UI building blocks, one folder per area (APIs in components.md)
  common/                shared widgets: Card, ListRow, Sheet, StateView, Theme, LogView, …
  device/                the iPhone page: hero, tabs (Device*View), app data, debug, perf chart, screenshot compare
  projects/              the Projects page: cards, build panel, sheets (new, packages, format, presets, build settings)
  logs/                  the Logs page: device/console/build log tabs, crash browser
  account/               the Apple ID page: sign-in sheet, revoke warning
  settings/              Settings parts: AI agents, editor setup
helper/
  run.py                 starts a module of the package (`python3 -I helper/run.py iosstudio …`); bin/ uses it
  root/                  scripts that run as root through pkexec (root-tasks.sh, the Swift hold hook, xcspec.py)
  iosstudio/             the helper and the device daemon: one stdlib-only package (the real daemon adds pymobiledevice3)
    __main__.py, cli.py    `<group> <action>` -> features/ module (MODULES), imported lazily
    core/                  shared by everything; used qualified (`output.ok(…)`, `appdirs.STATE_DIR`) so tests can patch it
      output.py              the output protocol: ok()/fail()/event()/step()/progress()/log()/done()/prompt(), HelperError, fix()
      subproc.py             tool_env(kind), run_cmd(), run_group(), Runner (streaming, prompts, cancel, timeout, group kill),
                             stream_cmd(), kill_group(), which(), pymd3_python(), demo_delay()
      fileio.py              atomic_write(), locks, load_json()/save_json(), tail_text(), dir_size(), prune()
      appdirs.py             XDG folders read at call time, sdk_bundle(), tilde(), REPO, settings(), project roots, cache_get()/cache_put()
      flags.py               parse_flags(): --key value / --flag -> dict
      jsonc.py               JSON with comments, span-preserving (editor and agent configs)
      xtool_yml.py           xtool.yml reader and a key update that keeps the rest of the file
    features/              one module or package per helper group, plus logic the daemon features share
      projects/              groups `projects`, `build`: model.py (discovery, templates), store.py (projects.json, builds.json,
                             build-logs/), build.py (the build flow), runner.py (BuildRunner: xtool output -> events),
                             renew.py (auto-renew timer), commands.py (flags in, JSON out)
      setup/                 groups `setup`, `toolchain`: facts.py, steps.py, installers.py, downloads.py, sources.py, pins.py,
                             xip.py, toolchain.py, runtime.py, commands.py
      watch/                 group `watch` (compile on save): fswatch.py (inotify/polling), watcher.py (scheduler),
                             builds.py, lifecycle.py, runtime.py (lock/status files), commands.py
      packages/              group `packages`: manifest.py, manifest_edit.py, add.py, update.py, project.py, swift.py,
                             demo_mode.py, commands.py
      editor_setup/          group `editor`: editors.py (once per machine), project.py (per project), actions.py, env.py, demo.py
      agents/                group `agents` (connect the MCP server to AI tools): clients.py, writers.py, server.py, rpc.py, demo.py
      account.py             group `account` (xtool)
      appconfig.py           group `app`: launcher keybinding, window rule, opacity, about
      debugger.py            group `debug` (LLDB on your own apps); lldb_driver.py runs inside lldb's Python
      screenshots.py         group `screenshots`: baselines and visual diffs
      format.py              `projects format` / `format-lint`: swift-format and SwiftLint
      build_settings.py, run_presets.py, project_check.py, diagnostics.py   used by projects/
      app_data.py            plist <-> JSON, store summaries and rows, reset plans (shared with the daemons)
      ui_inspect.py          UI inspection without a device (shared with device/features/ui/)
      crash_symbols.py       keeps each build's binary + dSYM, resolves crash frames to file:line
      config_files.py        edits other programs' config files without losing anything
      xip_version.py         the Xcode version inside a .xip, without unpacking it
    demo/                  the helper's demo mode: demo.py (router + made-up answers), demo_art.py, debugger_demo.py
    device/                the device daemon, its broker and client
      protocol.py            the wire format (JSON lines, errors)
      backend.py             the Backend base class: what the broker calls on a daemon
      kit.py                 FeatureRegistry + load_features(), Resources, CursorRing
      broker.py              the shared daemon: socket, clients, events, syslog fan-out, idle exit
      client.py              asyncio client (stdlib only, no package imports: the MCP server loads it by path); runtime_base()
      link.py                USB and Wi-Fi transport; network.py, models.py   shared helpers
      real/                  the real daemon: daemon.py (session, tunnel, dispatch, --serve/--once), core.py (what features import)
      demo/                  the made-up iPhone: daemon.py (DemoBackend), core.py (its shared state)
      features/<name>/       real.py + demo.py (+ core.py and helpers): appdata, apps (index.py = AppIndex, lookup.py,
                             install.py), cameratest, conditions, console, crashes, ddi, debug, files, location, metrics
                             (stats.py), mirror, pairing, pcap, profiles, screen, syslog, ui
mcp/
  iosstudio_mcp/         the MCP server (see mcp.md#for-tool-authors)
    server.py, state.py, helper.py, models.py, prompts.py, resources.py, guide.md
    core/                  registry.py (@tool, slim_schemas), jobs.py (JobManager), job_status.py, deadline.py (25 s),
                           budget.py, params.py, errors.py
    backends/              device.py, projects.py, builds.py, console.py, crashes.py
    tools/                 one module per area (101 tools), listed in tools/__init__.py MODULES
  pyproject.toml         its dependencies (installed into a private venv on first run)
  evaluation.xml, EVALUATION.md   demo-mode evaluation questions and results
bin/                   ios-studio (launcher), iosstudio-helper, iosstudio-deviced, iosstudio-mcp
templates/             Swift app templates used by New app
tests/                 helper/, cli/, mcp/, qml/, e2e/, support/ (shared fakes), fixtures/<area>/
pyproject.toml         dev tools only (pytest and ruff config, the dev dependency group); not a package
install.sh             launcher integration (and --uninstall)
.github/               CI and release workflows, release-note categories, scripts/check-version.sh
```

## QML wiring

QML imports between these folders are relative, so qmllint can follow them: a page imports
`"../components/common"` and its own area folder, a component in `components/device/` imports `"../common"`
(and `"../../core"` if it uses Stream, Resource, Host or CommandCheck), and everything imports
`"<up>/lib/Model.js" as Model`. Files in the same folder see each other without an import. Quickshell only
resolves folders it found by following imports from `shell.qml`; pages load by URL, so `shell.qml` imports
`"pages"` to make Quickshell scan them (and through them every `components/` folder). A new folder must be
imported from a file that is already reached, or its types are "not a type" at runtime.

`shell.qml` creates one `AppState` (`app`), and `AppState` creates every service on it: `app.setup`, `app.toolchain`, `app.account`, `app.device`, `app.projects`, `app.logs`, `app.watch`. Each page gets `required property var app`. Every service exposes `status` (`unknown`, `loading`, `ready`, `needs-setup`, `error`), a `summary` for the Home page and sidebar badges, and `refresh()`.

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

Groups and their modules are in `MODULES` in `helper/iosstudio/cli.py`: `setup`, `toolchain` (features/setup/), `account`, `projects`, `build` (features/projects/), `app` (appconfig.py), `agents` (features/agents/, see [mcp.md](mcp.md)), `debug` (debugger.py), `screenshots`, `editor` (features/editor_setup/), `watch` (features/watch/) and `packages` (features/packages/). `format` and `format-lint` are short for `projects format` / `projects format-lint`. Each feature's commands and flags are in its page under [features/](features/README.md). `cli.py` imports modules lazily, so one broken module doesn't take the others down. With `IOSSTUDIO_DEMO` set, every command goes to `demo/demo.py` instead.

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

`services/DeviceService.qml` starts `bin/iosstudio-deviced` when the app opens. That process is only a stdio bridge. The daemon itself is a shared broker on `$XDG_RUNTIME_DIR/ios-studio/deviced.sock` (`helper/iosstudio/device/broker.py`, with `real/daemon.py` or `demo/daemon.py` as its backend). A process started without `XDG_RUNTIME_DIR` (the MCP SDK's `stdio_client` drops it) uses `/run/user/<uid>` when that folder exists and is yours, so it still finds the app's daemon; only without it does everything fall back to `/tmp/ios-studio-<uid>` (`client.runtime_base()`, also used by watch mode). The bridge starts it if it isn't running, and it exits 120 s after its last client leaves, so the app and the MCP server (`helper/iosstudio/device/client.py`) share one tunnel to the phone. Demo mode uses its own socket under `$XDG_RUNTIME_DIR/ios-studio-demo/`. Diagnostics go to `deviced.log` next to the socket.

Requests and replies are JSON lines with an `id`. The daemon also pushes events such as `devices`, `connection` and `syslog`. Pages call it through `app.device.request(cmd, args, callback)` and listen on `app.device.daemonEvent(name, data)`. The Logs page uses the same daemon for the live log and crash reports.

The daemon also has a `--once <cmd> '<json>'` mode for background jobs (the daily auto-renew timer uses it to install), and it writes the last known device state to `~/.local/state/ios-studio/device.json`.

Every command and event is documented in [deviced-protocol.md](deviced-protocol.md).

### Device daemon features

`real/daemon.py` is only the core. Each feature group is a folder `helper/iosstudio/device/features/<name>/` with `real.py` (the phone) and `demo.py` (the made-up iPhone) side by side, plus any shared logic (`core.py`, or named helpers such as `apps/index.py`). At start each daemon imports its side of every folder (`kit.load_features`) and calls its `register(reg)`. When a module fails to import or register, the daemon logs the traceback to `deviced.log`, skips it and keeps serving the rest. `daemon.features` lists what loaded, what failed and which module owns each command. A command name that is already taken makes the second module fail, so two features can't silently shadow each other. Real and demo must register the same names: `tests/helper/test_device_protocol.py::Parity` checks it.

A feature module:

```python
# helper/iosstudio/device/features/perf/real.py
from iosstudio.device.real.core import emit, err   # never import real.daemon (it runs as __main__)
from iosstudio.device.kit import CursorRing

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

`d.resources` (`kit.Resources`) holds things that stay open for clients: a mirror stream, a recording, a condition, a perf tap, a packet capture, a port forward. `acquire(key, client, open_fn, close_fn, scope=..., timeout=...)` counts one hold per client and runs `open_fn` once. `release(key, client)` returns how many holders are left and closes the resource at zero. The resource also closes when its last holder disconnects, after `timeout` seconds (`touch(key)` restarts the timer), on a tunnel drop (`scope="tunnel"`), on a device switch (`"tunnel"` or `"session"`) and at shutdown (all scopes). `forget(key)` drops a resource that ended by itself. Use it for anything that changes the phone, so a crashed client never leaves a network condition switched on. `features/mirror/real.py` (the live view and recordings) is a working example; it also shows the DisplayService video stream over `s.userspace_udp()`.

`kit.CursorRing` buffers streamed entries with bounds on count and bytes. `read(cursor)` returns `entries`, `nextCursor`, `hasMore`, `missed` and `bufferStart`, with the same semantics as the MCP server's `ios_logs_read`.

The demo twin registers the same command names with made-up data. Its handlers get `(ctx, args)`, where `ctx` is the demo backend (`ctx.resources`, `ctx.client()`). A plain `def` handler runs in a worker thread, an `async def` one on the event loop. Shared fake data and `event()` live in `demo/core.py`. Document new commands and events in [deviced-protocol.md](deviced-protocol.md).

To test in parallel checkouts without touching anyone else's daemon, give each one its own runtime directory:

```bash
export IOSSTUDIO_RUNTIME_DIR=$XDG_RUNTIME_DIR/ios-$NAME IOSSTUDIO_DEMO=1   # keep it short: socket paths max out at 107 bytes
python3 helper/run.py iosstudio.device.client daemon.features     # starts this checkout's demo daemon on demand
python3 helper/run.py iosstudio.device.client device.info
python3 helper/run.py iosstudio.device.client daemon.shutdown '{"all": true}'
```

`IOSSTUDIO_RUNTIME_DIR` replaces `$XDG_RUNTIME_DIR` as the base of `ios-studio[-demo]/` (socket, lock, log, demo files) for `bin/iosstudio-deviced`, `device/client.py` and the MCP server. A real daemon started under it answers every request with `busy` while the default real daemon is running, because a second tunnel would break the first one's connection.

## Running from source

Setup and demo mode are in [CONTRIBUTING.md](../CONTRIBUTING.md#run-it-in-demo-mode). The rest:

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

| Area | Folder | Command |
|---|---|---|
| Helper and daemon modules, in process (stdlib only) | `tests/helper/` | `.venv/bin/pytest tests/helper` |
| The helper as a subprocess, fake xtool/swift/swift-format (`slow`) | `tests/cli/` | `.venv/bin/pytest tests/cli` |
| MCP tools (`mcp`; need mcp, pydantic, pillow) | `tests/mcp/` | `.venv/bin/pytest tests/mcp` |
| QML core: Resource, lib/Model.js (`qmltestrunner` from `qt6-declarative`; skipped without it) | `tests/qml/` | `.venv/bin/pytest tests/qml` |
| Real Swift toolchain, network (`slow`, `network`) | `tests/e2e/` | `.venv/bin/pytest tests/e2e -m ""` |
| Everything CI runs | `tests/` | `.venv/bin/pytest --durations=10` |

`pytest` skips `network` tests by default (`addopts` in `pyproject.toml`); `-m ""` runs everything, `-m "not slow"` skips
the subprocess and toolchain tests. `tests/conftest.py` points every `XDG_*` folder, `IOSSTUDIO_RUNTIME_DIR` and `TMPDIR`
into pytest's own numbered temp folder (`/tmp/pytest-of-$USER/pytest-N`, of which pytest keeps the last 3; a killed
run's folder rotates out too) and sets `IOSSTUDIO_DEMO_DELAY=0`. Shared fakes are in `tests/support/` (`import support`), fixtures in
`tests/fixtures/<area>/`, and `pyproject.toml` puts `helper/`, `mcp/` and `tests/` on the path. Patch a shared folder
or output where the code reads it (`mock.patch.object(appdirs, "STATE_DIR", …)`). No test needs xtool, pymobiledevice3,
an iPhone or your home folder; tests that use Swift, `swift-format` or `llvm-symbolizer` skip without them.

## CI

`.github/workflows/ci.yml` runs on every pull request, on pushes to `main` and by hand (Actions › CI › Run workflow). A newer push to the same branch cancels the older run. It has three jobs:

- Python, on 3.11 (the oldest the helper runs on, because of `tomllib`), 3.14 (what Omarchy ships) and 3.15. It compiles every file in `helper/`, `mcp/` and `tests/`, runs `tests/helper` and `tests/cli` with that Python and only pytest installed (the helper must stay stdlib-only), makes the dev venv with `uv sync --group dev` and runs every test but the `network` ones in it with `--durations=10`, then checks that `bin/iosstudio-mcp --prepare` (the same uv path users get) still builds the MCP venv. No test needs xtool, pymobiledevice3, an iPhone or your home folder: they use fake tools, fixtures and temp folders.
- Lint: ruff 0.17.4 with the rules in `pyproject.toml`: pyflakes, pycodestyle's real errors (unused imports and variables, undefined names, syntax) and import order (`I`, fixable with `ruff check --fix`), no style rules. `E731` (a lambda assigned to a name) and `E741` (names like `l`) are off. Then shellcheck at warning level on `install.sh`, the shell scripts in `bin/`, `helper/root/`, `.github/scripts/` and the dSYM fixture's `build.sh`.
`tests/qml` is skipped there (no `qmltestrunner` on the runner); run it locally.
- QML: qmllint on every `.qml` file in an `archlinux:latest` container with `qt6-declarative` and `quickshell` from Arch's repos. `qs.Commons` and `qs.Ui` are symlinks into Omarchy's shell that only `install.sh` makes, so their imports don't resolve there. qmllint's default levels only fail on syntax errors; the thousands of unresolved-import, unqualified and missing-property warnings that follow from that are counted in the log but don't fail the job.

To run the same checks locally:

```bash
python3 -m compileall -q helper mcp tests        # writes __pycache__ folders (gitignored)
python3 -m pytest tests/helper tests/cli           # stdlib + pytest only
uv sync --group dev && .venv/bin/pytest --durations=10
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

The paths below are the defaults: `$XDG_CONFIG_HOME`, `$XDG_STATE_HOME`, `$XDG_CACHE_HOME` and `$XDG_DATA_HOME` replace `~/.config`, `~/.local/state`, `~/.cache` and `~/.local/share` when set, for the app, helper, daemon and MCP server alike.

| What | Path |
|---|---|
| Settings | `~/.config/ios-studio/settings.json` |
| State (build history, renewals, device, setup progress; projects.json has each project's build settings and run presets; editor-auto.json what editor setup wrote into each project) | `~/.local/state/ios-studio/` |
| Latest lint result per project (format and lint) | `~/.local/state/ios-studio/lint/` |
| Full diffs of format dry runs that didn't fit (the newest 10 are kept) | `~/.cache/ios-studio/format/` |
| Swift packages: manifest descriptions, product lists and SwiftPM scratch folders (the newest 100 of each), full `Package.swift` diffs that didn't fit (the newest 10) | `~/.cache/ios-studio/packages/` |
| One lock per project so two package changes never run at once | `~/.local/state/ios-studio/packages/` |
| Build logs (the newest 300 builds' logs are kept) | `~/.local/state/ios-studio/build-logs/` |
| Cache (screenshots, crash files, account lists, each iPhone's app list `apps-<udid>.json`) | `~/.cache/ios-studio/` |
| Large MCP results saved to a file | `~/.cache/ios-studio/mcp/` |
| Kept builds for crash symbolication (binary + dSYM per UUID; last 10 per project, 1 GB in total) | `~/.cache/ios-studio/symbols/` |
| Screenshots, screen recordings, packet captures, metrics CSVs and memgraphs, camera test results (the oldest are deleted past a count and size cap, see `fileio.prune`) | `~/.cache/ios-studio/screens/`, `recordings/`, `captures/`, `metrics/`, `cameratest/` |
| App executables unpacked from `.ipa` files for the debugger (the newest 2 per project) | `~/.cache/ios-studio/debug-builds/` |
| Watch mode (lock, status, events, owners, log; demo mode uses `ios-studio-demo/`) | `$XDG_RUNTIME_DIR/ios-studio/watch*` (`$IOSSTUDIO_RUNTIME_DIR` overrides the base; without `XDG_RUNTIME_DIR`, `/run/user/<uid>` when it's yours, else `/tmp/ios-studio-<uid>`: `device/client.py: runtime_base()`, shared with the device daemon) |
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

The UI and error rules are in [ARCHITECTURE.md](../ARCHITECTURE.md#key-patterns). Also:

- Every async surface has a loading, empty and error state. Errors show the plain `message` and a fix button.
- Destructive actions go through `app.confirm({...})` with Cancel as the default.
- Desktop notifications (`omarchy-notification-send`) only fire while the window isn't focused.
- Settings edits to Hyprland files only touch marker-delimited blocks, back the file up first and roll back if `hyprctl configerrors` reports a problem.

## Reference

- [components.md](components.md): the page contract and every shared component's API
- [deviced-protocol.md](deviced-protocol.md): device daemon commands and events
- [setup-steps.md](setup-steps.md): the Setup steps, what each installs, and the install events
- [features/](features/README.md): one page per feature: how it works, its limits, the agent tools and the code
