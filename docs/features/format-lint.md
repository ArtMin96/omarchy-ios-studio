# Format and lint Swift code

iOS Studio formats a project's Swift code with swift-format and lists its style findings, so code looks the
same everywhere without a Mac or Xcode. swift-format comes with the Swift toolchain that Setup installs
(`/usr/lib/swift/usr/bin/swift-format`), so there is nothing extra to install. When
[SwiftLint](https://github.com/realm/SwiftLint) is on your PATH and starts, its findings are added too.

Formatting always starts as a dry run that shows a diff. Nothing is written until you apply it.

## Which files

`Package.swift` (and `Package@swift-*.swift`), and every `.swift` file under `Sources/`, `Tests/` and
`Plugins/`. Left out: hidden folders, `.build/`, `build/`, `xtool/`, `DerivedData/`, `Pods/` and similar build
output, symlinks that point outside the project, and files over 1 MB (generated code). At most 3000 files.
You can also pick files; a file outside the project is refused and listed in `errors`.

## Configuration

swift-format looks for a `.swift-format` file next to each file and then in each folder above it, the same
as on the command line. So the project's `.swift-format` applies, a nested one in a source folder applies
to the files under it, and one in a folder above the project (a shared `~/Developer/.swift-format`) applies
when the project has none. Without any, swift-format's defaults apply (2-space indentation, 100 columns).
`config_path` in the result says which one applied at the project root: `.swift-format` inside the
project, an absolute path above it, or `null` for the defaults. A `.swift-format` that isn't valid JSON
stops the run with a `bad-config` error rather than failing on every file.

`swift-format dump-configuration > .swift-format` writes the defaults as a starting point.

SwiftLint reads `.swiftlint.yml` itself (including `excluded:`).

## Formatting

The dry run runs `swift-format format <file>` on each file, a few in parallel, and compares the output with
the file. The result is a unified diff with `a/` and `b/` paths relative to the project, and the list of
files that would change. Files swift-format can't parse (a syntax error) are listed in `errors` with the
first error and left alone; the other files are still formatted.

Applying runs the same thing again and writes each file whose content changes:

- through a temporary file in the same folder and a rename, so a file is never half written; its
  permissions are kept
- only files whose content changes, so the others keep their modification time and editors don't reload them
- not when the file changed since it was read (an editor saved it meanwhile): that file is listed in
  `errors` and left alone
- never a file outside the project, even through a symlink

The diff shown is cut to 12 KB by default, at a file or hunk boundary where possible. A cut diff has
`truncated: true`, and the whole diff is saved to `~/.cache/ios-studio/format/` (`full_output_path`); the
newest 10 diffs are kept. A run stops after 20 seconds with `timed_out` and the results so far.

## Linting

`swift-format lint` (with the same configuration) and, when it runs, `swiftlint lint --quiet --reporter json`.
Each finding has the shape of a build diagnostic (see `helper/diagnostics.py`):

| Field | |
|---|---|
| `severity` | `warning`, or `error` when the linter says so (a file swift-format can't parse, a SwiftLint rule set to error) |
| `category` | always `style` |
| `file`, `line`, `col` | absolute path, like compiler diagnostics |
| `rule` | swift-format's rule (`Indentation`, `Spacing`, `NeverForceUnwrap`, …; `syntax` for a file that doesn't parse) or SwiftLint's rule id |
| `source` | `swift-format` or `swiftlint` |
| `message`, `hint` | whitespace findings (`Indentation`, `Spacing`, `AddLines`, `RemoveLine`, `TrailingWhitespace`, `TrailingComma`, `LineLength`) have the hint "Format project fixes this."; a file that doesn't parse has one saying to build it to see the compiler's error. Hints are in app terms; the MCP tools swap in tool names |

Findings are sorted errors first, then by file and line, and capped (200 by default) with `total`,
`truncated`, `counts` per severity, `by_source` and the 20 most frequent rules in `by_rule`. Style findings
are never build errors: a build that succeeded stays successful.

A SwiftLint that is on PATH but doesn't start (a mise shim without a version set, for example) is skipped,
and `note` says why. `swiftlint: false` means SwiftLint didn't run.

The result of the last whole-project lint is kept per project in `~/.local/state/ios-studio/lint/`, so the
app can show it again after a restart without linting (`--last`). A lint of a few chosen files doesn't
replace it.

## Command line

`bin/iosstudio-helper` (JSON on stdout, `{"ok": true, "data": {...}}`). `format` and `format-lint` are short
for `projects format` and `projects format-lint`; `--path` works as well as `--project`.

```bash
bin/iosstudio-helper format --project ~/Developer/App                       # dry run
bin/iosstudio-helper format --project ~/Developer/App --apply               # write the changed files
bin/iosstudio-helper format --project ~/Developer/App --file Sources/App/A.swift Sources/App/B.swift
bin/iosstudio-helper format --project ~/Developer/App --files '["Sources/App/A.swift"]' --max-bytes 4000
bin/iosstudio-helper format-lint --project ~/Developer/App [--file …] [--max 200]
bin/iosstudio-helper format-lint --project ~/Developer/App --last           # the stored result, no new run
```

`format` returns `path`, `apply`, `files_checked`, `files_changed` (relative paths, at most 200),
`files_changed_total`, `diff`, `diff_bytes` (size of the whole diff), `truncated`, `full_output_path`,
`config_path`, `nested_configs`, `tool` (`swift-format`), `tool_version`, `errors` (`{file, message}`, at most
50), `errors_total`, `timed_out`, `files_skipped`, `elapsed_ms` and `note`.

`format-lint` returns `path`, `diagnostics`, `total`, `truncated`, `counts`, `by_source`, `by_rule`,
`files_checked`, `config_path`, `tools`, `tool_version`, `swiftlint`, `swiftlint_version`, `errors`, `timed_out`,
`elapsed_ms`, `at` (Unix time of the lint), `cached` (true for `--last`) and `note`. `--last` on a project
that was never linted returns an empty result with `cached: false` and `at: null`.

Without swift-format the answer is `{"ok": false, "error": {"code": "missing-swift-format", "message", "fix",
"hint"}}`; an invalid `.swift-format` gives `bad-config` with a `hint`.

`IOSSTUDIO_SWIFT_FORMAT` and `IOSSTUDIO_SWIFTLINT` point at other binaries (the tests use fakes). Otherwise
swift-format is looked up next to the `swift` on PATH, then in `/usr/lib/swift/usr/bin`, then on PATH, then as
`swift format`.

## In the app

Both live under a project card's More (⋮), next to Check build, so the card's right side (days-left badge,
More, Run) stays as it is. `F` and `L` do the same for the highlighted project.

- **Format project…** opens a sheet that runs the dry run: a spinner while swift-format reads the files, then
  "3 files would change" with the number of files checked, the style used (the project's `.swift-format` or
  "Default style") and the swift-format version. Files swift-format couldn't read are listed in an amber box
  ("2 files couldn't be read and stay as they are", the first five with their error). The diff is monospace
  with old and new line numbers: removed lines in the theme's error colour, added ones in its success colour,
  each on a faint tint with a bar on the left, every file as a header row with its path and every hunk as an
  "@@ … @@" band. Trailing spaces on changed lines show as faint dots. Long lines scroll sideways while the
  line numbers and the file path stay put. A cut diff says "Showing the start of a long diff." with the path of
  the whole one and Open (your editor). Apply to N files writes them, shows a toast and closes the sheet; when
  that project's style warnings are on screen, they are checked again. Apply is greyed out while it checks,
  after an error, and when there's nothing to change, and its tooltip says which. "Already formatted" and an
  error state (with the helper's hint, e.g. "Open Setup" when swift-format is missing) replace the diff.
- **Lint project** runs the lint and opens the build panel on Problems. The button spins while it runs, and its
  tooltip has the last count and when. When the panel shows that project's build, the style warnings follow
  its problems under a **Style** header; for another project the panel switches to that project's style
  warnings only ("5 style warnings", "WeatherGlance · ~/…" under it, with Show build to go back; a build starting goes back
  too). The section has the count, Hide/Show and Check again, a line with the tools, the style and when it
  ran, and, when formatting fixes some of them, "Formatting fixes 3 of these." with Format project…. Each
  warning reads "rule · file:line", with a dim glyph (amber only for a rule set to error), and a click
  opens the file at the line in your editor, like a compiler error.
- Style warnings never make a build look failed: the result banner, the headline and the Problems tab's colour
  only count the build's own problems. The tab's number includes the style warnings while they're shown.
- Hide is remembered (`showStyleWarnings` in settings.json); hidden, the section says "5 style warnings
  hidden" and the tab doesn't count them.
- The panel shows the last saved lint of the project it's about (`format-lint --last`) without running
  anything, so the warnings are still there after a restart.

## AI agents (MCP)

- `ios_format(project_path, files=None, apply=false, max_bytes=None)`: dry run by default, with the files that
  would change and the diff (as plain lines in the text block). Show it to the user, then call again with
  `apply=true`. The diff gets the share of the 12 KB budget left after the other fields; a cut one has
  `truncated`, a `note` and `full_output_path` with the whole diff. `files_changed` is capped at 50 and
  `errors` at 20, with totals.
- `ios_format_lint(project_path, files=None, max_items=50, cached=false, max_bytes=None)`: findings with
  `file` relative to the project, `rule`, `source` and `hint` (the helper's app wording becomes the tool to call:
  `ios_format(apply=true)` for whitespace, `ios_build_start(action='compile')` and `ios_build_diagnostics` for a
  file that doesn't parse); `counts`, `by_source` and `by_rule` cover the
  whole project, and a cut list has `truncated` and a `note`. `cached=true` returns the stored result of the
  last whole-project lint without running anything.

## Demo mode

With `IOSSTUDIO_DEMO` set, WeatherGlance has two files to format (and a `.swift-format`), HabitTracker one, and
RecipeBox is already formatted. Lint returns a few swift-format and SwiftLint warnings with real-looking paths
in the demo projects. Applying answers as if done and writes nothing (`demo: true`; the app's toast says nothing was written).
