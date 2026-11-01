# iOS Studio

Build iPhone apps on Omarchy Linux and run them on your own iPhone, without a Mac.

![iOS Studio home page](docs/screenshots/home.png)

iOS Studio is a desktop app for [Omarchy](https://omarchy.org/). It installs the iOS toolchain for you, signs you in to your Apple ID, pairs your iPhone and builds your apps. You don't need a terminal after the first install command. It uses your Omarchy theme and font and follows theme changes live.

## What it does

| Page | What you do there |
|---|---|
| Home | Your next step in one button, what's ready, your iPhone and the days until re-signing, your apps with Run, recent builds and crashes. |
| Setup | Install everything with one button: Swift, xtool, the iOS SDK, the iPhone tools. |
| Apple ID | Sign in, see your team, certificates, profiles and the free-account limits. |
| iPhone | Pair your phone, manage installed apps, watch its screen, fake its location. |
| Projects | Create an app from a template, build it, run it on your iPhone, re-sign it. |
| Logs | Read your iPhone's live log, open crash reports, look back at build logs. |
| Toolchain | Check versions, see disk usage, free space, repair or remove everything. |
| Settings | Project folders, editor, notifications, a launcher shortcut, tiled or floating, opaque window. |

## What Linux can't do

- There is no iOS Simulator on Linux. Your apps run on a real iPhone.
- The iOS SDK only ships inside Apple's Xcode download. You download it once in your browser with your Apple Account (a free one works), and iOS Studio takes it from there.
- A free Apple ID signs apps for 7 days and allows 3 of your apps on a phone at a time. TestFlight and the App Store need a paid Apple Developer Program membership.

## Install

You need Omarchy. Open a terminal (Super + Return) and paste:

```bash
git clone https://github.com/ArtMin96/omarchy-ios-studio ~/.local/share/omarchy-ios-studio && ~/.local/share/omarchy-ios-studio/install.sh
```

Then open **iOS Studio** from the app launcher. The Setup page does the rest.

From a terminal or a keybinding, `ios-studio <page>` opens iOS Studio on a page (`home`, `setup`, `account`, `device`, `projects`, `logs`, `toolchain`, `settings`), or focuses it if it's already open. `ios-studio crashes` goes straight to Logs › Crashes. `ios-studio --log` prints the app's log (add `-f` to follow it).

`install.sh` only touches your home folder: a launcher entry, an icon, the `ios-studio` command, an Omarchy theme hook and a Hyprland rule that keeps the window opaque (turn it off in Settings). It doesn't install the iOS tools; Setup does that, and asks for your password once for system packages.

## First run

1. **Install everything.** On the Setup page, press Install everything. Expect about 30 to 45 minutes and 15 to 20 GB of disk while it builds.
2. **Download Xcode.** Setup opens Apple's download page. Sign in, download the Xcode version Setup names (not a beta) and leave the file in Downloads. iOS Studio finds it and builds the iOS SDK.
3. **Sign in to your Apple ID.** Enter your email and password, then the 6-digit code Apple sends to your other devices.
4. **Pair your iPhone.** Plug it in, unlock it and tap Trust. iOS Studio also sets up Wi-Fi, so after this you can leave the cable off. Then turn on Developer Mode (Settings › Privacy & Security › Developer Mode) and let the phone restart.
5. **Run an app.** On Projects, press New app, pick a template, then Run. The first time, trust your Apple ID on the phone: Settings › General › VPN & Device Management.

The [user guide](docs/guide.md) covers each page and the common tasks. If something goes wrong, see [troubleshooting](docs/troubleshooting.md).

## Keyboard shortcuts

Press `?` in the app to see these.

| Keys | Action |
|---|---|
| `1` – `8` | Jump to a page |
| `Ctrl Tab` | Next / previous page |
| `j` `k` / `↑` `↓` | Move the cursor |
| `Enter` | Open or run the highlighted item |
| `Esc` | Close the top sheet, clear search, or go back |
| `r` | Refresh this page |
| `/` | Search on this page |
| `x` | Remove the highlighted item (asks first) |
| `c` | Copy the highlighted value |
| `?` | Show or hide the shortcut sheet |

Settings can add `Super + Shift + I` to your Hyprland bindings to open iOS Studio from anywhere.

## Screenshots

| | |
|---|---|
| ![Home](docs/screenshots/home.png) Home | ![Setup](docs/screenshots/setup.png) Setup |
| ![Apple ID](docs/screenshots/account.png) Apple ID | ![iPhone](docs/screenshots/iphone.png) iPhone |
| ![Projects](docs/screenshots/projects.png) Projects | ![Build](docs/screenshots/build.png) Build panel |
| ![Logs](docs/screenshots/logs.png) Logs | ![Toolchain](docs/screenshots/toolchain.png) Toolchain |
| ![Settings](docs/screenshots/settings.png) Settings | |

## Uninstall

1. To remove the iOS tools too (Swift, xtool, the iOS SDK and caches), open Toolchain › Remove everything first. Your projects and iPhone pairing stay.
2. Open Settings › Uninstall iOS Studio. It removes the launcher entry, icon, the `ios-studio` command, the theme hook and iOS Studio's Hyprland rules and shortcut, then quits.

   From a terminal, `ios-studio --uninstall` does the same.

3. Delete the app folder (Settings › About shows where it is; `~/.local/share/omarchy-ios-studio` if you followed the install above). Your settings and history stay in `~/.config/ios-studio`, `~/.local/state/ios-studio` and `~/.local/share/ios-studio` until you delete them.

## For developers

See [docs/development.md](docs/development.md) for the architecture, the helper protocol and how to run from source.

## Credits

iOS Studio is a front end. The hard parts come from these projects:

- [xtool](https://github.com/xtool-org/xtool): builds, signs and installs iOS apps from Linux.
- [omarchy-apple-dev](https://github.com/joshuaswarren/omarchy-apple-dev): turns Xcode's SDK into something Linux can build with, and pins a working toolchain.
- [omarchy-ios-kit](https://github.com/JonathanInTheClouds/omarchy-ios-kit): ideas for the daily workflow and the Swift hold.
- [pymobiledevice3](https://github.com/doronz88/pymobiledevice3): talks to the iPhone over USB and Wi-Fi.
- [Omarchy](https://omarchy.org/) and its Quickshell UI kit, which iOS Studio reuses for its look.

## License

MIT. See [LICENSE](LICENSE).
