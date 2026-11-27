# Setup steps

What the Setup page shows, in order. `bin/iosstudio-helper setup check` returns these as `steps[]`;
`setup install --step <id>` installs or repairs one; `setup install --all` runs every missing
auto-installable step (one password prompt for all root work). Every install supports `--dry-run`.

States: `done`, `missing`, `broken` (installed but not working, or older than what iOS Studio now pins:
shows **Repair**), `optional`, `unknown` (only the iPhone can tell). `essential: true` steps decide `summary.ready`.
A `broken` row with `update: true` (only `apple-dev` today) works as it is and has an update to the channel's pin
(see [Pins](#pins-and-the-update-channel)): every install path repairs it like any `broken` step, but people see an
update. Setup shows an accent download glyph with a neutral hint and an **Update** button, and when updates are all
that's left the top card says "An update is ready" with an **Update** button. On the Toolchain page the item is
`status: "update"`: its version and "update available". Toolchain › Updates usually lists the same checkout as an
update row (`updates status` compares it with the same pin on Tested); then the item has no button of its own, the
row's **Update** runs the update with its backup, and the sidebar counts it once (`Model.toolchainUpdateIds`).
Without such a row the item's **Update** runs Setup's install. `summary.updates` lists these rows and
`summary.broken` doesn't; with nothing else left the summary is "1 update ready" at level `info`. MCP:
`ios_toolchain_check` reports the step as state `update`, and `ios_status` names it with
`ios_toolchain_update(component=...)` (the tool `ios_toolchain_updates` points at for the same update) without
counting it against `toolchain_ready`.

| id | Group | Title | What the user reads | Root | Size / time | How it's done |
|---|---|---|---|---|---|---|
| `platform` | System | This computer | Needs an Arch-based Linux (like Omarchy) on a 64-bit Intel/AMD or ARM computer. | – | – | check only |
| `packages` | System | System packages | Tools for talking to an iPhone over USB, building software, packaging apps and saving screen recordings (ffmpeg). | yes | 50–150 MB, < 1 min | `pacman -S --needed` |
| `usbmuxd` | System | USB connection service | Carries the cable connection to the iPhone; starts by itself when you plug it in. | yes | seconds | optional restart drop-in + udev trigger |
| `swift` | Toolchain | Swift compiler | The Swift toolchain that compiles your apps. | yes | 1 GB download, 3.5 GB, 5–10 min | AUR `swift-bin` pinned to `f78c5ec` (6.4.2-2): tarball downloaded by us with progress, `makepkg` as the user (runuser), `pacman -U`; also holds Swift and fixes xcspec in the same prompt |
| `swift-hold` | Toolchain | Keep Swift on this version | Stops system updates from changing Swift behind your back. | yes | seconds | `IgnorePkg = swift-bin` + Omarchy `pre-refresh-pacman` hook |
| `xcspec` | Toolchain | Swift build settings fix | Lets localized text files build on Linux. | yes | seconds | `helper/root/xcspec.py apply` |
| `apple-dev` | Toolchain | iOS build scripts | omarchy-apple-dev, which turns Xcode's SDK into something Linux can build with. | – | few MB | clone `~/.local/share/omarchy-apple-dev` @ the pin (tested: `9f8352fc`, v0.4.8; see [Pins](#pins-and-the-update-channel)). Our checkout at another commit is `broken` with `update: true` and `action.label` "Update" ("You have 79bda71; iOS Studio is tested with 9f8352f. …", on Latest with an applied pin "…; the Latest channel has 1a2b3c4 (untested). …"): fetch + checkout of the pin, then the follow-ups below. A checkout elsewhere (the `appleDevDir` setting) is accepted as is and never moved |
| `xtool` | Toolchain | xtool | Builds, signs and installs iPhone apps from Linux; compiled on your computer. | – | 1.8 GB build, 410 MB installed, ~14 min | `XTOOL_REPO`/`XTOOL_SHA` from the checkout's `install-toolchain.sh` (v0.4.8: fork `7cc56c3`, which already has the Apple sign-in fix #406 and the release-build fix #432), built as pinned into `~/.local/lib/omarchy-apple-dev/xtool-<sha>` (source `~/.cache/omarchy-apple-dev/xtool-<sha>`, the folders upstream uses); the binary is checked for the old user agent. A Setup build from another commit (also the older `xtool-<sha7>-gsa406` layout) is `broken` once the pin moves; after the new build starts, every other `xtool-*` build and source tree is deleted. Heartbeat progress |
| `rcodesign` | Toolchain | rcodesign | Signs App Store / TestFlight uploads, and each hot reload patch. | – | 10 MB | GitHub release, sha256-pinned (tested: 0.29.0); download base `IOSSTUDIO_GITHUB_DL` (tests) |
| `pymobiledevice3` | Toolchain | iPhone connection tools | Talks to your iPhone over USB or Wi-Fi. | – | 335 MB, 1–2 min | venv `~/.local/share/ios-studio/venv`, `pymobiledevice3==<pin>` (tested: 11.49.0); links `~/pymobile3-venv` to it if absent |
| `ipsw` | Toolchain | ipsw (debugger helper, optional) | Extracts system libraries from your iPhone so the debugger shows readable stack traces. iPhone › Debug needs it to unpack the debug symbols download. | – | 40 MB | GitHub release, sha256-pinned (tested: 3.1.743, the same pin as `install-toolchain.sh`); download base `IOSSTUDIO_GITHUB_DL` (tests) |
| `debugger` | Toolchain | Debugger support (optional) | lldb needs python312, compiled from source. Without it lldb doesn't start, and iPhone › Debug sends you here (`debug check` → `needs-python`). | yes | about 100 MB, 10–20 min | AUR build |
| `xcode` | Apple | Xcode 27.1 download | The iOS SDK only comes inside Apple's Xcode download. Download it once in your browser; iOS Studio finds it in Downloads. | – | ~2 GB download | `setup xip --open` / `--watch` / `--use PATH` |
| `sdk` | Apple | iOS SDK | Apple's iOS frameworks prepared for Linux. | – | 7 GB (15 GB free while building), 20–30 min | `xtool sdk build` → cache `~/.cache/xtool/darwin-iPhoneOS<v>.xtoolsdk`, then `install-toolchain.sh --mode full --repair` (registers it, darwin tools, macro server). With a current spare copy (same stamp) only the `--repair` runs: 2–5 min, no Xcode. Also `broken` when a darwin-tools input in the checkout is newer than the SDK's actool (see below) |
| `apple-id` | Apple | Apple Account | Sign in so apps can be signed for your iPhone. | – | a minute | `{kind:"page", page:"account", action:"signin"}` |
| `iphone` | iPhone | Pair your iPhone | Connect, unlock, tap Trust. | – | a minute | `{kind:"page", page:"device", action:"pair"}` |
| `developer-mode` | iPhone | Developer Mode | Settings › Privacy & Security › Developer Mode, then restart. | – | phone restarts | `{kind:"page", page:"device", action:"developer-mode"}`; state from `~/.local/state/ios-studio/device.json` `developerMode` |
| `uxplay` | iPhone | Screen mirroring (UxPlay, optional) | Your iPhone's screen live on this computer over Wi-Fi (AirPlay). | yes | 20 MB, 2–5 min | AUR `uxplay` build, `gst-plugins-bad` and `gst-plugin-va` (hardware video decode), enable avahi, open ufw ports for `uxplay -p` (TCP 7000,7001,7100; UDP 6000,6001,7011, 5353) |
| `wireshark` | iPhone | Wireshark (packet viewer, optional) | Opens the network recordings from the iPhone page's Network tab. | yes | about 100 MB, about a minute | `pacman -S --needed wireshark-qt` |
| `app-integration` | System | App launcher entry | Adds iOS Studio to the app launcher. | – | seconds | runs `install.sh` |

## Updating to a new omarchy-apple-dev pin

`setup install --step apple-dev` (the row's one **Update**, also part of `--all`) moves our checkout to the pin
and then runs, in the same job, each of `xtool` and `sdk` that the move turned from `done` to `broken` and that
can repair itself (`installed` lists what ran). Those follow-ups (`installers.follow_ups`) also take the steps they
depend on (the rows' `depends`) that aren't done, except root steps, and run in Setup's step order: xtool is
rebuilt before the SDK's darwin-tools repair that needs it, also when xtool was already outdated before (the scripts
downloaded fresh next to an older xtool build). A follow-up whose dependency still isn't done is logged, not run.
`--all` runs the same chain and skips what it already ran. So it redoes only what changed:

- **xtool** when `XTOOL_SHA` moved (the folder name tells which commit a build is from, no hashing).
- **The SDK's darwin tools** (actool, ibtool, momc, xcstringstool) when their inputs changed. `install-toolchain.sh`
  rebuilds them on every `--repair` (`install_darwin_tools` runs `swift build` in `tools/darwin-tools` and copies
  the tools in) but has no staleness check of its own, and the SDK stamp (`darwinTools=1.1.0`) did not change for
  v0.4.8's AssetKit bump. Setup compares mtimes instead: git rewrites only the files a commit changes and the
  install gives actool a fresh mtime, so any of `tools/darwin-tools/{Package.swift,Package.resolved,Sources/}`,
  `tools/{ibtool,ibmac.py,xcstringstool,xcstrings_symbols.py,momc,nibarchive.py,keyorder.py}` newer than the SDK's
  actool marks the SDK `broken` (about 20 stats per `setup check`).
- **The SDK itself** only when `XTOOL_SDK_VERSION` (the stamp) changed: from the spare copy if one has the new
  stamp, otherwise it needs the Xcode download (logged, not run).

## Pins and the update channel

The versions Setup installs resolve in one place, `setup/channel.py` `effective_pins(channel=None, override=None)`,
and every installer, row and `toolchain info` `pinned` value reads the result from `ctx.pins` (never the
`pins.py` constants directly):

- **tested** (the default, settings.json `"toolchainChannel"`): `pins.py`, the versions this iOS Studio release
  was tested with. This is the behaviour without the updates feature.
- **latest**: the setup state's `"channelPins"` (`{<id>: {version, sha256|commit, at}}`, written by an update
  only after its post-update check passed) laid over the tested pins. An entry that can't be verified (a binary
  without a SHA-256, an apple-dev commit that isn't a full SHA) is ignored.
- **override** (same shape, an update's target) wins on either channel.

The rows say which pin they compare against: "At the tested version …", "(latest: 11.50.0)", or for an older
apple-dev checkout "iOS Studio is tested with …" / "the Latest channel has … (untested)"; `toolchain info` items carry
the same word as `pinWord` next to `pinned`. xtool has no pin of
its own: it follows the `XTOOL_SHA` of the omarchy-apple-dev checkout. Switching to **latest** with nothing applied
yet changes nothing; switching back to **tested** makes the rows compare against `pins.py` again (an apple-dev
checkout at a newer commit then shows **Update**).

## Backups during an update

An update keeps the previous version until its post-update check passes. `channel.backup(component)` sets it aside
before the install, runs with `Ctx(flags, pins, keep_previous=True)`, then calls `Backup.discard()` (it works) or
`Backup.restore()` (it failed or was cancelled), newest component first:

| Component | backup() | restore() | discard() |
|---|---|---|---|
| `ipsw`, `rcodesign` | copies `~/.local/bin/<tool>` to `~/.cache/ios-studio/updates/prev/` | copies it back (removes the new one if there was none) | deletes the copy |
| `pymobiledevice3` | renames the venv to `venv.prev` (same folder, instant); `i_pymd3` then doesn't delete anything | deletes the new venv, renames `venv.prev` back | deletes `venv.prev` |
| `xtool` | remembers the link target; `i_xtool` doesn't prune old builds | relinks the old build, prunes the new one | prunes every other build (as a normal install does at once) |
| `apple-dev` | remembers HEAD and the SDK's actool mtime | `checkout --detach` the old HEAD; re-runs `install-toolchain.sh --mode full --repair` only if the update reinstalled the darwin tools (actool mtime changed), otherwise sets the rewritten darwin-tools inputs' mtimes back so the SDK stays `done` | nothing |

Each backup serialises itself (`to_json()` → `{kind: binary|venv|xtool|apple-dev, component, …}`, back with
`Backup.from_json()`) for the update's in-progress marker, so the next run can put back what a killed one set aside.

Backups are cleared under `fileio.lock(~/.local/state/ios-studio/updates.lock)` (`channel.updates_lock()`), held by an
update while it runs. Leftovers of an interrupted update show in `toolchain disk` as **Previous versions from
updates** (`update-backups`, safe to clean); `toolchain clean --item update-backups` refuses with `busy` while an
update runs.

## Streaming events (install, xip, clean, remove-all)

`step {id,title,state: running|done|failed|skipped, error?}`, `progress {step, fraction|null, detail, eta}`,
`log {step, text}`, `prompt {kind:"confirm", message, options}`, `done {ok, data|error}`.
`setup xip --watch` adds `watching {dir, needed, expectedSize}`, `download {file, size, expected, fraction, rate, eta}`,
`found {path, file, size}`, `rejected {xip, message}`; it ends with `done {ok:true, data:{xip}}` once a valid,
version-matched .xip is there (it keeps watching after a rejected file).

Every streamed command emits a heartbeat `progress` every 2 s (`runtime.StreamProgress`). It follows real numbers
when the output has them: build tasks (`[n/m]`, and swift-build's `[n / m]` with thin spaces), else `NN %` lines.
SwiftPM's per-package `[n/m] Fetching …` / `[n/m] Downloading …` counters only redraw: they are neither logged nor
taken for the build. Before real numbers arrive the bar runs on elapsed vs. estimate, up to 95 % (25 % for xtool, whose
package download and planning come first). Once they start, they fill the rest of the bar from where the estimate
got to (never backwards), the detail says "n of m", and the ETA follows the task rate since then.

`install-toolchain.sh --repair` ends with a "Survive status" block; its `pairing:` and `auth:` lines look in the
wrong folders for iOS Studio (xtool keeps the sign-in in `~/.config/xtool/data`) and name a terminal command, so
Setup drops exactly those lines inside that block (`installers.survive_noise`) and keeps the rest, errors included.

## Safety

- Root: one `pkexec helper/root/root-tasks.sh <flags>` per step (or per "Install everything"); never sudo;
  makepkg runs as the user via `runuser`.
- `toolchain clean` deletes only the paths of allow-listed items computed by `disk_items()`, each checked
  against fixed roots. "Old xtool builds" lists every `xtool-*` build and source tree but the active one, in both
  layouts. `toolchain remove-all` deletes `~/.local/lib/omarchy-apple-dev` and `~/.cache/omarchy-apple-dev` whole and keeps
  `~/.config/xtool` (Apple sign-in) unless
  `--remove-auth`, always keeps pairing records and projects.
