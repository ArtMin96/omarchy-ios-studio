# AI agents (MCP)

iOS Studio includes an [MCP](https://modelcontextprotocol.io) server, so AI coding agents such as Claude Code, Codex, opencode, Cursor, Gemini CLI, Crush and Zed can work on your iPhone apps. An agent can check your setup, create projects, build them, read compiler errors and build logs, fix the code, and install and launch the app on your iPhone.

The server runs on your computer over stdio and uses the same helper as the app. It never sees your Apple ID password or 2FA codes, and it can't pair a phone. For those steps the agent sends you to the iOS Studio window.

## Enable it

![Settings › AI agents](screenshots/agents.png)

Open Settings › AI agents in iOS Studio, or click Connect AI agents on Home. It lists the AI tools it finds on your computer:

1. If the server isn't ready yet, press Prepare. This creates a small Python environment for it in `~/.local/share/ios-studio/mcp-venv` (a one-time download of about 30 MB), so your agent doesn't have to wait the first time it starts the server.
2. Click Connect next to your tool, then start a new session in that tool so it loads the server.
3. Click the test tube button (Test the connection). iOS Studio starts the server the way your tool will and shows how many tools it offers.

How each tool is connected:

| Tool | How | Where it ends up |
|---|---|---|
| Claude Code | `claude mcp add --scope user` | `~/.claude.json` |
| Gemini CLI | `gemini mcp add -s user` | `~/.gemini/settings.json` |
| Codex | edits the config file | `~/.codex/config.toml` (`$CODEX_HOME` if set) |
| opencode | edits the config file | `~/.config/opencode/opencode.json` |
| Cursor | edits the config file | `~/.cursor/mcp.json` |
| Crush | edits the config file | `~/.config/crush/crushrc` (or `crush.json` if that's what you use) |
| Zed | edits the config file | `~/.config/zed/settings.json` |

When iOS Studio edits a config file, it changes only the `iosstudio` entry. It parses the file first (JSON with comments, TOML or crushrc) and leaves your other servers, their order, your comments and formatting alone. It checks the result before saving, keeps a timestamped backup in `~/.local/state/ios-studio/backups/`, and puts the original back if anything goes wrong. If the file already has a mistake in it, iOS Studio leaves it alone and tells you.

Disconnect removes the entry again. The copy button (Copy setup snippet) copies the manual setup for a tool, if you'd rather do it yourself. If a tool shows Needs update, its entry points to a different copy of iOS Studio, usually because the app folder moved; press Update.

With Connect in demo mode turned on, the tools you connect next get the server with made-up data (see [Demo mode](#demo-mode)).

The same actions work from a terminal:

```bash
bin/iosstudio-helper agents list                               # what's installed and connected
bin/iosstudio-helper agents connect --client codex --dry-run   # show the planned change; nothing is written
bin/iosstudio-helper agents connect --client codex             # also: disconnect, --demo
bin/iosstudio-helper agents test --client codex                # initialize + tools/list, prints the tool count
bin/iosstudio-helper agents prepare                            # create or update the server's environment
```

### Set it up by hand

The server is `bin/iosstudio-mcp` in your iOS Studio checkout. With the standard install, that's:

```
~/.local/share/omarchy-ios-studio/bin/iosstudio-mcp
```

The first start creates the server's Python environment (see above) if Prepare hasn't already. It uses `uv` if it's installed and `python -m venv` otherwise, and needs internet. Later starts are instant.

Register that path with your agent. Most config files don't expand `~`, so write the full path, such as `/home/you/.local/share/omarchy-ios-studio/bin/iosstudio-mcp`. The server name is `iosstudio`, without an underscore, because Gemini CLI doesn't allow underscores in server names.

Claude Code:

```bash
claude mcp add --scope user iosstudio -- "$HOME/.local/share/omarchy-ios-studio/bin/iosstudio-mcp"
```

Codex CLI (`~/.codex/config.toml`):

```toml
[mcp_servers.iosstudio]
command = "/home/you/.local/share/omarchy-ios-studio/bin/iosstudio-mcp"
args = []
startup_timeout_sec = 30
tool_timeout_sec = 120
```

opencode (`~/.config/opencode/opencode.json`):

```json
{
  "mcp": {
    "iosstudio": {
      "type": "local",
      "command": ["/home/you/.local/share/omarchy-ios-studio/bin/iosstudio-mcp"],
      "enabled": true,
      "timeout": 120000
    }
  }
}
```

Cursor (`~/.cursor/mcp.json`, also used by `cursor-agent`):

```json
{ "mcpServers": { "iosstudio": { "command": "/home/you/.local/share/omarchy-ios-studio/bin/iosstudio-mcp", "args": [] } } }
```

Gemini CLI:

```bash
gemini mcp add -s user --timeout 600000 iosstudio "$HOME/.local/share/omarchy-ios-studio/bin/iosstudio-mcp"
```

Without `-s user`, the server is added only to the current project. Don't add `--trust`, so Gemini still asks before it runs a destructive tool.

Crush (`~/.config/crush/crushrc`):

```bash
mcp add iosstudio --type stdio --command "$HOME/.local/share/omarchy-ios-studio/bin/iosstudio-mcp" --timeout 120
```

Zed (`~/.config/zed/settings.json`):

```json
"context_servers": {
  "iosstudio": { "command": "/home/you/.local/share/omarchy-ios-studio/bin/iosstudio-mcp", "args": [], "env": {} }
}
```

Then ask the agent something like *"Check that iOS development is ready"* or *"Build HabitTracker and fix the errors"*. If your agent supports MCP prompts, you can also run `fix_build_errors`, `diagnose_crash`, `new_app` or `get_ready` as slash commands.

## Tools

The server has 76 tools, all prefixed `ios_`. Start with `ios_status`. It reports what's ready and, for each problem, which tool fixes it.

| Area | Tools |
|---|---|
| Overview | `ios_status`, `ios_open_app` (opens the app window on a page, for steps only you can do) |
| Projects | `ios_project_list`, `ios_project_info`, `ios_project_templates`, `ios_project_create`, `ios_project_add`, `ios_project_convert`, `ios_project_set`, `ios_project_build_settings`, `ios_project_remove` |
| Builds | `ios_build_start`, `ios_build_status`, `ios_build_diagnostics`, `ios_build_log`, `ios_build_respond`, `ios_build_cancel`, `ios_build_history`, `ios_build_clean`, `ios_run` |
| Apple ID and signing | `ios_account_status`, `ios_signing_status`, `ios_project_renew`, `ios_project_autorenew`, `ios_account_logout` |
| Toolchain | `ios_toolchain_check`, `ios_toolchain_info`, `ios_toolchain_install`, `ios_toolchain_clean` |
| iPhone | `ios_device_list`, `ios_device_info`, `ios_device_select`, `ios_device_reconnect`, `ios_device_prepare` (Developer Mode and the developer disk image) |
| Apps on the iPhone | `ios_app_list`, `ios_app_install`, `ios_app_launch`, `ios_app_kill`, `ios_app_uninstall`, `ios_process_list`, `ios_location_set`, `ios_location_clear` |
| Screen | `ios_screenshot`, `ios_screen_record`, `ios_mirror_status` |
| Logs and crashes | `ios_app_run_and_capture`, `ios_logs_start`, `ios_logs_read`, `ios_logs_stop`, `ios_crash_list`, `ios_crash_get`, `ios_app_console_read` |
| Network | `ios_network_conditions`, `ios_network_condition_set`, `ios_network_condition_clear`, `ios_pcap_capture` |
| Performance | `ios_metrics_start`, `ios_metrics_summary`, `ios_metrics_series`, `ios_metrics_events`, `ios_metrics_stop`, `ios_launch_profile`, `ios_memory_leaks` |
| Files and camera tests | `ios_media_list`, `ios_media_pull`, `ios_files_list`, `ios_files_pull`, `ios_files_push`, `ios_files_delete`, `ios_camera_test` |
| Debugger | `ios_debug_backtrace`, `ios_debug_session_start`, `ios_debug_session_wait`, `ios_debug_session_command`, `ios_debug_session_end`, `ios_debug_symbols_status` |

`ios_metrics_start(bundle_id)` samples an app about once a second in the device service (CPU, memory, threads, energy, display frame rate, GPU, network, disk, wakeups and battery). The service keeps about an hour and computes the stats, so `ios_metrics_summary` stays around 1 to 2 KB: min, average, 95th percentile and max of the main series, the memory trend, CPU spikes, frame-rate drops, hitches, battery drain, energy by component, and anomalies in plain words, worst first. `ios_metrics_series` downsamples one metric to at most 60 buckets and saves every sample as a CSV, returned as a path and a resource link. `ios_metrics_events` pages through hangs, jetsam kills, crashes and hitches by cursor. A page stays under the size limit (`max_bytes` raises it), and `next_cursor` always points right after the last event returned, so reading on never skips one. A session ends with `ios_metrics_stop` or when the agent's session ends. `ios_launch_profile` quits and relaunches the app, and `ios_memory_leaks` pauses it while its memory is scanned, so both are dry runs unless `confirm=true`. They run as jobs: when the reply says `running`, call again with its `job_id`.

Builds and installs run as background jobs, because they can take longer than an agent waits for one tool call. `ios_app_install` waits up to `wait_seconds` (25 at most); a longer install returns `state: "running"` and an `install_id`, and calling it again with `install_id` waits for the result. The install keeps going in the server meanwhile. `ios_project_create` waits the same way; if it isn't done yet it says so, and calling it again with the same arguments waits for the same creation. `ios_build_start`, `ios_run`, `ios_project_renew` and `ios_toolchain_install` return a `build_id` straight away, and the agent checks on it with `ios_build_status`, which waits up to 25 seconds per call. `ios_build_cancel` stops a job and returns once it has really stopped. Cancelled before the install step, nothing is installed or launched. Cancelled while `ios_run` installs over Wi-Fi, the iPhone may still finish that install, but the app isn't launched, and the job's `error` says so. Jobs stop when the agent's session ends. The server keeps the last 50 finished jobs, each with its newest 2,000 log lines; `ios_build_log` reads older lines from the build's log file.

The iPhone tools talk to the same background service as the iOS Studio window, so the agent and the window share one connection to your phone. The service starts on the first iPhone tool call, not when the agent starts, and stops by itself about two minutes after the last of them closes.

A few tools need more explanation:

- `ios_build_diagnostics` covers more than the compiler, so an agent rarely needs the raw log. Each entry has a `category`: `compiler` (file:line errors), `linker` (one entry per block: the undefined or duplicate symbols, who references them and where, or a missing library or framework), `package` (versions that can't be resolved, repositories that can't be cloned, Package.swift errors, unknown packages or products), `signing` (Apple ID sign-in, the 10 app ids per week and 3 apps per iPhone limits, certificates, provisioning, code signing), `device` (not trusted, locked, Developer Mode off, not found), `toolchain` (missing SDK, or a compiler crash folded into one entry with the code it was working on), `config` (xtool.yml, Info.plist) or `other` (any other `error:` line). Non-compiler entries carry a one-line `hint`, which names the tool that fixes the problem where there is one, and a few `details`. `category=` filters the list and `categories` counts entries per category. A build refused before it starts (no iPhone, not signed in, a missing toolchain, a project that generates its own `xtool.yml`) has no output, so its error becomes the one entry, with a category and, where there is one, a hint. Only when nothing was recognised does the result point to `ios_build_log(level='error')`.
- `ios_project_info` on a project that generates its own `xtool.yml` says it isn't built until its build settings allow it, and shows `suggested_build_settings` from the project's own scripts; the iPhone tools still work on the installed app by bundle id.
- `ios_project_build_settings` is for projects that normally build through their own scripts. It views and changes four per-project settings: `prebuild`, a command run in the project folder before every build (re-signing included) as the build's first step, with its output in the build log; `env`, extra environment variables for the prebuild and xtool (merged; a null value removes one); `open_files`, the soft open-file limit raised for the build only (it fails with a clear error when the session's hard limit is lower); and `allow_generated`, which lets iOS Studio build a project whose xtool.yml says it's GENERATED (refused otherwise). The prebuild is split into arguments like a shell would and run without one, so `|`, `&&` and `$VAR` don't work: put such steps in a script. With no change arguments the tool shows the settings and what the project's own scripts suggest (`run.sh` exporting variables or raising `ulimit -n`, a `generate.sh` that writes xtool.yml); `ios_project_info` shows both too. A change is a dry run listing each change until `confirm=true`. A failing prebuild ends the build with one `config` diagnostic carrying its last output lines.
- `ios_app_run_and_capture` is for "launch it and tell me what happened". It launches an app (optionally installing its last signed build or `ipa_path` first), records its log for a few seconds, and reports whether it started, whether it was still running, the errors it logged and any new crash report with the crashing frames. If the install takes longer than the call can wait, the result has `state: "installing"`, nothing is launched, and `installed.install_id` is the id to pass back as `install_id` to wait for it and then capture.
- The phone logs about 2,000 lines a second, so `ios_logs_start` always needs a filter, such as the app's bundle id or a minimum level. The server keeps the newest 5,000 lines of each log session. The agent reads them with `ios_logs_read` from a cursor and can wait up to 25 seconds for new lines. A session nobody reads for 10 minutes stops by itself.
- `ios_screenshot` returns an image the agent can see, at most 1568 pixels on the long side and 350 KB, and the path of the saved file. With `inline=false` it returns only the path.
- `ios_screen_record` records the screen for up to 60 seconds into an MP4, for a bug report. It returns the path, length, size, frame rate and mode, and one small preview image (320 pixels, at most 60 KB). The recording runs in the background: if it isn't saved within `wait_seconds` (25 at most), the result has a `recording_id` to call again with. It uses the smooth stream on iOS 27 or later and screenshots otherwise. Over Wi-Fi the screenshot mode gets about 1.5 to 2 frames a second, because each screenshot of a busy screen takes the phone about 630 ms. `ios_mirror_status` is read-only and says which mode the live view uses, the frame rate it reaches, which modes are available and why not, and whether UxPlay and ffmpeg are installed.
- `ios_location_set` changes the location for the whole phone. It lasts until it's cleared or the background service stops.
- `ios_device_prepare` can't turn on Developer Mode for you. It tells you what to tap on the phone.
- `ios_network_condition_set` makes the whole phone act as if it's on a slow network (Edge, 3G, LTE, DSL, packet loss, slow DNS), running hot, or with a throttled GPU. Like the destructive tools it returns a dry run first, which says what changes and, over Wi-Fi, that it also slows the agent's own link to the phone; the agent asks you before it repeats the call with `confirm=true`. The condition switches off after `minutes` (default 5, at most 30), on `ios_network_condition_clear`, or when the MCP server stops. The two profiles that cut the network off are refused over Wi-Fi. `ios_network_conditions` lists the profiles and the active one in about 6 KB.
- `ios_pcap_capture` records the phone's traffic for up to 120 seconds, for one app (`bundle_id`) or the whole phone, and needs a USB cable. It returns packets and bytes per process, the top 10 remote hosts and ports, and protocol and service counts, read from the packet headers only. The full recording is a pcapng file whose path is in `file`. The call waits up to 25 seconds; a longer capture returns `state: "running"` and a `capture_id`, and calling the tool again with `capture_id` waits for the result (`stop=true` ends it early). The capture stops if the MCP server exits.
- `ios_media_list` lists the camera roll (dates, sizes, no images) in pages, and `ios_media_pull` copies photos and videos to a folder in your home folder, optionally as JPEG. The camera roll is never changed.
- `ios_files_list`, `ios_files_pull`, `ios_files_push` and `ios_files_delete` work in an app's files: the whole container for your own builds, Documents for App Store apps that share files. A push that would replace files returns `needs_confirm` and changes nothing until it's repeated with `confirm=true`; `ios_files_delete` is a dry run until `confirm=true`.
- `ios_camera_test` takes the app as `bundle_id` or `project_path`, like the other app tools. It runs your app's camera code on test images and videos and reports a summary of its `done.json`, the output files, key log lines and any crash. The app needs a small hook: see [camera-testing.md](camera-testing.md).
- Copies and camera tests can take longer than one call. They keep running in the server and return `state: "running"` with a `transfer_id` or `test_id`; calling the same tool again with that id waits for the result, and `cancel=true` stops it.
- The debugger tools attach LLDB to one of your own apps, which pauses it. `ios_debug_backtrace` needs `confirm=true` and runs as a job: if it's still running after `wait_seconds`, the agent calls it again with the `job_id`. It returns the top 20 frames of the crashed thread (or the main one), the other threads grouped by identical stacks, and a file with every frame.
- `ios_crash_get` resolves your app's frames to file:line against the build that crashed, which iOS Studio keeps on every Run, Renew and signed build (`~/.cache/ios-studio/symbols/`, the last 10 per project). The result has `app_frames` (frame number, function, `file` relative to `project_path`, line, column) and `symbolicated`: `full`, `partial`, `none` or `not-needed`, with a `symbolication_note` that says why and what to do (usually: rebuild that exact version and reproduce the crash). Resolved lines in `frames` and `section='threads'` read `<n> <image> <function> (<file>:<line>)`. See [features/crash-symbolication.md](features/crash-symbolication.md).
- `ios_debug_session_start` sets breakpoints (`File.swift:42` or a function name) and returns a `session_id`. `ios_debug_session_wait` waits for the next stop, and `ios_debug_session_command` steps (`next`, `step`, `finish`, `continue`, `pause`) or inspects (`frame_variables`, and `evaluate` with an expression of at most 500 characters). A stop shows its file:line, the top 20 frames and frame 0's variables one level deep. `ios_debug_session_end` detaches, so the app keeps running, or quits it with `kill=true`. A session nobody polls for 10 minutes detaches by itself, and so does every session when the server stops.
- What an app prints (Swift `print()`, stdout and stderr) never reaches the iPhone's log, so the server captures it when it launches the app: `ios_run`, `ios_app_run_and_capture` (on by default, `console=false` turns it off) and `ios_app_launch(console=true)`. `ios_app_run_and_capture` adds a `console` summary (lines printed, how many to stderr, the session id) and the stderr lines plus the latest ones after the log lines, sharing the size limit with them. `ios_app_console_read(bundle_id or session_id)` pages through the rest by cursor, like `ios_logs_read`: `grep`, `stream='stderr'`, `newest`, `wait_seconds` up to 25, `max_bytes`, and `truncated` with a `note` when a page is cut. The device service keeps 10,000 lines per launch, keeps the first 8 KB of a line, and drops (and counts) more than 2,000 lines a second. On a real iPhone stdout and stderr arrive together. An app that was already running has no captured output. See [features/app-console.md](features/app-console.md).
- `ios_debug_symbols_status` says whether lldb is installed and the state of this iPhone's debug symbols: `state` (missing, downloading, extracting, ready, outdated, incomplete or failed), `update_reason` when they need an update, `last_error`, and `how_to_fix`, which names the button the user presses in iPhone › Debug (Download, Update, Unpack again or Try again). An outdated set still works, but attaching takes minutes over Wi-Fi. The download (about 7 GB, once per iOS version) only starts from that tab; there is no tool for it.

The server also offers a few read-only resources: the live log of a job (`iosstudio://builds/{build_id}/log`), crash reports (`iosstudio://crashes/{file}`), large outputs a tool saved to a file (`iosstudio://cache/{kind}/{name}`), the last known iPhone state, the troubleshooting guide and this page. Each one returns at most 100 KB and says which tool reads the rest.

## Output size

Build logs, crash reports and the phone's log can be megabytes long, and an agent pays for every byte in its context window. Every tool follows the same rules, and new tools should too. The helpers are in `mcp/iosstudio_mcp/budget.py`.

- A result is at most 12 KB (about 3,000 tokens), counting the text block and `structuredContent` together. Tools that return bulk output (`ios_build_log`, `ios_build_diagnostics`, `ios_logs_read`, `ios_crash_get`, `ios_app_run_and_capture`, `ios_metrics_events`, `ios_camera_test`) take `max_bytes`, which raises that combined limit to at most 48,000. For every tool, a final check shrinks the longest lists, and then the longest strings, at any depth, until the result fits, marks it `truncated` and says what was cut. It never cuts cursors, ids or paths. It never turns a large result into an error.
- Every result is sent twice, so it reaches clients that give the model `content` and those that give it `structuredContent`. `structuredContent` has every field, log lines and crash text included. The text block shows the same result for reading: compact JSON of the other fields, then the log lines or crash text as plain lines. Because of the copy, bulk lines get a bit under half of the budget, about 5 KB of lines by default and about 22 KB with `max_bytes=48000`.
- Paged lists (`ios_project_list`, `ios_app_list`, `ios_files_list`, `ios_media_list`) are sized so the final check never has to cut them: `next_offset` always follows the last item returned.
- When something is cut, `note` says how much and how to get the rest, for example `Showing lines 19940–19999 (60 of 20000 lines). Call again with before=19940 for earlier lines`.
- Logs default to the last 60 lines. `ios_build_log` also takes `head`, `before`, `offset`/`limit`, `grep` with `context` lines, and `level`. Lines are cut at 300 characters (`line_chars` changes that) and repeated lines fold into one line ending in `×N`. For a finished build it reads the log file in `~/.local/state/ios-studio/build-logs/` directly (only the last 32 MB of a bigger one, with the file's own line numbers). The helper keeps the logs of the newest 300 builds and deletes older ones.
- Build diagnostics are grouped by severity, category and message, with a count and the first three `file:line:col` locations relative to the project (when there is a location). Notes are hidden unless `include_notes=true`. A list returns 25 groups by default, and a build status embeds at most 5. Messages are cut at 300 characters and `details` at 8 lines of 200.
- While a job runs, `ios_build_status` returns only its state, step, progress, ETA and what changed since the last poll (`new_log_lines`, `new_errors`). It returns the full view when the job finishes or needs input, or with `verbose=true`.
- Large artifacts go to files under `~/.cache/ios-studio/mcp/`. The result gives the path, the size and a `resource_link`, plus an excerpt that fits the budget. `ios_crash_get` returns the parsed summary and reads the rest one `section` at a time (`threads`, `images`, `raw`), paged with `start_line`; a section page leaves out the summary's frames so more lines fit, and `end_line` is always the next line to read.
- Screenshots are JPEG, at most 1568 pixels on the long edge. The quality steps down from 80 to 65 and then 50 until the image is under 350 KB. PNG is sent only if it fits.
- Errors are one line plus a next step. Details are cut at 300 characters, and raw stderr never goes into a result; the error points to the log file instead.
- A debugger backtrace shows 20 frames of one thread and groups the others by identical stacks (12 groups at most); every frame of every thread goes to `~/.cache/ios-studio/mcp/debug/`. A debugger stop shows 20 frames and 30 variables, with values cut at 200 characters.
- Other lists have caps too: `ios_process_list` returns 50 by default and 200 at most, and account and signing lists show 20 with totals. Free-form `data` and dry-run `would` lists stop at 20 items with "+N more".

## Safety

No tool takes your Apple ID password or a 2FA code. To sign in, pair the iPhone, turn on Developer Mode or download Xcode, the agent opens iOS Studio on the right page and asks you to do it there.

`ios_project_remove`, `ios_build_clean`, `ios_toolchain_clean`, `ios_account_logout`, `ios_app_uninstall`, `ios_files_delete`, `ios_launch_profile`, `ios_memory_leaks`, `ios_debug_backtrace` and `ios_project_autorenew` (`enable` installs the daily renewal timer and `disable` removes it) take a `confirm` argument, which is false by default. With `confirm` false, the tool changes nothing and lists what it would do, and the agent is told to ask you before it repeats the call with `confirm=true`.

When a job needs a decision, such as replacing your signing certificate or confirming a 2 GB toolchain download, it pauses. The agent shows you the question and sends back your answer. Options that can't be undone also need `confirm=true`. Installing system packages shows the usual polkit password dialog on your screen.

Every tool tells your agent whether it only reads, changes something or can destroy something. Agents that ask before risky actions (Zed, Codex, Gemini CLI) use this to decide when to ask.

Besides the projects iOS Studio already lists, the server only accepts project folders inside your home folder. To allow other places, set `IOSSTUDIO_MCP_ALLOW_PATHS` to a colon-separated list of folders. Commands always run with fixed arguments, never through a shell.

## Demo mode

Set `IOSSTUDIO_DEMO=1` in the server's environment to try everything on made-up data, with nothing real changed: a fake Apple ID, iPhone and three projects, one of which fails to build with two compiler errors. Use it to watch how an agent drives the tools, or for testing. Add `IOSSTUDIO_DEMO_FAIL=linker` (undefined symbols) or `IOSSTUDIO_DEMO_FAIL=package` (a version conflict) to make the other projects' builds fail too. That output goes through the real build-output parser, so `ios_build_diagnostics` shows real categories and hints.

```bash
claude mcp add --scope user iosstudio-demo -e IOSSTUDIO_DEMO=1 -- "$HOME/.local/share/omarchy-ios-studio/bin/iosstudio-mcp"
```

In other clients, add the variable to the server's `env` (`environment` in opencode).

To test the server without an agent, use the MCP Inspector:

```bash
npx @modelcontextprotocol/inspector --cli -e IOSSTUDIO_DEMO=1 bin/iosstudio-mcp --method tools/list
npx @modelcontextprotocol/inspector --cli -e IOSSTUDIO_DEMO=1 bin/iosstudio-mcp --method tools/call --tool-name ios_status
```

[`mcp/evaluation.xml`](../mcp/evaluation.xml) has fourteen demo-mode questions for checking how well an agent uses the tools. See [`mcp/EVALUATION.md`](../mcp/EVALUATION.md).

## Troubleshooting

- If the server doesn't start, run `bin/iosstudio-mcp < /dev/null` in a terminal. It prints setup progress and errors to stderr. Delete `~/.local/share/ios-studio/mcp-venv` to rebuild the environment.
- For more detail, set `IOSSTUDIO_MCP_LOG=DEBUG`. Logs go to stderr, which most agents show in their MCP logs.
- If a tool says the iPhone service's socket path is too long (`socket-path-too-long`), the runtime folder is nested too deep for a Unix socket (107 bytes at most). Set `IOSSTUDIO_RUNTIME_DIR` to a shorter folder and restart the server.
- If calls time out, raise the client's tool timeout to 120 seconds (Codex `tool_timeout_sec`, opencode `timeout`). Status calls wait at most 25 seconds each.
