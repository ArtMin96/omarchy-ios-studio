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

A phone has to be registered with your account before apps can be signed for it. Connect it with a cable and press Run once. That registers it.

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

### "This app doesn't share files"

App Store apps only show their Documents folder when they turn on file sharing, and this one doesn't. Your own apps, built in iOS Studio, always show their whole container.

### "Packet capture needs a USB cable"

The iPhone only shares its network traffic over a cable. Plug it in; the Start capture button turns on by itself.

### A network condition is marked "USB only", or asks to confirm over Wi-Fi

Over Wi-Fi, a network condition also slows down this computer's own link to the phone. 100% packet loss and Very poor network would cut that link off, so they can't be switched off again over Wi-Fi and need a USB cable. The other network conditions ask you to confirm first, because iOS Studio may lose the phone for a while (the condition then switches off by itself). Heat and GPU states work the same over Wi-Fi and USB.

### No hangs in the Performance report

The phone only reports hangs when Hang Detection is on. On the iPhone, open Settings › Developer › Hang Detection and switch it on.

### "The debugger needs Python 3.12, which isn't installed"

The debugger is lldb from the Swift toolchain, and it needs Python 3.12. Install "Debugger support" in Setup; it builds Python from source, so it takes a while. If the message says lldb itself is missing or didn't start, install or repair Swift in Setup.

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
