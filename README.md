# iOS Studio

Build iPhone apps on Omarchy Linux and run them on your own iPhone, without a Mac.

![iOS Studio home page](docs/screenshots/home.png)

iOS Studio is a desktop app for [Omarchy](https://omarchy.org/). It installs the iOS toolchain, signs you in to your Apple ID, pairs your iPhone and builds your apps. After the install command below, you can do everything from the app window. It uses your Omarchy theme and font and changes with them.

## What it does

| Page | What you do there |
|---|---|
| Home | See your next step, what's ready, your iPhone, how many days until apps need re-signing, your apps with Run, and recent builds and crashes. |
| Setup | Install Swift, xtool, the iOS SDK and the iPhone tools with one button. |
| Apple ID | Sign in and see your team, certificates, profiles and the free-account limits. |
| iPhone | Pair your phone and work with it: see the list below. |
| Projects | Create an app from a template, build it, run it on your iPhone and re-sign it. A failed build lists each problem with its file and line, or with a hint when the problem is outside your code. |
| Logs | Read your iPhone's live log, see what your app prints (`print()`), open crash reports with the file and line your app crashed on, and look back at build logs. |
| Toolchain | Check versions and disk usage, free space, repair or remove everything. |
| Settings | Project folders, editor, notifications, automatic re-signing, AI agents, a launcher shortcut, tiled or floating, opaque window. |

On the iPhone page you can:

- manage installed apps and fake the phone's location
- watch the screen live, take screenshots and record short videos for bug reports
- import photos and videos from the camera roll, browse your apps' files, and feed camera code test images
- make the phone act as if it's on a slow network, running hot or with a slow GPU, and record an app's traffic over USB
- measure an app's CPU, memory, energy and frame rate, with a report of anything unusual
- capture a backtrace or stop at breakpoints in your own apps with LLDB

![iPhone › Performance](docs/screenshots/performance.png)

Press `?` in the app to see the keyboard shortcuts.

## What Linux can't do

- There is no iOS Simulator on Linux, so your apps run on a real iPhone.
- The iOS SDK only comes inside Apple's Xcode download. You download it once in your browser with your Apple Account (a free one works), and iOS Studio does the rest.
- A free Apple ID signs apps for 7 days and allows 3 of your apps on a phone at a time. TestFlight and the App Store need a paid Apple Developer Program membership.

## Install

You need Omarchy. Open a terminal (Super + Return) and paste:

```bash
git clone https://github.com/ArtMin96/omarchy-ios-studio ~/.local/share/omarchy-ios-studio && ~/.local/share/omarchy-ios-studio/install.sh
```

Then open iOS Studio from the app launcher. The Setup page takes it from there.

`install.sh` only changes files in your home folder. It adds a launcher entry, an icon, the `ios-studio` command, an Omarchy theme hook and a Hyprland rule that keeps the window opaque (you can turn that off in Settings). It doesn't install the iOS tools. Setup does that, and asks for your password once for the system packages.

`ios-studio <page>` opens the app on a page, or brings it to the front if it's already open. The pages are `home`, `setup`, `account`, `device`, `projects`, `logs`, `toolchain` and `settings`. Run `ios-studio --help` for the other options.

## First run

1. On the Setup page, press Install everything. It takes 30 to 45 minutes and needs 15 to 20 GB of disk while it builds.
2. Setup opens Apple's download page. Sign in, download the Xcode version Setup names (not a beta) and leave the file in Downloads. iOS Studio finds it and builds the iOS SDK from it.
3. Sign in to your Apple ID with your email and password, then the 6-digit code Apple sends to your other devices.
4. Plug in your iPhone, unlock it and tap Trust. iOS Studio sets up Wi-Fi at the same time, so you won't need the cable after this. Then turn on Developer Mode on the phone (Settings › Privacy & Security › Developer Mode) and let it restart.
5. On Projects, press New app, pick a template, then Run. The first time, trust your Apple ID on the phone in Settings › General › VPN & Device Management.

The [user guide](docs/guide.md) walks through each page. If something goes wrong, see [troubleshooting](docs/troubleshooting.md).

## AI agents

iOS Studio comes with an MCP server, so coding agents can build, run and debug your iPhone apps. Open Settings › AI agents and click Connect next to Claude Code, Codex, opencode, Cursor, Gemini CLI, Crush or Zed. When a build fails, the agent gets each error sorted by kind (compiler, linker, Swift packages, signing, iPhone, toolchain or project settings) with a hint naming the tool that fixes it, so it rarely has to read the raw log. It can also read what your app prints while it runs. Agents can't sign in to your Apple ID or pair your phone for you, and they ask before they delete anything. See [docs/mcp.md](docs/mcp.md).

## Uninstall

1. If you want the iOS tools gone too (Swift, xtool, the iOS SDK and caches), open Toolchain › Remove everything first. Your projects and iPhone pairing stay.
2. Open Settings › Uninstall iOS Studio, or run `ios-studio --uninstall`. It removes the launcher entry, the icon, the `ios-studio` command, the theme hook and iOS Studio's Hyprland rules and shortcut. It also turns off automatic re-signing and disconnects any AI agents you connected, then quits.
3. Delete the app folder. Settings › About shows where it is; with the install above it's `~/.local/share/omarchy-ios-studio`. Your settings and history stay in `~/.config/ios-studio`, `~/.local/state/ios-studio` and `~/.local/share/ios-studio` until you delete them.

## More

- [docs/guide.md](docs/guide.md) walks through every page, and [docs/troubleshooting.md](docs/troubleshooting.md) explains the common errors.
- [docs/mcp.md](docs/mcp.md) covers the AI agent tools, and [docs/features/app-console.md](docs/features/app-console.md) how the app console captures `print()`.
- [docs/camera-testing.md](docs/camera-testing.md) has the hook for testing camera code with files.
- [docs/development.md](docs/development.md) explains how the app is built, how to run it from source and how to run the tests.
- To report a security problem, see [SECURITY.md](SECURITY.md).

## Credits

iOS Studio is a front end. The hard parts come from these projects:

- [xtool](https://github.com/xtool-org/xtool) builds, signs and installs iOS apps from Linux.
- [omarchy-apple-dev](https://github.com/joshuaswarren/omarchy-apple-dev) turns Xcode's SDK into something Linux can build with, and pins a working toolchain.
- [omarchy-ios-kit](https://github.com/JonathanInTheClouds/omarchy-ios-kit) gave ideas for the daily workflow and the Swift hold.
- [pymobiledevice3](https://github.com/doronz88/pymobiledevice3) talks to the iPhone over USB and Wi-Fi.
- [Omarchy](https://omarchy.org/) and its Quickshell UI kit, which iOS Studio reuses for its look.

## License

MIT. See [LICENSE](LICENSE).
