# App data: UserDefaults, SwiftData stores and resets

When an app behaves oddly, the first question is often "what has it saved?". Without Xcode there's no easy
way to see an app's UserDefaults or its SwiftData / Core Data store, or to put it back to its first launch.
iOS Studio reads both from the app's container on the iPhone. It shows the defaults as JSON and lets you
change one key, shows the stores' tables with their rows, and resets the app by wiping its data or reinstalling
your last build.

This works only for your own development builds, the apps iOS Studio built and installed. iOS hands their whole
container to a computer they trust (house_arrest `VendContainer`, the same access iPhone › Files uses). App
Store apps keep their data private.

## In the app

iPhone › Apps, click one of your apps. The switch under its header has three data parts (and Files): Defaults (the keys, their types and
values, with add, edit and delete), Stores (each store's tables and row counts, then a table's rows 50 at a
time) and Reset (Wipe data or Clean reinstall, each shown as a dry run first). The steps are in
[guide.md](../guide.md#look-at-and-reset-an-apps-data), and the components are in
[components.md](../components.md).

## Agent tools

| Tool | What it does |
|---|---|
| `ios_app_defaults(bundle_id, action='read', key?, max_bytes?)` | The defaults as JSON with each key's type. Keys that don't fit the output budget (about 12 KB) are named in `left_out`, and the whole JSON is written to `full_output_path`. `key=` returns one value in full. |
| `ios_app_defaults(bundle_id, action='set', key, value, type?, confirm=false, stop_app=false, refresh=true)` | Sets one key. Without `confirm=true` it's a dry run that returns the old and the new value. With it, it's a job of about 20 s (`state: "running"` and a `job_id`, then call again with `job_id`). The result says how the change was applied (`method`), whether the file holds it (`verified`), and with `method: "file"` gives `launch_args`. |
| `ios_app_defaults(bundle_id, action='delete', key, confirm=false, stop_app=false, refresh=true)` | Deletes one key, the same way. |
| `ios_app_defaults(bundle_id, action='refresh')` | After several changes made with `refresh=false`: installs the last build over the app once, so it sees them all. |
| `ios_app_store(bundle_id)` | Each store with its kind, size, tables, entity names, row counts and columns. |
| `ios_app_store(bundle_id, table, store?, offset=0, limit=20)` | One page of a table's rows (a table or entity name works), `next_offset` for the next page. |
| `ios_app_reset(bundle_id, mode='wipe'\|'reinstall', confirm=false, stop_app=false, project_path?)` | A dry run listing the 20 biggest items, the counts and the bytes. With `confirm=true` it runs as a job: `state: "running"` and a `job_id`, then call it again with `job_id`. |

Typical loop: `ios_app_defaults` → `ios_app_defaults(action='set', …)` (dry run) → ask the user →
`confirm=true, stop_app=true` → `ios_app_run_and_capture(bundle_id)` to see the app pick it up.

The daemon commands behind them (`appdata.defaults`, `appdata.defaults.set`, `appdata.defaults.delete`,
`appdata.defaults.refresh`, `appdata.stores`, `appdata.rows`, `appdata.reset`), with every argument, reply field and error code, are in
[deviced-protocol.md](../deviced-protocol.md#app-data-appdata-used-by-iphone--apps--an-app--data-and-the-mcp-app-data-tools).
The contract is the one the issue planned. Arguments added on top of it, all optional:

- `full` on `appdata.defaults`, for values that weren't shortened for display
- `stopApp` on the writes, to stop the app first
- `dryRun` on `defaults.set` and `defaults.delete`, for the agent's preview
- `refresh` (default true) on `defaults.set`, `defaults.delete` and wipes, and the `appdata.defaults.refresh`
  command (see [How a change reaches the app](#how-a-change-reaches-the-app))
- `artifact` (and `project` on `reset`), which pick the build to install

Replies also gained `types`, `format`, `backup`, `stopped`, `failed`, `files`, `category`, `cached` and the
column flags `meta` and `date`. Changes gained `method`, `verified`, `refreshed`, `artifact`, `attempts`,
`waited`, `needsRelaunch`, `refreshError`, `warning` and `launchArgs`; reads gained `waited` and `reverted`.

## UserDefaults

`UserDefaults.standard` lives in `Library/Preferences/<bundle id>.plist`, a binary property list. The file is
named after the installed bundle id. Free teams install apps as `XTL-<TEAM>.<bundle id>`, so that name is
tried first and the plain id second.

**Reading.** The file is read whole (up to 8 MB) and turned into JSON with `plistlib`. Plist types that JSON
doesn't have are tagged objects that turn back into the same plist values:

```json
{"launchCount": 42, "volume": 0.5, "hasSeenOnboarding": true, "recent": ["read", "run"],
 "lastOpened": {"$type": "date", "iso": "2026-11-06T08:30:00Z"},
 "avatar": {"$type": "data", "base64": "iVBORw0KGgo…", "size": 2048}}
```

`types` names each key's plist type (`string`, `int`, `float`, `bool`, `date`, `data`, `array`, `dict`, `uid`).
In the app's view, long strings, big Data and big arrays come back shortened, marked `truncated: true`, and the
full value is one call away. A shortened value can't be saved back, so an edit never cuts a value by accident.

**Writing.** Setting or deleting a key reads the file again, changes that one key and writes the file back in its
own format. Every other key stays exactly as it was, Data and dates included. A new key's type comes from its
JSON value (text stays text). Pass a `type` to pick it: `int` for "42", `bool` for "yes", `date` for an ISO
8601 text, `data` for base64, or `json` for an array or dictionary typed into a text field. The new file is
written next to the old one and renamed over it, so a dropped connection never leaves half a file. The old file
is saved first in `~/.cache/ios-studio/appdata/<bundle id>/backups/` (the newest 10 per app).

**Never while the app runs.** A running app keeps its defaults in memory and writes them back. So every write
first checks that the app is stopped, and fails with `app-running` otherwise. Stop app in the window, or
`stop_app=true` for an agent, kills the app and waits for it to exit.

### How a change reaches the app

The app doesn't read its settings file directly. `cfprefsd`, the iPhone's settings service, serves them, and it
keeps the app's settings in memory after the app exits. These were checked on an iPhone with CrashLab, an app
that prints a saved `note` and counts its launches:

| What was tried | Did the next launch see the new value? |
|---|---|
| Change the file after the app stopped | No. cfprefsd handed out its cached copy and wrote it back over the file at the app's next save. |
| Wait 60 s after the kill, then change the file | No. |
| Rewrite the file in place (same inode), or delete it and write a new one | No. |
| Change the file, then install the app's signed build over it (an upgrade) | Yes. The container stays, and the launch count carried on. |
| Launch with `-note value` arguments (NSArgumentDomain) | Yes, for that launch only. |

So a change runs like this (about 10 to 20 s over USB, longer over Wi-Fi):

1. Stop the app if asked to (`stopApp`).
2. Wait until 10 s have passed since the app was stopped. cfprefsd saves a killed app's last settings 2 to 7 s
   after the kill, not at the kill. A read or write before that would miss them, or have them land on top of
   the change. `app.kill` notes the time for every client, so this also covers the window's Stop button.
3. Wait until the file has been unchanged for 1 s, then change one key and write the file.
4. Install the newest signed build of the bundle id over the app (`refresh`, on by default). This is the same
   build a clean reinstall uses, from the build history, auto-renew or `xtool/<product>.ipa`. iOS keeps the
   container and drops cfprefsd's copy. The result has `method: "reinstall"` and the `artifact`.
5. Read the file back (`verified`). If cfprefsd's late save overwrote the change, apply it again, up to 3
   writes (`attempts`).

Without a build, or with `refresh: false`, only the file changes (`method: "file"`). Then `warning` says the
app may still get the old value. For a string, number or bool, `launchArgs` (`["-note", "edited"]`) apply it
for one launch through `app.launch` / `ios_app_launch(args=…)`. Several changes can be made with
`refresh: false` and applied together with one `appdata.defaults.refresh`. Every change is remembered. If a
later read finds a key back at its old value, the read lists it once in `reverted` (`{key, expected,
actual}`). A key the app itself changed to something new isn't flagged.

A wipe has the same problem: the deleted settings could come back from the cache. So a wipe also installs the
build over the app afterwards (`refreshed`). In one test a wipe without it started clean anyway, but that
wasn't reliable enough to depend on.

Reads right after a kill also wait out the 10 s, and say so in `waited`. Every command opens its own container
connection. A kept one went stale on the iPhone after a kill or an install and read as "no file" (the
transient `exists: false`, empty stores and `not-found` folders seen before).

## Stores

SwiftData saves to `Library/Application Support/default.store` unless the app names its own store. Core Data
uses `<Model>.sqlite`. Both are SQLite files in WAL mode, so the newest writes are often in the `-wal` file
next to the store. iOS Studio looks for files ending in `.store`, `.sqlite`, `.sqlite3` or `.db` under
`Library/Application Support` and `Documents`. It copies each one together with its `-wal` and `-shm` into
`~/.cache/ios-studio/appdata/<bundle id>/`, so the copy is one consistent snapshot. Then it folds the WAL into
the copy (a checkpoint, on the copy only) and opens it with `mode=ro&immutable=1`. Nothing is ever written to
the phone's files.

- **Kinds.** A store with Core Data's `Z_PRIMARYKEY` and `Z_METADATA` tables is `swiftdata` when the file
  ends in `.store` and `coredata` otherwise. Anything else is `sqlite`.
- **Entities.** Core Data names an entity's table `Z<ENTITY>`, with `Z_PK`, `Z_ENT` and `Z_OPT` columns. The
  friendly name comes from `Z_PRIMARYKEY.Z_NAME` (`ZCOMPLETION` → `Completion`). Core Data's own tables
  (`Z_PRIMARYKEY`, `Z_METADATA`, `Z_MODELCACHE`, many-to-many `Z_1TAGS` tables, and the persistent history
  tables `ATRANSACTION` and `ACHANGE`) are marked `meta`, and so are the `Z_PK`, `Z_ENT` and `Z_OPT` columns.
  `sqlite_*` tables are left out.
- **Rows.** Rows come in pages of up to 200, in primary key order. Cells are bounded:
  - text over 256 characters becomes `{"$type": "text", "preview", "length"}`
  - blobs become `{"$type": "blob", "size", "hex"}`, with the first 16 bytes
  - Core Data `TIMESTAMP` columns hold seconds since 2001 and become `{"$type": "date", "iso", "raw"}`
- **Cache.** A store is copied again only when one of its files changed size or modification time on the
  phone. Paging through a table reads the copy. A failed copy leaves nothing behind. Copies of stores the app no
  longer has are deleted, and only the 5 most recently used apps keep a cache.
- **Limits.** A store over 1 GB isn't copied (`too-big`). A file that isn't SQLite is listed with an error, and
  the other stores still show.

Reading a store while the app runs is allowed, but the copy may miss a write that's in progress. Stop the app
for an exact snapshot.

## Reset

Both kinds of reset are dry runs until confirmed. The dry run lists exactly what would be removed: the 200
biggest items in the daemon's reply (the agent tool shows 20), with how many files and bytes in total.

- **Wipe data** deletes everything inside `Documents`, `Library` and `tmp`: preferences, caches, Application
  Support with its stores, saved state and snapshots. It keeps the folders themselves (`Documents`, `Library`,
  `Library/Preferences`, `Library/Caches`, `Library/Application Support`, `tmp`), because iOS expects them to
  exist. Entries the iPhone doesn't allow deleting are skipped and listed. Then the app's last build is installed
  over it, so cfprefsd forgets the old settings too (see above). The app stays installed, with its signing and
  its 7-day profile.
- **Clean reinstall** uninstalls the app, and with it iOS deletes the whole container. Then it installs the
  newest signed build of that bundle id from this computer. That build comes from the build history, then the
  auto-renew records, then the projects' own `xtool/<product>.ipa`. An agent can name the project with
  `project_path`. With no build it fails with `no-build`.

Neither touches the app's keychain items, which iOS keeps even across an uninstall.

Both need the app stopped, like a defaults write (`app-running` unless `stop_app=true`).

## Demo mode

`IOSSTUDIO_DEMO=1` gives HabitTracker (stopped) and WeatherGlance (running) made-up saved data the first time
the demo daemon touches them. Each gets a binary preferences file with strings, numbers, a bool, dates, arrays,
a nested dictionary and Data blobs. HabitTracker also gets a SwiftData `default.store` with Habit (12 rows),
Completion (640 rows, some with long notes and photos) and Tag, Core Data's metadata and history tables, and an
old `Documents/habits.sqlite`. WeatherGlance gets a Core Data `WeatherGlance.sqlite`. Edits and resets change
those files (Files shows them too) until the demo daemon restarts. WeatherGlance being in front shows the
`app-running` refusal. A change answers the way the phone does: the made-up build is "installed" over the app
with install progress (`method: "reinstall"`), and with `refresh: false` it's `method: "file"`, with the
warning and `launchArgs`. The demo has no cfprefsd, so nothing is ever waited for or reverted.

## Tests

- `tests/helper/test_app_data.py` covers:
  - plist ↔ JSON round trips: binary and XML, Data, dates, UIDs, nested values, infinities, a `$type` key
  - shortened values and the reply limit
  - type inference and explicit types with their errors
  - set and delete keeping the format and the other keys
  - summaries, entity names, meta tables, the WAL checkpoint, read-only opening, paging and bounded cells, on
    the fixtures in `tests/fixtures/app-data/` (written by its `make_fixtures.py`)
  - the cache, failed pulls and pruning, the backup cap, reset plans, finding the last build
  - the demo commands
  - with pymobiledevice3 installed, the real daemon module against a fake container. A hook plays cfprefsd
    there: its late save over the first write, a copy that always wins, and a value put back
    (`reverted`). These runs also cover the refresh install, the 10 s wait after a kill (also after another
    client's kill), and no build or a failed install keeping the file change with `launchArgs`.
- `tests/mcp/test_mcp_app_data.py` runs the three tools against the demo modules: the budget and
  `full_output_path`, dry runs, `stop_app`, a change as a job (`job_id`), `refresh=false` with `launch_args`
  then `action='refresh'`, paging by entity name, and the reset job.

## Checked on a real iPhone

Checked with CrashLab, a development build installed as `XTL-<TEAM>.dev.iosstudio.crashlab`:

- reading defaults (a binary plist named after the `XTL-` id)
- the stores (`ZSAMPLE` 300 rows, `ZTAG`, metadata tables marked) and paging their rows
- both reset dry runs; the reinstall one finds `xtool/CrashLab.ipa`
- a change seen on the next launch, through the daemon and through `ios_app_defaults` over MCP, with the app
  running beforehand (`stop_app`)
- a delete falling back to the app's own default
- the app's own launch counter surviving a change
- a wipe followed by `launch 1`
- the 2 to 7 s cfprefsd delay after a kill
- the stale container connection after a kill or an install

Still to check:

- AFC `rename` over an existing file on other iOS versions (there's a fallback that deletes the old file
  first)
- a refresh over Wi-Fi, and with an expired 7-day profile (it should report `refreshError` and keep the file
  change)
- which `Library` entries other apps' phones deny during a wipe
