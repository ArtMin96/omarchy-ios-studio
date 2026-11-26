# Debug in editor: breakpoints in VS Code and Zed

Press F5 in VS Code (or start the debug task in Zed) and the editor's debugger attaches to your app on the iPhone. Breakpoints set in the editor stop the app, and the editor shows the stack, variables, stepping and its debug console, where `p` and `po` evaluate Swift. iOS Studio prepares everything the editor can't do by itself on Linux: the tunnel to the phone, the phone's debugserver, the DeviceSupport symbols, and a copy of the debugger that can evaluate Swift in an iPhone app. The debug adapter is `lldb-dap` from the Swift toolchain, run through iOS Studio's launcher.

## In iOS Studio

On the Projects page, open a project card's More (⋯) and click **Debug in editor**, next to Open in editor. The
first time, iOS Studio adds the editor's debug configuration to the project. A strip under the card's details then
shows how it goes, also when the card is collapsed:

- **Starting the debugger…** with a spinner while iOS Studio checks the debugger, writes the configuration and
  connects to the debugger on your iPhone.
- **Waiting for VS Code · press F5 on “Attach to MyApp on iPhone (iOS Studio)”** once the editor can attach (for
  Zed: start it from the debug panel; "your editor" when several are set up). If the app wasn't running, iOS Studio
  started it paused and the strip adds that it waits for the debugger.
- **VS Code is attached · the app pauses at breakpoints** (green) while the editor is connected.
- **VS Code detached · press F5 … to attach again** after the editor's session ends. The bridge keeps running.
- **Stop** ends it (and the editor's session, if one is attached). The strip then says why it ended (stopped, the
  app exited, after 2 hours) and leaves the card after 20 seconds, or when you close it.
- Notices from the helper show as quiet lines under it: no debug symbols, no build of the app on this computer, or Swift expressions may not work (the debugger couldn't be set up for iOS, for example because the iOS SDK isn't installed).
- An error (the app isn't installed, another debugger is attached, the toolchain has no lldb-dap) shows its message
  with its fix button and Try again.

The button is disabled, with the reason in its tooltip, while there's no iPhone, the project has no bundle ID, or a
bridge already runs for it. A bridge that's still running when iOS Studio starts (from another window or before a
restart) shows on its card too, and its Stop works the same. Quitting iOS Studio stops the bridges it started.
The UI is `services/EditorDebugService.qml` (`app.editorDebug`) and `components/projects/EditorDebugBar.qml`; the
states come from `Model.editorDebugEvent` in `lib/Model.js` (`tests/qml/tst_editor_debug.qml`).

## How it works

1. **The editor's debug configuration, once per project.** iOS Studio adds one configuration named "Attach to <App> on iPhone (iOS Studio)" to `.vscode/launch.json` (VS Code) or `.zed/debug.json` (Zed). Nothing else in the project is written. Every configuration and comment already in those files stays byte for byte (they're JSON with comments, edited as text). If our entry is already there and current, nothing changes. If it's outdated, only our entry is replaced; that's how a configuration written by an older iOS Studio (`"type": "swift"`) moves to the current one, the next time you click Debug in editor. The configuration has a single attach command, which reads the app's *attach file*. It names that file, and for VS Code the debugger, relative to your home folder, so the project carries no machine path and can be committed.
2. **Debug in editor, each time.** The helper (`debug editor`) first prepares the debugger the editors run (see [Swift expressions](#swift-expressions-p-po)): a stat check once it's built. It then asks the device daemon to forward the phone's debugserver to a loopback port (`debug.start`, the same plumbing as iPhone › Debug). If the app isn't running, iOS Studio launches it suspended, waiting for the debugger. iOS Studio then writes the attach file with the port and the app's process id and reports `ready`. It holds the forward open until you stop it, the app exits, or two hours pass.
3. **The attach file**, `~/.local/state/ios-studio/editor-debug/<bundle id>.lldb`, holds lldb commands. They do what iOS Studio's own debugger does (`helper/iosstudio/features/lldb_driver.py`):
   - `settings set target.memory-module-load-level minimal`, because otherwise lldb reads every system library out of the phone's memory, which takes minutes over Wi-Fi.
   - `iosstudio-sdk-path "<iPhoneOS.sdk>" "<lldb's library folder>"`, before the target exists: it runs `settings set target.sdk-path` so Swift expressions find the iOS SDK's modules. It does that only when the editor really runs iOS Studio's copy of lldb (its `liblldb` is in that folder), because with the toolchain's own lldb that setting makes expressions fail differently. Otherwise it prints a warning in the Debug Console and the attach goes on. The line is left out when the copy couldn't be prepared.
   - `platform select remote-ios --sysroot "<DeviceSupport symbols>"` when the debug symbols are downloaded.
   - `target create "<your local build>"` and the executable's path on the phone.
   - `iosstudio-connect connect://127.0.0.1:<port>`. A plain synchronous `process connect` fails against a debugserver that has no process yet, so this command connects asynchronously and waits.
   - `process attach --pid <pid>`.

   The helper commands (`iosstudio-check`, `iosstudio-sdk-path`, `iosstudio-connect`, `iosstudio-remote-file`, `iosstudio-fail`) come from `iosstudio_editor_attach.py` next to the attach file, a copy of `helper/iosstudio/features/editor_debug_lldb.py` loaded into lldb-dap's Python.
4. **After that it's the editor's session.** lldb-dap sends the breakpoints, and on `configurationDone` it resumes the app. A launched app starts running only then. While the bridge runs, it watches for the editor's connection to the forward (the open loopback connections in `/proc/net/tcp`, once a second). It reports `attached` when the editor connects and `detached` when it disconnects. You can attach again (F5) as often as you like while the bridge runs.
5. **When it ends**, iOS Studio releases the forward (`debug.stop`) and puts the stub back in the attach file. If iOS Studio launched the app suspended and no editor ever attached, it ends that process too; otherwise it would stay frozen. Pressing F5 without a running bridge fails with "Start 'Debug in editor' in iOS Studio first" in the editor's Debug Console. A stale attach file left behind by a crash fails the same way, because its first command checks that the iOS Studio process that wrote it is still running (by pid and start time). It never connects to an old port.

## Swift expressions (p, po)

At a breakpoint, type `p items.count`, `po user` or `expression -- name.uppercased()` in the editor's Debug Console (VS Code: the Debug Console's input; Zed: the debug panel's console). The first expression of a session can take up to 40 seconds while lldb loads the app's Swift modules from the iOS SDK; later ones answer in a moment. The Variables view works from the start.

The Swift toolchain's own lldb can't do this for an iPhone app on Linux: it finds no Xcode, falls back to the toolchain's Linux Swift modules and fails with "missing required module 'SwiftShims'". So the editors run iOS Studio's lldb-dap: `~/.cache/ios-studio/lldb-ios/lldb-dap`, a small launcher at a path that stays the same when Swift or the iOS SDK changes. It runs `lldb-dap` from a shadow copy of the toolchain whose Swift resource folder is the iOS SDK's (`helper/iosstudio/features/lldb_shadow.py`, shared with iPhone › Debug). The shadow is built once, within seconds (hard links or reflinks where the disk allows, else a ~230 MB copy), and checked with a few `stat` calls on every start. Debug in editor and setting the editor up in Settings › Code editor (re)build it and rewrite the launcher, so a new Swift or SDK is picked up by itself.

If the shadow can't be built (most often: the iOS SDK isn't installed), the launcher runs the toolchain's plain `lldb-dap`. Breakpoints, stepping and variables still work; Debug in editor shows "Swift expressions (p, po) may not work when debugging from your editor: …" with the reason, and agents get it as `expressions_note`.

## VS Code

You need the Swift extension (`swiftlang.swift-vscode`), the one Settings › Code editor recommends. VS Code installs the LLDB DAP extension (`llvm-vs-code-extensions.lldb-dap`) with it as a dependency, and that's the one the configuration uses: `"type": "lldb-dap"`, with iOS Studio's launcher as `debugAdapterExecutable`. `${userHome}` is filled in by VS Code, so the file has no machine path. There's nothing to set up:

```json
{
  "name": "Attach to MyApp on iPhone (iOS Studio)",
  "type": "lldb-dap",
  "request": "attach",
  "debugAdapterExecutable": "${userHome}/.cache/ios-studio/lldb-ios/lldb-dap",
  "attachCommands": [
    "script import os; lldb.debugger.HandleCommand('command source \"%s\"' % os.path.expanduser('~/.local/state/ios-studio/editor-debug/com.example.myapp.lldb'))"
  ],
  "timeout": 300
}
```

`timeout` is how long lldb-dap waits for the app to stop after attaching. Over Wi-Fi, without updated debug symbols, that can take minutes.

Why `"type": "lldb-dap"` and not `"type": "swift"`: the Swift extension's `swift` type always replaces `debugAdapterExecutable` with the toolchain's own lldb-dap (or `swift.debugger.path`, a setting for every Swift project), which can't evaluate Swift for an iPhone app. The LLDB DAP extension takes `debugAdapterExecutable` from the configuration, so only this configuration uses iOS Studio's lldb-dap. An older iOS Studio wrote `"type": "swift"`; Debug in editor updates that entry by itself. If `debug editor-config` notes that the LLDB DAP extension is missing, install it from Extensions.

## Zed

You need Zed's Swift extension, the one Settings › Code editor checks for. It provides the `Swift` debug adapter, which runs lldb-dap and passes the configuration through unchanged:

```json
[
  {
    "label": "Attach to MyApp on iPhone (iOS Studio)",
    "adapter": "Swift",
    "request": "attach",
    "attachCommands": ["script import os; lldb.debugger.HandleCommand(…)"],
    "timeout": 300
  }
]
```

The Swift extension runs the `lldb-dap` named in Zed's user settings (`~/.config/zed/settings.json`), else one on PATH (or through `swiftly` / `xcrun`); a Zed debug configuration can't name it. Setting Zed up in Settings › Code editor points it at iOS Studio's launcher. By hand, that's:

```json
{
  "dap": {
    "Swift": { "binary": "/home/you/.cache/ios-studio/lldb-ios/lldb-dap" }
  }
}
```

The launcher works for Swift programs on Linux too, so other Swift projects you debug in Zed keep working. Until it's set, `debug editor-config` adds a note that sends you to Settings › Code editor: Zed can't find lldb-dap at all, or it runs another lldb-dap, where Swift expressions may not work. If you set Zed up with an older iOS Studio (`binary` was the toolchain's `/usr/lib/swift/usr/bin/lldb-dap`), Settings shows Zed as needing setup, and **Set up** replaces that value (after a backup). Start the configuration from the debug panel (`debugger: start`).

## Neovim (nvim-dap)

iOS Studio doesn't change your Neovim config. With [nvim-dap](https://github.com/mfussenegger/nvim-dap), add an adapter for iOS Studio's lldb-dap launcher and one configuration per app. Run "Debug in editor" in iOS Studio, then `:DapContinue`:

```lua
local dap = require("dap")
dap.adapters["lldb-dap"] = {
  type = "executable",
  -- iOS Studio's lldb-dap: Swift expressions (p, po) work there (Debug in editor creates it)
  command = vim.fn.expand("~/.cache/ios-studio/lldb-ios/lldb-dap"),
  name = "lldb-dap",
}
dap.configurations.swift = {
  {
    name = "Attach to MyApp on iPhone (iOS Studio)",
    type = "lldb-dap",
    request = "attach",
    attachCommands = {
      [[script import os; lldb.debugger.HandleCommand('command source "%s"' % os.path.expanduser('~/.local/state/ios-studio/editor-debug/com.example.myapp.lldb'))]],
    },
    timeout = 300,
  },
}
```

Use your app's bundle id from `xtool.yml` (without an `XTL-<TEAM>.` prefix) in the file name.

## For AI agents (MCP)

- `ios_debug_editor_config(project_path, editor?, confirm=false)` shows what it would add to `.vscode/launch.json` or `.zed/debug.json`; `confirm=true` writes it.
- `ios_debug_editor_start(project_path | bundle_id, launch?, editor?, write_config?)` starts the bridge as a job and answers within 25 s with the port, the configuration's name and whether the editor is attached. `swift_expressions` says whether `p`/`po` work in the editor; when they may not, `expressions_note` says why. Call it again with `job_id` (and `wait_seconds`) to follow it.
- `ios_debug_editor_status(bundle_id?)` lists running bridges, also ones started from the app window.
- `ios_debug_editor_stop(bundle_id | job_id)` ends one. `ios_job_cancel` and quitting the server do the same.

The agent can't press F5 for you: it tells you which configuration to start in the editor.

## Limits

- **Attaching pauses the app** until lldb-dap has read it (seconds over USB, longer over Wi-Fi without the debug symbols). Download the symbols once under iPhone › Debug › Debug symbols, so Swift values (String, Array) read right and attaching is fast.
- **Only your own development-signed apps** can be debugged. App Store and system apps can't be (the phone refuses).
- **One debugger per app.** If iOS Studio's own debugger or an AI agent is attached, "Debug in editor" reports `busy`. Detach there first.
- Breakpoints in your code need a build of the app on this computer (iOS Studio uses the newest one: `xtool/<App>.app`, `.build/<config>/<Product>-App` or the `.ipa`). Build and run the app from iOS Studio first.
- Stopping the bridge while the editor is attached ends the editor's session.
- The bridge needs the Debugger support step in Setup: the attach file's commands run in lldb's Python.
- Swift expressions need the iOS SDK (Setup). The first one in a session can take up to 40 seconds.
- If `XDG_STATE_HOME` points outside your home folder, the configuration names the attach file by its absolute path.

## Helper commands (group `debug`)

```
debug editor-config --project P [--editor vscode|zed|neovim] [--dry-run]
    -> {project, bundleId, appName, configName, scriptPath, dryRun, applied, notes: [str],
        editors: [{id, name, path, state: missing|present|outdated,
                   action: {kind: write|merge|skip, path, scope: "project", detail, preview?, done?}}]}
```

Default editor: the one remembered by Settings › Code editor; without one, every installed one among VS Code and Zed. Neovim returns no editor and a note pointing here. `preview` is the new file (write) or a unified diff (merge). Without `--dry-run`, it also writes the stub attach file, and the launcher when there's none yet (running the toolchain's lldb-dap until the first bridge), so F5 before a bridge gives the readable message. Notes: Zed's `dap.Swift.binary` missing or naming another lldb-dap, VS Code's LLDB DAP extension missing.

```
debug editor --project P | --bundle-id B [--launch auto|attach|suspended] [--udid U] [--editor E] [--no-config]
```

A stream (JSON lines, ends with `done`). `--project` looks up the installed bundle id (`XTL-<TEAM>.<id>` on free teams) with `apps.list`. `--bundle-id` takes the installed id and finds the project by it. `--launch auto` attaches when the app runs, else launches it suspended. Events:

| event | fields |
|---|---|
| `debug` | `{mode: "editor", bundleId}` |
| `step` | ids `lldb`, `config` (unless `--no-config` or no project), `forward` |
| `notice` | ids `lldb-plain` (the shadow lldb couldn't be prepared: Swift expressions may not work; the message says why), `editor-config` (the config notes), `no-local-build`, `no-symbols`, `symbols-update`, `waiting` (a suspended launch waits for the editor), `editor-cut`, `keepalive` |
| `ready` | `{bundleId (as installed), appName, port, host, pid, launched, scriptPath, editors: [ids configured], configName, symbolsReady, localBuild, swiftExpressions}` |
| `editor` | `{state: attached|detached, connections}` |
| `forward` | `{port, host}`: the forward came back on a new port (after the phone reconnected); the attach file has it |
| `done` | `{ok: true, data: {ended: cancelled|app-exited|timeout, bundleId, appName, pid, launched, attached, killed, scriptPath}}`, or `{ok: false, error}` (e.g. `busy`, `not-installed`, `not-running`, `not-debuggable`, `needs-lldb`) |

Stop it with `{"cancel": true}` on stdin, by closing stdin, or with SIGTERM. Every 15 minutes it refreshes the forward with `debug.start {launch: "none"}`, because the daemon closes a forward nobody touched for an hour.

```
debug editor-status [--bundle-id B]
    -> {bridges: [{bundleId, installedBundleId, appName, project, port, host, pid, launched,
                   editor: waiting|attached|detached, since, helperPid, scriptPath}], checkedAt}
```

The bridges running now, read from `~/.local/state/ios-studio/editor-debug/<bundle id>.json`. Files of dead helpers are ignored. The folder keeps the newest 20 apps' files; running ones always stay.

Demo mode (`IOSSTUDIO_DEMO`): `editor-config` returns a plan and writes nothing. `editor` asks the demo daemon for `debug.start`, reports `ready` (with `swiftExpressions: true`), then `editor {state: "attached"}` after about three seconds, and ends on cancel. No files are written.

## Code and tests

- `helper/iosstudio/features/editor_debug.py`: configurations, the editors' lldb-dap (`prepare_dap`: the shadow and the launcher), the attach file and its stub, pruning, bridge state, the `/proc/net/tcp` check.
- `helper/iosstudio/features/lldb_shadow.py`: the shadow lldb and the launcher (`launcher_path`, `write_launcher`).
- `helper/iosstudio/features/editor_debug_bridge.py`: the `debug editor` stream and the one-shot commands.
- `helper/iosstudio/features/editor_debug_lldb.py`: the lldb commands that run inside lldb-dap.
- `helper/iosstudio/demo/debugger_demo.py`: demo answers.
- `tests/helper/test_editor_debug.py` covers config generation and merging, the attach file, pruning, state, and the bridge against a stand-in daemon (`tests/support/fake_deviced.py`).
- `tests/helper/test_editor_debug_shadow.py` covers the launcher, the configurations that point at it, migrating VS Code and Zed settings written before, the attach file's SDK step, `iosstudio-sdk-path` and the plain fallback note.
- `tests/cli/test_editor_debug_cli.py` covers demo shapes, the real CLI, and an end-to-end run. In that run, the toolchain's lldb-dap takes the written `launch.json` configuration, attaches through the bridge to a process under a local `lldb-server` (`IOSSTUDIO_DEBUG_PLATFORM=remote-linux`), stops at a breakpoint, reads a variable and detaches. It then gets the stub's message. The editor side runs the launcher named in the configuration, as VS Code does. It's skipped without the toolchain's lldb-dap, lldb's Python or `cc`. With `IOSSTUDIO_TEST_DARWIN_SDK=~/.config/swiftpm/swift-sdks/darwin.artifactbundle` it runs with the shadow lldb (built into the test's cache) and checks that lldb-dap maps only the shadow's liblldb and that the attach file sets the SDK.
