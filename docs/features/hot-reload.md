# Hot reload on the iPhone

Hot reload puts the code you save into the app that's already running on your iPhone, without building, installing and launching it again. Change a SwiftUI view's text, padding or layout, save, and the phone shows it a couple of seconds later, with the app still on the same screen and its state kept.

Linux has no SwiftUI previews and no Simulator, so without this every UI tweak means a full Run: build, sign, install, open the app and tap back to the screen you were on. Hot reload is the closest substitute. It works like InjectionIII: the changed files are compiled into a small library, signed with your development certificate, copied into the app's folder on the phone, and a little runtime inside the app loads it and points the app's functions at the new code.

It's off by default and turned on per project. Only Swift code under `Sources/` is reloaded; anything that changes how values are laid out in memory needs a new build, which Run again in the hot reload bar does (see [What can't be hot-reloaded](#what-cant-be-hot-reloaded)).

## Turning it on and off

Each project card has a Hot reload switch in its right-hand buttons, just before ⋮ More; `H` toggles it on the highlighted card. The flash glyph, the label and the switch are one click target, with a tooltip, and there's no confirm dialog. On cards 640 to 900 px wide the word "Hot reload" is hidden and the glyph and switch stay.

Turning it on sets the project up (below) and starts a session at once: iOS Studio builds the app, installs it, opens it and then watches your files. The session doesn't start when the project has no bundle ID, when its `xtool.yml` is generated and Build settings don't allow that, when no iPhone is connected, or while the project is already building; hot reload is still on then, and the bar's Start button, dimmed, says why in its tooltip. Turning it off stops a running session first, then takes hot reload out of the project again. Agents do the same with the tools in [Agent tools](#agent-tools).

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
- If some do, the block stays without its `linkerSettings` lines, and `.iosstudio/hot-reload/` gets the stub: the same `HotReload` module and API, but every call does nothing. That's mode `stub`. Your project keeps building, with no runtime and no linker flags in the app. The card then shows a quiet note listing the files that still import it (`importedBy`, also in `hotreload status`); once you've removed the imports, Clean up runs the turn-off again and gets you to `off`. Clean up shows busy while it runs; if some files still import `HotReload`, its message names them.

The runtime copy carries a version (`templates/hot-reload/version.txt` in iOS Studio, now 2). When iOS Studio comes with a newer runtime, turning hot reload on, or starting a session, copies the new one over the old.

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

Put both in each view you're changing. Adding them is itself a change a reload can't apply (`@ObserveHotReload` is a stored property, and `.hotReloadable()` changes the view type), so the first save after adding them is refused: press Run again in the hot reload bar.

Without them, a reload still replaces the code, but you only see it when the view redraws for another reason (you tap something, its state changes, you go back and forth), and changes to a `body`'s view structure are refused.

For UIKit code, `HotReload.onReload { … }` runs a closure on the main thread after each reload, for as long as you keep the token it returns. `HotReload.isActive` says whether the runtime is running (a debug build with hot reload on), and `HotReload.generation` counts the reloads since launch.

In a release build, and with the stub, all of these do nothing: `@ObserveHotReload` never fires, `.hotReloadable()` still returns `AnyView`, `isActive` is `false`.

## Start, stop, pause and resume

The switch is the setting. A session is the running part: it's what builds the app, watches your files and sends each change. Turning the switch on starts one.

While hot reload is on, a bar under the project's details (also when the card is collapsed) shows the session: a state pill (Building, Watching, Reloading, Reloaded, Paused, Reload failed, Stopped), a line saying what happened, such as "Reloaded ContentView.swift · 2.2 s · 01:57:19 · just now", and the buttons Pause or Resume and Stop or Start. The bar keeps one height while it's watching, reloading or reloaded, so it doesn't jump on each save, and below 640 px its buttons go under the text. Stop ends the session without turning hot reload off; the app keeps running the last reload. Stop also works while the session is still starting (the bar then says "Stopping"). When the switch is on but no session runs (after you reopen iOS Studio, or a session stopped), the bar says "Hot reload is on, but not running" and Start begins one again. A button that can't be used yet stays visible, dimmed, with the reason in its tooltip.

Pause keeps the session and the app running but stops sending changes. Saves are still noticed and counted: the bar shows "2 saved files waiting" and "ContentView.swift, ForecastView.swift reload together when you resume." Resume sends them all at once in one reload. Pause and Resume become usable once the session is watching. Pausing is handy when you're halfway through an edit that won't compile yet, or want to make several changes and see them together.

A session ends when you stop it, when you turn hot reload off, when the app is removed from the phone, when its first build fails, or when the program that started it closes: quitting iOS Studio ends the sessions it started ("Stopped: the app or tool that started hot reload has closed"), and an agent's ends with its MCP server. One project has at most one session; different projects can each have one.

Starting a session stops [watch mode](watch-mode.md) for that project, since watch mode's auto-run would install the app over the running one. The session says so in a notice.

The switch shows what `hotreload status` says. When you flip it in this window, the card shows your choice until a status check that started after the change answers (one starts right away), and from then on the status decides, so a change an agent or the helper made meanwhile shows on the card.

## What happens on each save

When a session starts, it:

1. checks that hot reload is on for the project (else `not-enabled`), that xtool and rcodesign are installed, that you're signed in to an Apple ID and that an iPhone is connected;
2. takes a snapshot of the sources (a digest of each watched file and a copy of each Swift file under `Sources/`, up to 64 MB of copies), so it knows what the build reads;
3. runs the same build as the Run button (with the project's selected run preset), which shows in the build history like any other build. If the project has a prebuild command, the snapshot is taken again when the prebuild finishes and before xtool starts, so the files it generates or rewrites (Swift files, `xtool.yml`, a plist) count as built, not as saves made during the build;
4. after the install, deletes old patches from the app's folder on the phone (the runtime also clears them at each launch) and opens the app with the console attached, so the runtime's `[HotReload]` lines show in Logs › Console;
5. records the baseline: the exact compile command of each Swift file, the app's link command, the sources from the snapshot, the object files as built, and the app's executable. The app is the one this build left in the project's `xtool/` folder: a build over a cable leaves `xtool/<App>.app`, one over Wi-Fi only `xtool/<App>.ipa`, so the newer of the two that's newer than the snapshot is used, and an older one (left by an earlier build) never is. From a `.ipa`, only `Info.plist`, the executable and `embedded.mobileprovision` are unpacked, into a staging folder of the cache that's deleted right after; nothing is written into the project. If the build left no app newer than the snapshot, the session stops with `no-baseline`: "Couldn't find the app this build made in the project's xtool folder (only older builds are there). Press Run to build it again.";
6. compiles one file in the background, so the compiler's module cache is warm before your first save;
7. waits up to 10 s for the app's runtime to say it's running, then watches `Sources/`. If the runtime never answers, the session reports `app-not-running` and keeps watching.

Files you save while that first build runs differ from the snapshot, so they're reloaded right after it (notice `changed-while-building`). A file of the app's module the snapshot didn't copy (past its 64 MB, or outside `Sources/`) is copied after the build, so if it changed during the build iOS Studio can't tell what the build read; the session says "<file> changed while building. Press Run to include it." (notice `changed-during-build`).

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

Saving `Package.swift`, `xtool.yml` or another file outside Swift sources doesn't reload; the session shows a notice with a Run again button. So does a Swift file in another target, such as `Sources/HabitKit/`: "Hot reload only updates the app target. Changes in HabitKit need a full Run." (notice `other-target`).

Measured on the CrashLab test app on a busy machine: check 0.04 s, compile 0.63 s, link 0.93 s and sign 0.02 s for a warm reload, so about 1.6 s on the computer. The first reload after the warm-up took about 4 s. The warm-up itself took 16 s (up to 110 s when the machine was overloaded), and recording the baseline 0.7 s. <!-- TODO(verify): push and apply times, and the whole save-to-screen time, measured on a real iPhone -->

The runtime logs one line per reload, for example `[HotReload] patch 3: swapped 4 functions (12 kept) in 3.1 ms`, to Logs › Console and Logs › Device log.

## What can't be hot-reloaded

A reload swaps functions. It can't change how a value is laid out in memory, which slots a class's method table has, or what type a `some View` stands for, because the running app already has values and code built around the old layout. iOS Studio checks each change before sending it and refuses these with a plain message and a Run again button. Run again stops the session, builds and installs the app with your changes, and starts a new session from that build.

What's fine: function and method bodies, computed properties (including `body`), new functions and methods, and extensions without new conformances.

What's refused, by `kind` in `lastError.blockers`:

| Kind | The change |
|---|---|
| `new-type` | adds a struct, class, enum, actor or protocol |
| `removed-type` | removes one |
| `stored-property` | adds, removes, reorders or changes the type of a stored property, `@State` and other property wrappers included; changes the type of, or removes, a static or global stored property (new ones are fine). The type is the annotation, else what the initial value plainly is (a literal, an array or dictionary of them, `Type(…)`, `Type.member`): `let total = 3` to `"3"` is refused. When the type can't be told that way (`3 * 4`, a closure, a function call), any edit to an instance property's initial value is refused, because the running code reads fields at fixed places in memory, while a static or global one is sent |
| `enum-case` | adds, removes or reorders an enum case |
| `class-method` | adds or removes an overridable member of a non-final class (it changes the class's method table; `class func`, `class var` and `class subscript` included), or a protocol requirement (static requirements included) |
| `generic-signature` | changes a type's declaration: its generic parameters, superclass or conformances |
| `opaque-type` | a `some View` (or other `some`) result now has a different concrete type; end `body` with `.hotReloadable()` and Run again once |
| `type-alias` | changes or removes a type alias (new ones are fine) |
| `new-file` | adds a Swift file to the module |
| `removed-file` | deletes one |
| `unchecked` | the build's object file for a changed file is missing, so iOS Studio can't compare it with the running app: "Can't check ContentView.swift against the running app. Press Run to rebuild." |
| `unparsed` | iOS Studio couldn't read the file well enough to be sure, so it doesn't guess |

A new value for an existing static or global stored property is fine to send, but the running app keeps the old value (it was set once, when the app first used it); the new one shows after the next Run.

The message names the first one, for example "Adds the stored property count to ContentView. This change needs a full build." The reply lists up to 10, and `blockersTotal` counts them all.

A new file is a `.swift` file in the app module's folders that isn't compiled into the app and wasn't there when the build started (the snapshot lists what was there). That includes a file you create while the first build runs, even one the build was too far along to pick up. Swift files that were already there but aren't compiled (left out with `exclude:`, or resources) are ignored, even if you edit them later, and so are hidden folders (`.build`, `.iosstudio`, `.git`, `.swiftpm`), nested packages and `Package.swift`. Where the snapshot doesn't reach (a folder file watching ignores, such as `build/`, or a project with more than 5,000 watched files), a file counts as new when it was created or moved there after the build started.

Two more cases need Run again: an app on the phone that wasn't built with hot reload on (`not-interposable`), and a change whose functions the app calls directly rather than through the pointer table. The second isn't an error: the reload goes through, and `lastReload.notSwapped` counts the functions whose change won't show.

## Errors

A failed reload doesn't end the session: the state becomes `failed`, `lastError` says why, and the next save tries again with all your changes. Every error has a plain message and, where it helps, a button. On the card, the bar shows the message, up to 3 blockers as `file:line` (a click opens the line in your editor) with "and N more", or the compiler's first error, the fix button, and "Still watching: fix it and save again." When the window isn't focused, a failed reload also sends a desktop notification.

| Code | What it means | What to do |
|---|---|---|
| `unsupported-change` | the change can't be swapped in (the blockers above, or a deleted file) | Run again |
| `new-file` | a Swift file was added | Run again |
| `compile-failed` | a changed file doesn't compile; `diagnostics` has up to 5, errors first, with file and line (`diagnosticsTotal` counts them all) | fix the code and save |
| `link-failed` | the patch couldn't be linked | Run again |
| `sign-failed` | rcodesign couldn't sign the patch with your certificate | check the Apple ID page, then Run again |
| `not-signed-in` | no development certificate for the app's team | sign in on the Apple ID page |
| `not-interposable` | the app was built without hot reload | Run again |
| `app-not-running` | the app didn't answer within 5 s: it isn't open (or iOS suspended it in the background), or the installed build has no runtime | open the app on the phone and save again, or Run again |
| `load-failed` | the app got the patch but couldn't load it; the message has the system's reason (`dlerror`), often code signing or library validation, or the runtime's own (a bigger async function, too many reloads: see [Limits](#limits)) | Run again; see [troubleshooting](../troubleshooting.md#the-app-couldnt-load-the-change-load-failed) |
| `no-baseline` | the records of the last build are missing or unreadable, the build left no app in `xtool/` newer than its start, its `.ipa` can't be read (or an entry in it is over 256 MB), or the compiler's plan for the module is over 16 MB | Run again |
| `not-enabled` | hot reload is off for the project | turn it on |
| `no-device` | no iPhone connected | connect it |
| `missing-tool` | xtool or rcodesign isn't installed | Setup installs both |
| `busy` | a session already runs for this project | stop it first |

A session that can't start ends with `stopReason`: `build-failed` (the build's own errors are in Problems and the build log) or `error`. If the app is removed from the phone while a session runs, it ends with `app-exited`.

Turning hot reload on or off can fail with `manifest-invalid` (the new `Package.swift` wouldn't work, so `Package.swift` was left as it is), `manifest-unsupported` (the package isn't declared as `let package = Package(…)`), `no-app-target`, `block-damaged` (the managed lines were edited by hand: put back the `// <<< iOS Studio hot reload` line, or delete the block from `// >>>` on), `changed` (`Package.swift` changed meanwhile), `package-busy` (a package change is running) or `missing-runtime` (iOS Studio's own runtime files are missing: update iOS Studio). Turning it off stops a running session before it checks `Package.swift`, so when it fails after that, the session is already stopped while `Package.swift` is unchanged: the helper's error then carries `sessionStopped: true`, and the agent tool's next step says so.

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
| `baseline.json` | the module, each file's compile command (compact: one shared command plus each file's own arguments; format version 2), the link command, the app's binary and bundle ID, the team |
| `sources/` | the Swift files as they were built (from the snapshot taken before the build) |
| `objects/` | their object files from that build |
| `app.bin` | the built app's executable (from `xtool/<App>.app`, or unpacked from `xtool/<App>.ipa`) |
| `patches/` | the signed patches; the newest 10 are kept |

The whole `hot-reload/` folder is kept under 300 MB: when a session starts, the oldest projects' folders go first, but never the folder of a project whose session is running. A new baseline replaces the old one at each session start: it's written into a staging folder (`.<key>-…`, which starts as the snapshot) and swapped in only when complete, so a failed capture keeps the old one. The app unpacked from a Wi-Fi build's `.ipa` goes to a staging folder too (`.<key>-ipa-…`), deleted when the baseline is recorded. Staging folders older than 6 hours (left by a killed session) are deleted.

In `$XDG_RUNTIME_DIR/ios-studio/` (the same base as [watch mode](watch-mode.md#files); demo mode uses `ios-studio-demo/`):

| File | What |
|---|---|
| `hotreload-<key>.lock` | `flock` held by the session while it runs, its pid inside |
| `hotreload-<key>.json` | the latest snapshot and the last 20 reloads (`history`); kept after the session stops |
| `hotreload-<key>-control.json` | pause or resume, read by the session on `SIGUSR1` |

A session that died without saying so (killed, crashed) is recorded as stopped with `stopReason: "session-died"` by the next `status`. Files of sessions stopped more than 24 hours ago are deleted when a session starts, and so are lock files that no session holds and no state file goes with (older than a minute). Pruning deletes a lock file only while it holds that lock itself, so a session starting at the same moment waits for it (up to half a second) or takes the new file; it never loses its lock. `status`, `stop`, `pause` and `resume` never create a lock file.

On the iPhone, in the app's `Library/Caches/HotReload/`: `runtime.json` (written by the runtime at each launch), `patch-<n>.dylib` and `ack-<n>.json` (the app's answer). The runtime deletes every patch and answer when the app launches and keeps only the newest 3 after each reload, and a session clears the folder when it starts. The folder is in the app's Caches, so iOS may also clear it, and it goes away with the app. If it's deleted while the app runs (also with all of `Library/Caches`), the runtime makes it again and writes a new `runtime.json`. Loaded patches stay in the app's memory until it quits (a few hundred KB each).

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

`run` watches the process that started it and stops about half a second after that process exits ("Stopped: the app or tool that started hot reload has closed", `stopReason: "stopped"`). The app and the MCP server stay alive for as long as their sessions should run, so this is what ends a session when you quit them. In a script, keep the parent alive while the session runs: run `hotreload run` in the foreground (or `wait` for it) instead of starting it in the background from a script that then exits, and use `hotreload stop` to end it.

`status --project P` returns `{project, enabled, mode, session}` (`session` is null when it never ran, and has `running`). With `--wait-seconds N` (at most 25) it returns as soon as a reload newer than `--after-seq` finishes (by default, newer than the newest one finished now), or the session stops, or N seconds pass (`timedOut`); pass the newest `seq` you saw, so a reload that finished just before the call isn't missed. Without `--project` it returns `{sessions, projects}`: the running sessions and those stopped in the last 10 minutes, and `{path: {enabled, mode}}` for every listed project (read from `Package.swift`, no Swift call).

`stop` sends SIGTERM and waits up to 10 s before killing the session (its whole shutdown takes at most 9 s): `{stopped, session}`. `pause` and `resume` return `{paused, session}`, or `not-running` when no session runs.

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
- `lastError`: `{code, message, fix?, file?, line?, blockers? (≤ 10), blockersTotal?, diagnostics? (≤ 5), diagnosticsTotal?, seq, time}`.
- `notice`: `{id, level, message, fix?, time}`, one of `runtime-updated` (the project's runtime copy was refreshed), `watch-stopped`, `needs-run` (`Package.swift`, `xtool.yml` or a resource changed: those need Run), `other-target` (a Swift file outside the app module changed: Run), `changed-while-building` (saves made during the first build, reloaded now), `changed-during-build` (a file copied after the build changed during it: Run), `back-to-build` and `polling` (inotify isn't available, so files are checked every second).
- `stopReason` (with `stoppedAt`): `stopped`, `app-exited`, `build-failed`, `error`, `session-died`.

The session's build is in the build history with `trigger: "hot-reload"` and a `buildId` of `hr-<time>`.

The device commands it uses (`hotreload.push`, `hotreload.clean`, `hotreload.runtime`) are in [deviced-protocol.md](../deviced-protocol.md#hot-reload-hotreload-used-by-the-hotreload-helper-groups-session).

## Agent tools

AI agents get four tools (details in [mcp.md](../mcp.md)):

- `ios_hot_reload_set(project_path, enabled, dry_run=false)` turns hot reload on or off for a project and returns `mode` and the `Package.swift` diff; `dry_run=true` only shows it. Turning it off stops a running session first.
- `ios_hot_reload_start(project_path, udid?, preset?, restart=false, job_id?)` runs a session as a job: build, install, launch, then watching. It returns once the session is watching, or a `job_id` to call again while it builds. If the app window already runs a session for the project, it reports that one instead of starting a second; `restart=true` stops it and builds again, which is the Run again of the app.
- `ios_hot_reload_status(project_path?, after_seq?, wait_seconds=0, history=5)` returns the state, the last reload with its step timings, or the last error with its blockers and compiler errors (`blockers_total` and `diagnostics_total` count them all; with more than the 10 blockers or 5 compiler errors listed, the result has `truncated` and a `note`). `history` takes at most 10 reloads, each message cut to 80 characters and 2 files. Each result has `last_seq`: after saving a file, pass it as `after_seq` with `wait_seconds=20` (at most 25) and the call returns when the next reload has finished. Without `project_path` it lists the sessions and the projects with hot reload on.
- `ios_hot_reload_control(project_path, action)` pauses, resumes or stops a session, also one the app window started.

A session an agent started stops when its job is cancelled or the agent's MCP server exits. Results never carry images.

## Limits

<!-- TODO(verify): the app-side loading (the runtime starting, dlopen of a signed patch from Caches, the swap, the redraw) is not confirmed on a real iPhone yet; update after the CrashLab test -->
- At the time of writing, everything up to the patch arriving on the phone is tested, but the app side (the runtime loading a patch and swapping functions on a real iPhone) hasn't been confirmed on a device yet.
- Only Swift code under `Sources/` in the app's own module. Code in Swift packages, resources, asset catalogs, `Info.plist` and `xtool.yml` need Run again.
- Code that's already running keeps the old version until it's called again: a running `Task`, closures and callbacks stored before the reload, and protocol witness tables built elsewhere.
- Static initializers in a patch run again when it loads. Globals and static stored properties keep using the app's storage, so a reload doesn't reset them or make a second `static let shared` instance.
- Every class a changed file declares (any Swift class, not only `@objc` ones) is registered a second time, and the system logs "Class … is implemented in both …" once per class and reload.
- A patch brings its own copy of the type metadata of the types its files declare. A child view declared in a reloaded file may lose its `@State`, and `as?` or `as!` between a value the app made and that type as the patch sees it can fail.
- An async function whose new body needs a bigger context than the build reserved (it keeps more values across an `await`) is refused with "<function> now needs more memory per call than the running app reserved. Press Run to rebuild." A smaller one is fine, and async methods are swapped through the class's method table too.
- Methods of a generic class (or any class whose metadata is built at run time) aren't swapped for calls through the method table, and they don't count in `swapped`.
- At most 100 reloads per run of the app; after that: "Too many reloads in this run (100). Press Run to start fresh."
- `private` and `fileprivate` functions change through the functions that call them, so the change shows once a caller is in the patch too (it is, when they're in the same file).
- arm64 only. Your own development builds only, signed with the same team as the app.
- The app has to be running to load a patch. iOS may suspend an app in the background, and then it can't answer within the 5 s (`app-not-running`); keep it open on the phone while you work.
- Release builds while it's on: see [above](#release-builds-turn-it-off-first).

## Demo mode

With `IOSSTUDIO_DEMO` set, `enable` and `disable` answer with the real block's diff against a made-up `Package.swift` and write nothing; WeatherGlance's views `import HotReload`, so turning it off there shows `stub`. `run` plays a scripted session in the demo runtime folder without building or sending anything: starting, building (with progress), installing, launching, watching, a reload of `ContentView.swift` as if it was saved during the build (notice `changed-while-building`), then a save of `ContentView.swift` every few seconds, alternating a reload that works (about 2.4 s, steps filled in) and one refused with `unsupported-change` (a stored property `count` added to `ContentView`); every fourth save is in another target (`Sources/<Name>Kit/`, notice `other-target`, nothing reloads). Pause collects the scripted saves as pending; resume reloads them at once. `status`, `stop`, `pause` and `resume` are the real commands. The demo iPhone's `hotreload.push` answers after about 300 ms with 3 functions swapped.

## Code

- `templates/hot-reload/`: the runtime package copied into projects, `full/` (a C loader, `HotReloadLoader`, with its symbol rules in `hr_rules.h`, and the Swift API, `HotReload`) and `stub/` (the same API as no-ops), with `version.txt`. Its [README](../../templates/hot-reload/README.md) explains the on-device files and the rebinding.
- `helper/iosstudio/features/hotreload/`: `commands.py` (the `hotreload` group), `project_setup.py` (the `Package.swift` block and the runtime copy), `session.py` (a session: start, scheduler, reloads), `state.py` (the runtime files, status, stop, pause and resume), `buildinfo.py` (the baseline from xtool's build records and its cache), `patch.py` (check, compile, link, sign), `checks.py` (what a reload can't change), `macho.py` (a small Mach-O reader), `signing.py` (rcodesign with xtool's identity).
- `helper/iosstudio/device/features/hotreload/`: `hotreload.push`, `clean` and `runtime` in `real.py` and `demo.py`, shared parsing in `core.py`.
- `helper/iosstudio/demo/hot_reload.py`: demo answers and the scripted session.
- `mcp/iosstudio_mcp/tools/hot_reload.py`: the four agent tools; a session is a job that keeps the newest snapshot.
- UI: `services/HotReloadService.qml` (`app.hotReload`: the switch, sessions, status), `components/projects/HotReloadBar.qml` (the bar under the card), `HotReloadStubNote.qml` (the stub note), the switch in `ProjectCard.qml`, and the `hotReload*` helpers in `lib/Model.js`. Their APIs are in [components.md](../components.md).

## Tests

- `tests/helper/test_hot_reload_setup.py` and `tests/cli/test_hot_reload_setup_cli.py`: the block added and removed byte for byte, `off` and `stub`, the runtime copy, dry runs, a manifest that wouldn't work.
- `tests/helper/test_hot_reload_buildinfo.py`, `test_hot_reload_patch.py`, `test_hot_reload_checks.py` (and `test_hot_reload_checks_review.py`: class vars, type aliases, static storage, regex literals), `test_hot_reload_macho.py` and `test_hot_reload_signing.py`: the pipeline against a fake xtool build and fake swiftc, ld64.lld and rcodesign, on small real object files in `tests/fixtures/hot-reload/`. Signing uses throwaway keys made with openssl and checks that no key reaches the disk.
- `tests/helper/test_hot_reload_session.py` and `tests/cli/test_hot_reload_session_cli.py`: the scheduler, error mapping, pause and resume, the runtime files, and a real-mode session with a fake xtool and a fake device daemon.
- `tests/helper/test_dev_hotreload.py`: the device feature with a fake house_arrest at the pymobiledevice3 boundary, which patch files a push takes (a store outside the daemon's cache folder, no symlinks, its own user's, not swapped after the check), and its demo twin.
- `tests/helper/test_hot_reload_build_window.py`: what happens while the first build runs: a file created during it is new, a prebuild's output counts as built, `-###` output over its cap, pruning a lock while a session starts.
- `tests/cli/test_hot_reload_runtime.py`: the runtime package's layout and the same API in full and stub; with the iOS SDK installed, both build for iOS, and the loader's symbol rules (`hr_rules.h`) are checked against a small app and patch built for `arm64-apple-ios` (addressors kept, async function records, a grown async context caught).
- `tests/qml/tst_hot_reload.qml`: the `Model.hotReload*` helpers (stream events into an entry, the bar's label, pill and buttons); `tst_hot_reload_reconcile.qml`: a switch flipped here against later status replies.
- `tests/mcp/test_mcp_hot_reload.py`: the agent tools against a fake helper (with a fake `hotreload run` stream), and with the real helper's demo session: start, waiting for the next reload, pause, resume, stop; `test_mcp_hot_reload_bounds.py`: cut blocker lists, the history budget, a failed off after the session stopped, the stop grace.
- `tests/helper/test_hot_reload_e2e.py` (slow): the whole pipeline with the real toolchain on a copy of `~/Projects/CrashLab`; skipped without it, xtool, the SDK, openssl or rcodesign.

See [development.md](../development.md#tests) for how to run them and the test hooks.
