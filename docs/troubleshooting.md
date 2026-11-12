# Troubleshooting

Most errors in iOS Studio come with a button that fixes them or takes you to the right place. This page explains the common ones.

If a whole page shows "This page couldn't load", see [For developers](#for-developers) at the end.

## Signing in

### "Apple's sign-in helper is busy right now"

Sign-in, signing and the account lists all go through a helper server that xtool uses to talk to Apple. It sometimes answers "server busy" (HTTP 503). Wait a minute and try again. The same cause shows up during a build as "Apple's signing service didn't answer".

### The 2FA code doesn't arrive

Apple sends the code to your other Apple devices, not by text message. If nothing shows up after a minute, get one yourself:

- iPhone or iPad: Settings › [your name] › Sign-In & Security › Two-Factor Authentication › Get Verification Code.
- Mac: System Settings › [your name] › Sign-In & Security.

Press "Didn't get a code?" in the sign-in sheet for the same steps. If a code is rejected, it may have expired; sign in again to get a fresh one.

### Other sign-in errors

| Message | What to do |
|---|---|
| That Apple ID or password isn't right | Check both. Your Apple ID is the email address you use with Apple. |
| Apple has locked this Apple ID | Unlock it at iforgot.apple.com, then sign in again. |
| Apple needs you to accept the latest developer agreement | Sign in at developer.apple.com, accept it, come back. |
| This Apple ID has no developer team yet | Sign in once at developer.apple.com. Apple creates a free team. |
| iOS Studio couldn't reach Apple / Apple took too long | Check your internet connection and retry. |

## Signing and installing

### "Using this Apple ID on another computer?" (certificate revoke)

Each computer needs its own signing certificate, and the secret half of it never leaves the computer that made it. When you build on a second computer with the same Apple ID, iOS Studio asks before replacing the old certificate.

- If you replace it, apps you installed from the other computer stop opening until you install them again from here.
- If you're not sure, choose Don't revoke. Nothing changes, and the build stops with "a new certificate is needed".

### Only 3 apps at a time

A free Apple ID can have 3 of your own apps on an iPhone at once. Remove one on the iPhone page (Apps tab, your apps are marked "Yours"), then run again.

### 10 App IDs per week

Every new bundle ID registers an App ID with Apple. Free accounts get 10 new ones per 7 days. When you hit the limit, reuse an existing bundle ID or wait a few days. Apple ID › Free account limits shows how many you've used. Pick a bundle ID you'll keep when you create an app, and don't rename it often.

### "Signing failed" on a new iPhone

A phone has to be registered with your account before apps can be signed for it. Connect it with a cable and press Run once. That registers it. xtool can't register a phone over Wi-Fi, so a phone that was only ever connected wirelessly fails with Apple's "There are no current iOS devices on this team" (HTTP 409); the build shows it as a signing problem with this fix.

### "Your iPhone doesn't trust this developer yet"

The app installed, but iOS won't open apps from a developer it hasn't trusted. On the iPhone: Settings › General › VPN & Device Management, tap your Apple Development profile, then Trust. You only do this once per Apple ID on each phone.

### My app stopped opening after a week

Free signing lasts 7 days. Press Re-sign & reinstall on the project card, or turn on Auto-renew. See [Renew before the 7 days run out](guide.md#renew-before-the-7-days-run-out).

## The iPhone

### Developer Mode is off, or the switch is missing

Settings › Privacy & Security › Developer Mode, switch it on and tap Restart. After the restart, unlock the phone and tap Turn On.

The switch only appears after a developer app has been installed once. If you don't see it, plug the phone in and press Show the switch on the iPhone page, or run one of your apps once from Projects.

### Phone not found on Wi-Fi

- Wake and unlock the phone. A sleeping iPhone often stops answering on Wi-Fi.
- Make sure both are on the same Wi-Fi network. Guest networks and some office networks block devices from seeing each other.
- Wi-Fi only works after you've paired once. If the iPhone page says "Not paired", pair with the cable or over Wi-Fi first.
- If it still doesn't show up, plug in the cable. iOS Studio reconnects as soon as the phone is reachable.

### "This computer isn't trusted by your iPhone yet"

Unlock the phone. When it asks "Trust This Computer?", tap Trust and enter your passcode.

### "Your iPhone is locked"

Unlock it and try again. Installing and launching apps need an unlocked phone.

### "Couldn't prepare your iPhone for developer tools"

Some features, like screenshots and location, need Apple's developer disk image loaded on the phone. Press Try again. Keep the phone unlocked and connected while it loads.

### "The iPhone service keeps stopping as it starts"

The background process that talks to the phone failed four times in a row before it was ready, so iOS Studio stopped restarting it. The message names its log (`deviced.log` in `$XDG_RUNTIME_DIR/ios-studio/`); the last lines there say why. Fix that (often the iPhone connection tools need repair, see below), then press Retry on the iPhone page.

### "The iPhone service can't start: its socket path is N bytes long"

The runtime folder is so deep that the service's socket path doesn't fit the 107-byte limit for Unix sockets. This only happens with a custom `IOSSTUDIO_RUNTIME_DIR` or `XDG_RUNTIME_DIR`; point it at a shorter folder.

## Screen, files, network and debugging

### "Screen streaming isn't available while the camera or microphone is in use"

While the Smooth view streams, the phone keeps the camera and microphone for itself. If an app is already using one of them, the live view falls back to screenshots and says so. Close that app on the phone, or keep using Screenshots if your app needs the camera.

### The Screenshots view is slow

The phone sets the pace. Over Wi-Fi each screenshot takes about 0.6 seconds on a busy screen, so 1.5 to 2 frames a second is normal (a still screen is a little faster). On iOS 27 or later, pick Smooth instead.

### Elements shows nothing

Elements reads the app that's in front on the iPhone, so "No app on the screen" means it found none. Unlock the phone, open the app and leave it in front while it reads (the home screen, the lock screen or Control Center don't count). If the list stops early with "the focus left the app" or "couldn't move further", an alert, a permission prompt or the keyboard is up: answer or close it on the phone and press Read the screen again. "Can't read the screen yet" with Developer Mode means the phone's accessibility inspector needs Developer Mode; "Couldn't reach the iPhone's accessibility service" means the same, or a locked phone.

### "… isn't the app in front" (wrong-app)

The command was told which app to read (an AI agent passes its bundle id, and a press uses the app of the last read), and another app is showing. iOS Studio refuses rather than reading or pressing the wrong app. Bring that app to the front and unlock the phone, then try again. If it says the app isn't installed or isn't running, install or launch it first.

### No outlines on the phone for elements

On iOS 27.3 the iPhone doesn't report where an element is on the screen, so rows in Elements can't be outlined; the list says so under it. Audit issues come with their position and are outlined. The demo iPhone has positions, so you see outlines there.

### Pressing an element does nothing

The ids in the list belong to the screen as it was read. If the app moved on since (a sheet opened, a list scrolled, a row was added), the press fails with "Read the screen again" or misses: press Read the screen again and press the fresh row. Some controls don't respond to an accessibility press at all ("The iPhone didn't accept the press"); try the cell or button around them. If "Nothing on the screen changed", the app may still be loading: read the screen again after a moment.

### "This app doesn't share files"

App Store apps only show their Documents folder when they turn on file sharing, and this one doesn't. Your own apps, built in iOS Studio, always show their whole container.

### App data: "Stop the app before changing its settings"

A running app keeps its own copy of its settings and data in memory and writes it back, so iOS Studio never changes them while the app runs. Press Stop app (agents pass `stop_app=true`), then make the change. The app reads the new values the next time it starts.

### App data: I changed a setting but the app still shows the old value

iOS keeps a copy of each app's settings in memory, even after the app stops, and hands that copy to the app's next launch. Changing the file alone doesn't reach the app. So after a change iOS Studio installs your last build of the app over it (its data stays), and iOS then drops that copy. If the change says it was only written to the file, no build was found or installing it failed (for example, its 7-day profile expired). Build or renew the app (Projects › Run), then make the change again. To try a value right away, launch the app with the launch arguments shown (`-key value`); they apply to that launch only. The file as it was before each change is kept in `~/.cache/ios-studio/appdata/<bundle id>/backups/`.

### App data: Reading right after Stop takes 10 seconds

iOS saves a stopped app's last settings a few seconds after it stops (up to about 7 s on an iPhone 17 Pro). Data waits that long after a stop, so it never shows or edits a half-saved file.

### App data: "Only your own apps … share their saved data"

Settings and stores are only readable in development builds you installed from iOS Studio. App Store apps keep theirs private, and no setting changes that.

### App data: Clean reinstall says there's no build

Clean reinstall installs your last signed build of the app again, and none was found on this computer. Build the project once (Projects › Run, or `ios_build_start(action='ipa')`), then try again. Wipe data works without a build.

### "Packet capture needs a USB cable"

The iPhone only shares its network traffic over a cable. Plug it in; the Start capture button turns on by itself.

### A network condition is marked "USB only", or asks to confirm over Wi-Fi

Over Wi-Fi, a network condition also slows down this computer's own link to the phone. 100% packet loss and Very poor network would cut that link off, so they can't be switched off again over Wi-Fi and need a USB cable. The other network conditions ask you to confirm first, because iOS Studio may lose the phone for a while (the condition then switches off by itself). Heat and GPU states work the same over Wi-Fi and USB.

### No hangs in the Performance report

The phone only reports hangs when Hang Detection is on. On the iPhone, open Settings › Developer › Hang Detection and switch it on.

### "The debugger needs Python 3.12, which isn't installed"

The debugger is lldb from the Swift toolchain, and it needs Python 3.12. Install "Debugger support" in Setup; it builds Python from source, so it takes a while. If the message says lldb itself is missing or didn't start, install or repair Swift in Setup.

### F5 in VS Code or Zed says "attachCommands failed to attach to a process"

Look at the editor's Debug Console. "Start 'Debug in editor' in iOS Studio first" means iOS Studio isn't holding the connection to the phone for this app: press Debug in editor on the project's card and wait until it says it's waiting for the editor, then press F5 again. In Zed, a message that lldb-dap wasn't found means its Swift extension can't find the toolchain's debugger: set Zed up again in Settings › Code editor. If you switched VS Code's Swift extension to CodeLLDB, switch it back to lldb-dap. See [features/editor-debugging.md](features/editor-debugging.md).

### Attaching the debugger takes minutes

Look at the Debug symbols card on iPhone › Debug. If it says Needs an update or Incomplete, press Update symbols (or Unpack again, if it offers that). Symbols unpacked by older versions of iOS Studio don't include libdispatch, so while attaching lldb has to look up one symbol about a thousand times over the connection, and over Wi-Fi that takes a couple of minutes with the app paused. Your current symbols keep working while the update runs. A USB cable also makes attaching faster.

### "The debug symbols need about N GB free while they download"

The download is about 7 GB per iOS version, and only a few hundred MB stay once it's done. Free some space (Toolchain shows what can go safely), then try again. Debugging works without the symbols, but Swift strings and arrays can read wrong and system frames show only library names.

### Nothing in Logs › Console

The console only shows apps that iOS Studio launched: Run on a project, or Launch on the Console tab. An app you opened on the phone itself, or one that was already running, has nowhere to send its output. Messages from `os_log`, `Logger` and `NSLog` go to the iPhone's log instead, so look for them on the Device log tab. If a launch says it opened the app "without the console", the phone didn't offer one: update iOS Studio's iPhone tools in Setup and launch again.

### print() output arrives late or all at once

The phone gives the app a terminal for its output, so `print()` should show up line by line. If lines come in big chunks, something in the app switched stdout to full buffering, or writes without a newline (`print(x, terminator: "")` is shown after a second of quiet). Put `setvbuf(stdout, nil, _IOLBF, 0)` at the start of the app (or call `fflush(stdout)` after writing), and end lines with a newline. On the iPhone stdout and stderr share that terminal, so the stderr filter only appears in demo mode.

### A crash report shows `MyApp + 0x…` instead of file and line

The note under "Where it crashed" says why. "No build on this computer matches" means the crash came from a build iOS Studio didn't keep: one made before iOS Studio started keeping builds, one made on another computer, or one you rebuilt over. "Pruned" means it was kept but is older than the last 10 builds of the project. Rebuild that exact version (same code and settings), install it with Run, reproduce the crash and open the new report. "No debug info" means a release build: it shows function names only; use the debug configuration for lines. See [features/crash-symbolication.md](features/crash-symbolication.md).

## Building

### "Your code has N errors"

The Problems tab in the build panel lists each error with its file and line. Click one to open it in your editor.

Problems that aren't in your code (linker, Swift packages, signing, the iPhone, the toolchain or project settings) have no file. They show their category, a few lines of detail and a hint saying what to do.

### "Connect your iPhone first"

Run and Re-sign & reinstall install the app on your iPhone, so they need a connected phone. Plug it in or wake it on Wi-Fi, then press Run again. Check build and Build .ipa work without a phone.

### Watch mode says "checking files every second"

Watch mode uses inotify, one watch per folder under `Sources/`. When the system's limit is used up (other watchers such as editors, Syncthing or `node_modules` trees count too), it falls back to comparing file times once a second: it still works, a little later and with a little more disk activity. Polling looks at no more than 20,000 files, so files beyond that in a very large `Sources/` tree aren't noticed. To raise the limit:

```bash
cat /proc/sys/fs/inotify/max_user_watches          # often 8192 or 65536
echo fs.inotify.max_user_watches=524288 | sudo tee /etc/sysctl.d/90-inotify.conf
sudo sysctl --system
```

Then stop and start watch mode again. `IOSSTUDIO_WATCH_POLL=1` forces polling, for file systems without inotify (some network mounts).

### Saving doesn't start a build in watch mode

Only `Sources/`, `Package.swift`, `xtool.yml` and `.plist`/`.entitlements` files in the project folder count; `Package.resolved`, `Tests/`, `.build/` and hidden folders don't. A save that doesn't change the file's content is ignored too. Editors that save through a temporary file and a rename (Vim, Neovim, Zed, JetBrains IDEs) are supported. If the project is being built from the Build button at the same time, the watch build waits for it.

### Watch mode stopped by itself

It stops when nothing uses it any more: the app (or the AI agent's session) that started it has closed. It also stops when another project is watched (one at a time), when the project folder goes away, and if the watcher process was killed. The message on the project card, or `stop_reason` in `ios_watch_status`, says which. Its log is `$XDG_RUNTIME_DIR/ios-studio/watch.log`. Once it grows past 1 MB it is cut back to its last 64 KB when a watcher starts.

### No completion or errors in my editor

Completion comes from SourceKit-LSP, and for an xtool project it needs `.bsp/xtool.json` in the project folder. iOS Studio writes it after a successful build and when you create or convert a project, so a project you added and never built doesn't have it yet: the project card says "Build once to enable completion". Build it once (Check build is enough), then reopen the folder in your editor. Also check:

- The editor is set up in Settings › Code editor. Zed and VS Code need their Swift extension, which you install yourself; the row says so when it's missing.
- You opened the project folder itself, not a folder above it.
- The card says the file is stale: it starts something other than `xtool dev build-server`, or `xtool` isn't on PATH (start the editor from a shell where `which xtool` works). iOS Studio doesn't replace a file it didn't write; delete `.bsp/xtool.json` and build again.
- `xtool.yml` has `skipLSP: true`: remove it.

See [features/editor-setup.md](features/editor-setup.md).

### "Failed to initialize build server: JSON-RPC connection closed"

The editor started `xtool dev build-server` and it quit right away, almost always because xtool's iOS SDK isn't set up (`~/.config/swiftpm/swift-sdks/darwin.artifactbundle` is missing). The project card says "xtool's iOS SDK isn't set up: finish it on the Toolchain page". Finish the SDK step on the Toolchain page (or Setup), then restart the language server or reopen the folder. In Zed, also check that the window isn't in Restricted Mode: click Trust and Continue when it asks about the folder.

### Old `xtool dev build-server` processes keep running

Each editor window starts its own build server, and one can stay behind after the editor quits. List them with `pgrep -af 'xtool dev build-server'` and stop them with `pkill -f 'xtool dev build-server'` (close your editors first; an open one starts a new server as needed).

### "swift-format isn't installed"

swift-format comes with the Swift toolchain: iOS Studio looks for it next to `swift` (normally `/usr/lib/swift/usr/bin/swift-format`), then on PATH, then as `swift format`. Install or repair Swift on the Setup page. Check with `/usr/lib/swift/usr/bin/swift-format --version`.

### Format leaves a file out

swift-format can't parse it, almost always because of a syntax error. It leaves that file alone and formats the rest. Build the project to see the compiler's error, fix it and format again. A file that changed while formatting (you saved it in your editor) is also left alone: run Format again. A "isn't valid JSON" error means the project's `.swift-format` is broken; `swift-format dump-configuration` prints a valid one to start from.

### Format uses the wrong indentation

swift-format reads `.swift-format` from the file's folder and each folder above it, so a `.swift-format` higher up (in `~/Developer`, say) applies when the project has none, and one in a source folder wins over the project's. The format sheet (and `config_path` in the result) says which one applied; without any, swift-format's defaults are 2 spaces and 100 columns.

### SwiftLint findings don't show up

SwiftLint is optional and only runs when `swiftlint` is on PATH and starts. A mise shim without a version set is on PATH but fails; the lint result's note says so. Run `swiftlint version` in a terminal: if it fails, set a version (`mise use -g aqua:realm/SwiftLint@<version>`) or install SwiftLint another way. SwiftLint reads `.swiftlint.yml`, including `excluded:`. See [features/format-lint.md](features/format-lint.md).

### Adding a package fails with "can't be cloned" or "Couldn't fetch"

The package's git repository couldn't be downloaded. iOS Studio never waits for a password, so a private repository (or a URL with a typo) fails at once instead of hanging. Check the URL in a browser; it is the repository's address (`https://github.com/owner/repo`), not a release or file page. For a private repository, set up git credentials or an SSH key so `git ls-remote <url>` works in a terminal without asking anything, and use the `git@host:owner/repo.git` form for SSH. "Could not resolve host" or "timed out" is the network: check the connection and add it again. Nothing in the project changes when an add fails: `Package.swift` and `Package.resolved` are put back.

### "No version matches the rule" when adding or updating a package

The version you asked for doesn't exist, or another package needs a different version of the same dependency. Pick a lower version or another rule (Up to next major from an older version, or a branch such as `main`), or update the other package too. A package without release tags needs Branch or Revision.

### A package was added but the app doesn't build

Check build after adding a package compiles it for iOS. Some packages don't build for iOS with xtool: packages with binary targets (`.binaryTarget`, XCFrameworks made for Xcode), packages whose build tool plugins or macros only run on macOS, and packages that import macOS-only modules (`AppKit`) or need a newer iOS than the app's. The build's Problems list shows the error with the package's files; remove the package again (Packages… › Remove) or pick another version.

### "Removing … can't be done safely"

Removing a package edits `Package.swift` as text (SwiftPM has no command for it). When the package is added in a way that isn't a plain list entry (built from a variable with `+`, added with `package.dependencies.append(...)`, a URL put together from strings), iOS Studio doesn't guess: it shows what it found and changes nothing. Remove the entry and the `.product(...)` lines of the targets by hand, then refresh the list.

### "Another package change for this project is still running"

Adding, updating, resolving and removing packages take turns per project, whether they come from the app or an AI agent. Wait for the other one to finish. A change waiting for a build of the same project shows "Waiting for another Swift command on this project (a build?) to finish" and continues by itself.

### A project's build buttons are off

Its `xtool.yml` is generated by the project's own scripts, so iOS Studio doesn't build it until you allow that in the project's More › Build settings (see [Build a project that has its own build script](guide.md#build-a-project-that-has-its-own-build-script)). Until then you can build it with its own scripts; once the app is installed, the iPhone page, Logs and Performance still work with it.

### "The iOS SDK isn't installed"

Open Setup and run the iOS SDK step. If you updated Xcode or Swift, the SDK may need rebuilding.

### "That file is Xcode X, but your Swift needs Xcode Y"

Each Swift version works with exactly one Xcode version. Download the exact version Setup names, not a newer one or a beta.

### "This project's xtool.yml is generated by its own scripts"

Its `xtool.yml` starts with a `GENERATED` comment, so iOS Studio doesn't build it until you allow that: a stale generated file could build something other than the real app. Open the project's More › Build settings, set the script that regenerates `xtool.yml` as the prebuild command (plus any environment variables its `run.sh` exports) and turn on Build generated project. See [Build a project that has its own build script](guide.md#build-a-project-that-has-its-own-build-script).

### "The prebuild command … failed"

The command in the project's Build settings exited with an error before xtool ran. The problem shows its last output lines and the build log has all of it. Fix the script, or change the command: it runs without a shell, so `a && b` or `$VAR` need a script of their own, and `./script.sh` needs its executable bit (or use `bash script.sh`).

### "The open-file limit can't be raised to N"

The open-file limit in the project's Build settings is above your session's hard limit (`ulimit -Hn`). Lower the setting, or raise the hard limit with `DefaultLimitNOFILE=` in `/etc/systemd/user.conf` or a `nofile` line in `/etc/security/limits.conf`, then log out and back in.

### "The build hit the system's open-file limit" (Too many open files)

Builds get a soft limit of 65536 (or your hard limit, if lower). If a project needs more, set Open-file limit in its Build settings; if the hard limit is too low, raise it as above.

### A setting in xtool.yml does nothing

xtool ignores keys it doesn't know without a word, and keys are case-sensitive: `bundleId:` or `iconFile:` is skipped, and xtool uses its default instead. The project card's settings warnings (and the Problems list, marked "Pre-build check") name the unknown key, its line and the key you probably meant. Rename it and press Check again. See [Settings warnings on a project card](guide.md#settings-warnings-on-a-project-card).

### The app crashes when it opens the camera (or asks for location, photos, contacts…)

iOS ends an app that asks for a permission without the matching text in its `Info.plist`, such as `NSCameraUsageDescription` for the camera or `NSLocationWhenInUseUsageDescription` for location. There's no error at build time. The project check warns about it: "The code uses the camera (ScannerView.swift:24) but Info.plist has no NSCameraUsageDescription". Add that key to `Info.plist` with one sentence saying why the app needs it (iOS shows it in the permission prompt). If the project has no `Info.plist` yet, create one and point `infoPath` in `xtool.yml` at it. The list of features and keys is in [features/project-check.md](features/project-check.md#usage-descriptions).

### "… needs a paid Apple Developer account" (push notifications, app groups, iCloud)

The project's entitlements file asks for something a free Apple ID can't get. xtool leaves it out when it signs for a free team, so the app installs but that feature doesn't work on the iPhone (no push token, no shared app group folder, no iCloud). Remove the entitlement, or sign with a paid account; with a paid account you can ignore the warning. The full list is in [features/project-check.md](features/project-check.md#paid-only-entitlements).

### The app icon is blank, or black around the edges

A blank icon means `xtool.yml` has no `iconPath`. A black background means the icon has transparency, which iOS fills with black. The icon must be a 1024×1024 PNG without transparency; the project card's settings warnings say which of these is wrong.

To fix either, open the project card's More (⋮) › Set icon…, choose any picture and press Set icon. It saves a correct PNG and sets `iconPath` for you. Run the app again to see the new icon. See [Set the app icon](guide.md#set-the-app-icon).

### The German preset still shows English

A run preset's language only changes text the app has translated. `-AppleLanguages (de)` asks for German; if the app has no German localization (no `de.lproj` folder or German entries in its string catalog), iOS falls back to the app's own language, usually English. Dates, numbers and currency still follow the preset's region. Add the translations and run again.

If the app does have German, check that it was started by iOS Studio with the preset: the card says "Runs with German" and the build panel's launch step names it. Launch arguments only reach a fresh start of the app. iOS Studio closes a running copy before it launches, but opening the app from the home screen, or switching back to one you opened there, runs it without the preset. Press Run again, or Launch on Logs › Console. Your iPhone's own language never changes. See [features/run-presets.md](features/run-presets.md#things-to-know).

### "No run preset named '…'"

Run or an agent asked for a preset the project doesn't have (it was renamed or deleted, or the name is misspelled). The message lists the presets the project has; names don't depend on upper or lower case. The build doesn't start. In the app, pick one in Run's menu or choose Default (no preset). An agent calls `ios_project_presets(project_path)` for the names. With `ios_app_launch` or `ios_app_run_and_capture`, "No listed project builds …" or "Several projects build …" means the agent needs to pass `project_path` so the server knows whose presets to use.

### A run preset can't be saved

The error says which rule it breaks: a name longer than 40 characters or already used, more than 64 launch arguments or environment variables, an environment variable name with characters other than letters, digits and `_` (or starting with a digit), or a language or region that isn't a code like `de`, `pt-BR`, `zh-Hans` or `de_DE`, `en_GB`. A project can have 50 presets.

### A screenshot compare says "changed" but nothing seems different

Look at the diff image: the pink pixels are what changed. Usual causes are an animation or a spinner caught mid-move, a blinking cursor, a date or time inside the app (only the status bar is ignored), or content that loads late. Wait for the screen to settle before comparing, raise the threshold a little, or ignore a busy area (`--mask` for the helper). `size-mismatch` means the screenshot comes from another iPhone model or orientation than the baseline: save a baseline for that phone. Without Pillow a full-size compare takes a second or two; see [features/screenshot-compare.md](features/screenshot-compare.md#speed-and-pillow).

## Hot reload

### I saved, but the change didn't show up

Check the project card or Logs › Console first: if the reload failed, the message says why (the entries below). If it says it reloaded:

- The view has no `@ObserveHotReload var reload`. The new code is in the app, but SwiftUI only shows it when the view draws again. Add the property (and `import HotReload`), press Run again in the hot reload bar, and later saves redraw it.
- The change is in a view that isn't on screen, or in code that only runs once (an `init`, a `.task` or `.onAppear` that already ran, a `Task` or callback created before the reload). Those keep the old code until they run again: go back and forth to the screen.
- The reload counted functions it couldn't swap (`notSwapped` in the reload's details): the app calls them directly. Press Run again.
- The file isn't in the app's own module (code in a Swift package, a resource). Hot reload only swaps Swift files under the app's `Sources/` folder; press Run again. A Swift file in another target of the project gets its own message (below).

### "… This change needs a full build" (can't hot-reload)

A reload swaps functions; it can't change how values are laid out in memory. Adding or removing a type, a stored property (`@State` and other property wrappers too), an enum case, a method of a non-final class (`class var` too), a conformance or a file is refused, and so is changing the type of a static or global stored property or changing a type alias, and so is a `body` that returns a different kind of view without `.hotReloadable()`. The message names the change, for example "Adds the stored property count to ContentView." Press Run again: it builds and installs the app with the change, and the next saves reload from there. See [features/hot-reload.md](features/hot-reload.md#what-cant-be-hot-reloaded) for every kind.

### "Your app didn't take the change: it isn't running on your iPhone"

The patch reached the phone, but the app didn't answer within 5 s. It's closed, iOS suspended it in the background, or the app on the phone is a build without hot reload (installed from somewhere else, or before you turned it on). Open the app on the phone and save again: each reload has all your changes, so nothing is lost. If it's open and still doesn't answer, press Run again.

### "The app couldn't load the change" (load-failed)

The app got the patch but iOS refused to load it. The message ends with the system's reason. Most often it's code signing: iOS only loads a patch signed by the same team as the app, so it fails when the app on the phone was signed with another Apple ID. Press Run again so the app and its patches are signed with the Apple ID you're signed in with now. If it keeps failing, check the Apple ID page for a revoked or expired certificate.

### "Can't check ContentView.swift against the running app. Press Run to rebuild."

The object file the last build made for that file is missing from the hot reload cache, so iOS Studio can't compare the change with what the app on the phone runs, and it won't send something it hasn't checked. Press Run again: the new build records it again.

### "ContentView.swift changed while building. Press Run to include it."

You changed a file while the session's first build ran, and it's one iOS Studio could only copy after the build (a very large module, or a file outside `Sources/`), so it can't tell which version the app was built from. Press Run again. Files under `Sources/` that you save during the build are simply reloaded once it's done.

### "Late.swift is a new file: new files need a full build."

A reload can't add a file to the app. That includes a Swift file you created while the session's first build was running: the build may already have planned its files, so the app on the phone doesn't have it even if the build finished after you made it. Press Run again: the new build includes the file, and saves reload from there. Swift files that were in the module's folder before the build but aren't compiled (left out with `exclude:`, resources) never count as new.

### "Hot reload only updates the app target. Changes in … need a full Run."

The file you saved is in another target of the project (a library under `Sources/<Target>/`). Hot reload only patches the app's own module; press Run again to build the change into the app.

### "… now needs more memory per call than the running app reserved. Press Run to rebuild."

An async function's new body keeps more values alive across an `await` than the version the app was built with, and callers set aside room for the old size, so the app refuses the patch (`load-failed`). Press Run again. Changes that keep the function as small, or make it smaller, reload fine.

### "Too many reloads in this run (100). Press Run to start fresh."

Each reload stays loaded in the app until it quits, so the runtime takes at most 100 per run. Press Run again and keep going.

### "The hot reload lines at the end of Package.swift were changed by hand"

iOS Studio only edits the lines between `// >>> iOS Studio hot reload` and `// <<< iOS Studio hot reload`, and won't touch them when the end marker is gone. Put back the `// <<< iOS Studio hot reload` line after the block, or delete the lines from `// >>> iOS Studio hot reload` on, then turn hot reload on or off again.

## Setup and updates

### Swift broke after a system update

iOS Studio pins Swift (the AUR package `swift-bin`) to the version that matches the iOS SDK. The Setup step "Keep Swift on this version" adds `swift-bin` to pacman's `IgnorePkg`, and installs an Omarchy hook that puts it back after `omarchy refresh pacman`. If Swift changed anyway, open Setup: affected steps show Repair. The xtool and "Swift build settings fix" steps often need a repair after a Swift change.

### "Another program is installing system packages right now"

A system update or another install is running. Wait for it to finish, then press the step's button again.

### "Not enough free disk space"

Building the iOS SDK needs about 15 GB free while it runs. Free space with Toolchain › Disk usage (see [Free space](guide.md#free-space)), or elsewhere on your disk.

### The iPhone tools need repair

pymobiledevice3 lives in its own Python environment. A Python update can break it. Open Setup and press Repair on "iPhone connection tools".

## AI agents

### The agent doesn't see iOS Studio's tools

Start a new session in the agent after you connect it; most agents only load MCP servers at startup. Then press the test tube next to the tool in Settings › AI agents. It starts the server the way your agent will and tells you how many tools it found.

### "Needs update" next to an agent

The agent is registered with a server path that isn't this copy of iOS Studio, usually because the app folder moved. Press Update.

### "couldn't be read" next to an agent

iOS Studio found a mistake in that agent's config file and won't edit it. Fix the file by hand (the message names it), then press Check again.

### Prepare fails

Preparing the server downloads its Python packages, so it needs internet. [mcp.md](mcp.md#troubleshooting) shows how to run the server by hand and see the error.

## For developers

The app writes its errors to the Quickshell log of the running instance:

```bash
ios-studio --log        # add -f to follow, -t 50 for the last 50 lines
```

That's `qs log -p <app folder>`; the app folder is shown in Settings › About.

See [development.md](development.md) for how the pieces fit together.
