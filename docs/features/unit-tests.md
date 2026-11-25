# Unit tests on the computer (swift test)

There is no Simulator on Linux, so most checks of an iPhone app need the iPhone: build, sign, install, open,
tap through. Logic doesn't need any of that. Parsing, calculations, formatting, validation and state machines can
be tested on this computer with `swift test` in a few seconds, as long as that code lives in a library target that
only imports Foundation. iOS Studio runs those tests from the project card, shows each failure with its file and
line, and puts the failures in Problems. AI agents run the same tests with `ios_test_run`.

UI code can't run here: UIKit and SwiftUI only exist on Apple platforms. Screens are still checked on the iPhone
(Run, hot reload, the screen as data).

## What can be tested

The tests and the code they test are built for Linux, so they can use what Swift has on Linux: Foundation,
Dispatch, XCTest, Swift Testing (`import Testing`), Observation, RegexBuilder and Synchronization. Both test
libraries work in the same project, even in the same test target.

They can't import UIKit, SwiftUI or another Apple-only framework (SwiftData, Core Data, Combine, Core Location,
AVFoundation, os/OSLog and so on). An import inside an `#if` block, such as `#if canImport(UIKit)`, is fine.

The usual layout is the one the "App with unit tests" template makes:

```
Package.swift
Sources/
  <Name>/              the app: SwiftUI screens, built for the iPhone; depends on AppCore
  AppCore/             the logic, Foundation only
Tests/
  AppCoreTests/        tests of AppCore (XCTest and Swift Testing)
```

The app target imports `AppCore` and calls into it. Only `AppCore` and its tests are built on the computer; the
app target never is.

## The "App with unit tests" template

New app (`n`) › App with unit tests makes a small tip calculator:

- `Sources/AppCore/TipCalculator.swift`: the logic (tip, total, split between people, parsing an amount the user
  typed), Foundation only.
- `Sources/<Name>/`: the SwiftUI screen, which uses AppCore.
- `Tests/AppCoreTests/TipCalculatorTests.swift` (XCTest) and `ParseAmountTests.swift` (Swift Testing, with
  parameterized `@Test(arguments:)`). They pass.

The template's own `Package.swift` declares the three targets. An app can't be named after one of the template's
targets (`AppCore`, `AppCoreTests`): New app asks for another name.

Templates can now bring more than `Sources/*.swift`: a `Package.swift.template` (replaces the generated
manifest), `Sources/<Target>/**` (extra targets) and `Tests/**`. `{{NAME}}` in the manifest and in `.swift` files
becomes the app's name. See `create_app` in `helper/iosstudio/features/projects/model.py`.

To add tests to an existing project, move the logic into a library target that only imports Foundation, make the
app target depend on it, and add a `.testTarget` in `Tests/<Target>Tests/` that depends on the library.

## How a run works

Plain `swift test` builds every target, so on Linux it fails on the app target (`no such module 'SwiftUI'`), and
`--filter` doesn't change what's built. iOS Studio builds only the test product instead:

| Step | What happens |
|---|---|
| Checking the test targets | Reads the package with `swift package dump-package` (cached; see [Files](#files)): its name, the test targets and the local targets they depend on. No test target ends the run with `no_tests`. Then it scans the tested targets and the test targets for imports of Apple-only frameworks, skipping `#if` blocks; one found ends the run with `host_unsupported` before anything is built. On the first run it copies the packages xtool already downloaded (`.build/repositories`, `checkouts`, `workspace-state.json`) into the test build folder, reflinked where the file system can, so they aren't fetched again. |
| Building the tests | `swift build --scratch-path .build/host-tests --product <Package>PackageTests --build-system native`. Only the test targets and what they depend on are compiled. The native build system is about five times faster warm; when a Swift doesn't accept it, the run builds again with the default build system and remembers that for this Swift. Compile errors go to Problems with their file and line (`build_failed`). A "no such module 'SwiftUI'" error in tested code becomes `host_unsupported`. |
| Running the tests | `swift test --skip-build` with the same scratch path, `--filter` when there is one, and Swift Testing's event stream (`--experimental-event-stream-output`, version 0). XCTest's results are read from its console output, Swift Testing's from the event stream, so each test shows as it finishes. |

Both swift commands run at a lower priority (`nice 10`), so the desktop stays responsive. The build has 20 minutes,
the run 10. Cancel stops swift and everything it started.

The separate scratch folder, `.build/host-tests`, matters: a host build in xtool's `.build` would point
`.build/debug` at the wrong platform, rewrite xtool's package state and wait on its lock. With its own folder a
test run never waits on xtool's lock and never disturbs an iPhone build, watch mode or hot reload. Two test runs
of one project can't overlap (`busy`).

Timings on CrashLab: the first run takes about 45 seconds (it builds the test dependencies from nothing). After
that a run takes a few seconds: the build about 2 seconds with no changes and 3 to 4 after a one-line change, the
tests themselves 1 to 2.

A test that crashes (a force unwrap of `nil`, `fatalError`) ends the test process. That test is counted as
failed with the crash message and its file and line, the result says which test crashed, and the tests after it
don't run.

## In the app

- **Run tests** in the project card's More row, next to Check build, or `T` (Shift+T) for the highlighted card. It
  opens the Unit tests sheet and starts a run. It's off while that project's iPhone build runs, because both build
  the same code.
- **The sheet** ("Unit tests · <name>") shows:
  - the summary under the title: "12 passed · 2 failed · 1 skipped · 4.2 s", the last run ("Last run 5 minutes
    ago: …") before one ran in this session, or the test targets;
  - a filter field, "Filter tests (a regular expression)": Enter runs only the matching tests;
  - while it runs, the three steps (Checking the test targets, Building the tests, Running the tests) with the
    number of finished tests;
  - the results: failures first, each with its name, suite, the library when the project uses both, the message and
    `file:line`, which opens your editor at the line; then the skipped tests; then one "N passed" row that expands
    to the passed tests;
  - a warning when a test crashed the test process;
  - states for "No unit tests yet" (Open in editor, Check again), "This code can't run on the computer" (the
    UIKit or SwiftUI imports, each with its `file:line`, and Check again), "The tests didn't compile" (the compile
    errors), "No test matches the filter" (Run all tests), "Test run cancelled", "Swift isn't ready for tests" (its
    fix opens Setup) and other errors with Try again;
  - the footnote "Tests run on this computer with swift test. UI code is tested on the iPhone."
- **Buttons**: Run tests (Run again after a run), Cancel while it runs, **Run failed only** after a failing run
  (a filter that matches exactly the failed tests), **Show in Problems** and Close.
- **Problems**: when a run ends with failing tests or compile errors, they go to the build panel's Problems like a
  build's ("2 tests failed in HabitTracker"), and its log tail to Log. Click a failure to open it at its line. Try
  again there runs the tests again. A run that ends while a build runs leaves the build panel alone.
- Closing the sheet doesn't stop the run. When it ends and the sheet isn't showing it, a toast says how it went.
  Several projects can run their tests at once.

## Helper commands

```
iosstudio-helper test info --project P
iosstudio-helper test run --project P [--filter REGEX]
```

`test info` is cheap and answers one JSON object: `package`, `has_tests`, `test_targets` (`[{name, path}]`),
`tested_targets` (the local targets the tests depend on), `host_blockers` (`[{target, file, line, module}]`, at
most 20) and `last_run` (`{at, ok, counts, duration, filter}` or null). A folder without `Package.swift` is
`not_project`.

`test run` streams JSON lines: `step` (`prepare`, `build`, `run`), `log` (at most 2,000 per run; the log file has
every line), `diagnostic` (compile errors with category `compiler`, failing tests with category `test`), `test`
(one per finished test: `id` such as `AppCoreTests.TipCalculatorTests/testTipAndTotal`, `name`, `suite`,
`library` `xctest` or `swift-testing`, `status` `passed`, `failed` or `skipped`, `duration`, and `file`, `line`
and `message` for a failure) and a final `done`. Its `data` is the result: `counts` (`total`, `passed`, `failed`,
`skipped`), `duration`, `failures` (at most 50), `crashed` (the id of the test that crashed, or null),
`no_tests_matched` and `log_path`. `{"cancel": true}` on stdin or SIGTERM cancels it.

A filter that matches nothing is a success with `no_tests_matched: true`. The errors:

| Code | When |
|---|---|
| `no_tests` | The package has no test target. |
| `host_unsupported` | The tested code imports UIKit, SwiftUI or another Apple-only framework (found by the scan, or by the build's "no such module" error). `detail` lists `file:line: import Module`. |
| `build_failed` | The tests or the code they test didn't compile; the errors came as diagnostics. |
| `tests_failed` | At least one test failed or crashed. |
| `bad_filter` | The filter isn't a valid regular expression, or is longer than 500 characters. |
| `busy` | A test run of this project is already going. |
| `timeout` | The build went over 20 minutes or the run over 10 (a test that hangs). |
| `cancelled` | Cancelled. |
| `toolchain` | No Swift for this computer; the fix opens Setup's Swift step. |

## AI agents (MCP)

`ios_test_run(project_path, filter=None, job_id=None, wait_seconds=20, max_bytes=None)` runs the tests as a job
(ids `test-N`). It waits up to `wait_seconds`; a longer run returns `state: "running"` with the step and the counts
so far, and calling it again with `job_id` waits for the result. `ios_job_status` and `ios_job_cancel` work on it.

The result has `counts`, `duration_seconds`, `failures` (at most 20, each `id — file:line — message` with the
path relative to the project and the message cut to 200 characters; `failures_total` counts them all), `crashed`,
`no_tests_matched`, compile errors grouped as in `ios_build_diagnostics` (`build_failed`, at most 20 groups),
`full_output_path` (the run's log), `error` with a code from the table above, and `next_steps`. It fits the output
budget; a cut result has `truncated` and a `note`. Asked again while a run of the project is going, it returns
that run (`busy: true`) instead of starting another.

New projects' `AGENTS.md` tells agents to use it for logic in a test target and to check UI code on the iPhone.
See [mcp.md](../mcp.md) and [agent-instructions.md](agent-instructions.md).

## Files

| What | Where |
|---|---|
| The test build | `<project>/.build/host-tests/` (`ios_build_clean` and the helper's `build clean` delete it with the rest of `.build`) |
| One log per run (the newest 20, at most 50 MB in all; one log stops growing at 20 MB) | `~/.local/state/ios-studio/test-runs/<time>-<package>.log` |
| Each project's last run (100 projects) | `~/.local/state/ios-studio/test-runs/last.json` |
| One lock per project, so two runs never overlap | `~/.local/state/ios-studio/test-runs/locks/` |
| Package facts from `dump-package` per project, keyed on `Package.swift`'s time and size and the swift binary (200 projects), and which build system each Swift accepts | `~/.cache/ios-studio/test-packages.json` |

The app keeps the last 8 finished runs in memory, with at most 2,000 result rows and 200 log lines each.

## Limits

- No UI tests and no XCTest on the iPhone. Code that imports UIKit or SwiftUI is tested on the iPhone.
- The import scan reads the first 64 KB of each file, at most 2,000 files, and skips every `#if` block, whatever
  its condition. An import it misses still fails the build with "no such module", which is reported as
  `host_unsupported` too.
- A remote package that only builds for Apple platforms can't be a dependency of the tested code.
- Swift Testing's event stream is experimental (version 0 is pinned), and `--build-system native` is deprecated and
  prints a warning each run; the fallback to the default build system is automatic.
- A crash ends the test process, so the tests after it don't run until the crash is fixed.
- The filter is a regular expression matched against `Target.Suite/test` ids, as `swift test --filter` does.

## Demo mode

With `IOSSTUDIO_DEMO` set nothing is built or run: WeatherGlance's 12 tests (XCTest and Swift Testing) pass,
HabitTracker has 2 failures (one XCTest assertion and one `#expect`) and a skipped test, and RecipeBox has no test
target. Any other project passes. `IOSSTUDIO_DEMO_TESTS=pass|fail|none|host` gives every project one outcome
(`host`: the tested code imports SwiftUI). Each demo run writes a small log and its outcome to `demo-last.json`
in `test-runs/`, so `test info` shows the last run.

## Troubleshooting

See [troubleshooting.md](../troubleshooting.md#unit-tests).

## Code

`helper/iosstudio/features/testing/`: `commands.py` (the `test` group), `project.py` (package facts, the cache and
the import scan), `runner.py` (the steps, seeding, build and run, cancel and time limits), `xctest.py` (XCTest's
console), `events.py` (Swift Testing's event stream), `results.py` (test results and their diagnostics), `store.py`
(logs, `last.json`, the lock). `helper/iosstudio/demo/testing.py` (demo mode), `templates/app-with-tests/`,
`mcp/iosstudio_mcp/tools/testing.py` (`ios_test_run`), `services/TestService.qml` (`app.tests`),
`components/projects/ProjectTestsSheet.qml`, `components/projects/TestResultRow.qml`, `services/ProjectService.qml`
(`showTestRun`), `components/projects/BuildPanel.qml`, and the `test*` helpers in `lib/Model.js`.

## Tests

`tests/helper/test_swift_test_parse.py` (both parsers on real Swift 6.4.2 output in `tests/fixtures/swift-test/`),
`tests/helper/test_swift_test_runner.py` (package facts, the cache, the scan, seeding, filters, logs),
`tests/cli/test_swift_test_cli.py` (every outcome through the helper with the fake swift
`tests/support/fake_swift_tests.py`), `tests/cli/test_testing_demo_cli.py`, `tests/helper/test_template_app_with_tests.py`
(the template; it builds and runs its tests with the real swift when there is one), `tests/mcp/test_mcp_testing.py` and
`tests/qml/tst_unit_tests.qml`. See [development.md](../development.md#tests).
