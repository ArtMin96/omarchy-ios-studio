# App console: what your app prints

Most Swift developers debug with `print()`. On a real iPhone that output goes to the app's stdout,
which never reaches the device log, so before this feature it simply disappeared. iOS Studio now
launches your app with its stdout and stderr attached to a socket and shows what it prints.

## Where it shows up

- **Logs › Console** (the app): the newest launch, live. Launch starts one of your apps with the
  console on (it restarts the app if it's running). Run on a project does the same after installing.
  The toolbar filters the text, pauses the view, copies, exports to Downloads and clears the view.
  stderr lines are amber (red when they look like an error) with `err` next to the time, when the
  phone sends stderr separately (see the limits below). When there are several launches a picker
  switches between them; the view follows the newest one unless you pick an older one.
- **MCP**: `ios_run`, `ios_app_run_and_capture` (on by default, `console=false` turns it off) and
  `ios_app_launch(console=true)` capture it. `ios_app_run_and_capture` is a job (call it again with
  `job_id` while `state` is `running`) and returns a `console` summary
  and the stderr lines plus the latest ones; `ios_app_console_read(bundle_id or session_id)` pages
  through everything with a cursor (`grep`, `stream`, `newest`, `wait_seconds`, `max_bytes`). See
  [mcp.md](../mcp.md).
- **Daemon**: `app.launch` with `console: true` and the `console.*` commands, see
  [deviced-protocol.md](../deviced-protocol.md#app-console-console-used-by-logs--console-and-ios_app_console_read).

## How it works

CoreDevice can attach a launched process's standard input, output and error to a socket. The
daemon opens one with pymobiledevice3's `OpenStdioSocketService`
(`com.apple.coredevice.openstdiosocket`), reads its 16-byte identifier, and passes it to
`AppServiceService.launch_application(..., stdio_identifier=...)`. That is what
`pymobiledevice3 developer core-device launch-application --console` and Xcode's
`devicectl device process launch --console` do. The phone runs the app on a pseudoterminal and
writes its output to the socket, and closes the socket when the app exits.

```
Swift print() ──pty──▶ coredeviced ──stdio socket (tunnel)──▶ device daemon
                                                       LineSplitter ▶ ConsoleSession ring
                                                         │ `console` events (100 ms)   │ console.read (cursor)
                                                         ▼                              ▼
                                                   Logs › Console               ios_app_console_read
```

- `helper/iosstudio/device/features/console/core.py`: shared by both daemons. `LineSplitter` turns bytes into lines,
  `ConsoleSession` holds one launch's lines, `register_commands()` adds the commands, events and
  cleanup.
- `helper/iosstudio/device/features/console/real.py`: the iPhone side (the stdio socket, the launch).
  `app.launch` (`device/features/apps/real.py`) hands a `console: true` request to it, and it calls
  `app.launch` back with the socket's identifier, so launch errors (locked, untrusted developer)
  read the same as before.
- `helper/iosstudio/device/features/console/demo.py`: a made-up app that prints, including the awkward cases.
- `components/logs/LogConsoleTab.qml`: the tab. It reads the daemon's buffer when it opens (and whenever it
  falls behind), then appends the live `console` events.
- `mcp/iosstudio_mcp/tools/console.py`: `ios_app_console_read` and the capture summary.

## Limits and choices

| | |
|---|---|
| Lines | Split on `\n`; a CR before it is dropped, and a carriage return inside a line (a progress bar) keeps what was drawn last. Terminal escape sequences and control characters are removed. |
| Partial lines | `print(x, terminator: "")` waits for its newline; after a second of quiet (or when the app exits) it's shown as a line of its own. |
| Encoding | UTF-8; invalid bytes become U+FFFD. A character split across two reads is joined first. |
| Long lines | The first 8 KB are kept (never in the middle of a character), the rest is counted (`cut`). The app shows the first 600 characters, MCP 300 by default (`line_chars` up to 4000). |
| Buffer | 10,000 lines or 2 MB per launch. Older lines are overwritten; readers see them as `missed`. |
| Rate | More than 2,000 lines a second (bursts of 4,000) are dropped and counted (`dropped`), with a note line saying how many. This bounds memory and keeps the window responsive when an app prints in a tight loop. |
| Sessions | At most 6 apps captured at once (a 7th stops the oldest capture), 4 finished ones kept for reading. Relaunching an app ends its previous session. |
| Cleanup | A session ends when the app exits (the phone closes the socket), on `console.stop`, when the tunnel drops (`lost`), on a device switch and at daemon shutdown. The read tasks are cancelled and the sockets closed every time. |
| stdout vs stderr | On the iPhone both go through one pseudoterminal, so they arrive interleaved as stream `out` and can't be told apart. `splitStderr: true` (daemon only, experimental) asks for a second socket for stderr; the demo app always sends them apart. |
| Already running | No output: stdout is fixed when the process starts. Launch it from iOS Studio. |

### Buffering

Because the app's stdout is a pseudoterminal, the C library treats it as a terminal and line-buffers
it, so `print()` lines appear as they're printed (stderr is unbuffered). If an app switches stdout to
full buffering, or a library writes without newlines, output arrives in chunks. Put
`setvbuf(stdout, nil, _IOLBF, 0)` at the start of the app, or call `fflush(stdout)`.

`os_log`, `Logger` and `NSLog` go to the device log (Logs › Device log), not here.

## Testing without an iPhone

```bash
python3 -m pytest tests/helper/test_app_console.py      # splitting, caps, cursor, lifecycle
.venv/bin/pytest tests/mcp/test_mcp_console.py           # paging, budget, filters
```

Demo mode (`ios-studio --demo`, or `IOSSTUDIO_DEMO=1` for the daemon and the MCP server) has a fake
app that prints a few lines, a partial line, invalid UTF-8, a 16 KB line, a short burst and stderr
warnings, then a heartbeat until it's stopped or relaunched.

## Needs a check on a real iPhone

- `print()` output arrives line by line, and `FileHandle.standardError` output arrives too.
- The app keeps running when the capture stops (the daemon exits, or `console.stop`): closing the
  socket must not send it SIGHUP. If it does, the console should stay open as long as the app runs.
- `splitStderr: true`: does the phone accept two sockets, and does stderr arrive on the second?
- The fallback when the stdio socket can't be opened (older iOS): the app still launches.
- The stdio socket ends with the tunnel when Wi-Fi drops (the session turns `lost`).
