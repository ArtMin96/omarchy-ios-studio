# Contributing

Thanks for helping. Read [ARCHITECTURE.md](ARCHITECTURE.md) first (one page). The detailed reference is
[docs/development.md](docs/development.md).

## Set up

You need Omarchy (for Quickshell and its UI kit), `git`, `uv` and Python 3.11+.

```bash
git clone https://github.com/ArtMin96/omarchy-ios-studio && cd omarchy-ios-studio
uv sync --group dev        # .venv/ with pytest, ruff and the MCP server's dependencies
```

The UI needs Omarchy's UI kit linked into the checkout. `./install.sh` does that and also installs the
launcher, which then points at this checkout. In a second checkout (a worktree), link only the kit:

```bash
ln -sfn "$OMARCHY_PATH/shell/Commons" Commons && ln -sfn "$OMARCHY_PATH/shell/Ui" Ui   # both gitignored
```

## Run it in demo mode

Demo mode uses a made-up Apple ID, iPhone and projects and never touches your real ones. When you work next
to a real install, also give the checkout its own folders, so nothing reaches your real settings, state or
device daemon:

```bash
t=$(mktemp -d)
export XDG_STATE_HOME=$t/state XDG_CONFIG_HOME=$t/config XDG_CACHE_HOME=$t/cache XDG_DATA_HOME=$t/data
export IOSSTUDIO_RUNTIME_DIR=$t/run    # the daemon socket and watch mode (keep XDG_RUNTIME_DIR: Wayland lives there)
export IOSSTUDIO_DEMO=1
```

| What | Command |
|---|---|
| The app | `IOSSTUDIO_DEMO=ready qs -n -p .` (or `bin/ios-studio --demo`, which refuses while a real iOS Studio window is open); `qs log -p .` reads its log |
| The helper | `bin/iosstudio-helper setup check`, `bin/iosstudio-helper projects list` |
| The device daemon | `python3 helper/run.py iosstudio.device.client device.info` (starts the demo daemon on demand); `… daemon.features`; `… daemon.shutdown '{"all": true}'` |
| The MCP server | `bin/iosstudio-mcp` (stdio; builds its venv under `$XDG_DATA_HOME` on first run). To call tools without an agent, see [docs/mcp.md](docs/mcp.md#demo-mode) (MCP Inspector) |

## Tests and checks

| Area | Folder | Command |
|---|---|---|
| Helper and daemon modules, in process (stdlib only) | `tests/helper/` | `.venv/bin/pytest tests/helper` |
| The helper as a subprocess, with fake xtool/swift (`slow`) | `tests/cli/` | `.venv/bin/pytest tests/cli` |
| MCP tools (`mcp`) | `tests/mcp/` | `.venv/bin/pytest tests/mcp` |
| QML core (`Resource`, `lib/Model.js`); needs `qmltestrunner` from `qt6-declarative`, skipped without it | `tests/qml/` | `.venv/bin/pytest tests/qml` |
| Real Swift toolchain and network (`slow`, `network`) | `tests/e2e/` | `.venv/bin/pytest tests/e2e -m ""` |
| Everything CI runs | `tests/` | `.venv/bin/pytest` (skips `network`; `-m "not slow"` for a quick run) |

```bash
.venv/bin/ruff check                                  # pyflakes, real pycodestyle errors, import order
/usr/lib/qt6/bin/qmllint path/to/File.qml             # fails only on syntax errors; read the warnings for your file
```

Tests never need xtool, pymobiledevice3, an iPhone or your home folder: `tests/conftest.py` isolates every
XDG folder, and `tests/support/` has the fakes (`isolated_env()`, `helper_json()`, fake tools, `FakeMCP`,
a pymobiledevice3 stub). Add tests in a new file named for your change.

## Recipes

### Add a helper command

1. Write `def my_action(flags: dict) -> int` in the feature's module under `helper/iosstudio/features/`.
   Finish with `return output.ok(data)`, or `raise output.HelperError(code, message, fix)` with a
   plain-language message. A long job streams `output.step()/progress()/log()` and ends with `output.done()`.
2. Add it to the module's `GROUPS = {"group": {"action": my_action}}`. A new group also needs one line in
   `MODULES` in `helper/iosstudio/cli.py`.
3. Run external programs through `helper/iosstudio/core/subproc.py` (`run_cmd(…, timeout=…)` or `Runner`, environment from
   `tool_env(kind)`), and read paths from `helper/iosstudio/core/appdirs.py`.
4. Give demo mode an answer in `helper/iosstudio/demo/demo.py` (`run()`).
5. Test it in `tests/helper/` (in process) and `tests/cli/` (`helper_json("group", "action", …)`).

### Add a device feature

1. Make `helper/iosstudio/device/features/<name>/` with `__init__.py`, `real.py` and `demo.py`. Each has
   `register(reg)` and registers the same command names with `@reg.command("name.verb")`. Shared, stdlib-only
   logic goes in `core.py` next to them.
2. `real.py` imports from `iosstudio.device.real.core` (`emit`, `err`), reaches the phone only through the
   session's tunnel (`s = d.need_session(a)`), and holds anything that changes the phone through
   `d.resources.acquire(…)`. `demo.py` imports from `iosstudio.device.demo.core`.
3. The parity test `tests/helper/test_device_protocol.py::Parity` fails until real and demo register the
   same commands. Add tests for the logic (fake pymobiledevice3 at the service boundary, as in
   `tests/helper/test_app_index.py`).
4. Document every command and event in [docs/deviced-protocol.md](docs/deviced-protocol.md), then try it:
   `python3 helper/run.py iosstudio.device.client name.verb '{"arg": 1}'`.

The session API and `Resources` are in [docs/development.md](docs/development.md#device-daemon-features).

### Add an MCP tool

Put it in the area's module in `mcp/iosstudio_mcp/tools/` (a new module goes in `MODULES` in
`tools/__init__.py`), then check each point ([docs/mcp.md](docs/mcp.md#for-tool-authors) explains them):

- [ ] Registered with `@tool(mcp, "Title", kind="read" | "read_remote" | "write" | "destructive")`, not `@mcp.tool()`.
- [ ] Reads and writes are separate tools; a destructive one takes `confirm` and dry-runs without it.
- [ ] Returns a Pydantic model from `models.py`; lists are paged, output fits the budget (`core/budget.py`).
- [ ] Shared parameters come from `core/params.py` (`ProjectPath`, `BundleId`, `JobId`, …); background work
      uses `job_id`, never a `<feature>_id`.
- [ ] Answers within 25 s: step timeouts go through `deadline.clamp()`; longer work is a job
      (`state.jobs.run(...)`, then `follow_job()` and `still_running()`).
- [ ] No blocking I/O in the handler (`asyncio.to_thread`, bounded reads); phone calls go through `dcall()`.
- [ ] Imports only `core/` and `backends/`, never another tool module.
- [ ] Description at most 400 characters; details go to `mcp/iosstudio_mcp/guide.md` (`iosstudio://guide`).
- [ ] Works in demo mode; a test in `tests/mcp/`; the tool table in `docs/mcp.md` updated
      (`tests/mcp/test_mcp_slim.py` checks the `tools/list` size).

### Add a QML view that loads data

Put it in `components/<area>/`, import `"../common"` and `"../../core"`, and let a `Resource` hold the data:

```qml
import "../common"
import "../../core"
import "../../lib/Model.js" as Model

ColumnLayout {
  id: root
  required property var app
  property bool active: false                   // true while the tab is on screen

  Resource {
    id: items
    active: root.active && root.app.device.connected
    autoLoad: true
    initial: []
    fetch: root.app.device.fetcher("my.list")    // or root.app.cli.fetcher(["group", "action"])
  }
  StateView {
    Layout.fillWidth: true
    app: root.app; resource: items
    errorTitle: "Couldn't read the list"
    emptyGlyph: Model.Glyph.info; emptyTitle: "Nothing here yet"
    ListView { model: items.data; … }           // virtualized for long lists
  }
}
```

Data that several pages need belongs in a service in `services/`, as a `Resource` property. Use sizes from
`Style.*` and colours from `Commons.Color.*` / `Theme`, never hard-coded pixels or colours; destructive
actions go through `app.confirm(...)`. Check the view in demo mode with a zoomed screenshot (icon and text
alignment, spacing). APIs: [docs/components.md](docs/components.md).

### Change the hot reload runtime

`templates/hot-reload/` is copied into users' projects and runs inside their apps, so treat it like a release:

1. Bump `templates/hot-reload/version.txt`. Projects get the new copy the next time hot reload is turned on or a
   session starts; without the bump they keep the old one.
2. Keep `full/` and `stub/` declaring the same public API; `tests/cli/test_hot_reload_runtime.py` checks that,
   and with the iOS SDK installed it builds both for iOS and checks the loader's symbol rules (`hr_rules.h`)
   against a small app and patch built for `arm64-apple-ios`.
3. The loader's file protocol (`runtime.json`, `patch-<n>.dylib`, `ack-<n>.json`) is shared with
   `helper/iosstudio/device/features/hotreload/` and the session: change them together and update
   `templates/hot-reload/README.md`.
4. Try it on a real iPhone: neither demo mode nor the tests run the runtime inside an app.

## Pull requests

- Branch from `main` and target `main`. Don't stack a feature PR on another feature branch.
- Update `README.md` and every doc your change affects in the same PR (feature docs live in
  [docs/features/](docs/features/README.md)).
- Test on a real iPhone as well as in demo mode. Use the existing test app `~/Projects/CrashLab` for that;
  don't create new test apps.
- When a convention changes, update `CLAUDE.md` and `.claude/rules/` in the same PR so AI agents follow it too.
- In git worktrees, never use `git stash` (the stash is shared by every worktree); make a temporary commit instead.
- Run `.venv/bin/pytest` and `.venv/bin/ruff check` before you push. CI also runs shellcheck and qmllint
  (see [docs/development.md](docs/development.md#ci)).
