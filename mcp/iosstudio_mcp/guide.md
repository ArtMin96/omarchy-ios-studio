# iOS Studio MCP guide

How the tools fit together. The server's instructions are the short version; docs/mcp.md has the whole story.

## Start

- Start with ios_status: it reports toolchain, Apple ID, iPhone and projects, and names the tool that fixes each problem.
- Projects are SwiftPM packages with an xtool.yml; sources are in Sources/<App>/. Find them with ios_project_list and pass the absolute path (or the project name) as project_path.
- udid parameters are optional and default to the active iPhone.
- Human-only steps: Apple ID sign-in (never ask for passwords or 2FA codes), the Xcode .xip download, iPhone pairing/Trust, Developer Mode, and polkit password dialogs. Explain the step and use ios_open_app(page=...).

## Builds and projects

- Builds are background jobs: ios_build_start (or ios_run to build+install+launch) returns a job_id; poll ios_job_status(job_id, wait_seconds=20) until succeeded/failed, then ios_build_diagnostics: each error has a category (compiler file:line, linker, package, signing, device, toolchain, config) and the non-compiler ones a one-line fix hint. ios_build_log has the raw output, rarely needed. action='compile' needs neither an Apple ID nor an iPhone.
- Compile on save: ios_watch_start(project_path) compiles after every save (one watched project, shared with the app; saves during a build restart it); after editing, ios_watch_status(wait_seconds=20) returns state, counts and the first errors; the full list is ios_build_diagnostics(job_id='w-N'). ios_watch_stop ends it. Replacing another project's watcher can take ~15 s: state='starting', call ios_watch_start again.
- Agent instructions: ios_project_create and ios_project_convert add AGENTS.md (what works on Linux, the tool loop, free-account limits) and a CLAUDE.md that imports it, never overwriting existing files; agent_files=true/false overrides Settings › AI agents. Read a project's AGENTS.md before editing it.
- After a change, follow the verify_change prompt: ios_project_check, ios_build_start(action='compile') + ios_build_diagnostics until clean, then ios_run, ios_app_run_and_capture, ios_ui_tree/ios_ui_press/ios_screenshot, ios_a11y_audit for UI changes, ios_format_lint. No Simulator, xcodebuild or host swift test for app code; without an iPhone, say it was only compiled.
- ios_project_check finds xtool.yml typos, missing NS*UsageDescription keys, bad icons and entitlements a free account can't use before a build (under a second, warnings only, never changes files).
- Format and lint: ios_format(project_path) is a dry run with a bounded diff (honors .swift-format); after the user agrees, ios_format(apply=true) writes only the files that change. ios_format_lint lists swift-format (and SwiftLint, if installed) style findings with file:line; they never fail a build.
- Swift packages: ios_package_list shows dependencies with versions and the targets using them; ios_package_add(project_path, url) adds one and links its product (a job: call again with job_id; several products -> ask the user, call again with product=); ios_package_update/ios_package_resolve are jobs that report changed versions; ios_package_remove is a dry run with a diff until confirm=true. Then ios_build_start(action='compile').
- A project that builds through its own scripts (a GENERATED xtool.yml, env vars, ulimit -n) needs ios_project_build_settings first (dry run, then confirm=true after the user agrees); ios_project_info shows suggested settings.
- Run presets (saved launch args, env, language/region, UI-test flag per project): ios_project_presets lists them, ios_project_preset_set saves/deletes/selects one; pass preset='<name>' to ios_run, ios_app_launch or ios_app_run_and_capture.
- Editor completion: ios_editor_setup(editor='auto') sets up VS Code, Zed or Neovim for Swift once per machine (dry run, then confirm=true after the user agrees); projects get their .bsp/xtool.json on build/create, and ios_project_info's `editor` says whether a project is ready.

## Jobs, results and safety

- Every call answers within 25 s. Jobs: anything that can outlast a call (builds, toolchain installs and versions, renewals, package changes, installs, run-and-capture, accessibility audits, file copies, camera tests, app data changes and big store reads, profiles, screenshot matrices, recordings, packet captures, new projects) returns state='running' and a job_id. Call the tool that started it again with job_id (and wait_seconds ≤25) for its full result, or ios_job_status(job_id) for any job; ios_job_cancel(job_id) stops one. If a job's state is needs_input, show its pending_prompt to the user and send THEIR answer with ios_job_respond.
- Results are kept small (about 12 KB). A cut result has truncated=true and a `note` saying how to get more (before=/offset=, grep=, a filter, or max_bytes up to 48000); big outputs are saved to a file whose path comes back as full_output_path.
- Destructive tools (ios_project_remove, ios_build_clean, ios_toolchain_clean, ios_account_logout, ios_app_uninstall, ios_files_delete, ios_app_defaults_set, ios_project_preset_set (delete), ios_screenshot_baseline_delete, ios_app_reset, ios_project_autorenew enable/disable) take confirm=false by default and return a dry run; so does a destructive answer in ios_job_respond. Ask the user, then repeat with confirm=true.
- Free Apple IDs: apps stop opening 7 days after signing (ios_project_renew re-signs), at most 3 of your apps per iPhone, 10 new app ids per week; bundle ids are registered as XTL-<TEAM>.<bundle id>.

## On the iPhone

- On the iPhone: ios_app_run_and_capture(bundle_id) launches an app and reports its logs, what it printed (print()) and any new crash in one call; ios_app_console_read pages through its print() output later (apps launched by ios_run, ios_app_run_and_capture or ios_app_launch(console=true)); ios_logs_start/read/stop follow the log with a cursor; ios_crash_list/get read crash reports; ios_screenshot shows the screen and ios_screen_record records a short MP4 of it (for bug reports); ios_app_list/ios_device_info describe the phone. Developer Mode is turned on by the human; ios_device_prepare explains how and mounts the developer disk image (a first mount downloads it and keeps going: call ios_device_prepare again). A crash report that takes longer than a call to copy keeps copying: call ios_crash_get again.
- Screen as data: ios_ui_tree(bundle_id) lists the screen's accessibility elements (id, role, label, value, frame) in reading order, paged with cursor; ios_ui_press(element_id or label, bundle_id) taps one on the real iPhone and reports what changed; ios_a11y_audit(bundle_id) runs Apple's accessibility audit. Typical loop: ios_run -> ios_ui_tree -> ios_ui_press -> ios_ui_tree/ios_screenshot. No typing or swipes.
- Visual checks: ios_screenshot_compare(project_path, name, save_baseline=true) saves a screen's baseline; the same call without save_baseline later returns diff %, a verdict, changed regions and a diff image path (status bar ignored; image bytes only with include_image=true). ios_screenshot_baselines lists baselines, ios_screenshot_baseline_delete deletes one (dry run first). ios_screenshot_matrix captures screens over several presets or languages (job: call again with job_id).
- Files: ios_media_list/ios_media_pull read the camera roll; ios_files_list/pull/push/delete work in an app's container; ios_camera_test runs the app's camera code on test files (needs the hook in docs/camera-testing.md). Long copies, tests and installs (ios_app_install) return state='running' with a job_id: call the same tool again with it.
- App data (your own apps only): ios_app_defaults reads UserDefaults as JSON; ios_app_defaults_set sets/deletes a key (dry run, then confirm=true; never while the app runs: stop_app=true; it reinstalls the app's last build over it so iOS drops its cached settings, a ~20 s job: call again with job_id); ios_app_store lists SwiftData/Core Data stores with tables and row counts, and pages a table's rows (read-only; a big store is a job: call again with job_id); ios_app_reset(mode='wipe'|'reinstall') resets the app to first launch (the dry run and the reset are jobs: call again with job_id).

## Testing and performance

- Network testing: ios_network_conditions lists slow-network, thermal and GPU profiles; ios_network_condition_set (dry run first, then confirm=true) applies one to the whole phone for a few minutes, ios_network_condition_clear ends it. ios_pcap_capture records traffic over USB and returns a header-only summary and a pcapng path.
- Performance: ios_metrics_start(bundle_id) samples CPU, memory, energy, FPS, network and battery about once a second; ios_metrics_summary gives stats and anomalies (possible leak, spikes, hangs, jetsam); ios_metrics_stop ends it. ios_launch_profile and ios_memory_leaks change the app's state: confirm with the user first.
- Debugger (your own apps only; attaching pauses the app): ios_debug_backtrace(bundle_id, confirm=true) for every thread's stack; ios_debug_session_start with breakpoints, then ios_debug_session_wait / ios_debug_session_command, and always ios_debug_session_end.

## Details

- ios_run: xtool installs over USB; over Wi-Fi the server installs the .ipa through the device service. Either way the server then launches the app. When it can't install, result.needs_install is true. A cancelled Wi-Fi install may still finish on the phone, but the app isn't launched.
- ios_logs_read lines read `<seq> <time> <level> <process>[<pid>] <subsystem>: <message>`; the buffer holds 5000 lines, and `dropped` means the phone outpaced the stream. App console: attaching to an app that was already running gives no output; more than 2,000 lines a second are dropped (`dropped`).
- ios_project_check findings also appear as category='config' in ios_build_diagnostics.
- ios_package_add: packages with binary targets or macOS-only plugins may not build for iOS. ios_package_remove can't safely edit manifests built from variables or `.append` (edit-invalid).
- Run presets: language adds `-AppleLanguages (de)`, region `-AppleLocale de_DE`.
- ios_format never writes outside the project and skips .build/ and hidden folders. ios_project_autorenew's timer renews projects with under 2 days left.
- ios_editor_setup: Neovim uses LazyVim's lang.swift extra (backed up first) or writes a new lua/plugins file; other Neovim configs are left alone. VS Code projects get .vscode/ files on their next build.
- ios_screenshot: JPEG, quality 80 → 65 → 50 until it fits 350 KB; png only if it fits. ios_location_set is simulated only while iOS Studio's device service runs.
- ios_ui_press: in changed_values, `label` can be the element's identifier when the label itself changed.
- ios_metrics_series units: rss MB, wakeups /s, diskR/diskW KB/s, gpuMem MB, battMw mW; energy parts eCpu, eGpu, eNet, eDisplay, eLoc, eOverhead.
- ios_screenshot_matrix relaunches the app plainly at the end if it was running before, else stops it; a cancel keeps the shots so far.
- ios_network_conditions profiles include Edge, 3G, LTE, DSL, 100% loss and high-latency DNS.
