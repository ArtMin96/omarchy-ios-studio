# Architecture

iOS Studio is a desktop app for Omarchy that builds, signs, installs and debugs iPhone apps without a Mac.
This page is the map. [CONTRIBUTING.md](CONTRIBUTING.md) has the setup and recipes, and
[docs/development.md](docs/development.md) has the detailed reference.

## Processes

```
 UI: Quickshell / QML ──── argv ────▶ helper CLI ──▶ xtool, swift, swift-format, lldb, pkexec
 (shell.qml, services/,               bin/iosstudio-helper
  pages/, components/)                one process per call: one JSON object,
   │                                  or JSON lines for long jobs
   │ JSON lines over stdio                  ▲
   │ (bin/iosstudio-deviced bridge)         │ argv
   ▼                                        │
 device broker / daemon ◀── Unix socket ── MCP server (stdio, for AI agents)
 one per user, pymobiledevice3             bin/iosstudio-mcp, mcp/iosstudio_mcp/
 $XDG_RUNTIME_DIR/ios-studio/deviced.sock
   │ the only tunnel (USB or Wi-Fi)
   ▼
 iPhone
```

- **UI**: a standalone Quickshell config (`qs -n -p <repo>`) with its own window and Hyprland class
  `ios-studio`. It reuses Omarchy's UI kit (`qs.Commons`, `qs.Ui`) through symlinks that `install.sh` makes.
- **Helper** (`bin/iosstudio-helper <group> <action> --flags`): stdlib-only Python under
  `/usr/bin/python3 -I`. A one-shot command prints one `{"ok": …}` object; a long job (build, install,
  sign-in) streams JSON lines and reads answers to prompts on stdin. Secrets only travel over stdin.
- **Device daemon** (`bin/iosstudio-deviced`): one broker per user in the pymobiledevice3 venv. It owns the
  only connection to the phone (a second tunnel breaks the first), so the window, the MCP server and
  background jobs all share it. It exits two minutes after its last client leaves.
  Wire format: [docs/deviced-protocol.md](docs/deviced-protocol.md).
- **MCP server** (`bin/iosstudio-mcp`): a stdio server for AI coding agents. It calls the same helper and
  is one more client of the same daemon. See [docs/mcp.md](docs/mcp.md).

Demo mode (`IOSSTUDIO_DEMO=1`, or `ios-studio --demo`) swaps the helper's answers and the daemon for made-up
data with no side effects, on a separate socket (`ios-studio-demo/`).

## Layers and folders

| Layer | Folder | What's in it |
|---|---|---|
| Helper core | `helper/iosstudio/core/` | `output` (the JSON protocol), `subproc` (`Runner`, `tool_env`), `appdirs` (XDG paths), `fileio` (locks, atomic writes), `flags`, `jsonc`, `xtool_yml` |
| Helper features | `helper/iosstudio/features/` | one module or package per helper group (`projects/`, `setup/`, `watch/`, `packages/`, `hotreload/`, `editor_setup/`, `agents/`, `format.py`, `account.py`, …). `cli.py` maps a group to its module and imports it lazily. |
| Helper demo | `helper/iosstudio/demo/` | the made-up answers for demo mode |
| In-app runtime | `templates/hot-reload/` | the only iOS Studio code that runs inside the user's app: the Swift package hot reload copies into a project (`full/`: C loader + Swift API; `stub/`: the same API as no-ops). See [docs/features/hot-reload.md](docs/features/hot-reload.md) |
| Device daemon | `helper/iosstudio/device/` | `protocol.py` (wire format), `backend.py` (what the broker calls), `kit.py` (feature registry, `Resources`, `CursorRing`), `broker.py` (socket, clients, idle exit), `client.py` (asyncio client, loadable by path), `real/` (the pymobiledevice3 daemon), `demo/` (the made-up iPhone) |
| Device features | `helper/iosstudio/device/features/<name>/` | `real.py` and `demo.py` side by side, each with `register(reg)`; shared logic in `core.py` (e.g. `apps/index.py` is the `AppIndex`) |
| MCP | `mcp/iosstudio_mcp/` | `core/` (the rules: `registry.py` with `@tool`, `jobs.py`, `deadline.py`, `budget.py`, `params.py`, `errors.py`), `backends/` (device, projects, builds, console, crashes), `tools/` (one module per area) |
| QML plumbing | `core/` | `Cli.qml`, `Stream.qml` (helper calls), `Resource.qml`, `Host.qml`, `CommandCheck.qml`, `ThemeWatcher.qml` |
| QML helpers | `lib/Model.js` | glyphs, pages, shortcuts, formatters |
| QML data | `services/` | one service per area (Device, Project, Log, Account, Setup, Toolchain, Watch) |
| QML screens | `pages/`, `components/<area>/` | one page per sidebar entry; widgets in `common/`, `device/`, `projects/`, `logs/`, `account/`, `settings/` |

Dependencies point down: pages → components → services → `core/` → helper/daemon. A device feature never
imports a daemon module (`real/daemon.py` runs as `__main__`), only `real/core.py` or `demo/core.py`. An MCP tool
module imports only `core/` and `backends/`, never another tool module.

## Key patterns

- **Services own data, pages show it.** `shell.qml` creates one `AppState` (`app`), which holds every service
  (`app.device`, `app.projects`, …). A page gets `required property var app` and never calls the helper for
  data another page also needs.
- **`Resource` + `StateView`.** A `Resource` holds one piece of backend data with its state (`idle`,
  `loading`, `ready`, `error`), de-duplicates loads, honors a `ttl`, and keeps old data on screen while
  reloading. `StateView` shows the loading skeleton, the error with Retry, the empty state or the content.
  See [docs/components.md](docs/components.md#loading-data-resource--stateview).
- **Errors are for people.** Every error has a plain-language `message` and, where possible, a `fix` that
  deep-links the UI (`page`, `step`, `action` or `url`). The app never says "run X in a terminal". Root
  work goes through one `pkexec` per step, never `sudo`.
- **One way to run tools.** Helper code starts external programs through `helper/iosstudio/core/subproc.py`: `tool_env(kind)`
  builds the environment, `Runner` streams output with timeouts, cancel and process-group kill.
- **One registry per daemon.** Each device feature registers its commands; a module that fails to load is
  logged and skipped. Real and demo must register the same command names (a parity test checks it).
  Things held open for clients (mirror, network condition, packet capture) go through `Resources`, so a
  crashed client never leaves the phone changed.
- **MCP: `@tool(kind=…)`, 25 s, jobs.** Every tool is registered with `@tool(mcp, "Title", kind=...)`
  (`read`, `read_remote`, `write`, `destructive`), returns a typed Pydantic model and fits an output budget
  (12 KB by default). Every call answers within 25 s (`mcp/iosstudio_mcp/core/deadline.py`); longer work becomes a job in the
  one `JobManager` and returns a `job_id` that `ios_job_status`, `ios_job_respond` and `ios_job_cancel`
  understand. Writes are separate tools from reads, and destructive ones dry-run until `confirm=true`.

## Performance rules

- **Cheap first, expensive in the background.** Show what's cached or quick at once and fill in the
  slow parts after (device info from cache, app sizes and icons later, disk usage only when asked).
- **Cache and invalidate.** The `AppIndex` keeps each phone's app list in memory and on disk, serves it
  as `stale` while it refreshes in the background, and drops it on install or uninstall. A single-app
  lookup goes through it instead of listing every app.
- **Lazy pages and tabs.** Pages and the iPhone page's tabs load on first use (`Loader`, asynchronous);
  a hidden tab's Resources are inactive and make no requests.
- **Virtualized lists.** Long lists are `ListView`s, not `Repeater`s; icons and events are batched.
- **Bounded everything.** Rings and caches have count and byte caps, subprocesses have timeouts, background
  tasks are tracked and cancelled on shutdown, and files on disk are pruned (`fileio.prune`).

## Where state lives

| What | Where |
|---|---|
| Settings | `~/.config/ios-studio/settings.json` |
| Projects, build history and logs, device state, setup progress | `~/.local/state/ios-studio/` |
| Caches (screenshots, crash files, app index, symbols, MCP overflow files, hot reload baselines and patches) | `~/.cache/ios-studio/` |
| Hot reload's runtime package in a project (git-ignored) | `<project>/.iosstudio/hot-reload/` |
| pymobiledevice3 venv, MCP venv | `~/.local/share/ios-studio/venv`, `…/mcp-venv` |
| Daemon socket, lock and log; watch mode; hot reload sessions | `$XDG_RUNTIME_DIR/ios-studio/` (`ios-studio-demo/` in demo mode; `IOSSTUDIO_RUNTIME_DIR` overrides the base) |
| UI state (current page, toasts, overlays) | in memory, on `AppState` and the services |
| MCP jobs | in memory in the server, newest 25 finished per kind |

The full table is in [docs/development.md](docs/development.md#where-things-live).
