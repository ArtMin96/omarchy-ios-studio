# Development

How iOS Studio is put together, for people who want to change it.

## Overview

iOS Studio is a standalone [Quickshell](https://quickshell.org/) config with a Python backend.

- UI: QML, started with `qs -n -p <repo>`. `shell.qml` declares `//@ pragma AppId ios-studio` (that's also the Hyprland window class), a `ShellRoot` and one `FloatingWindow`. Closing the window quits the app.
- Helper (`bin/iosstudio-helper`): stdlib-only Python under `/usr/bin/python3 -I -B`. One call per action, or a stream of JSON lines for long jobs.
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
  setup.py, account.py, projects.py, appconfig.py   one module per helper group
  agents.py          Settings › AI agents: connect the MCP server to coding agents
  deviced.py         device daemon backend (pymobiledevice3)
  device_broker.py   shared daemon: socket, clients, events, syslog fan-out, idle exit
  device_client.py   asyncio client for the broker (used by the MCP server)
  device_link.py, device_crash.py, device_models.py   device helpers
  demo.py, demo_art.py, deviced_demo.py   demo mode: made-up helper answers, art and iPhone
  root/              scripts that run as root through pkexec
mcp/
  iosstudio_mcp/     the MCP server: server.py, tools/*.py (48 tools), prompts, resources
  pyproject.toml     its dependencies (installed into a private venv on first run)
  evaluation.xml, EVALUATION.md   demo-mode evaluation questions and results
bin/                 ios-studio launcher, iosstudio-helper, iosstudio-deviced, iosstudio-mcp
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

Groups: `setup`, `toolchain` (setup.py), `account` (account.py), `projects`, `build` (projects.py), `app` (appconfig.py), `agents` (agents.py: connect the MCP server to AI tools, see [mcp.md](mcp.md)). `main.py` imports modules lazily, so one broken module doesn't take the others down.

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

`setup install` accepts `--dry-run`. There are no automated tests: check that QML loads without warnings in `qs log` and that the helper commands you touched return valid JSON.

## Where things live

| What | Path |
|---|---|
| Settings | `~/.config/ios-studio/settings.json` |
| State (build history, renewals, device, setup progress) | `~/.local/state/ios-studio/` |
| Cache (screenshots, crash files, account lists) | `~/.cache/ios-studio/` |
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
