# Setup steps

What the Setup page shows, in order. `bin/iosstudio-helper setup check` returns these as `steps[]`;
`setup install --step <id>` installs or repairs one; `setup install --all` runs every missing
auto-installable step (one password prompt for all root work). Every install supports `--dry-run`.

States: `done`, `missing`, `broken` (installed but not working: shows **Repair**), `optional`,
`unknown` (only the iPhone can tell). `essential: true` steps decide `summary.ready`.

| id | Group | Title | What the user reads | Root | Size / time | How it's done |
|---|---|---|---|---|---|---|
| `platform` | System | This computer | Needs an Arch-based Linux (like Omarchy) on a 64-bit Intel/AMD or ARM computer. | – | – | check only |
| `packages` | System | System packages | Tools for talking to an iPhone over USB, building software, packaging apps and saving screen recordings (ffmpeg). | yes | 50–150 MB, < 1 min | `pacman -S --needed` |
| `usbmuxd` | System | USB connection service | Carries the cable connection to the iPhone; starts by itself when you plug it in. | yes | seconds | optional restart drop-in + udev trigger |
| `swift` | Toolchain | Swift compiler | The Swift toolchain that compiles your apps. | yes | 1 GB download, 3.5 GB, 5–10 min | AUR `swift-bin` pinned to `f78c5ec` (6.4.2-2): tarball downloaded by us with progress, `makepkg` as the user (runuser), `pacman -U`; also holds Swift and fixes xcspec in the same prompt |
| `swift-hold` | Toolchain | Keep Swift on this version | Stops system updates from changing Swift behind your back. | yes | seconds | `IgnorePkg = swift-bin` + Omarchy `pre-refresh-pacman` hook |
| `xcspec` | Toolchain | Swift build settings fix | Lets localized text files build on Linux. | yes | seconds | `helper/root/xcspec.py apply` |
| `apple-dev` | Toolchain | iOS build scripts | omarchy-apple-dev, which turns Xcode's SDK into something Linux can build with. | – | few MB | clone `~/.local/share/omarchy-apple-dev` @ `79bda716` (an existing checkout elsewhere is accepted through the `appleDevDir` setting) |
| `xtool` | Toolchain | xtool | Builds, signs and installs iPhone apps from Linux; compiled on your computer. | – | 1.8 GB build, ~7–14 min | pin from omarchy-apple-dev + cherry-pick xtool-org/xtool `70e1568` (#406, Apple sign-in 503 fix); heartbeat progress |
| `rcodesign` | Toolchain | rcodesign | Signs App Store / TestFlight uploads. | – | 10 MB | GitHub release, sha256-pinned |
| `pymobiledevice3` | Toolchain | iPhone connection tools | Talks to your iPhone over USB or Wi-Fi. | – | 335 MB, 1–2 min | venv `~/.local/share/ios-studio/venv`, `pymobiledevice3==11.49.0`; links `~/pymobile3-venv` to it if absent |
| `ipsw` | Toolchain | ipsw (optional) | Readable stack traces in the debugger. iPhone › Debug needs it to unpack the debug symbols download. | – | 40 MB | GitHub release, sha256-pinned |
| `debugger` | Toolchain | Debugger support (optional) | lldb needs python312, compiled from source. Without it lldb doesn't start, and iPhone › Debug sends you here (`debug check` → `needs-python`). | yes | 10–20 min | AUR build |
| `xcode` | Apple | Xcode 27.1 download | The iOS SDK only comes inside Apple's Xcode download. Download it once in your browser; iOS Studio finds it in Downloads. | – | ~2 GB download | `setup xip --open` / `--watch` / `--use PATH` |
| `sdk` | Apple | iOS SDK | Apple's iOS frameworks prepared for Linux. | – | 7 GB (15 GB free while building), 20–30 min | `xtool sdk build` → cache `~/.cache/xtool/darwin-iPhoneOS<v>.xtoolsdk`, then `install-toolchain.sh --mode full --repair` (registers it, darwin tools, macro server) |
| `apple-id` | Apple | Apple Account | Sign in so apps can be signed for your iPhone. | – | a minute | `{kind:"page", page:"account", action:"signin"}` |
| `iphone` | iPhone | Pair your iPhone | Connect, unlock, tap Trust. | – | a minute | `{kind:"page", page:"device", action:"pair"}` |
| `developer-mode` | iPhone | Developer Mode | Settings › Privacy & Security › Developer Mode, then restart. | – | phone restarts | `{kind:"page", page:"device", action:"developer-mode"}`; state from `~/.local/state/ios-studio/device.json` `developerMode` |
| `uxplay` | iPhone | Screen mirroring (optional) | Your iPhone's screen live on this computer over Wi-Fi (AirPlay). | yes | 20 MB, 2–5 min | AUR `uxplay` build, `gst-plugins-bad` and `gst-plugin-va` (hardware video decode), enable avahi, open ufw ports for `uxplay -p` (TCP 7000,7001,7100; UDP 6000,6001,7011, 5353) |
| `wireshark` | iPhone | Wireshark (optional) | Opens the network recordings from the iPhone page's Network tab. | yes | about 100 MB, about a minute | `pacman -S --needed wireshark-qt` |
| `app-integration` | System | App launcher entry | Adds iOS Studio to the app launcher. | – | seconds | runs `install.sh` |

## Streaming events (install, xip, clean, remove-all)

`step {id,title,state: running|done|failed|skipped, error?}`, `progress {step, fraction|null, detail, eta}`,
`log {step, text}`, `prompt {kind:"confirm", message, options}`, `done {ok, data|error}`.
`setup xip --watch` adds `watching {dir, needed, expectedSize}`, `download {file, size, expected, fraction, rate, eta}`,
`found {path, file, size}`, `rejected {xip, message}`; it ends with `done {ok:true, data:{xip}}` once a valid,
version-matched .xip is there (it keeps watching after a rejected file).

Long silent builds emit a heartbeat `progress` every 2 s (elapsed vs. estimate, capped at 95 %).

## Safety

- Root: one `pkexec helper/root/root-tasks.sh <flags>` per step (or per "Install everything"); never sudo;
  makepkg runs as the user via `runuser`.
- `toolchain clean` deletes only the paths of allow-listed items computed by `disk_items()`, each checked
  against fixed roots. `toolchain remove-all` keeps `~/.config/xtool` (Apple sign-in) unless
  `--remove-auth`, always keeps pairing records and projects.
