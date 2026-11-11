# Debug in editor: breakpoints in VS Code and Zed

Press F5 in VS Code (or start the debug task in Zed) and the editor's debugger attaches to your app on the iPhone. Breakpoints set in the editor stop the app, and the editor shows the stack, variables, stepping and its debug console. iOS Studio prepares everything the editor can't do by itself on Linux: the tunnel to the phone, the phone's debugserver and the DeviceSupport symbols. The editor's own debug adapter, `lldb-dap` from the Swift toolchain, does the debugging.

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
- Notices from the helper (no debug symbols, no build of the app on this computer) show as quiet lines under it.
- An error (the app isn't installed, another debugger is attached, the toolchain has no lldb-dap) shows its message
  with its fix button and Try again.

The button is disabled, with the reason in its tooltip, while there's no iPhone, the project has no bundle ID, or a
bridge already runs for it. A bridge that's still running when iOS Studio starts (from another window or before a
restart) shows on its card too, and its Stop works the same. Quitting iOS Studio stops the bridges it started.
The UI is `services/EditorDebugService.qml` (`app.editorDebug`) and `components/projects/EditorDebugBar.qml`; the
states come from `Model.editorDebugEvent` in `lib/Model.js` (`tests/qml/tst_editor_debug.qml`).

## How it works

1. **The editor's debug configuration, once per project.** iOS Studio adds one configuration named "Attach to <App> on iPhone (iOS Studio)" to `.vscode/launch.json` (VS Code) or `.zed/debug.json` (Zed). Nothing else in the project is written. Every configuration and comment already in those files stays byte for byte (they're JSON with comments, edited as text). If our entry is already there and current, nothing changes. If it's outdated, only our entry is replaced. The configuration has a single attach command, which reads the app's *attach file*. It names that file relative to your home folder, so the project carries no machine path and can be committed.
2. **Debug in editor, each time.** The helper (`debug editor`) asks the device daemon to forward the phone's debugserver to a loopback port (`debug.start`, the same plumbing as iPhone › Debug). If the app isn't running, iOS Studio launches it suspended, waiting for the debugger. iOS Studio then writes the attach file with the port and the app's process id and reports `ready`. It holds the forward open until you stop it, the app exits, or two hours pass.
3. **The attach file**, `~/.local/state/ios-studio/editor-debug/<bundle id>.lldb`, holds lldb commands. They do what iOS Studio's own debugger does (`helper/iosstudio/features/lldb_driver.py`):
   - `settings set target.memory-module-load-level minimal`, because otherwise lldb reads every system library out of the phone's memory, which takes minutes over Wi-Fi.
   - `platform select remote-ios --sysroot "<DeviceSupport symbols>"` when the debug symbols are downloaded.
   - `target create "<your local build>"` and the executable's path on the phone.
   - `iosstudio-connect connect://127.0.0.1:<port>`. A plain synchronous `process connect` fails against a debugserver that has no process yet, so this command connects asynchronously and waits.
   - `process attach --pid <pid>`.

   The helper commands (`iosstudio-check`, `iosstudio-connect`, `iosstudio-remote-file`, `iosstudio-fail`) come from `iosstudio_editor_attach.py` next to the attach file, a copy of `helper/iosstudio/features/editor_debug_lldb.py` loaded into lldb-dap's Python.
4. **After that it's the editor's session.** lldb-dap sends the breakpoints, and on `configurationDone` it resumes the app. A launched app starts running only then. While the bridge runs, it watches for the editor's connection to the forward (the open loopback connections in `/proc/net/tcp`, once a second). It reports `attached` when the editor connects and `detached` when it disconnects. You can attach again (F5) as often as you like while the bridge runs.
5. **When it ends**, iOS Studio releases the forward (`debug.stop`) and puts the stub back in the attach file. If iOS Studio launched the app suspended and no editor ever attached, it ends that process too; otherwise it would stay frozen. Pressing F5 without a running bridge fails with "Start 'Debug in editor' in iOS Studio first" in the editor's Debug Console. A stale attach file left behind by a crash fails the same way, because its first command checks that the iOS Studio process that wrote it is still running (by pid and start time). It never connects to an old port.

## VS Code

You need the Swift extension (`swiftlang.swift-vscode`), the one Settings › Code editor recommends. VS Code installs the LLVM lldb-dap extension (`llvm-vs-code-extensions.lldb-dap`) with it as a dependency. The configuration uses `"type": "swift"`. For Swift 6 and newer toolchains, the Swift extension runs it with lldb-dap from the same toolchain as `swift` (`debugAdapterExecutable`), so there's nothing to set up:

```json
{
  "name": "Attach to MyApp on iPhone (iOS Studio)",
  "type": "swift",
  "request": "attach",
  "attachCommands": [
    "script import os; lldb.debugger.HandleCommand('command source \"%s\"' % os.path.expanduser('~/.local/state/ios-studio/editor-debug/com.example.myapp.lldb'))"
  ],
  "timeout": 300
}
```

`timeout` is how long lldb-dap waits for the app to stop after attaching. Over Wi-Fi, without updated debug symbols, that can take minutes. If you switched the Swift extension to CodeLLDB (`swift.debugger.debugAdapter`), this configuration doesn't work: CodeLLDB has no `attachCommands`. Keep the default, lldb-dap.

Why `"type": "swift"` and not `"type": "lldb-dap"`: both run lldb-dap with the same attach fields. Only the Swift extension knows where the toolchain's lldb-dap is; the plain lldb-dap extension looks for `lldb-dap` on PATH, and the Swift toolchain doesn't put it there.

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

The Swift extension looks for `lldb-dap` on PATH (or `swiftly` / `xcrun`), and the Swift toolchain keeps it next to `swift` instead (for example `/usr/lib/swift/usr/bin/lldb-dap`). Tell Zed where it is, once, in its user settings (`~/.config/zed/settings.json`):

```json
{
  "dap": {
    "Swift": { "binary": "/usr/lib/swift/usr/bin/lldb-dap" }
  }
}
```

`debug editor-config` lists this as a note when `lldb-dap` isn't on PATH. Start the configuration from the debug panel (`debugger: start`).

## Neovim (nvim-dap)

iOS Studio doesn't change your Neovim config. With [nvim-dap](https://github.com/mfussenegger/nvim-dap), add an adapter for the toolchain's lldb-dap and one configuration per app. Run "Debug in editor" in iOS Studio, then `:DapContinue`:

```lua
local dap = require("dap")
dap.adapters["lldb-dap"] = {
  type = "executable",
  -- lldb-dap sits next to the real `swift` binary
  command = vim.fn.fnamemodify(vim.fn.resolve(vim.fn.exepath("swift")), ":h") .. "/lldb-dap",
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

## Limits

- **Attaching pauses the app** until lldb-dap has read it (seconds over USB, longer over Wi-Fi without the debug symbols). Download the symbols once under iPhone › Debug › Debug symbols, so Swift values (String, Array) read right and attaching is fast.
- **Only your own development-signed apps** can be debugged. App Store and system apps can't be (the phone refuses).
- **One debugger per app.** If iOS Studio's own debugger or an AI agent is attached, "Debug in editor" reports `busy`. Detach there first.
- Breakpoints in your code need a build of the app on this computer (iOS Studio uses the newest one: `xtool/<App>.app`, `.build/<config>/<Product>-App` or the `.ipa`). Build and run the app from iOS Studio first.
- Stopping the bridge while the editor is attached ends the editor's session.
- The bridge needs the Debugger support step in Setup: the attach file's commands run in lldb's Python.
- If `XDG_STATE_HOME` points outside your home folder, the configuration names the attach file by its absolute path.

## Helper commands (group `debug`)

```
debug editor-config --project P [--editor vscode|zed|neovim] [--dry-run]
    -> {project, bundleId, appName, configName, scriptPath, dryRun, applied, notes: [str],
        editors: [{id, name, path, state: missing|present|outdated,
                   action: {kind: write|merge|skip, path, scope: "project", detail, preview?, done?}}]}
```

Default editor: the one remembered by Settings › Code editor; without one, every installed one among VS Code and Zed. Neovim returns no editor and a note pointing here. `preview` is the new file (write) or a unified diff (merge). Without `--dry-run`, it also writes the stub attach file, so F5 before a bridge gives the readable message.

```
debug editor --project P | --bundle-id B [--launch auto|attach|suspended] [--udid U] [--editor E] [--no-config]
```

A stream (JSON lines, ends with `done`). `--project` looks up the installed bundle id (`XTL-<TEAM>.<id>` on free teams) with `apps.list`. `--bundle-id` takes the installed id and finds the project by it. `--launch auto` attaches when the app runs, else launches it suspended. Events:

| event | fields |
|---|---|
| `debug` | `{mode: "editor", bundleId}` |
| `step` | ids `lldb`, `config` (unless `--no-config` or no project), `forward` |
| `notice` | ids `editor-config` (the config notes), `no-local-build`, `no-symbols`, `symbols-update`, `waiting` (a suspended launch waits for the editor), `editor-cut`, `keepalive` |
| `ready` | `{bundleId (as installed), appName, port, host, pid, launched, scriptPath, editors: [ids configured], configName, symbolsReady, localBuild}` |
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

Demo mode (`IOSSTUDIO_DEMO`): `editor-config` returns a plan and writes nothing. `editor` asks the demo daemon for `debug.start`, reports `ready`, then `editor {state: "attached"}` after about three seconds, and ends on cancel. No files are written.

## Code and tests

- `helper/iosstudio/features/editor_debug.py`: configurations, the attach file and its stub, pruning, bridge state, the `/proc/net/tcp` check.
- `helper/iosstudio/features/editor_debug_bridge.py`: the `debug editor` stream and the one-shot commands.
- `helper/iosstudio/features/editor_debug_lldb.py`: the lldb commands that run inside lldb-dap.
- `helper/iosstudio/demo/debugger_demo.py`: demo answers.
- `tests/helper/test_editor_debug.py` covers config generation and merging, the attach file, pruning, state, and the bridge against a stand-in daemon (`tests/support/fake_deviced.py`).
- `tests/cli/test_editor_debug_cli.py` covers demo shapes, the real CLI, and an end-to-end run. In that run, the toolchain's lldb-dap takes the written `launch.json` configuration, attaches through the bridge to a process under a local `lldb-server` (`IOSSTUDIO_DEBUG_PLATFORM=remote-linux`), stops at a breakpoint, reads a variable and detaches. It then gets the stub's message. It's skipped without the toolchain's lldb-dap, lldb's Python or `cc`.
