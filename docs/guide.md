# User guide

A short tour of each page, then the tasks you'll do most. For error messages, see [troubleshooting](troubleshooting.md).

Open iOS Studio from the app launcher. You can also jump straight to a page with `ios-studio <page>`: `home`, `setup`, `account`, `device`, `projects`, `logs`, `toolchain` or `settings`. Add a tab after a colon (`ios-studio logs:crashes`), or use the shortcut `ios-studio crashes`. If iOS Studio is already open, it comes to the front instead of opening twice.

In a narrow window, such as tiled next to your editor, the sidebar shrinks to icons. Hover an icon to see the page name. Badges turn into dots.

## The pages

### Home

![Home](screenshots/home.png)

From top to bottom:

- Next step: one card with the most useful thing to do now and a button for it. That can be finishing Setup, signing in, connecting or unlocking the iPhone, turning on Developer Mode, renewing signing that's about to run out, creating your first app, or, once everything is ready, running your latest app. While a build runs, the card shows its progress.
- Readiness: a check for Toolchain, iOS SDK, Apple ID and iPhone.
- Quick actions: New app, Open Setup, Mirror screen and Connect AI agents.
- The iPhone card shows the name, model, iOS version, connection, battery and Developer Mode, with a fix button when something needs you.
- The Apple ID card shows your account, whether it's free or paid, and how many days are left until your apps need re-signing, with Renew.
- Your apps lists recent projects, each with Run. `j`/`k` move between them and `Enter` runs one.
- Recent builds and Crashes show the last few builds with their result, and the newest crash per app with how many reports it has. Click one to open it in Logs.

On a fresh machine, Home shows what Setup will do instead.

### Setup

![Setup](screenshots/setup.png)

Everything iOS Studio needs, grouped into System, Toolchain, Apple and iPhone. Each step shows whether it's done, missing or broken, how big it is and how long it takes. Broken steps get a Repair button.

- Install everything runs every missing step in order. You type your password once, for the system packages and Swift. Everything else goes into your home folder.
- You can stop an install. Finished steps stay installed.
- The Xcode step opens Apple's download page and watches your Downloads folder. When the file arrives, iOS Studio checks that it's the right version and builds the iOS SDK from it. If you already have the file somewhere else, use "I already have it…".
- The iPhone steps walk you through Trust, Developer Mode and trusting your own apps.

### Apple ID

![Apple ID](screenshots/account.png)

Sign in with your Apple ID (email, password, then a 6-digit code). Teams on the paid Apple Developer Program can sign in with an App Store Connect API key instead.

Once you're signed in, the page shows:

- your team, its ID, and whether it's free or paid
- when your apps' signing runs out, with a Renew now button
- the free-account limits and how much of each you've used: new App IDs this week (10), registered devices, and the 7-day signing period
- your provisioning profiles, certificates, App IDs and registered devices

Sign out is at the bottom. Apps already on your phone keep working until their signing runs out.

### iPhone

![iPhone](screenshots/iphone.png)

If no phone is paired, the page offers two ways to pair: with a cable (any iPhone) or over Wi-Fi only (iOS 27 or later). Once paired, it shows the phone's model, iOS version, battery, storage, Developer Mode and connection, with these tabs:

| Tab | What it does |
|---|---|
| Apps | Lists installed apps, yours first. Open, stop or remove an app, or copy its bundle ID. |
| Screen | A live view built from screenshots (up to about 2 a second over Wi-Fi). Take a screenshot, save it to Pictures, copy it or rotate it. For a smooth mirror, it can set up AirPlay through UxPlay. |
| Location | Makes the phone think it's somewhere else. Pick a city or type coordinates. Restore real location puts it back. |
| Profiles | The signing profiles on the phone, active and expired. |
| Info | Name, model, iOS version, UDID, serial number, network addresses, battery and storage. |

The link button at the top pairs another iPhone. The phone reconnects by itself whenever it's reachable.

### Projects

![Projects](screenshots/projects.png)

Your apps. iOS Studio looks for xtool projects (a folder with `Package.swift` and `xtool.yml`) in your project folders, which you set in Settings.

- New app (`n`) creates an app from a template: a simple SwiftUI app, a tabs app, or a list with a detail screen.
- The folder button (`a`) adds a project you already have. A plain Swift package can be turned into an app; iOS Studio adds an `xtool.yml` and leaves your code alone.
- Run builds the app, signs it, installs it on your iPhone and opens it.
- Each project card also has Check build (compile only), Build .ipa, Re-sign & reinstall, Auto-renew, Open in editor and Open folder.
- The build panel (`b`) shows progress, a Problems list with the file and line of each error, and the full log. Hiding the panel doesn't stop the build.

![Build panel](screenshots/build.png)

### Logs

![Logs](screenshots/logs.png)

Three tabs:

- Device log is your iPhone's live log. Filter by app or process and by level, search, pause with `Space`, and export the visible lines to Downloads. It keeps streaming while you're on other pages.
- Crashes lists crash reports from the phone, with new ones marked. Each report opens with a summary (the reason, the app and version, the crashed thread and the first frames of where it crashed) and the full report below. You can copy or save it.
- Build logs has every build you ran, with its problems and full log. Rebuild runs it again.

### Toolchain

![Toolchain](screenshots/toolchain.png)

Installed versions of xtool, Swift, the iOS SDK, pymobiledevice3, rcodesign, ipsw and omarchy-apple-dev, each with Install or Repair. Below that you see disk usage per piece, how much can be freed safely, and a Clean button for each item. Remove everything is at the bottom.

### Settings

![Settings](screenshots/settings.png)

- Project folders: where iOS Studio looks for apps. Until you change the list, it uses whichever of `~/Developer`, `~/Projects`, `~/projects`, `~/Code`, `~/code`, `~/src`, `~/dev` and `~/Work` exist. New apps go to `~/Developer` (created when you make your first app), or to the first folder in your list.
- Code editor: the command for Open in editor. Leave it empty to use Omarchy's default editor.
- Default iPhone, for when more than one is connected.
- Notifications for a finished build, a connected iPhone or signing that's about to expire. They only appear while you're in another window.
- Re-sign apps automatically: adds a background job that checks once a day at 10:00 and renews the apps you marked with Auto-renew. See [Renew before the 7 days run out](#renew-before-the-7-days-run-out).
- AI agents: connects coding agents such as Claude Code to iOS Studio. See [AI agents](#connect-an-ai-agent) below.
- Open with Super + Shift + I adds a shortcut to `~/.config/hypr/bindings.lua`. iOS Studio backs up the file first and undoes the change if Hyprland rejects it.
- Window: tile like other apps, or open as a floating window.
- Opaque window (on by default) keeps the window solid so text is easier to read. Turn it off to use Omarchy's slightly see-through windows.
- About shows versions and where iOS Studio keeps its files, plus Run setup again.
- Uninstall iOS Studio removes the launcher entry, the icon, the `ios-studio` command, the theme hook and iOS Studio's Hyprland rules and shortcut, turns off automatic re-signing and disconnects your AI agents, then quits. It's the same as `ios-studio --uninstall`. The toolchain stays; Toolchain › Remove everything removes it.

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

Projects also has `n` (new app), `a` (add a project) and `b` (build panel). On the Device log, `Space` pauses.

## Common tasks

### Run my app on my iPhone

1. Connect the iPhone (cable or Wi-Fi) and unlock it.
2. On Projects, press Run on your app.
3. The first time only, on the iPhone: Settings › General › VPN & Device Management › your Apple ID › Trust.

If the build fails, the Problems tab in the build panel lists each error. Click one to open it in your editor.

### Renew before the 7 days run out

With a free Apple ID, apps stop opening 7 days after you signed them. The project card and the Apple ID page show how much time is left.

- To renew by hand, press Re-sign & reinstall on the project card. Your app's data on the phone stays.
- To renew automatically, turn on Auto-renew on the project card and Re-sign apps automatically in Settings. iOS Studio then checks once a day and renews marked apps that expire within 2 days, as long as the iPhone is reachable. If it needs a new certificate, it sends you a notification instead of replacing it.

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
3. Read the summary at the top. The reason and "Where it crashed" usually point at the problem. Save or copy the full report if you need to share it.

### Free space

The iOS SDK and its build copies are large. Open Toolchain and look at Disk usage. Items marked safe, such as old SDK copies, build leftovers, old xtool builds and the Swift download, can be cleaned without breaking anything. Once the SDK is built, you can usually delete the Xcode download too. Each Clean asks first and shows how much it frees.

### Connect an AI agent

![Settings › AI agents](screenshots/agents.png)

1. Open Settings › AI agents, or press Connect AI agents on Home.
2. If the iOS Studio server isn't ready yet, press Prepare. It downloads about 30 MB once.
3. Press Connect next to your tool, then start a new session in that tool.

Ask the agent something like "Check that iOS development is ready" or "Build HabitTracker and fix the errors". Signing in, pairing and turning on Developer Mode stay with you: the agent opens iOS Studio on the right page and asks you to do it there. Turn on Connect in demo mode to let an agent try things on made-up data first. [mcp.md](mcp.md) has the details and manual setup.
