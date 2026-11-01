# User guide

A short tour of each page, then the tasks you'll do most. For error messages, see [troubleshooting](troubleshooting.md).

Open iOS Studio from the app launcher. You can also jump straight to a page with `ios-studio <page>`: `home`, `setup`, `account`, `device`, `projects`, `logs`, `toolchain` or `settings`. Add a tab with a colon (`ios-studio logs:crashes`), or use the shortcut `ios-studio crashes`. If iOS Studio is already open, it comes to the front instead of opening twice.

In a narrow window, such as tiled next to your editor, the sidebar shrinks to icons. Hover an icon to see the page name; badges become dots.

## The pages

### Home

One glance at where you are, from top to bottom:

- **Next step**: one card with the most useful thing to do now and a button for it: finish Setup, sign in, connect or unlock the iPhone, turn on Developer Mode, renew signing that's about to run out, create your first app, or, once everything is ready, run your latest app. While a build is running, it shows its progress.
- **Readiness**: a check for Toolchain, iOS SDK, Apple ID and iPhone.
- **Quick actions**: New app, Open Setup and Mirror screen.
- **iPhone** card: name, model, iOS version, connection, battery and Developer Mode, with a fix button when something needs you.
- **Apple ID** card: your account, free or paid, and how many days are left until your apps need re-signing, with Renew.
- **Your apps**: recent projects, each with Run. `j`/`k` move between them and `Enter` runs one.
- **Recent builds** and **Crashes**: the last few builds with their result, and the newest crash per app with how many reports it has. Click one to open it in Logs.

On a fresh machine, Home shows what Setup will do for you instead.

### Setup

The list of everything iOS Studio needs, grouped into System, Toolchain, Apple and iPhone. Each step shows whether it's done, missing or broken, how big it is and how long it takes. Broken steps get a Repair button.

- Install everything runs every missing step in order. You type your password once, for the system packages and Swift. Everything else goes into your home folder.
- You can stop an install. Finished steps stay installed.
- The Xcode step opens Apple's download page and watches your Downloads folder. When the file arrives, iOS Studio checks it's the right version and builds the iOS SDK from it. If you already have the file somewhere else, use "I already have it…".
- The iPhone steps walk you through Trust, Developer Mode and trusting your own apps.

### Apple ID

Sign in with your Apple ID (email, password, then a 6-digit code). Teams on the paid Apple Developer Program can sign in with an App Store Connect API key instead.

Once signed in, the page shows:

- your team, its ID, and whether it's free or paid
- when your apps' signing runs out, with a Renew now button
- the free-account limits and how much of each you've used: new App IDs this week (10), registered devices, and the 7-day signing period
- your provisioning profiles, certificates, App IDs and registered devices

Sign out is at the bottom. Apps already on your phone keep working until their signing runs out.

### iPhone

If no phone is paired, the page offers two ways to pair: with a cable (any iPhone) or over Wi-Fi only (iOS 27 or later). Once paired, it shows the phone's model, iOS version, battery, storage, Developer Mode and connection, with these tabs:

| Tab | What it does |
|---|---|
| Apps | Lists installed apps, yours first. Open, stop or remove an app, or copy its bundle ID. |
| Screen | A live view built from screenshots (up to about 2 a second over Wi-Fi). Take a screenshot, save it to Pictures, copy it, rotate it. For a smooth mirror, it can set up AirPlay through UxPlay. |
| Location | Make the phone think it's somewhere else. Pick a city or type coordinates. Restore real location puts it back. |
| Profiles | The signing profiles on the phone, active and expired. |
| Info | Name, model, iOS version, UDID, serial number, network addresses, battery and storage. |

The link button at the top pairs another iPhone. The phone reconnects by itself whenever it's reachable.

### Projects

Your apps. iOS Studio looks for xtool projects (a folder with `Package.swift` and `xtool.yml`) in your project folders, which you set in Settings.

- New app (`n`) creates an app from a template: a simple SwiftUI app, a tabs app, or a list with a detail screen.
- The folder button (`a`) adds a project you already have. A plain Swift package can be turned into an app; iOS Studio adds an `xtool.yml` and leaves your code alone.
- Run builds the app, signs it, installs it on your iPhone and opens it.
- Each project card also has Check build (compile only), Build .ipa, Re-sign & reinstall, Auto-renew, Open in editor and Open folder.
- The build panel (`b`) shows progress, a Problems list with the file and line of each error, and the full log. Hiding the panel doesn't stop the build.

### Logs

Three tabs:

- **Device log**: your iPhone's live log. Filter by app or process and by level, search, pause with `Space`, and export the visible lines to Downloads. You can keep it streaming while you're on other pages.
- **Crashes**: crash reports from the phone, new ones marked. Each report opens with a summary first (the reason, the app and version, the crashed thread, the first frames of where it crashed) and the full report below. Copy or save it.
- **Build logs**: every build you ran, with its problems and full log. Rebuild runs it again.

### Toolchain

Installed versions of xtool, Swift, the iOS SDK, pymobiledevice3, rcodesign, ipsw and omarchy-apple-dev, each with Install or Repair. Below that, disk usage per piece, how much can be freed safely, and a Clean button per item. Remove everything is at the bottom.

### Settings

- Project folders: where iOS Studio looks for apps. Until you change the list it uses whichever of `~/Developer`, `~/Projects`, `~/projects`, `~/Code`, `~/code`, `~/src`, `~/dev` and `~/Work` exist. New apps go to `~/Developer` (created when you make your first app), or to the first folder in your list.
- Code editor: the command for Open in editor. Empty means Omarchy's default editor.
- Default iPhone, for when more than one is connected.
- Notifications: build finished, iPhone connected, signing about to expire. They only appear while you're in another window.
- Open with Super + Shift + I: adds a shortcut to `~/.config/hypr/bindings.lua`. iOS Studio backs up the file first and undoes the change if Hyprland rejects it.
- Window: tile like other apps, or open as a floating window.
- Opaque window (on by default): keeps the window solid so text stays easy to read. Turn it off to use Omarchy's slightly see-through windows.
- Uninstall iOS Studio: removes the launcher entry, the `ios-studio` command and iOS Studio's Hyprland rules, then quits (same as `ios-studio --uninstall`). The toolchain stays; Toolchain › Remove everything removes it.
- About: versions and where iOS Studio keeps its files, plus Run setup again.

## Common tasks

### Run my app on my iPhone

1. Connect the iPhone (cable or Wi-Fi) and unlock it.
2. On Projects, press Run on your app.
3. The first time only, on the iPhone: Settings › General › VPN & Device Management › your Apple ID › Trust.

If the build fails, the Problems tab in the build panel lists each error. Click one to open it in your editor.

### Renew before the 7 days run out

With a free Apple ID, apps stop opening 7 days after you signed them. The project card and the Apple ID page show how much time is left.

- To renew by hand, press Re-sign & reinstall on the project card. Your app's data on the phone stays.
- To renew automatically, turn on Auto-renew on the project card. iOS Studio then checks once a day and renews apps that expire within 2 days, as long as the iPhone is reachable.

### Pair a new iPhone over Wi-Fi

Wi-Fi-only pairing needs iOS 27 or later and both devices on the same Wi-Fi.

1. On the iPhone, turn on Developer Mode: Settings › Privacy & Security › Developer Mode.
2. In iOS Studio, open iPhone and press Pair over Wi-Fi (or the link button at the top, if a phone is already paired).
3. On the iPhone, open Settings › Developer › Paired Macs and tap iOS Studio under Other Devices.
4. A 6-digit code appears on one side. Type it on the other: the code iOS Studio shows goes into the iPhone, and the code the iPhone shows goes into iOS Studio.

With a cable it's simpler: plug in, unlock, tap Trust. iOS Studio sets up Wi-Fi access at the same time.

### Read a crash report

1. Open Logs › Crashes with the iPhone connected.
2. "My apps" shows only your apps' crashes. Pick one.
3. Read the summary at the top: the reason and "Where it crashed" usually point at the problem. Save or copy the full report if you need to share it.

### Free space

The iOS SDK and its build copies are large. Open Toolchain and look at Disk usage. Items marked safe, such as old SDK copies, build leftovers, old xtool builds and the Swift download, can be cleaned without breaking anything. Once the SDK is built, the Xcode download can usually go too. Each Clean asks first and shows how much it frees.
