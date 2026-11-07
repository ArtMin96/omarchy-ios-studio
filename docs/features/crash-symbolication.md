# Crash symbolication: file and line numbers in crash reports

A crash report from the iPhone names your app's frames as `MyApp + 0x41d0`. That says nothing about
where the crash happened. iOS Studio resolves those frames to the function, file and line, such as
`Forecast.first() (Sources/MyApp/Forecast.swift:6:17)`, using the exact build that crashed.

## Where it shows up

- **Logs › Crashes** (the app): in "Where it crashed", your app's frames show the function, and the
  file:line below it. Click one to open that line in your editor (Settings › Code editor). A line under
  the frames says where the numbers come from, or why there are none and what to do.
- **MCP**: `ios_crash_get` returns `app_frames` (frame number, function, file relative to
  `project_path`, line, column), `symbolicated` (`full`, `partial`, `none` or `not-needed`) and a
  `symbolication_note` with the reason and the fix. Resolved frames in `frames` and in
  `section='threads'` read `<n> <image> <function> (<file>:<line>:<col>)`. `ios_app_run_and_capture`
  reports a new crash the same way. See [mcp.md](../mcp.md).
- **Daemon**: `crash.pull`'s summary has a `symbolication` object, see
  [deviced-protocol.md](../deviced-protocol.md#crash-reports-used-by-the-logs-page).

## How it works

Every build overwrites `.build`, so a crash from yesterday's build can't be resolved against today's.
After each successful build that can end up on the phone (Run, Renew and signed builds; not
compile-only checks), `helper/iosstudio/features/projects/build.py` calls `crash_symbols.remember_build()`, which:

1. finds the executable the build produced (`.build/<config>/<Product>-App`, the file xtool signs),
2. reads its Mach-O `LC_UUID` (iOS writes the same UUID into every crash report's `usedImages`),
3. copies it to `~/.cache/ios-studio/symbols/<uuid>/` and runs `dsymutil` on it. Swift on Linux leaves
   the debug info in the `.o` files, so the dSYM has to be made now, while they exist.

Reading a report (`crash.pull`), `helper/iosstudio/features/crash_symbols.py`:

1. picks the images inside the app bundle (`…/Bundle/Application/…/MyApp.app/…`); system libraries
   stay as the report has them,
2. looks each UUID up in the cache, and else in the current build products of the projects you built
   here (`.build`, `xtool/<App>.app`, the last signed `xtool/<App>.ipa`). A match found there is
   kept in the cache too,
3. runs `llvm-symbolizer` (from the Swift toolchain) on the dSYM with the addresses
   `__TEXT address + imageOffset`, minus one byte for every frame but the first (those are return
   addresses, and the line you want is the call's),
4. demangles Swift names with `swift-demangle`, and makes file paths relative to the project.

The result is saved next to the pulled report as `<report>.ips.symbols.json`. A full result is reused;
anything less is worked out again on the next read, so rebuilding the right version fixes it.

The app keeps each report's summary in `~/.local/state/ios-studio/logs.json` so the Crashes list opens
offline. Opening a report whose saved summary is not final (`partial`, `none`, or saved by a version
without symbolication) asks the daemon again and keeps showing the saved one until the answer arrives,
or if the iPhone is offline. The background pull fills in a missing status once per session.

## Statuses

| `symbolicated` | Meaning |
|---|---|
| `full` | Every app frame of the crashed thread resolved (compiler-generated ones, like the synthesized `main`, to a name only). |
| `partial` | Some frames resolved: another image of the app had no matching build, or the build has no debug info (`reason: no-debug-info`, names only; release builds). |
| `none` | `reason`: `no-build` (built before iOS Studio kept symbols, on another computer, or rebuilt since), `pruned` (kept once, then pruned), `no-symbolizer` (llvm-symbolizer missing), `unsupported` (an old plain-text report) or `error` (the symbolizer itself failed; the message names the error). |
| `not-needed` | No frames in your app's own code (for example a jetsam report). |

## The cache

`~/.cache/ios-studio/symbols/<uuid>/` holds the executable, `<Exe>.dSYM/` and `meta.json` (project,
product, config, build time, whether it has line info). `index.json` lists every UUID ever kept, so a
pruned build can say so. The newest 10 builds per project are kept, and 1 GB in total (oldest first;
the newest build always stays). A small SwiftUI app takes about 1 to 3 MB per build. Delete the folder
to clear it; nothing else depends on it.

## Limits

- Only crashes from builds made on this computer since this feature, or still in the project's build
  folder, resolve. Rebuilding the same code with the same settings usually gives the same UUID.
- System frames are not resolved; the device names most of them already.
- Release builds keep function names only.
- Check by hand: `python3 helper/run.py iosstudio.features.crash_symbols symbolicate <report.ips>` and
  `python3 helper/run.py iosstudio.features.crash_symbols remember <project> [product] [config]`.

## Tests

`tests/helper/test_crash_symbolication.py` (stdlib) and `tests/mcp/test_mcp_crash_symbolication.py` (MCP venv). The
fixture `tests/fixtures/crash-symbolication/Crashy.dwarf` is the dSYM of `Crashy.swift` built for
arm64-apple-ios (`build.sh` rebuilds it); synthetic `.ips` reports point at its UUID and known offsets.
