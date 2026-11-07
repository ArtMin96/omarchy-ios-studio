# Swift package dependencies

An app's packages are listed in its `Package.swift` and pinned in `Package.resolved`. iOS Studio lists them
with the versions in use, adds one by its git URL and links it to the app, updates them and removes one, so
you don't have to edit `Package.swift` by hand or get a target dependency wrong. It uses the Swift toolchain's
own `swift package` commands wherever one exists (`add-dependency`, `add-target-dependency`, `resolve`,
`update`, `dump-package`); only removing has no command, so that one edits the file as Swift text and checks
the result before writing.

## In the app

Packages… in a project card's ⋮ menu (`P`) opens the project's packages: each one with its version rule
("from 1.1.0"), the version it resolved to, and the targets and products that use it. From there you add a
package by URL (with a version rule and the target to link, the app's by default), update one or all, resolve
the ones not fetched yet, and remove one after looking at the change to `Package.swift`. Adding shows its
steps while it runs, updating and resolving show their progress above the list, and all three can be cancelled.
After adding, Check build compiles the app with the new package. See [UI](#ui) for the details.

## UI

`components/projects/ProjectPackagesSheet.qml`, opened from Packages… under a project card's More (⋮) or with `P` on the
highlighted card (listed in the `?` sheet). One sheet, several views:

- **List.** "4 packages" with Update all and Add package…, then one row per package: name (bold, elides only when
  the line is full), a `local` badge for path packages, the version rule on the right ("from 1.1.0",
  "branch main") and a badge with the resolved version (amber "not fetched" when it has none); the URL or path
  under it, elided in the middle so the repository name stays; then "Used by <targets> · <products>" (amber "Not
  linked to a target yet"). Each row has Update (off for local packages, saying why) and Remove. An amber
  "Unresolved" line with Resolve appears only when something isn't fetched or there's no `Package.resolved`.
  Loading, "No packages yet" (with Add package…) and a read error (with Try again) replace the list.
- **Update and resolve** run inline: a line with a spinner, the latest progress line from the helper and Cancel;
  then "1 package updated" with one "swift-collections 1.1.4 → 1.2.1" line per change, "Everything is up to
  date", "cancelled, nothing changed" or the error. While one runs, the other row actions are disabled and their
  tooltips say why.
- **Add.** Package URL (checked as you type: https, ssh, git@ or a local path; a URL the project already uses is
  named), Version rule (Up to next major, Up to next minor, Exact version, Branch, Revision, each with a one-line
  explanation) beside Version, Branch or Commit (empty means the newest release), and the target (the app's by
  default). Add package shows the helper's steps (Checking the package, Adding to Package.swift, Fetching,
  Reading products, Linking, Checking Package.swift) with what each found, and Cancel. A refused URL or version
  offers Back rather than Try again.
- **Products.** When the package has several libraries, a checklist (checked shows only in the checkbox); Link N libraries links them in one
  `add-product` call. Not now keeps the package without linking it.
- **Added.** What was linked, the requirement and resolved version, the `Package.swift` diff, and Check build,
  which closes the sheet and starts a compile-only build in the build panel.
- **Remove.** The dry run's diff and the target dependencies it unlinks, with a danger "Remove <name>". When the
  edit wouldn't be valid the sheet says so with the helper's reason, keeps the diff for doing it by hand, and
  offers no Remove button.

After a change the card's settings check and the editor status run again (`svc.packagesChanged`). The sheet
can't be dismissed while something runs (Cancel is there instead). In demo mode results say that nothing was
written.

## Listing

The list comes from `swift package dump-package` (run with its own scratch folder, so a build that holds the
project's `.build/` lock never blocks it, and cached until `Package.swift` changes: the first listing of a
project takes a few seconds because SwiftPM compiles the manifest) and from `Package.resolved`, read
directly. All three `Package.resolved` formats are understood (1 from Swift 5.5 and older, 2, and 3 with
`originHash`).

For each package:

- `identity` (SwiftPM's name for it: the URL's last path part, lowercased), `name`, `kind` (`remote`, `local`
  for `.package(path:)`, `registry` for `.package(id:)`), `url` or `path`
- `requirement`: `rule` (`from` = up to the next major version, `upToNextMinor`, `exact`, `range`, `branch`,
  `revision`, `local`) and `text` ("from 1.2.0", "up to next minor from 1.2.0", "exactly 1.2.0",
  "1.0.0 ..< 1.5.0", "branch main", "revision abc1234")
- `resolved`: `version` (or `branch`) and the short `revision` from `Package.resolved`; null when it isn't
  resolved yet, and for local packages
- `used_by` and `products_used`: the targets that link one of its products and which. `.product(name:package:)`
  entries count, and so do bare `"Name"` entries that name one of the package's products (not a target of the
  project with the same name)

Also `targets` (the app's own target first, marked `app`; test targets last), `app_target`, `resolved_file`
and `unresolved` (declared packages missing from `Package.resolved`). A project that also has
`Package@swift-*.swift` files gets a note: newer Swift versions read those, and only `Package.swift` is changed.

## Adding

1. **Check**: the URL is an `https://` or SSH (`git@host:owner/repo`) git URL, or a folder with a
   `Package.swift` (a local package, written as `.package(path:)` exactly as given). A link to a page inside a
   GitHub repository (`…/tree/main/…`) is cut back to the repository. A package already in `Package.swift`
   (same identity or URL) is `already-added`. Without a version, the rules `from`, `upToNextMinor` and `exact`
   use the newest release tag (`git ls-remote --tags`; pre-releases are skipped).
2. **Add**: `swift package add-dependency <url> --from 1.2.0` (or `--up-to-next-minor-from`, `--exact`,
   `--branch`, `--revision`, `--type path`).
3. **Fetch**: `swift package resolve`, its output turned into progress ("Fetching swift-collections",
   "swift-collections 1.7.5"). git never asks for a password (`GIT_TERMINAL_PROMPT=0`, SSH in batch mode), so
   a private or misspelled repository fails at once.
4. **Read products**: the package's library products, from its checkout's manifest. Products whose name starts
   with `_` are listed last and never picked on their own.
5. **Link**: with one product (or the products you named), `swift package add-target-dependency <product>
   <target> --package <identity>`. With several and none named, adding stops here with `needs_product` and the
   list; linking the chosen ones is `add-product`.
6. **Check** `Package.swift` again with `dump-package` and confirm the target lists the product.

If anything fails after step 2, or the add is cancelled, `Package.swift` and `Package.resolved` are put back
exactly as they were (the fetched checkout stays in `.build/`, unused). The error says what went wrong with a
hint, and has diagnostics in the build diagnostics' shape with category `package`: `fetch-failed` (can't
clone), `network`, `resolve-failed` (no version matches), `manifest-invalid`, `unknown-product`,
`unknown-target`, `no-products`, `no-versions`.

Adding a package doesn't prove it builds for iOS: run Check build (a compile-only build). Packages with binary
targets, macOS-only build tool plugins or macros, or `AppKit` imports usually don't.

## Updating and resolving

Update runs `swift package update` (one package, or all) and resolve runs `swift package resolve`. Both
compare `Package.resolved` before and after and report `changes`: `[{identity, from, to}]`, where `from` or
`to` is a version, `branch@commit`, or null for a pin that was added or dropped, and a one-line `summary`
("2 packages updated: swift-collections 1.1.3 → 1.1.4, swift-log 1.6.1 (new)" or "Everything is up to
date"). `Package.swift` isn't touched. A failed or cancelled run puts `Package.resolved` back.

## Removing

SwiftPM has no `remove-dependency`, so iOS Studio edits `Package.swift` itself, on tokens from a small Swift
lexer (string literals including multi-line `"""` and raw `#"…"#` strings with interpolation, `//` and nested
`/* */` comments, balanced brackets), never with a plain search and replace. It removes:

- the package's `.package(…)` entry (matched by identity or URL: `url:`, `path:`, `id:`, or the old
  `.package(name:url:)` form), wherever the array holding it is: `Package(dependencies: [...])` or a
  `let dependencies: [Package.Dependency] = [...]` variable
- every `.product(name: …, package: "<identity or old-style name>")` in target dependencies
- bare `"Name"` and `.byName(name: "Name")` dependencies naming one of the package's products, unless a target
  of the project has that name

An entry on its own lines goes with its lines and a comment after it on the same line; an entry sharing a line
goes with its comma; an array left empty becomes `[]`. Commented-out lines and strings that merely mention
`.package(` stay as they are. Windows line endings are kept.

The dry run returns the unified diff, `would_remove` (`{package, target_deps: [{target, product}]}`) and
`valid`: the edited file is written to a temporary folder and checked with `swift package dump-package`, and
the result must no longer declare the package or use its products. Removing for real (confirm) writes the file
atomically (keeping its permissions), runs `dump-package` again in place and puts the old file back if that
fails, then drops the package's pins from `Package.resolved`, along with the pins only it needed (from
`swift package show-dependencies`, offline), in SwiftPM's own layout; the file is deleted when no pin is left,
as SwiftPM does.

When the package isn't a plain list entry (added with `package.dependencies.append(...)`, a URL built from
strings, an array joined with `+`) the edit is refused with `edit-invalid`, the problems and the diff, and
nothing is written.

## The helper commands

```
iosstudio-helper packages list --project P [--fresh]
iosstudio-helper packages add --project P --url U [--rule from|upToNextMinor|exact|branch|revision]
                 [--version V] [--target T] [--product X [Y …] | --products '["X","Y"]'] [--dry-run]
iosstudio-helper packages add-product --project P --package ID --product X [--target T]
iosstudio-helper packages update --project P [--package ID]
iosstudio-helper packages resolve --project P
iosstudio-helper packages remove --project P --package ID [--confirm] [--max-bytes N]
```

`list`, `add-product` and `remove` print one JSON object. `add` (also its `--dry-run`), `update` and
`resolve` stream JSON lines and end with `done`:

```json
{"event":"step","id":"fetch","title":"Fetching","state":"pending"}
{"event":"step","id":"fetch","title":"Fetching swift-collections","state":"running"}
{"event":"step","id":"fetch","title":"Fetching swift-collections","state":"running","detail":"swift-collections 1.7.5"}
{"event":"progress","fraction":0.366,"detail":"swift-collections 1.7.5","eta":null}
{"event":"log","text":"Computed https://github.com/apple/swift-collections at 1.7.5 (9.93s)"}
{"event":"step","id":"fetch","title":"Fetching swift-collections","state":"done"}
{"event":"done","ok":true,"data":{…}}
```

Every step is announced as `pending` first. Step ids: `add`: `check`, `add`, `fetch`, `products`, `link`,
`validate`; `update`: `update`, `changes`; `resolve`: `resolve`, `changes`. States: `pending`, `running`,
`done`, `failed`, `cancelled`, `skipped` (`link` when a product has to be chosen, detail "Choose a
product"). A running step's `detail` changes as swift reports progress; git's object counters
("Fetching swift-collections 42%") come at most twice a second and aren't logged. A failure ends with
`{"event":"done","ok":false,"error":{"code","message","hint","diagnostics",…}}`.

A running command stops on `{"cancel": true}` on stdin or on SIGTERM: swift's whole process group is killed
and the files are put back (`error.code` `cancelled`). Package changes take turns per project through a lock in
`~/.local/state/ios-studio/packages/` (`package-busy` otherwise). A change that has to wait for a running
build's lock on `.build/` says so in its detail and continues by itself.

## For AI agents

`ios_package_list`, `ios_package_add`, `ios_package_update`, `ios_package_resolve` and `ios_package_remove`
(see [../mcp.md](../mcp.md)). Add, update and resolve are jobs: they return `state: "running"` and a `job_id`;
calling the same tool with `job_id` (and `wait_seconds`, at most 25) returns the steps, progress and finally
the result, and `ios_job_cancel(job_id)` cancels (`ios_job_status(job_id)` works too). When a package has several products the agent
asks the user which one and calls `ios_package_add` again with `product=`; that only links it. Remove is a dry
run until `confirm=true`. Diffs are cut to the result budget, the whole diff saved to `full_output_path`
(the newest 10 in `~/.cache/ios-studio/packages/diffs/`). Hints in results name the MCP tool to call next.

## Demo mode

With `IOSSTUDIO_DEMO` set, WeatherGlance has swift-collections (from 1.1.0, resolved 1.1.4), swift-algorithms
and a local package; HabitTracker has Kingfisher, swift-algorithms on a branch and an unresolved
ConfettiSwiftUI. Add, update, resolve and remove stream made-up steps and return canned results; nothing is
fetched or written.
