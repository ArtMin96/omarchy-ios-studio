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
| Screen | Watch the phone's screen, record short videos for bug reports, and take screenshots. See [Screen](#screen) below. |
| Files | Import photos and videos from the camera roll, browse and change an app's files, and test camera code with files. See [Files](#files) below. |
| Location | Makes the phone think it's somewhere else. Pick a city or type coordinates. Restore real location puts it back. |
| Network | Test how your app copes with a bad connection: make the whole phone act as if it's on Edge, 3G, LTE, DSL or a lossy network, or running hot, or with a slow GPU. Record an app's traffic over a USB cable and see which servers it talks to. See [Network](#network) below. |
| Performance | Measures one app over time: CPU, memory, energy, frame rate, network, disk and battery, with a report of anything unusual. See [Measure how my app performs](#measure-how-my-app-performs). |
| Debug | Pauses one of your own apps to show what every thread is doing, or stops it at breakpoints so you can step through your code. See [Debugging](#debug-my-app-on-the-iphone). |
| Profiles | The signing profiles on the phone, active and expired. |
| Info | Name, model, iOS version, UDID, serial number, network addresses, battery and storage. |

The link button at the top pairs another iPhone. The phone reconnects by itself whenever it's reachable.

#### Screen

![iPhone › Screen while recording](screenshots/screen.png)

Press Start live view to watch the phone. The badge next to the title says how it's shown and how many frames a second actually arrive. There are three ways to show it:

- Smooth streams the screen as video on iOS 27 or later, which is much smoother than screenshots. While it runs, the phone shows its screen-sharing indicator and apps can't use the camera or microphone. If an app is already using them, or the phone runs an older iOS, the view switches to screenshots by itself and says why.
- Screenshots works on every iPhone and leaves the camera and microphone alone. The phone sets the pace: over Wi-Fi each screenshot takes about 0.6 seconds on a busy screen, so expect 1.5 to 2 frames a second, a little more when the screen is still. Speed caps it lower.
- AirPlay opens UxPlay, a separate receiver window with sound. On the phone, open Control Center › Screen Mirroring and pick iOS Studio. If UxPlay isn't installed, the button takes you to its Setup step.

Record saves a short video of the screen, up to 30 seconds, 1 minute or 5 minutes. The phone shows REC and a timer while it records, and Stop recording ends it early. The video is an MP4 in `~/.cache/ios-studio/recordings/`. Show in folder and Copy path help you attach it to a bug report, and the finished video plays inline when Qt Multimedia is installed. Recordings in Screenshots mode need ffmpeg; if it's missing, the error has a button that installs it from Setup.

Take screenshot saves a full-resolution still. Save to Pictures and Copy image use the latest frame or screenshot.

#### Files

![iPhone › Files › Photos & videos](screenshots/files.png)

Files has three parts, and nothing here ever changes the camera roll.

- Photos & videos shows the camera roll as thumbnails. Select some (Shift+click selects a range) and import them to a folder (`~/Pictures/iPhone` unless you pick another). HEIC photos can be saved as JPEG.
- App files browses an app's files. Your own apps show their whole container; App Store apps show only their Documents folder, and only if they share files. Save files to this computer, add files with Add files or by dragging them onto the list, make folders and delete.
- Camera test runs your app's camera code on test images and videos. See [Test camera code with files](#test-camera-code-with-files).

#### Network

![iPhone › Network with a condition on](screenshots/network.png)

Simulated conditions come from the developer tools on the phone, so they need Developer Mode. Pick one from the list, choose how long it should last (1 to 30 minutes, 5 by default) and press Apply. It affects the whole phone and every app on it, so iOS Studio asks first. Only one condition is on at a time; applying another replaces it.

While one is on, a yellow banner at the top of the tab shows it with a countdown and a Reset now button. The iPhone page title and the sidebar also show it, so you don't forget it on another page. It switches off by itself when the time runs out, when you press Reset now, when you close iOS Studio, or when the connection to the phone drops.

Over Wi-Fi, a network condition also slows down this computer's own link to the phone, and iOS Studio may lose the phone for a moment (the condition then switches off). The two profiles that cut the network off almost completely, 100% packet loss and Very poor network, are only offered over a USB cable. Heat and GPU states work the same over Wi-Fi and USB.

Packet capture needs a USB cable, because the phone only shares its traffic over a cable. Pick an app (or the whole iPhone) and a duration, press Start capture, and use the app. You see the packet count while it records. When it stops, the tab shows the data sent and received, the protocols, and the servers the app talked to. The summary reads only the packet headers, never their contents, and leaves out iOS Studio's own connection to the phone. The recording is a standard pcapng file in `~/.cache/ios-studio/captures/` (the newest 20 are kept). Open in Wireshark opens it, and Setup can install Wireshark if you don't have it.

### Projects

![Projects](screenshots/projects.png)

Your apps. iOS Studio looks for xtool projects (a folder with `Package.swift` and `xtool.yml`) in your project folders, which you set in Settings.

- New app (`n`) creates an app from a template: a simple SwiftUI app, a tabs app, or a list with a detail screen.
- The folder button (`a`) adds a project you already have. A plain Swift package can be turned into an app; iOS Studio adds an `xtool.yml` and leaves your code alone.
- Run builds the app, signs it, installs it on your iPhone and opens it, with its output shown in Logs › Console. It needs a connected iPhone; without one, Run takes you to the iPhone page.
- Each project card also has Check build (compile only), Build .ipa, Re-sign & reinstall, Auto-renew, Build settings, Open in editor and Open folder. Check build and Build .ipa work without an iPhone; Re-sign & reinstall needs one.
- A project whose `xtool.yml` is generated by its own scripts has its build buttons off until its Build settings allow it (see [Build a project that has its own build script](#build-a-project-that-has-its-own-build-script)).
- The build panel (`b`) shows progress, a Problems list and the full log. Hiding the panel doesn't stop the build.

The Problems list covers more than compiler errors. An error in your code shows its file and line, and clicking it opens that line in your editor. Problems outside your code have no file: linker errors, Swift packages that can't be fetched or resolved, signing, the iPhone, the toolchain and project settings (`xtool.yml`, `Info.plist`). These show their category, a few lines of detail (the missing symbols of a linker error, for example) and a hint saying what to do.

![Build panel](screenshots/build.png)

### Logs

![Logs](screenshots/logs.png)

Four tabs:

- Device log is your iPhone's live log. Filter by app or process and by level, search, pause with `Space`, and export the visible lines to Downloads. It keeps streaming while you're on other pages.
- Console shows what your app prints: Swift `print()`, stdout and stderr. See [See what my app prints](#see-what-my-app-prints).
- Crashes lists crash reports from the phone, with new ones marked. Each report opens with a summary (the reason, the app and version, the crashed thread and the first frames of where it crashed) and the full report below. Your app's frames show the file and line, and a click opens them in your editor. You can copy or save the report.
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

Projects also has `n` (new app), `a` (add a project) and `b` (build panel). On the Device log and the Console, `Space` pauses.

## Common tasks

### Run my app on my iPhone

1. Connect the iPhone (cable or Wi-Fi) and unlock it.
2. On Projects, press Run on your app.
3. The first time only, on the iPhone: Settings › General › VPN & Device Management › your Apple ID › Trust.

If the build fails, the Problems tab in the build panel lists each problem. Click an error in your code to open it in your editor. For the others, such as a package that can't be downloaded or a signing problem, follow the hint under the message.

### See what my app prints

On a real iPhone, `print()` output never reaches the phone's log, so iOS Studio collects it when it starts the app.

1. Press Run on Projects, or open Logs › Console, pick one of your apps and press Launch. Launch restarts the app if it's already running.
2. The output appears live. Filter it, pause with `Space`, copy, export it to Downloads or clear the view. stderr lines are marked `err`.
3. Each launch keeps its own output. With several launches, a picker switches between them; the view follows the newest one.

Only apps that iOS Studio started show output here. Messages from `os_log`, `Logger` and `NSLog` go to the Device log tab instead. If nothing shows up, see [troubleshooting](troubleshooting.md#nothing-in-logs--console). [features/app-console.md](features/app-console.md) explains how it works and its limits.

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

![iPhone › Performance with a report](screenshots/performance.png)

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
4. Your app's frames show the function and the file and line it crashed on. Click one to open that line in your editor. This works for crashes from builds you ran from iOS Studio on this computer; if a crash came from an older build, the note under the frames says so, and rebuilding that version and reproducing the crash gives you the lines.

### Test camera code with files

![Files › Camera test with results](screenshots/camera-test.png)

Camera code is hard to test by pointing the phone at things. Files › Camera test feeds your app chosen images and videos instead and shows what it made of them.

1. Add the test hook to your app once. [camera-testing.md](camera-testing.md) has the Swift code to paste.
2. Build and run the app on the iPhone from Projects.
3. On the iPhone page, open Files › Camera test. Pick the app, add images or videos (or drop them on the list) and press Run test.

iOS Studio copies the files into the app's Documents/CameraTestInput, restarts the app with `-cameraTestMode 1`, waits for the app to write Documents/CameraTestOutput/done.json, and copies the results back. You get done.json, any images the app wrote, the app's log during the run and a warning if it crashed. If no done.json shows up before the timeout, the app is missing the hook or the launch argument.

### Debug my app on the iPhone

![iPhone › Debug with a backtrace](screenshots/debug.png)

Open iPhone › Debug and pick your app. Only your own apps are listed: iOS lets a debugger attach only to apps built and signed for development, so App Store and system apps can't be debugged. For readable results, build and run the app from Projects first, so iOS Studio has its build.

- Capture backtrace, for an app that hangs or spins: the app pauses for a few seconds while the debugger reads every thread, then runs on. The thread that stopped on an exception (else the main thread) opens first, and your own code is highlighted with its file and line. Click a frame to copy it.
- Breakpoints: pick a file from the app's Sources and type a line, or type a function name such as `ForecastModel.refresh`. Then press Start debugging. Attach to the running app keeps it as it is; Restart it paused closes it and starts it again, paused until the debugger is ready, so breakpoints in startup code also hit. When the app stops, you see where, the call stack and the variables of the selected frame. Continue, Step over, Step in and Step out move on; Pause stops a running app. Detach ends debugging and the app keeps running, and the stop button quits the app instead.
- Debug symbols: the iPhone's Swift runtime and the system libraries lldb needs, downloaded once per iOS version. Without them everything still works, but Swift values such as strings and arrays can read wrong, system frames show only library names and attaching is slow. It needs ipsw from Setup. See [Debug symbols](#debug-symbols) below.

The debugger is lldb from the Swift toolchain. It needs Python 3.12, which Setup's optional "Debugger support" step installs; the Debug tab links there when it's missing. Over Wi-Fi, attaching takes about 30 seconds.

#### Debug symbols

The Debug symbols card at the bottom of iPhone › Debug shows where the symbols for the phone's iOS version stand and offers one button to fix them:

| The card says | What it means | Button |
|---|---|---|
| Not downloaded | No symbols for this iOS version yet. | Download debug symbols |
| Ready | lldb uses them by itself. | none (Remove only) |
| Needs an update | They came from an older version of iOS Studio and miss system libraries, so attaching can take minutes over Wi-Fi while the app stays paused. They still work until you update. | Update symbols |
| Incomplete | Some libraries are missing; the card names them. | Update symbols |
| Failed | The last download or unpack failed; the card shows why. | Try again |

When the download is still on disk, the button is Unpack again instead: it unpacks the libraries from that download in about a minute, without downloading anything. That happens after a failed or cancelled unpack, and whenever Keep the download is on. Updating keeps your current symbols working until the new ones are ready, and Cancel leaves them as they were. While the symbols need an update, the Debug tab also shows a warning under the app picker with the same button.

Disk use: the download is about 7 GB and needs that much free space while it runs. The unpacked symbols take a few hundred MB. Keep the download (the switch under the buttons) keeps the 7 GB so the symbols can be unpacked again later; it's off by default, and turning it off deletes a kept download once the symbols are ready. Remove deletes the symbols and any kept download for this iOS version, after asking. Everything lives in `~/.cache/ios-studio/DeviceSupport/`.

### Free space

The iOS SDK and its build copies are large. Open Toolchain and look at Disk usage. Items marked safe, such as old SDK copies, build leftovers, old xtool builds and the Swift download, can be cleaned without breaking anything. Once the SDK is built, you can usually delete the Xcode download too. Each Clean asks first and shows how much it frees.

iOS Studio also deletes its own oldest files as new ones arrive. It keeps the logs of the last 300 builds, and the newest 30 screenshots, 20 screen recordings, 20 packet captures, 10 memory graphs from leak checks and 20 camera test results, fewer if they grow past a size limit. Save anything you want to keep somewhere else.

### Build a project that has its own build script

Some projects build with their own `run.sh`: it regenerates `xtool.yml` first, sets environment variables or raises the open-file limit. iOS Studio refuses a project whose `xtool.yml` is generated (its Run button is off) until you allow it:

1. On Projects, open the project's More (`m`) and choose Build settings.
2. If the project's scripts suggest settings, a note lists them; Use these fills the form. Check the prebuild command's arguments (a script may want a server address, for example).
3. Prebuild command: the script that regenerates `xtool.yml`, such as `./generate.sh`. It runs in the project folder before every build, without a shell, so `|`, `&&` and `$VARIABLES` don't work.
4. Environment variables: Add variable for each `NAME = value` the build needs.
5. Open-file limit: the number the script passes to `ulimit -n`, or empty for 65536.
6. Turn on Build generated project and press Save.

The next Run starts with Running the prebuild command; its output is in the build log. See [features/project-build-settings.md](features/project-build-settings.md).

### Connect an AI agent

![Settings › AI agents](screenshots/agents.png)

1. Open Settings › AI agents, or press Connect AI agents on Home.
2. If the iOS Studio server isn't ready yet, press Prepare. It downloads about 30 MB once.
3. Press Connect next to your tool, then start a new session in that tool.

Ask the agent something like "Check that iOS development is ready" or "Build HabitTracker and fix the errors". Signing in, pairing and turning on Developer Mode stay with you: the agent opens iOS Studio on the right page and asks you to do it there. Turn on Connect in demo mode to let an agent try things on made-up data first. [mcp.md](mcp.md) has the details and manual setup.
