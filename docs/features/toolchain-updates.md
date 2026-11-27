# Toolchain updates: tested and latest

iOS Studio installs its toolchain at versions pinned in each release (`helper/iosstudio/features/setup/pins.py`).
Toolchain updates show when the upstream projects publish something newer and install it on request. A failed
update is undone. Covered: omarchy-apple-dev (the iOS build scripts), xtool, pymobiledevice3, ipsw and
rcodesign. The iOS SDK, Xcode and Swift only get notices.

## Channels

The Toolchain page has a Tested / Latest (untested) choice. It's saved as `toolchainChannel` in
`~/.config/ios-studio/settings.json` and defaults to `tested`.

- **Tested** is the versions this iOS Studio release was tested with. It's the same as having no update
  channel at all.
- **Latest** is the newest upstream release of each component that iOS Studio can verify, marked untested.
  When an update installs a latest version and it checks out, the version is saved in Setup's state
  (`channelPins` in `~/.local/state/ios-studio/setup.json`), so Setup's own repairs on Latest reinstall that
  version and not the tested one.

Switching back to Tested lists what would be reinstalled (`updates channel --set tested` → `reinstall`), then
`updates apply --all --channel tested` puts the tested versions back. The channel is saved once that apply
succeeds.

## What's checked, and how each version is verified

| Component | Newest version | Verified by |
|---|---|---|
| `apple-dev` | newest non-prerelease GitHub release; its tag resolved with `git ls-remote` | the commit (`git rev-parse HEAD` after the fetch) |
| `xtool` | the `XTOOL_SHA` in that release's `install-toolchain.sh` | the commit (it has no pin of its own) |
| `ipsw` | `releases/latest` | `checksums.txt` must list this computer's tarball |
| `rcodesign` | newest `apple-codesign/*` release of apple-platform-rs | `SHA256SUMS` |
| `pymobiledevice3` | PyPI JSON | the wheel's sha256 on PyPI; `requires_python` must fit the system Python |

A version that can't be verified is never offered. It shows up as a notice instead (`ipsw-unverified` and the
like). No signatures are checked (cosign isn't stdlib); checksums fetched over HTTPS are what's used.

Notices: `sdk` (the latest build scripts need the iOS SDK rebuilt: that update is blocked until a spare SDK
copy with the new stamp exists, fix → Setup › Xcode), `xcode` (the SDK comes from an older Xcode than the one
for your Swift), `swift` (a newer swift-bin in the AUR; it needs a matching Xcode and SDK, so iOS Studio stays
on its version), `xtool-upstream` (a newer xtool-org release; the AppImages have no checksums, and iOS Studio
uses the xtool the build scripts pin).

## When it checks

At most once a day, only while the app window has focus, plus Check now. The answer is cached in
`~/.cache/ios-studio/toolchain-updates.json` with each URL's ETag. A repeat check sends `If-None-Match`, and
GitHub doesn't count 304 answers against its rate limit. A release's checksum file and a file at a commit are
downloaded once. A check makes four GitHub API calls at most (the unauthenticated limit is 60 an hour) and
takes about 3 s.

- Each request has 8 s and the whole check has 40 s. Sources run in parallel.
- A 403 or 429 from GitHub (`X-RateLimit-Remaining: 0` or `Retry-After`) stores `rate.resetAt`, and nothing
  is checked before then, even with Check now.
- A failed or partial check keeps the previous answers and still sets `nextCheckAt` 3 hours later (or at the
  reset), so a network outage doesn't trigger a retry every hour.

## Safety

Nothing installs without a click. Every update:

1. Takes `~/.local/state/ios-studio/updates.lock`, so only one update runs at a time (the second one gets
   `busy`), and deletes what earlier updates left behind.
2. Writes an in-progress marker, `~/.local/state/ios-studio/updates-inprogress.json`, with the component, the
   target and the backups (each `Backup.to_json()`: `{kind, component, …}`), before anything changes.
3. Keeps the previous version. Binaries are copied to `~/.cache/ios-studio/updates/prev/`, the
   pymobiledevice3 venv is renamed to `venv.prev`, the old xtool build stays, and the scripts' old commit is
   remembered (`setup/channel.py` `backup()`).
4. Installs with Setup's own installers, pinned to the target.
5. Checks the result. The tool must report the target version, xtool must not contain the old Apple sign-in
   code, and no Setup step that was done before the update may be anything else afterwards.
6. On success, deletes the backups and records the pin. On a failure, a cancel or any error, it puts the
   previous version back, newest first: xtool before the build scripts, and the SDK's build tools rebuilt
   at the old commit when the update had replaced them. Then it deletes the backups.

If the process is killed mid-update (SIGKILL, or the app closed while updating), the marker stays.
`updates status` reports it as `"interrupted": {component, since}`, and the next `updates apply` puts the
backups back first (`Backup.from_json()`; an unknown kind is skipped). An apply without `--component` or `--all`
does only that: the app's **Put back** button, and for agents `ios_toolchain_update()` with no component (a dry run
first, then `confirm=true`). Leftover backups also show up in `toolchain disk` as "update-backups".

Cancel (`{"cancel": true}` on stdin or SIGTERM) puts the component in progress back before the apply ends, which
can take a while for the build scripts (an SDK tools rebuild at the old commit). The app waits for that. The MCP
server gives a cancelled update job up to 120 s (`KILL_GRACE` in `mcp/iosstudio_mcp/tools/toolchain.py`) before it
kills the helper, so `ios_job_cancel` can return while the job still says `running`; when the server shuts down it
asks the update to cancel and never kills it. An update that dies anyway shows as `interrupted`.

### pymobiledevice3

pymobiledevice3 is the iPhone connection. Updating it is refused (`iphone-busy`, fix → the iPhone page)
while a debug, screen-mirroring or hot reload session uses the connection. The check reads the daemon's
held resources (`daemon.features`: `mirror`, `record`, `debug:<bundleId>`) and the hot reload session locks.
Otherwise the update asks once, because the connection restarts, unless `--yes` is given. It stops the device
daemon before the venv moves and again once the new venv is in place (a daemon the app restarted in between
would run from the moved venv). The daemon starts again by itself when something needs the iPhone. The new
venv is checked by importing pymobiledevice3 from it.

The installed version is the one in the venv in use, found the way Setup finds it (`setup/facts.py`
`pymd3_venvs()`, dist-info folder names only): ours (`~/.local/share/ios-studio/venv`) first, then
`~/pymobile3-venv`, omarchy-apple-dev's path, symlinks followed. An update always installs ours. A
`~/pymobile3-venv` that is a venv of its own (made by omarchy-apple-dev's scripts) stays as it is: ours takes over
once it's installed, and a rollback that removes ours goes back to it.

## Commands (helper group `updates`)

| Command | Output |
|---|---|
| `updates status` | one object: `{channel, checkedAt, nextCheckAt, due, checking, error, count, components, notices, interrupted}`; cache and files only, no subprocess |
| `updates check [--force]` | JSON lines: `progress`, one `{"event": "component", id, latest}` per component, `done {ok, data: status}` |
| `updates apply (--component ID \| --all) [--channel tested\|latest] [--dry-run] [--yes]` | JSON lines: Setup's `step`/`progress`/`log`/`prompt`, `{"event": "verified", id, source}`, `{"event": "rollback", id, reason}` (each with a `log` line), `done {applied, rolledBack, status}`; a dry run adds `dryRun` and `would: [{id, installed, target}]` |
| `updates channel --set tested\|latest` | one object: `{channel, reinstall: [ids]}` (installs nothing) |

A component in `components` has `{id, name, installed, tested, latest, target, state, untested, notesUrl,
latestAt, applicable, blockedReason, checksum: {source}, restarts}`. apple-dev also has `latestTag`. Commits
are shown as 7 characters. `state` is one of these:

- `current`: installed is the target.
- `update`: installed differs from the target and the target can be installed.
- `newer-than-tested`: on Latest, installed is the target and it's newer than the tested pin. On Tested
  the same install shows as `update`.
- `blocked`: an SDK rebuild is needed, there's no build for this computer, or it's your own checkout.
- `unknown`: not installed (Setup installs it), or a binary changed since the last check.

`--component xtool` updates the build scripts when they're behind, because xtool follows them. Cancel with
`{"cancel": true}` on stdin or SIGTERM. Base URLs can be overridden for tests: `IOSSTUDIO_GITHUB_API`,
`IOSSTUDIO_GITHUB_RAW`, `IOSSTUDIO_PYPI`, `IOSSTUDIO_AUR` and `IOSSTUDIO_GITHUB_DL` (the release download base Setup's
ipsw and rcodesign installers use, default `https://github.com`; the checksum is checked either way).

Agent tools: `ios_toolchain_updates` (read-only; `refresh` runs a check as a job) and `ios_toolchain_update`
(dry run unless `confirm=true`; with no component it puts back an interrupted update), in
`mcp/iosstudio_mcp/tools/toolchain.py`. `ios_status` points an older build scripts checkout at
`ios_toolchain_update(component='apple-dev')` too. See [docs/mcp.md](../mcp.md).

## An older build scripts checkout

When our omarchy-apple-dev checkout is behind the channel's pin (after an iOS Studio release moved the tested pin,
say), three views show the same update. Setup's iOS build scripts row is `broken` with `update: true`, worded "You
have 79bda71; iOS Studio is tested with 9f8352f." (on Latest with an applied pin: "the Latest channel has …
(untested)"; see [setup-steps.md](../setup-steps.md)). `updates status` lists the checkout as an `update` row.
The Toolchain page's Installed item says "79bda71 · update available" and leaves the button to that Updates row,
which updates with a backup; the sidebar counts the update once. Setup's own **Update** runs Setup's install of
the same pin.

## Limits

- Installed versions of ipsw and rcodesign come from the last check's `--version` probe, keyed by the binary's
  mtime and size. A binary Setup replaced since then reads as `unknown` until the next check or apply.
- Only our own omarchy-apple-dev checkout (`~/.local/share/omarchy-apple-dev`) and Setup-built xtool builds
  are updated. A checkout you keep elsewhere (`appleDevDir`) is yours to move.
- pymobiledevice3's wheel checksum comes from PyPI, but pip installs by version (`pymobiledevice3==X`); the
  venv is checked afterwards by importing it.
- Swift, Xcode and the SDK are never updated here: Swift needs root, about 1 GB and a rebuilt SDK.

## Code and tests

- `helper/iosstudio/features/updates/`: `commands.py` (the group), `upstream.py` (fetches, ETags, rate
  limits, per-source verification), `check.py` (the daily check and the cache), `model.py` (status: cache
  plus installed versions, notices, the in-progress marker), `apply.py` (backup, install, verify, roll back,
  pymobiledevice3's daemon handling).
- `helper/iosstudio/features/setup/channel.py`: pins per channel, backups and the updates lock.
- `helper/iosstudio/demo/updates_demo.py`: demo mode, state in `$XDG_STATE_HOME/ios-studio/demo-updates.json`.
  demo.py's `toolchain info` and Setup rows read that file too, so the Installed list and the Setup rows show
  what a demo update installed (`pinned` and the tested/latest wording as on the real channel).
- `tests/support/updates_fakes.py`: a local HTTP server standing in for GitHub (API, raw, release downloads),
  PyPI (JSON and a simple index pip installs from) and the AUR, with ETag/304/403/429; a file:// git remote;
  fake release binaries and wheels; a fake device daemon.
- `tests/helper/test_updates_check.py` covers the check, the cache, the rules for when a check runs, ETags,
  rate limits, unverifiable versions, notices and status. `tests/cli/test_updates_apply_cli.py` covers a
  real apply, a failed check followed by a rollback, cancel, an interrupted update put back, a dry run,
  switching back to Tested, the busy lock, pymobiledevice3 sessions, and apple-dev with xtool in process; it runs
  the helper as the app does (`/usr/bin/python3 -I`), pointed at the fake with the base-URL variables above.
- `tests/helper/test_setup_update_integration.py` covers backups through JSON and recovery from a marker, the
  download base and the lazy `GROUPS`. `tests/helper/test_updates_shapes.py` runs one story through the real group
  (on the fakes) and the demo and compares every key and type, the stream events and the states; its `DEMO_GAPS`
  is empty, so any difference fails. `tests/helper/test_toolchain_update_coherence.py` checks that an older
  checkout reads as one update in Setup, `toolchain info` and `updates status` (demo variant
  `IOSSTUDIO_DEMO_APPLE_DEV=outdated` included).
- Demo variants: `IOSSTUDIO_DEMO_UPDATES=rollback` (every update fails its check and rolls back),
  `IOSSTUDIO_DEMO_UPDATES=interrupted` (starts with an interrupted ipsw update until an apply puts it back), and
  `IOSSTUDIO_DEMO_APPLE_DEV=outdated` (the older checkout above, until a demo update installs the scripts).
