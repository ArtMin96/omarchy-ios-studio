# Screenshot compare: baselines and visual diffs

Checking by eye that a change didn't break another screen takes a lot of looking. Save a screenshot of a
screen as its baseline once, and later screenshots of that screen are compared with it: you get the share of
the screen that changed, a verdict, boxes around what changed and a diff image that shows it. Agents use the
same loop to check their own UI changes, and the matrix mode captures a set of screens in several languages
or launch configs, for example for App Store screenshots.

In the app it's iPhone › Screen › Compare (see [Check a screen against a baseline](../guide.md#check-a-screen-against-a-baseline)); agents use
`ios_screenshot_compare`, `ios_screenshot_baselines` (list), `ios_screenshot_baseline_delete` (a dry run until `confirm=true`) and `ios_screenshot_matrix` (see [mcp.md](../mcp.md)).

## What counts as a change

Both screenshots are compared pixel by pixel.

- **Tolerance.** A pixel only counts as changed when one of its red, green or blue values differs by more than
  24 (of 255). That keeps JPEG and compression noise out; a real change of text or colour is far above it.
  `--tolerance` (`tolerance=` for agents) changes it, from 0 (any difference) to 255.
- **Status bar.** The clock, battery and signal change all the time, so a band at the top is ignored: 54 pt on
  iPhones with Face ID (notch or Dynamic Island, screens taller than 2:1) and 20 pt on iPhones with a Home
  button, times the screen scale (3 for current iPhones, so 162 pixels on a 1206 x 2622 screenshot). In
  landscape nothing is masked, since iPhones hide the status bar there. `--mask-top PX` (`mask_top=`) sets
  the band, `0` turns it off, and `--mask '[{"x":0,"y":800,"w":1206,"h":200}]'` ignores more areas (pixels).
- **Diff %** is the changed pixels as a share of the compared ones (the masked areas don't count). Above the
  threshold (0.1 % by default, `--threshold`) the verdict is `changed`, otherwise `same`. A changed title on
  a full-size iPhone screenshot was about 0.4 %, a single changed digit far less, so keep the threshold low; regions are reported even
  when the verdict is `same`.
- **Regions.** Changed pixels are grouped on a grid of 8 pt cells; cells within two cells of each other join,
  so the letters of one label make one box. A single cell with one changed pixel is noise and is dropped.
  Boxes are exact pixel bounds, biggest first, at most 50 (`regions_total` has the full count; agents get the
  10 biggest).
- **Different sizes** (another iPhone model, or rotated) give the verdict `size-mismatch`, not a diff.

The diff image is the current screenshot dimmed, with changed pixels in pink, each region boxed in yellow and
the ignored areas striped.

## Where the files go

Everything is in iOS Studio's data folder, not in your project, per project (the folder name plus a short
hash of its path, so two checkouts of the same app stay apart):

```
~/.local/share/ios-studio/screenshots/<Project>-<hash>/
  baselines/<name>.png, <name>.json       the baselines and their details (when, size, iPhone, bundle id)
  compare/<time>-<name>/current.png       each compare: a copy of the screenshot ...
  compare/<time>-<name>/diff.png          ... and its diff image
  matrix/<time>/<config>/<name>.png       matrix sets, with manifest.json
```

Screen names keep letters, digits, `.`, `_` and `-`; anything else becomes `-` (`Home screen!` is saved as
`Home-screen`), at most 60 characters. Looking a name up ignores case.

Limits and cleanup:

- Baselines stay until you delete them. Deleting one removes its image and its details together, plus every
  kept compare run of that screen name (matched ignoring case), and then the project's folder here if nothing
  else is left in it. The dry run says how many compare runs (and how many bytes) would go.
- Compare runs: the newest 30 per project and at most 200 MB of them are kept; older ones are deleted after
  each compare (the one just made always stays).
- Matrix sets: the newest 10 per project and at most 500 MB.
- A compare that fails halfway leaves nothing behind (its folder is removed), and files are written to a
  temporary name first, so a crash never leaves a half-written PNG.
- Removing a project from iOS Studio's list (`projects remove`, `ios_project_remove`) deletes its folder here
  too. The project's own files are never touched.

## Speed and Pillow

With Pillow, a compare of two full-size iPhone screenshots takes about 0.3 s. The helper runs on the system
Python, which usually has no Pillow, so `compare` runs itself again with the device service's Python
(`~/.local/share/ios-studio/venv` or `~/pymobile3-venv`), which has it. Without Pillow anywhere, a small
pure-Python PNG reader and writer does the same work (8-bit, non-interlaced PNGs: gray, RGB, palette, RGBA,
which covers iPhone screenshots); a full-size compare then takes a second or two, more when most of the
screen changed. Both give the same numbers and the same diff image. `IOSSTUDIO_SCREENSHOTS_PURE=1` forces the
pure-Python code. Saving a JPEG as a baseline needs Pillow (it's stored as PNG).

## Matrix: one launch per config

`ios_screenshot_matrix` (agents only) loops over launch configs and screen names:

- **Configs** are run presets of the project by name (see [run-presets.md](run-presets.md)), and/or inline
  configs with `name`, `language`, `region`, `args`, `env` and `ui_test_mode`, resolved exactly like presets
  (`language: "fr"` adds `-AppleLanguages (fr)`). No configs means one launch with the app's defaults. At
  most 12.
- **Names** are the screens to capture after each launch, in order, at most 12. A plain name is captured as
  it is. A name with `press` (`{"name": "Settings", "press": "Settings"}`) first presses that element by
  label or accessibility identifier, like `ios_ui_press`, then waits `wait` seconds (1.5 by default) and
  captures. Presses chain: the second name starts from the screen the first one left, so list screens in the
  order you'd tap through them. No names means one capture called `launch`.

For each config the installed app is launched with that config (a running copy is ended first, as every
iOS Studio launch does), and after `settle_seconds` (3 by default) the names are captured. A failed launch,
press or capture is recorded with its error and the matrix goes on. The result is one PNG per config and
name in `matrix/<time>/<config>/<name>.png` and a `manifest.json` with the resolved configs and every shot.

It runs as a job in the MCP server: the first call returns `state: "running"` and a `job_id`; calling
again with `job_id` waits (25 s per call at most). While it runs the reply is a few counters; once it's done it
lists the shots 20 at a time (`offset=` for the next page, or read the manifest at `full_output_path`).
`ios_job_cancel(job_id)` stops it and keeps the shots taken so far, as does a failure. At the end the app is put back:
relaunched without the matrix's arguments if it was running before, stopped if it wasn't
(`app_restored` says which). Ending the agent's session cancels a running matrix.

`manifest.json` says how the set ended, so it can be read later without the agent's session. While the
matrix runs its `state` is `running`; at the end it becomes `done`, `cancelled` or `failed`, with `error`
(why it failed), `app_restored`, `finished` (Unix time), `expected` (configs x names), `captured`, `failed`
(shots with an `error`) and `complete` (true only when it's `done` and every shot was captured). A manifest
still saying `running` belongs to a matrix whose MCP server was killed before it could finish it.

Compare a matrix shot with a baseline with `ios_screenshot_compare(shot_path=<path>, name=...)`.

## Helper commands

```bash
bin/iosstudio-helper screenshots list    --project ~/Developer/MyApp
bin/iosstudio-helper screenshots save    --project ~/Developer/MyApp --name Home --shot shot.png [--device D --bundle B --preset P]
bin/iosstudio-helper screenshots compare --project ~/Developer/MyApp --name Home --shot shot.png [--threshold 0.1 --tolerance 24 --mask-top auto|PX --mask JSON]
bin/iosstudio-helper screenshots delete  --project ~/Developer/MyApp --name Home [--confirm]   # a dry run without --confirm
```

Each prints one JSON object (`{"ok": true, "data": {...}}`):

- `list`: `{project, dir, baselines: [{name, path, created, width, height, bytes, device, bundle, preset}]}`,
  sorted by name; `created` is Unix time.
- `save`: `{name, baseline_path, width, height, replaced, created}`.
- `compare`: `{name, verdict, diff_percent, threshold, tolerance, changed_pixels, compared_pixels, regions:
  [{x, y, w, h, pixels}], regions_total, width, height, masked_top, baseline_path, current_path, diff_path,
  baseline_created, backend, elapsed_ms}`. `verdict` is `same`, `changed`, `no-baseline` (then
  `diff_percent` and `diff_path` are null; `current_path` is still kept) or `size-mismatch` (with
  `baseline_width` and `baseline_height`). Paths are absolute.
- `delete`: `{name, baseline_path, deleted, dryRun, width, height, created, compare_runs, compare_bytes,
  project_dir, project_dir_removed}` (`compare_runs` are that screen's runs, deleted with it under
  `--confirm`; `project_dir_removed` is true when the project's folder was empty afterwards and removed); an unknown name fails with
  `no-baseline` and lists the project's baselines.

`matrix-plan`, `matrix-save` and `matrix-finish` (`--state done|cancelled|failed [--error] [--restored]`) are
what `ios_screenshot_matrix` uses to resolve the configs, file the shots and record how the set ended. Error codes: `usage`, `invalid-name`, `no-shot`, `bad-image`, `no-baseline`, `not-found` (project
folder gone), and `unknown-preset` / `invalid-preset` from a matrix's configs.

## Demo mode

With `IOSSTUDIO_DEMO` set, the same commands work on made-up baselines in the demo folder
(`$XDG_RUNTIME_DIR/ios-studio-demo/screenshots/`). WeatherGlance has `Home`, which differs from the demo
iPhone's screenshot in two text lines and a badge (three regions, about 0.8 %), and `Today`, which differs
only in the status bar's clock and compares as 0 %. HabitTracker has `Streaks`. Saving and deleting work on
the demo folder only.

## Checked on a real iPhone

Two screenshots of a small test app on an iPhone 17 Pro (iOS 27.3, 1206 x 2622) where only the title
changed: one region around the title, 0.36 %, the status bar band 162 pixels; Pillow and the pure-Python code
gave the same result.
