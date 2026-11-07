# Run presets: launch arguments, environment and language

Testing an app in German, with a debug menu switched on or with test data means launching it with
arguments. Typing them again for every run gets old, so a run preset saves them under a name, per project,
like the Run options of an Xcode scheme. Pick a preset once and Run uses it from then on; an agent passes
its name to `ios_run`.

A preset only changes how the app is launched. It doesn't change the build, your project's files or the
iPhone's own settings.

## What a preset holds

| Field | What it is | Becomes |
|---|---|---|
| `name` | 1 to 40 characters, unique per project (case-insensitive), no line breaks or control characters | |
| `args` | launch arguments, passed to the app as they are | themselves, first |
| `env` | environment variables (`NAME` is letters, digits and `_`, not starting with a digit) | the launch environment |
| `language` | an app language: `de`, `fr`, `pt-BR`, `zh-Hans`, `es-419` | `-AppleLanguages (de)` |
| `region` | a locale for dates, numbers and currency: `de_DE`, `en_GB`, `zh_Hans_CN` | `-AppleLocale de_DE` |
| `uiTestMode` | a switch for your app's own code | `-UITestMode YES` |

The generated arguments come after the preset's own `args`, in the order of the table. So the German
preset `{"name": "German", "language": "de", "region": "de_DE"}` launches the app with
`-AppleLanguages (de) -AppleLocale de_DE`.

Spelling is fixed before saving: `de-de` as a language becomes `de-DE`, `pt_br` becomes `pt-BR`, and
`de-de` as a region becomes `de_DE`.

### How the app sees them

iOS puts launch arguments of the form `-Key value` into the app's `UserDefaults` (the argument domain),
for that launch only. `-AppleLanguages` and `-AppleLocale` are keys iOS itself reads, so the app's text
and formats change without any code. The phone's own language setting stays as it is.

iOS does nothing with `-UITestMode`. Your app reads it, for example to skip onboarding or load test data:

```swift
let uiTesting = UserDefaults.standard.bool(forKey: "UITestMode")
let showDebugMenu = UserDefaults.standard.bool(forKey: "ShowDebugMenu")   // from args ["-ShowDebugMenu", "YES"]
```

Every argument is also in `ProcessInfo.processInfo.arguments`, and environment variables are in
`ProcessInfo.processInfo.environment`.

### Things to know

- **The app needs the language.** `-AppleLanguages (de)` only changes the text when the app has a German
  localization (a `de.lproj` folder or German strings in a string catalog). Without one, iOS falls back to
  the development language, usually English. Dates and numbers still follow `region`.
- **Only a fresh launch gets the arguments.** Launch arguments are read when the process starts. iOS
  Studio's launches (Run, Logs › Console's Launch, and the MCP tools) end a running copy of the app first,
  so they always start it fresh. If you open the app from the home screen afterwards, or it was relaunched
  some other way, it runs without the preset.
- **Right-to-left languages.** Arabic and Hebrew (in the language list) also flip the layout, which is a
  quick way to test it.

## Where they're stored

In iOS Studio's state, not in the project folder, so they don't end up in git:
`~/.local/state/ios-studio/projects.json`, under `meta[<project path>]`:

```json
"meta": {
  "/home/you/Developer/HabitTracker": {
    "runPresets": [
      {"name": "German", "args": [], "env": {}, "language": "de", "region": "de_DE", "uiTestMode": false},
      {"name": "Debug flags", "args": ["-ShowDebugMenu", "YES"], "env": {"LOG_LEVEL": "debug"},
       "language": null, "region": null, "uiTestMode": false}
    ],
    "runPreset": "German"
  }
}
```

`runPreset` is the selected preset, the one Run uses; without it Run is a plain launch. Both keys are
left out when empty. An older `projects.json` without them reads fine, and an entry that can't be read
(no name, a second one with the same name, an invalid value) is skipped. The selection follows a renamed
preset and is cleared when its preset is deleted.

## Limits

| | At most |
|---|---|
| presets per project | 50 |
| name | 40 characters |
| launch arguments per preset | 64, each up to 1000 characters, no NUL |
| environment variables per preset | 64, each value up to 4096 characters, no NUL |

A preset that breaks a rule isn't saved; the error (`invalid-preset`) says which rule, for example
"'1X' isn't a valid environment variable name" or "'german' isn't a language code. Use one like de, fr,
pt-BR or zh-Hans." The Run presets sheet checks the same rules while you type.

## In the app

- **Run is a split button**: `[▶ Run | ▾]`. The chevron opens a menu with Default (no preset), the
  project's presets (a check on the selected one, its language beside it) and Edit presets… (New preset…
  when there are none). Picking one saves the choice, so it stays after a restart. `j`/`k` or the arrow
  keys move in the menu, Enter picks, Esc closes. When Run is off (another build running, no iPhone), the
  menu still works.
- **"Runs with German"** after the bundle ID on the project card shows the selected preset.
- **More › Run presets** and **Edit presets…** open the Run presets sheet: the list (Use for Run, edit,
  delete, New preset) and an editor with name, language (Same as the iPhone, a list of 13, or Other language code… for any code), region
  (with a "Use de_DE" suggestion for the chosen language), launch arguments, environment variables and the
  UI test mode switch. "What Run passes to the app" previews the exact arguments and environment. Deleting
  asks first.
- **Running**: Run (also from Home) uses the selected preset. The build panel's subtitle reads "Run
  (German)", the launch step "Opening the app on your iPhone · German", and the result "Installed and opened
  on your iPhone with the run preset “German”". Build history records the preset too.

Check build, Build .ipa and Re-sign & reinstall don't use presets: they don't launch the app.

See [Run with a preset](../guide.md#run-with-a-preset-language-arguments-environment) in the guide.

## Helper

```
iosstudio-helper projects presets --path P                                list
iosstudio-helper projects presets --path P --save '<preset JSON>' [--rename-from OLD]
iosstudio-helper projects presets --path P --delete NAME [--dry-run]
iosstudio-helper projects presets --path P --select NAME                  ('' clears it)
iosstudio-helper projects presets --path P --resolve NAME                 what a launch gets
iosstudio-helper build run --path P --udid U --preset NAME
```

Only one of `--save`, `--delete`, `--select` and `--resolve` at a time. `--save` adds a preset, or replaces
the one with the same name; with `--rename-from` it replaces that one under the new name. The list answer
is `{path, presets, selected}`, each preset with `resolved: {args, env}`, plus `saved` or `deleted` and
`dryRun` after a change. `--resolve` returns `{name, args, env}`. `projects list` and `projects info`
carry `runPresets` (with `resolved`) and `runPreset` for every project.

`build run --preset NAME` looks the preset up before anything builds, so an unknown name fails at once
(`unknown-preset`, "No run preset named 'Germn'. This project has: German, Debug flags."). The result's
`launch` is `{bundleId, udid, args, env, preset}`, and the build history entry has `preset`. The helper
doesn't launch the app itself: whoever runs the build (the app, or the MCP server) passes `args` and `env`
to the device service's `app.launch`.

## MCP

- `ios_project_presets(project_path, name=, max_bytes=)` (read-only) returns each preset with `resolved`,
  the exact args and env a launch gets, and `selected`; `name=` shows one.
- `ios_project_preset_set(project_path, action=, preset=, name=, rename_from=, confirm=)` changes them.
  `save` takes `preset={'name': 'German', 'language': 'de', 'region': 'de_DE'}` (fields `name`, `args`,
  `env`, `language`, `region`, `ui_test_mode`) and says `replaced=true` when it overwrote one;
  `rename_from` renames. `delete` is a dry run unless `confirm=true`. `select` sets the preset iOS Studio's
  Run button uses (`name=''` clears it).
- `ios_run(project_path, preset='German')` builds, installs and launches with it. An unknown name fails
  before the build starts and lists the project's presets. The result has `preset`, `launch_args` and
  `launch_env`, and `ios_build_history` shows `preset`.
- `ios_app_launch(bundle_id, preset='German', project_path=)` and `ios_app_run_and_capture(bundle_id or
  project_path, preset='German')` launch an installed app with it. The preset's project is `project_path`,
  else the listed project that builds `bundle_id` (plain or `XTL-<TEAM>.` form); if none or several do,
  the error asks for `project_path`. Their results have `preset` and the args and env that were passed.

Agents pass `preset=` themselves: the selection only applies to the Run button, and `ios_run` without
`preset` launches plainly.

**Merging with explicit arguments.** `ios_app_launch` and `ios_app_run_and_capture` also take `args` and
`env`. With a preset, the launch gets the preset's arguments (generated ones included) first, then the
explicit `args`; the environment is the preset's with the explicit `env` on top, so an explicit value wins.
For example `preset='Debug flags', args=['-Verbose'], env={'LOG_LEVEL': 'trace'}` launches with
`-ShowDebugMenu YES -Verbose` and `LOG_LEVEL=trace`.

Errors: `unknown-preset` (the message lists the names) and `invalid-preset` (the message says which rule).

## Demo mode

With `IOSSTUDIO_DEMO=1` (or `ios-studio --demo`), HabitTracker has "German" (`de`, `de_DE`, selected) and
"Debug flags" (`-ShowDebugMenu YES`, `LOG_LEVEL=debug`), WeatherGlance has "Japanese" (`ja`, `ja_JP`) and
"UI tests" (`-ResetData`, `WEATHER_STUB=sunny`, UI test mode), and RecipeBox has none. Saving, deleting and
selecting answer as if it worked but change nothing (MCP adds "Demo mode: nothing is written to disk").
Unknown names fail as they would for real, and a demo Run echoes the preset's args and env in its
`launch`. The `fresh` scenario has no presets.

## Limits

- Presets are launch-time only. They can't change build settings, `Info.plist` or anything an app reads
  before `main`.
- Language and region are argument-domain `UserDefaults`; code that reads the phone's settings some other
  way (a server that looks at the IP address, for example) won't see them.
- iOS Studio doesn't check that the app has a localization for the chosen language.
- Presets apply to launches from iOS Studio and its MCP tools only, not to opening the app on the phone.

## Still to check on a real iPhone

- That `-AppleLanguages (de)` and `-AppleLocale de_DE` switch a localized app's text and formats through
  `app.launch` (CoreDevice's launch), and that the phone's language setting stays as it was.
- That `-UITestMode YES` and other `-Key value` arguments reach `UserDefaults`, and env values reach
  `ProcessInfo.environment`.
- That a launch with arguments really ends a running copy first, also without the console.
- That `ios_run` over USB now opens the app with the preset after `xtool dev run` installs it (before
  this change it only installed it).

## Code

`helper/iosstudio/features/run_presets.py` (fields, `normalize`, `validate`, `resolve`, storage, `act`),
`helper/iosstudio/features/projects/` (`projects presets` in commands.py, `model.launch_preset` and the `launch` data of `build run` in build.py),
`helper/iosstudio/demo/demo.py` (demo presets), `mcp/iosstudio_mcp/tools/run_presets.py` (`ios_project_presets`, `ios_project_preset_set`),
`mcp/iosstudio_mcp/backends/projects.py` (`resolve_preset`, `merge_launch`), `mcp/iosstudio_mcp/tools/build.py`
(`preset` on `ios_run`), `mcp/iosstudio_mcp/backends/builds.py` (the launch in `make_installer`), `tools/device.py` and `tools/logs.py` (`preset` on `ios_app_launch` and
`ios_app_run_and_capture`), `services/ProjectService.qml` (the preset API, `--preset` on Run),
`components/projects/RunSplitButton.qml`, `components/projects/RunPresetsSheet.qml`, `components/projects/ProjectCard.qml`,
`components/projects/BuildPanel.qml`, `lib/Model.js` (the preset helpers). Tests: `tests/helper/test_run_presets.py`, `tests/cli/test_run_presets_cli.py` and
`tests/mcp/test_mcp_run_presets.py`.
