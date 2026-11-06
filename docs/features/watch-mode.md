# Watch mode: compile on save

Watch mode compiles a project every time you save, so errors show up in Problems a few seconds after you stop typing, without pressing Build. It runs the same compile-only build as the Build button (no Apple ID or iPhone needed), so each one is in the build history with its log and diagnostics. With auto-run on, a clean compile is also run on the iPhone.

Turn it on per project in the app, or from an agent with `ios_watch_start` (see [mcp.md](../mcp.md)). Both use the same watcher.

## What triggers a build

- Files under `Sources/` (all folders, including ones you create later), and in the project folder `Package.swift`, `xtool.yml` and any `.plist` or `.entitlements` file.
- Not: `Package.resolved` (builds rewrite it themselves), `Tests/`, `.build/`, `xtool/`, `.bsp/`, hidden files and folders, and editor temp, swap and backup files (`.swp`, `~`, Vim's `4913` probe, `.tmp…`, JetBrains' `___jb_tmp___`, Emacs' `#file#`).
- A save that doesn't change the file's content (`:w` without edits, a script that regenerates an identical `xtool.yml`) is ignored.

Folders are watched, not files, so editors that save by writing a temporary file and renaming it over the original (Vim and Neovim with `backupcopy=no`, Zed, JetBrains IDEs) are seen as a change of the real file. Editors that write in place (VS Code) work too.

## Timing

- **Debounce:** a build starts 600 ms (`debounce_ms`, 100 to 10,000) after the last save, so a burst of saves (save all, a formatter, a branch switch) makes one build. If saves never pause, a build still starts at least every 4 s.
- **Supersede:** a save during a build cancels it and starts one more after it. There's never more than one build. Cancelling costs little: SwiftPM keeps what it already compiled.
- **Endless saves:** when the last 2 builds in a row were cancelled by saves, the next one is let finish even if you keep saving (the status says "this build finishes first"), and the saves made meanwhile build again after the debounce. So saves that come faster than a build takes (a file watcher, a generator) still give a result, and the counting starts over after a build that wasn't cancelled.
- **Busy project:** when the project is already being built (the Build button, or an agent's `ios_build_start`), the watch build waits and tries again every 2 s.
- **First build:** starting watch mode compiles the current state once.

Measured on a template app (`swiftui-app`, an Intel Core i7-8550U laptop, warm build cache): saving a file with an error showed it 8 to 10 s later (about 0.15 s to notice the save, 0.6 s debounce, the rest is `swift build`); fixing it showed "Compiled cleanly" about 9 s later. A cancelled build stops within about 2 s, and stopping watch mode during a compile took 2.6 s with nothing left running. The first build of a fresh project takes longer (about 20 s).

## Auto-run

With auto-run on, each clean compile is followed by Run: build, sign, install and launch, as the Run button does (with the project's selected run preset). It only runs when an iPhone is connected (the iPhone service is running and reports it connected) and you're signed in to an Apple ID; otherwise the status says why it was skipped ("Not run: Arthur's iPhone isn't connected right now."). A save while the run is still building cancels it and compiles first. While a run is installing on the phone, the next clean compile is not run again ("The previous run is still installing"); the install is never interrupted by a save.

## One watcher, shared

There's at most one watched project per user, because builds are heavy. Starting watch mode on another project stops the first and says so (`replaced`). Whoever starts it, the app and agents see the same status, and either can stop it.

The watcher is its own process, `iosstudio-helper watch serve`, started detached by whoever asks first. It isn't a service that keeps running: it records who uses it (its owners, as process id plus start time) and stops by itself within a second once none of them is alive.

- The app follows it with `watch run` (a stream of snapshots); that process is the app's owner entry. Closing the app or turning watch mode off ends it.
- The MCP server starts or joins it with `watch run --detach --owner <server pid>`. When the agent's session ends, the server exits, and the watcher stops unless the app is also using it.
- If the watcher process is killed, its lock is released by the kernel; the next `watch status` (the app asks now and then) notices, records it as `stopped` with `stopReason: "watcher-died"`, and a new watcher starts cleanly. The build it was running dies with it (the build helper has a parent-death signal, and kills xtool and swift-build on the way out).

Stopping (`watch stop`, `ios_watch_stop`, the app's toggle) cancels a running compile, which shows as `cancelled` in the build history, and waits for its process tree to be gone (killing it after 5 s). The last status stays readable for 10 minutes.

Why a separate process rather than a thread in the app or the MCP server: both need to see and stop the same watcher, either may close first, and neither should have to keep running for the other. A small process found through a lock file does that without a new background service.

## Files

In `$XDG_RUNTIME_DIR/ios-studio/` (`$IOSSTUDIO_RUNTIME_DIR` overrides the base; without `XDG_RUNTIME_DIR`, as in some agents' trimmed environments, `/run/user/<uid>` when it's yours, else `/tmp/ios-studio-<uid>`, the same base the iPhone service uses; demo mode uses `ios-studio-demo/`):

| File | What |
|---|---|
| `watch.lock` | `flock` held by the watcher while it lives, its pid inside. A free lock means no watcher. |
| `watch.json` | The latest snapshot (below). |
| `watch-events.jsonl` | Every snapshot as a JSON line (cut back past 1 MB); `watch run` tails it. |
| `watch-owners.json` | `{pid: start time}` of the processes using the watcher. |
| `watch-control.json` | Settings changed while it runs (auto-run, debounce), applied on `SIGUSR1`. |
| `watch.log` | The watcher's stderr. |

Builds are recorded in `~/.local/state/ios-studio/builds.json` like any other, with `buildId: "w-<n>"` (the number keeps counting across watchers) and `trigger: "watch"`, so the history can call them "Compile on save". The auto-run's own Run build has `trigger: "watch"` too.

## Helper commands

```
iosstudio-helper watch run    --project P [--auto-run] [--debounce-ms 600] [--no-initial-build]
iosstudio-helper watch run    --project P ... --detach [--owner PID]       # start or join, then return
iosstudio-helper watch status [--project P] [--wait-seconds N]
iosstudio-helper watch stop   [--project P]
```

`watch run` without `--detach` streams: a `watch-start` event when it joined an existing watcher (`attached`) or replaced another project's (`replaced: {path, project}`), then one `watch` event per snapshot, and `{"event": "done", "ok": true, "data": <snapshot>}` when the watcher stops. Written to its stdin, `{"cmd": "stop"}` stops the watcher and `{"cmd": "quit"}` (or `{"cancel": true}`, SIGTERM) only detaches this follower. Called for the project that's already watched, it joins, and changes only the settings passed: `--auto-run` (or `--auto-run true|false`, `--no-auto-run`) and `--debounce-ms` apply without a restart, and one that isn't passed keeps the watcher's current value. A new watcher gets auto-run off and 600 ms. `--detach` without `--owner` makes the calling process the owner.

`watch status --wait-seconds N` (at most 25) returns as soon as a build that finished after the call is in and nothing newer is pending, the watcher stops, or N seconds pass (`timedOut`). With `--project` for a project that isn't the watched one, it returns `state: "off"` and `other: {project, path, state}`. `watch stop --project P` does nothing when another project is watched.

A snapshot (`watch` event, `watch.json`, `watch status` data):

```json
{"event": "watch", "seq": 12, "state": "failed", "reason": "build-finished", "message": "2 errors",
 "project": "WatchDemo", "path": "/home/me/Developer/WatchDemo", "pid": 81234,
 "mode": "inotify", "autoRun": false, "debounceMs": 600, "owners": 1, "startedAt": 1793991966.1,
 "updatedAt": 1793992104.3, "changed": ["Sources/WatchDemo/ContentView.swift"], "changedCount": 1,
 "buildId": "w-2", "logFile": "…/build-logs/20261106-…-compile.log", "builds": 2, "superseded": 0,
 "lastBuild": {"id": "w-2", "result": "failed", "duration": 9.5, "time": 1793992094.8, "errors": 2,
               "warnings": 0, "errorCode": "build-failed", "errorMessage": "The build failed.",
               "logFile": "…", "diagnostics": [{"severity": "error", "category": "compiler",
               "file": "/home/me/Developer/WatchDemo/Sources/WatchDemo/ContentView.swift",
               "line": 11, "col": 13, "message": "cannot find 'Txt' in scope"}]},
 "watching": true}
```

- `state`: `watching` (idle), `changed` (waiting for the debounce, or for another build of the project), `compiling`, `ok`, `failed`, `stopped`; `watch status` says `off` when there's no recent watcher.
- `reason`: what this snapshot is about: `started`, `changed`, `build-started`, `build-log`, `progress` (about once a second while compiling, with `progress: {fraction, detail}`), `superseding`, `superseded`, `busy`, `build-finished`, `run`, `owners`, `settings`, `stopped`.
- `lastBuild`: the latest finished build (ok or failed; cancelled ones only count in `superseded`). `diagnostics` holds the first 5, errors first, then warnings; the whole list is in the build history (`ios_build_diagnostics(build_id='w-2')`).
- `run` (auto-run): `{state: checking|building|installing|launching|launched|failed|skipped, message, buildId, forBuild, bundleId?, errorCode?}`. `buildId` is the run's own build (its own `w-N` in the history); `forBuild` is the clean compile it follows (`lastBuild.id` at the time), so the two can be shown together.
- `mode`: `inotify`, or `poll` with `modeReason` (the inotify watch limit is reached, or inotify isn't available). See [troubleshooting](../troubleshooting.md#watch-mode-says-checking-files-every-second).
- When stopped: `stoppedAt` and `stopReason`: `stopped` (asked), `replaced` (another project), `no-owner` (the app and agent that used it are gone), `project-gone`, `watcher-died`.

## Limits

- Auto-run's device check reads the iPhone service's last-known state (`~/.local/state/ios-studio/device.json` says `connected`, and the service's socket exists); it doesn't ask the phone. Right after the phone drops off (unplugged, out of Wi-Fi) the state can still say connected, and the run then fails at the install step (`run.state: "failed"`) instead of being skipped.
- Stopping watch mode during an auto-run install stops iOS Studio's side, but the iPhone may still finish that install (see #30).
- One project at a time, per user.

## Demo mode

With `IOSSTUDIO_DEMO` set, `watch serve` watches nothing and builds nothing. It plays a loop: watching, a file changed, compiling, failed with one error (`cannot find 'habbits' in scope` at `Sources/<Project>/ContentView.swift:42:17`), changed, compiling, ok (with auto-run, "launched"), and again. Start, status and stop work as usual, in the demo runtime folder.

## Tests

`tests/test_watch.py` and `tests/test_mcp_watch.py`, see [development.md](../development.md#running-from-source).
