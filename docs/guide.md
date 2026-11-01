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
| Network | Test how your app copes with a bad connection: make the whole phone act as if it's on Edge, 3G, LTE, DSL or a lossy network, or running hot, or with a slow GPU. Record an app's traffic over a USB cable and see which servers it talks to. See [Network](#network) below. |
| Performance | Measures one app over time: CPU, memory, energy, frame rate, network, disk and battery, with a report of anything unusual. See [Measure how my app performs](#measure-how-my-app-performs). |
| Profiles | The signing profiles on the phone, active and expired. |
| Info | Name, model, iOS version, UDID, serial number, network addresses, battery and storage. |

The link button at the top pairs another iPhone. The phone reconnects by itself whenever it's reachable.

#### Network

Simulated conditions come from the developer tools on the phone, so they need Developer Mode. Pick one from the list, choose how long it should last (1 to 30 minutes, 5 by default) and press Apply. It affects the whole phone and every app on it, so iOS Studio asks first. Only one condition is on at a time; applying another replaces it.

While one is on, a yellow banner at the top of the tab shows it with a countdown and a Reset now button. The iPhone page title and the sidebar also show it, so you don't forget it on another page. It switches off by itself when the time runs out, when you press Reset now, when you close iOS Studio, or when the connection to the phone drops.

Over Wi-Fi, a network condition also slows down this computer's own link to the phone, and iOS Studio may lose the phone for a moment (the condition then switches off). The two profiles that cut the network off almost completely, 100% packet loss and Very poor network, are only offered over a USB cable. Heat and GPU states work the same over Wi-Fi and USB.

Packet capture needs a USB cable, because the phone only shares its traffic over a cable. Pick an app (or the whole iPhone) and a duration, press Start capture, and use the app. You see the packet count while it records. When it stops, the tab shows the data sent and received, the protocols, and the servers the app talked to. The summary reads only the packet headers, never their contents, and leaves out iOS Studio's own connection to the phone. The recording is a standard pcapng file in `~/.cache/ios-studio/captures/` (the newest 20 are kept). Open in Wireshark opens it, and Setup can install Wireshark if you don't have it.

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

### Measure how my app performs

1. On the iPhone page, open the Performance tab.
2. Pick the app. Running apps come first, and your own apps are marked "yours".
3. Press Start measuring, then use the app on the phone as you normally would. If the app isn't open yet, the tab waits for it.

The charts update about once a second. Hover over one to read the values at that moment on every chart. Marks on the charts show events: red when the app was killed or crashed, yellow for a hang or a hitch. The time buttons show the last 2, 5 or 15 minutes, or everything (about the last hour is kept).

The report above the charts says what stood out, for example "Memory grew 3.2 MB/min for 4 minutes: a possible leak", CPU staying above 80%, the frame rate dipping, a hang, or the app being killed for using too much memory. It also lists the average, 95th percentile and highest value of the main numbers. The export button saves every sample to Downloads as a CSV file.

The frame rate is for the whole screen, not only your app, because that's what the phone reports. Measuring keeps going while you're on other pages, and stops when you press Stop or close iOS Studio.

Hangs only show up when Hang Detection is on. On the iPhone, open Settings › Developer › Hang Detection and switch it on. The Developer menu is there once Developer Mode is on.

Below the charts are three deeper checks, and each one asks before it starts:

- Track hitches records every frame for 60 seconds and marks the ones that took too long. The phone and your computer work harder while it runs.
- Profile launch quits the app, starts it again and shows how long loading, static initializers and the first frame took. Anything unsaved in the app is lost. Loader timing works only for your own apps.
- Check for leaks pauses the app for a few seconds and counts objects that nothing points to any more. It works only for your own apps while they're running. It saves a memory graph file you can open in Xcode on a Mac.

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
