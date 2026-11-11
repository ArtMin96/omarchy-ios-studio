# Hot reload on the iPhone

Hot reload puts the code you save into the app that's already running on your iPhone, without building, installing and launching it again. Change a SwiftUI view's text, padding or layout, save, and the phone shows it a couple of seconds later, with the app still on the same screen and its state kept.

Linux has no SwiftUI previews and no Simulator, so without this every UI tweak means a full Run: build, sign, install, open the app and tap back to the screen you were on. Hot reload is the closest substitute. It works like InjectionIII: the changed files are compiled into a small library, signed with your development certificate, copied into the app's folder on the phone, and a little runtime inside the app loads it and points the app's functions at the new code.

It's off by default and turned on per project. Only Swift code under `Sources/` is reloaded; anything that changes how values are laid out in memory needs a normal Run (see [What can't be hot-reloaded](#what-cant-be-hot-reloaded)).

## Turning it on and off

<!-- TODO(verify): the switch's name, place and confirm dialog once the UI branch lands -->
Each project card has a Hot reload switch. Turning it on sets the project up (below) and starts a session: iOS Studio builds the app, installs it, opens it and then watches your files. Turning it off stops a running session first, then takes hot reload out of the project again. Agents do the same with the tools in [Agent tools](#agent-tools).

Turning it on changes two things in the project:

1. It copies a small Swift package, the hot reload runtime, into `.iosstudio/hot-reload/`, and writes `.iosstudio/.gitignore` containing `*`, so git ignores the whole folder.
2. It adds this block to the end of `Package.swift` (here for an app target called `HabitTracker`):

```swift
// >>> iOS Studio hot reload (managed by iOS Studio: turn hot reload off there to remove this)
package.dependencies.append(.package(path: ".iosstudio/hot-reload"))
for target in package.targets where target.name == "HabitTracker" {
    target.dependencies.append(.product(name: "HotReload", package: "hot-reload"))
    target.linkerSettings = (target.linkerSettings ?? []) + [
        .unsafeFlags(["-Xlinker", "-interposable", "-Xlinker", "-u", "-Xlinker", "_iosstudio_hot_reload_loader"],
                     .when(configuration: .debug)),
    ]
}
// <<< iOS Studio hot reload
```

The block links the `HotReload` library into the app and adds two linker flags. `-interposable` makes the app call its own functions through a table of pointers that the runtime can rewrite; `-u _iosstudio_hot_reload_loader` makes sure the runtime is linked in even though nothing calls it. Nothing else in `Package.swift` changes, and removing the block gives back the file byte for byte. The app target is found the way [Packages](packages.md) finds it.

Every change is checked first: iOS Studio runs `swift package dump-package` on a temporary copy of the new `Package.swift`, and only writes the real file (in one step) when that works and the app target links `HotReload`. If it doesn't, nothing is changed and the error says why. While it runs it holds the same per-project lock as package changes, so the two never edit `Package.swift` at once.

Turning it off depends on your code:

- If no Swift file under `Sources/` has `import HotReload`, the block and `.iosstudio/hot-reload/` are removed (mode `off`).
- If some do, the block stays without its `linkerSettings` lines, and `.iosstudio/hot-reload/` gets the stub: the same `HotReload` module and API, but every call does nothing. That's mode `stub`. Your project keeps building, with no runtime and no linker flags in the app. The reply lists the files that import it (`importedBy`), so you can remove the imports and turn it off again to get to `off`.

The runtime copy carries a version (`templates/hot-reload/version.txt` in iOS Studio). When iOS Studio comes with a newer runtime, turning hot reload on, or starting a session, copies the new one over the old.

## Using it in your code

A reload swaps the code, but SwiftUI only shows the new `body` when the view draws again. Two small additions make that happen:

```swift
import SwiftUI
import HotReload

struct ContentView: View {
    @ObserveHotReload var reload

    var body: some View {
        VStack(spacing: 12) {
            Text("Today's habits")
                .font(.title)
            HabitList()
        }
        .padding()
        .hotReloadable()
    }
}
```

- `@ObserveHotReload var reload` redraws the view after each reload. You don't have to read `reload` (it's the number of reloads so far).
- `.hotReloadable()` at the end of `body` wraps the view in `AnyView`, so a reload may change what kind of view `body` returns: add a `VStack`, swap a `Text` for a `Label`, add a modifier. Without it, iOS Studio refuses changes like that (see `opaque-type` below), because the app has the old view type built in and swapping a different one in would corrupt memory.

Put both in each view you're changing. Adding them is itself a change a reload can't apply (`@ObserveHotReload` is a stored property, and `.hotReloadable()` changes the view type), so press Run once after adding them.

Without them, a reload still replaces the code, but you only see it when the view redraws for another reason (you tap something, its state changes, you go back and forth), and changes to a `body`'s view structure are refused.

For UIKit code, `HotReload.onReload { … }` runs a closure on the main thread after each reload, for as long as you keep the token it returns. `HotReload.isActive` says whether the runtime is running (a debug build with hot reload on), and `HotReload.generation` counts the reloads since launch.

In a release build, and with the stub, all of these do nothing: `@ObserveHotReload` never fires, `.hotReloadable()` still returns `AnyView`, `isActive` is `false`.

## Start, stop, pause and resume

<!-- TODO(verify): the controls' names and places on the card once the UI branch lands -->
The switch is the setting. A session is the running part: it's what builds the app, watches your files and sends each change. Turning the switch on starts one. When the switch is on but no session runs (after you reopen iOS Studio, or a session stopped), Start begins one again, and Stop ends it without turning hot reload off.

Pause keeps the session and the app running but stops sending changes. Saves are still noticed and counted ("Paused: 2 saved files wait for resume"). Resume sends them all at once in one reload. Pausing is handy when you're halfway through an edit that won't compile yet, or want to make several changes and see them together.

A session ends when you stop it, when you turn hot reload off, when the app is removed from the phone, when its first build fails, or when the program that started it closes (the iOS Studio window, or the agent's MCP server). One project has at most one session; different projects can each have one.

Starting a session stops [watch mode](watch-mode.md) for that project, since watch mode's auto-run would install the app over the running one. The session says so in a notice.

## What happens on each save

When a session starts, it:

1. checks that hot reload is on for the project (else `not-enabled`), that xtool and rcodesign are installed, that you're signed in to an Apple ID and that an iPhone is connected;
2. runs the same build as the Run button (with the project's selected run preset), which shows in the build history like any other build;
3. after the install, deletes old patches from the app's folder on the phone (the runtime also clears them at each launch) and opens the app with the console attached, so the runtime's `[HotReload]` lines show in Logs › Console;
4. records the baseline: the exact compile command of each Swift file, the app's link command, copies of the sources and object files as built, and the app's executable;
5. compiles one file in the background, so the compiler's module cache is warm before your first save;
6. waits up to 10 s for the app's runtime to say it's running, then watches `Sources/`. If the runtime never answers, the session reports `app-not-running` and keeps watching.

Saves made while that first build runs end up in the baseline, so they aren't reloaded: change the file again, or press Run.

Then each save, once you stop saving for 150 ms (and at least every 2 s if saves never pause), makes one reload:

| Step | What it does |
|---|---|
| check | compares each changed file with the baseline and refuses changes a reload can't apply (below) |
| compile | compiles each changed file with its baseline command, up to 4 at a time |
| check, again | compares the compiled `body` view types with the baseline's, and checks that the app calls the changed functions through the pointer table |
| link | links them into `patch-<n>.dylib` with `ld64.lld` (through the Swift driver if that fails) |
| sign | signs it with your development certificate (rcodesign; the private key goes through a pipe and never touches the disk) |
| push | copies it to the app's `Library/Caches/HotReload/` on the phone and waits up to 5 s for the app's answer |
| apply | the runtime in the app loads it, points the app at the new functions, and redraws the `@ObserveHotReload` views |

Each patch holds every file that differs from the baseline, not only the last one you saved, so the newest patch alone has all your changes and nothing is lost if one fails. A save during a reload makes exactly one more reload after it. A file you put back the way it was built is taken out of the next patch, and its functions go back to the app's own code (`reverted`). When every file matches the build again, nothing is sent ("Back to the built version"); if an earlier reload had swapped something, a `back-to-build` notice says to press Run to start the app fresh. Patch numbers keep counting across sessions of a project, so a new session never reuses an old number.

Saving `Package.swift`, `xtool.yml` or another file outside Swift sources doesn't reload; the session shows a notice that it needs Run.

Measured on the CrashLab test app on a busy machine: check 0.04 s, compile 0.63 s, link 0.93 s and sign 0.02 s for a warm reload, so about 1.6 s on the computer. The first reload after the warm-up took about 4 s. The warm-up itself took 16 s (up to 110 s when the machine was overloaded), and recording the baseline 0.7 s. <!-- TODO(verify): push and apply times, and the whole save-to-screen time, measured on a real iPhone -->

The runtime logs one line per reload, for example `[HotReload] patch 3: swapped 4 functions (12 kept) in 3.1 ms`, to Logs › Console and Logs › Device log.

## What can't be hot-reloaded

A reload swaps functions. It can't change how a value is laid out in memory, which slots a class's method table has, or what type a `some View` stands for, because the running app already has values and code built around the old layout. iOS Studio checks each change before sending it and refuses these with a plain message and a Run again button. Run again builds and installs the app with your changes and starts a new baseline.

What's fine: function and method bodies, computed properties (including `body`), new functions and methods, and extensions without new conformances.

What's refused, by `kind` in `lastError.blockers`:

| Kind | The change |
|---|---|
| `new-type` | adds a struct, class, enum, actor or protocol |
| `removed-type` | removes one |
| `stored-property` | adds, removes, reorders or changes the type of a stored property, `@State` and other property wrappers included |
| `enum-case` | adds, removes or reorders an enum case |
| `class-method` | adds or removes an overridable member of a non-final class (it changes the class's method table), or a protocol requirement |
| `generic-signature` | changes a type's declaration: its generic parameters, superclass or conformances |
| `opaque-type` | a `some View` (or other `some`) result now has a different concrete type; end `body` with `.hotReloadable()` and Run once |
| `new-file` | adds a Swift file to the module |
| `removed-file` | deletes one |
| `unparsed` | iOS Studio couldn't read the file well enough to be sure, so it doesn't guess |

The message names the first one, for example "Adds the stored property count to ContentView. This change needs a full build." The reply lists up to 10.

Two more cases need Run: an app on the phone that wasn't built with hot reload on (`not-interposable`), and a change whose functions the app calls directly rather than through the pointer table. The second isn't an error: the reload goes through, and `lastReload.notSwapped` counts the functions whose change won't show.

## Errors

A failed reload doesn't end the session: the state becomes `failed`, `lastError` says why, and the next save tries again with all your changes. Every error has a plain message and, where it helps, a button.

| Code | What it means | What to do |
|---|---|---|
| `unsupported-change` | the change can't be swapped in (the blockers above, or a deleted file) | Run again |
| `new-file` | a Swift file was added | Run again |
| `compile-failed` | a changed file doesn't compile; `diagnostics` has up to 5, errors first, with file and line | fix the code and save |
| `link-failed` | the patch couldn't be linked | Run again |
| `sign-failed` | rcodesign couldn't sign the patch with your certificate | check the Apple ID page, then Run again |
| `not-signed-in` | no development certificate for the app's team | sign in on the Apple ID page |
| `not-interposable` | the app was built without hot reload | Run again |
| `app-not-running` | the app didn't answer within 5 s: it isn't open (or iOS suspended it in the background), or the installed build has no runtime | open the app on the phone and save again, or Run again |
| `load-failed` | the app got the patch but couldn't load it; the message has the system's reason (`dlerror`), often code signing or library validation | Run again; see [troubleshooting](../troubleshooting.md#the-app-couldnt-load-the-change-load-failed) |
| `no-baseline` | the records of the last build are missing or unreadable | Run again |
| `not-enabled` | hot reload is off for the project | turn it on |
| `no-device` | no iPhone connected | connect it |
| `missing-tool` | xtool or rcodesign isn't installed | Setup installs both |
| `busy` | a session already runs for this project | stop it first |

A session that can't start ends with `stopReason`: `build-failed` (the build's own errors are in Problems and the build log) or `error`. If the app is removed from the phone while a session runs, it ends with `app-exited`.

Turning hot reload on or off can fail with `manifest-invalid` (the new `Package.swift` wouldn't work, so nothing changed), `manifest-unsupported` (the package isn't declared as `let package = Package(…)`), `no-app-target`, `block-damaged` (the managed lines were edited by hand: put back the `// <<< iOS Studio hot reload` line, or delete the block from `// >>>` on), `changed` (`Package.swift` changed meanwhile), `package-busy` (a package change is running) or `missing-runtime` (iOS Studio's own runtime files are missing: update iOS Studio).

## Release builds: turn it off first

Turn hot reload off before you build for TestFlight or the App Store. The block asks for the linker flags in debug builds only (`.when(configuration: .debug)`), but the build system xtool uses ignores that condition, so a release build while hot reload is on is still linked with `-interposable` and keeps the runtime's marker symbol. The runtime's code itself is debug only (the package compiles it under its own `IOSSTUDIO_HOT_RELOAD` define, set for debug), so a release build doesn't watch or load anything, but it shouldn't ship with those flags. Turning hot reload off removes them, also in `stub` mode.

## Files

In the project:

| Path | What |
|---|---|
| `Package.swift` | the managed block at the end (above) |
| `.iosstudio/hot-reload/` | the runtime package (`full`) or the stub, with `.iosstudio-runtime.json` (`{version, kind}`) |
| `.iosstudio/.gitignore` | `*`: git ignores the folder |

On the computer, in `~/.cache/ios-studio/hot-reload/<key>/` (key = the first 12 hex digits of the SHA-1 of the project path):

| Path | What |
|---|---|
| `baseline.json` | the module, each file's compile command, the link command, the app's binary and bundle ID, the team |
| `sources/` | the Swift files as they were built |
| `objects/` | their object files from that build |
| `app.bin` | the built app's executable |
| `patches/` | the signed patches; the newest 10 are kept |

The whole `hot-reload/` folder is kept under 300 MB: when a session starts, the oldest projects' folders go first. A new baseline replaces the old one at each session start.

In `$XDG_RUNTIME_DIR/ios-studio/` (the same base as [watch mode](watch-mode.md#files); demo mode uses `ios-studio-demo/`):

| File | What |
|---|---|
| `hotreload-<key>.lock` | `flock` held by the session while it runs, its pid inside |
| `hotreload-<key>.json` | the latest snapshot and the last 20 reloads (`history`); kept after the session stops |
| `hotreload-<key>-control.json` | pause or resume, read by the session on `SIGUSR1` |

A session that died without saying so (killed, crashed) is recorded as stopped with `stopReason: "session-died"` by the next `status`. Files of sessions stopped more than 24 hours ago are deleted when a session starts.

On the iPhone, in the app's `Library/Caches/HotReload/`: `runtime.json` (written by the runtime at each launch), `patch-<n>.dylib` and `ack-<n>.json` (the app's answer). The runtime deletes every patch and answer when the app launches and keeps only the newest 3 after each reload, and a session clears the folder when it starts. The folder is in the app's Caches, so iOS may also clear it, and it goes away with the app. Loaded patches stay in the app's memory until it quits (a few hundred KB each).

The format of those files and how the runtime swaps functions are in [templates/hot-reload/README.md](../../templates/hot-reload/README.md).

## Helper commands

```
iosstudio-helper hotreload enable  --project P [--dry-run] [--max-bytes N]
iosstudio-helper hotreload disable --project P [--dry-run] [--max-bytes N]
iosstudio-helper hotreload run     --project P [--udid U] [--preset ID] [--debounce-ms 150]   # streaming
iosstudio-helper hotreload status  [--project P] [--wait-seconds N] [--after-seq S]
iosstudio-helper hotreload stop    --project P
iosstudio-helper hotreload pause   --project P
iosstudio-helper hotreload resume  --project P
```

`enable` returns `{enabled: true, mode: "on", appTarget, changed, diff, truncated, fullDiffPath?, packagePath, dryRun, runtimeUpdated}`. `disable` returns `{enabled: false, mode: "off" | "stub", importedBy, changed, diff, …}` and, when a session was running, stops it first (`sessionStopped`). `diff` is the unified diff of `Package.swift`, cut at `--max-bytes` (12,000 by default); the whole diff is then saved in `~/.cache/ios-studio/packages/diffs/` (`fullDiffPath`). `--dry-run` returns the diff and changes nothing.

`run` streams one `hotreload` event per snapshot and ends with `{"event": "done", "ok": true, "data": <the last snapshot with history>}` when it was stopped or the app went away, or `ok: false` with `error` (the `lastError`) when it couldn't start. `--debounce-ms` takes 50 to 5,000. Written to its stdin, `{"cancel": true}` or `{"cmd": "stop"}` stops it, and `{"cmd": "pause"}` / `{"cmd": "resume"}` pause and resume; SIGTERM, SIGINT and SIGHUP stop it too. A second `run` for the same project fails with `busy`.

`status --project P` returns `{project, enabled, mode, session}` (`session` is null when it never ran, and has `running`). With `--wait-seconds N` (at most 25) it returns as soon as a reload newer than `--after-seq` finishes (by default, newer than the newest one finished now), or the session stops, or N seconds pass (`timedOut`); pass the newest `seq` you saw, so a reload that finished just before the call isn't missed. Without `--project` it returns `{sessions, projects}`: the running sessions and those stopped in the last 10 minutes, and `{path: {enabled, mode}}` for every listed project (read from `Package.swift`, no Swift call).

`stop` sends SIGTERM and waits up to 5 s before killing the session: `{stopped, session}`. `pause` and `resume` return `{paused, session}`, or `not-running` when no session runs.

A snapshot (the `hotreload` event, the state file and `status`):

```json
{"event": "hotreload", "seq": 41, "state": "watching", "message": "Watching for saves",
 "project": "HabitTracker", "path": "/home/me/Developer/HabitTracker", "bundleId": "dev.me.habittracker",
 "pid": 4242, "udid": "00008110-…", "debounceMs": 150, "startedAt": 1793991966.1, "updatedAt": 1793992104.3,
 "buildId": "hr-1793991966", "reloads": 2, "patchSeq": 3, "paused": false, "pending": [], "pendingCount": 0,
 "notice": null, "progress": null,
 "lastReload": {"seq": 3, "ok": true, "files": ["Sources/HabitTracker/ContentView.swift"], "fileCount": 1,
                "ms": 2100, "steps": {"check": 40, "compile": 630, "link": 930, "sign": 20, "push": 420, "apply": 3.1},
                "swapped": 4, "reverted": 0, "vtable": 0, "notSwapped": 0, "time": 1793992102.0},
 "lastError": null, "stopReason": null}
```

- `state`: `starting`, `building`, `installing`, `launching`, `watching`, `paused`, `reloading`, `reloaded` (for 1.5 s, then `watching`), `failed` (the last reload failed; still watching) and `stopped`.
- `progress`: while building, `{fraction, detail}`; while reloading, `{step, detail}` with `step` one of `check`, `compile`, `link`, `sign`, `push`.
- `lastReload.steps` are milliseconds; `push` is the copy plus the wait for the answer, `apply` the runtime's own time in the app. `swapped` counts the functions the app now runs from the patch, `reverted` those put back to the app's code, `vtable` the class method entries rewritten.
- `lastError`: `{code, message, fix?, file?, line?, blockers? (≤ 10), diagnostics? (≤ 5), seq, time}`.
- `notice`: `{id, level, message, fix?, time}`, one of `runtime-updated` (the project's runtime copy was refreshed), `watch-stopped`, `needs-run` (`Package.swift`, `xtool.yml` or a resource changed: those need Run), `back-to-build` and `polling` (inotify isn't available, so files are checked every second).
- `stopReason` (with `stoppedAt`): `stopped`, `app-exited`, `build-failed`, `error`, `session-died`.

The session's build is in the build history with `trigger: "hot-reload"` and a `buildId` of `hr-<time>`.

The device commands it uses (`hotreload.push`, `hotreload.clean`, `hotreload.runtime`) are in [deviced-protocol.md](../deviced-protocol.md#hot-reload-hotreload-used-by-the-hotreload-helper-groups-session).

## Agent tools

AI agents get four tools (details in [mcp.md](../mcp.md)):

- `ios_hot_reload_set(project_path, enabled, dry_run=false)` turns hot reload on or off for a project and returns `mode` and the `Package.swift` diff; `dry_run=true` only shows it. Turning it off stops a running session first.
- `ios_hot_reload_start(project_path, udid?, preset?, restart=false, job_id?)` runs a session as a job: build, install, launch, then watching. It returns once the session is watching, or a `job_id` to call again while it builds. If the app window already runs a session for the project, it reports that one instead of starting a second; `restart=true` stops it and builds again, which is the Run again of the app.
- `ios_hot_reload_status(project_path?, after_seq?, wait_seconds=0, history=5)` returns the state, the last reload with its step timings, or the last error with its blockers and compiler errors. Each result has `last_seq`: after saving a file, pass it as `after_seq` with `wait_seconds=20` (at most 25) and the call returns when the next reload has finished. Without `project_path` it lists the sessions and the projects with hot reload on.
- `ios_hot_reload_control(project_path, action)` pauses, resumes or stops a session, also one the app window started.

A session an agent started stops when its job is cancelled or the agent's MCP server exits. Results never carry images.

## Limits

<!-- TODO(verify): the app-side loading (the runtime starting, dlopen of a signed patch from Caches, the swap, the redraw) is not confirmed on a real iPhone yet; update after the CrashLab test -->
- At the time of writing, everything up to the patch arriving on the phone is tested, but the app side (the runtime loading a patch and swapping functions on a real iPhone) hasn't been confirmed on a device yet.
- Only Swift code under `Sources/` in the app's own module. Code in Swift packages, resources, asset catalogs, `Info.plist` and `xtool.yml` need Run.
- Code that's already running keeps the old version until it's called again: a running `Task`, closures and callbacks stored before the reload, and protocol witness tables built elsewhere.
- Static initializers in a patch run again when it loads, and an Objective-C class defined in a changed file is registered twice (the system logs "implemented in both").
- `private` and `fileprivate` functions change through the functions that call them, so the change shows once a caller is in the patch too (it is, when they're in the same file).
- arm64 only. Your own development builds only, signed with the same team as the app.
- The app has to be running to load a patch. iOS may suspend an app in the background, and then it can't answer within the 5 s (`app-not-running`); keep it open on the phone while you work.
- Release builds while it's on: see [above](#release-builds-turn-it-off-first).

## Demo mode

With `IOSSTUDIO_DEMO` set, `enable` and `disable` answer with the real block's diff against a made-up `Package.swift` and write nothing; WeatherGlance's views `import HotReload`, so turning it off there shows `stub`. `run` plays a scripted session in the demo runtime folder without building or sending anything: starting, building (with progress), installing, launching, watching, then a save of `ContentView.swift` every few seconds, alternating a reload that works (about 2.4 s, steps filled in) and one refused with `unsupported-change` (a stored property `count` added to `ContentView`). Pause collects the scripted saves as pending; resume reloads them at once. `status`, `stop`, `pause` and `resume` are the real commands. The demo iPhone's `hotreload.push` answers after about 300 ms with 3 functions swapped.

## Code

- `templates/hot-reload/`: the runtime package copied into projects, `full/` (a C loader, `HotReloadLoader`, and the Swift API, `HotReload`) and `stub/` (the same API as no-ops), with `version.txt`. Its [README](../../templates/hot-reload/README.md) explains the on-device files and the rebinding.
- `helper/iosstudio/features/hotreload/`: `commands.py` (the `hotreload` group), `project_setup.py` (the `Package.swift` block and the runtime copy), `session.py` (a session: start, scheduler, reloads), `state.py` (the runtime files, status, stop, pause and resume), `buildinfo.py` (the baseline from xtool's build records and its cache), `patch.py` (check, compile, link, sign), `checks.py` (what a reload can't change), `macho.py` (a small Mach-O reader), `signing.py` (rcodesign with xtool's identity).
- `helper/iosstudio/device/features/hotreload/`: `hotreload.push`, `clean` and `runtime` in `real.py` and `demo.py`, shared parsing in `core.py`.
- `helper/iosstudio/demo/hot_reload.py`: demo answers and the scripted session.
- `mcp/iosstudio_mcp/tools/hot_reload.py`: the four agent tools; a session is a job that keeps the newest snapshot.
<!-- TODO(verify): the UI files once the UI branch lands -->
- UI: `services/HotReloadService.qml` and `components/projects/HotReload*.qml`.

## Tests

<!-- TODO(verify): the UI tests (tests/qml) once the UI branch lands -->
- `tests/helper/test_hot_reload_setup.py` and `tests/cli/test_hot_reload_setup_cli.py`: the block added and removed byte for byte, `off` and `stub`, the runtime copy, dry runs, a manifest that wouldn't work.
- `tests/helper/test_hot_reload_buildinfo.py`, `test_hot_reload_patch.py`, `test_hot_reload_checks.py`, `test_hot_reload_macho.py` and `test_hot_reload_signing.py`: the pipeline against a fake xtool build and fake swiftc, ld64.lld and rcodesign, on small real object files in `tests/fixtures/hot-reload/`. Signing uses throwaway keys made with openssl and checks that no key reaches the disk.
- `tests/helper/test_hot_reload_session.py` and `tests/cli/test_hot_reload_session_cli.py`: the scheduler, error mapping, pause and resume, the runtime files, and a real-mode session with a fake xtool and a fake device daemon.
- `tests/helper/test_dev_hotreload.py`: the device feature with a fake house_arrest at the pymobiledevice3 boundary, and its demo twin.
- `tests/cli/test_hot_reload_runtime.py`: the runtime package's layout and the same API in full and stub; with the iOS SDK installed, both build for iOS.
- `tests/mcp/test_mcp_hot_reload.py`: the agent tools against a fake helper (with a fake `hotreload run` stream), and with the real helper's demo session: start, waiting for the next reload, pause, resume, stop.
- `tests/helper/test_hot_reload_e2e.py` (slow): the whole pipeline with the real toolchain on a copy of `~/Projects/CrashLab`; skipped without it, xtool, the SDK, openssl or rcodesign.

See [development.md](../development.md#tests) for how to run them and the test hooks.
