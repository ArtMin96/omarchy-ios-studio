# {{NAME}}

An iPhone app in Swift, built on Linux with xtool and iOS Studio. There is no Mac, Xcode or Simulator in
this setup. The app's code is in `Sources/{{NAME}}/`; its bundle ID, `{{BUNDLE_ID}}`, is set in `xtool.yml`.

## Building and running

Build with the iOS Studio MCP server's tools and pass this folder as `project_path`.

- Start with `ios_status`: it reports the toolchain, Apple ID and iPhone, and names the tool that fixes
  each problem.
- `ios_project_info` shows the project's settings and recent builds.
- Run `ios_project_check` before the first build to catch config mistakes.
- Compile with `ios_build_start(action="compile")`. It needs no Apple ID or iPhone.
- Read errors and warnings with `ios_build_diagnostics`, the full output with `ios_build_log`.
- Build, install and launch on the iPhone with `ios_run`.
- See the app's `print()` output with `ios_app_run_and_capture`, and read more later with
  `ios_app_console_read`.
- Launch arguments and environment are saved presets: `ios_project_presets`.
- `ios_watch_start` recompiles on every save.
- Make the app icon from any image with `ios_project_icon`: it shows a preview first.
- Add dependencies with `ios_package_add` and list them with `ios_package_list`; don't edit
  `Package.resolved` by hand.
- Don't create an `.xcodeproj`, `.xcworkspace` or call `xcodebuild`. They don't exist here.

Builds and launches can take minutes. A tool that isn't done answers `state="running"` with a `job_id`:
call the same tool again with that `job_id`, or `ios_job_status`. With `state="needs_input"`, show the
question to the user and send their answer with `ios_job_respond`.

## Checking a change

The `verify_change` MCP prompt walks through these steps for one change.

- Compile and read `ios_build_diagnostics` until there are no errors.
- Run it with `ios_run`, then look at the screen with `ios_screenshot` and `ios_ui_tree`.
- Tap through the flow with `ios_ui_press` (by element id or label).
- Check accessibility with `ios_a11y_audit`.
- Compare against a saved baseline with `ios_screenshot_compare`.
- After a crash, read the report with `ios_crash_list` and `ios_crash_get`.
- Inspect or clear the app's stored data with `ios_app_defaults` and `ios_app_reset`.

## Code style

- Swift 6 language mode (see `swift-tools-version` in `Package.swift`), with strict concurrency checks.
- SwiftUI for the interface. The package has exactly one library product, the app.
- Run `ios_format_lint` before finishing, and `ios_format` to fix the formatting.

## Testing

- There is no iOS Simulator and no on-device XCTest or UI testing.
- UIKit and SwiftUI don't exist on the Linux host, so `swift build` and `swift test` on the host fail on
  app code. That's expected; compile with `ios_build_start` instead.
- Test on the real iPhone: run the app, then check it with `ios_screenshot`, `ios_ui_tree`,
  `ios_ui_press` and the console output.

## Limits

- A free Apple ID signs apps for 7 days; after that they have to be built and installed again.
- A free Apple ID can have at most 3 apps on one iPhone and register 10 new app IDs a week, so don't
  change the bundle ID without a reason.
- Only the user can sign in to the Apple ID, enter two-factor codes, tap Trust on the iPhone, turn on
  Developer Mode or answer password dialogs. Never ask for a password or code. Explain the step and open
  the right page with `ios_open_app`.
