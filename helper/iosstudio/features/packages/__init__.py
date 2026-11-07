"""Swift package dependencies of a project: list, add, update, resolve and remove (stdlib only).

    packages list --project P
    packages add --project P --url U [--rule from|upToNextMinor|exact|branch|revision] [--version V]
                 [--target T] [--product X [Y …] | --products '<json list>'] [--dry-run]          (streams)
    packages add-product --project P --package ID --product X [Y …] [--target T]
    packages update --project P [--package ID]                                                    (streams)
    packages resolve --project P                                                                  (streams)
    packages remove --project P --package ID [--confirm] [--max-bytes N]

`list` reads Package.swift through `swift package dump-package` (cached by the manifest's mtime and size, run
with its own scratch folder so a running build's lock on .build/ never blocks it) and Package.resolved directly
(formats 1, 2 and 3, see manifest.py).

`add` edits the manifest with `swift package add-dependency`, fetches with `swift package resolve`, reads the
package's library products from its checkout, and links one with `swift package add-target-dependency` (default
target: the app's). With several products and no --product it stops after fetching with needs_product and the
product list; `add-product` then links the chosen one(s). A failure after the first edit puts Package.swift and
Package.resolved back as they were. Without --version, rule from/upToNextMinor/exact uses the newest release tag
(`git ls-remote --tags`).

`update` and `resolve` run `swift package update [ID]` / `swift package resolve` and report `changes`, the pins
that moved, from Package.resolved before and after.

`remove` has no SwiftPM command, so it edits Package.swift as text (manifest_edit.remove_package): the
.package entry and every target dependency on the package. Without --confirm it's a dry run: the diff and
whether the result is valid (checked with `dump-package` on a copy in a temp folder). With --confirm the file is
written atomically, checked again in place (put back if it fails), and the package's pins (and those only it
needed) are dropped from Package.resolved.

Streaming commands send `step` events (ids below), `progress` (fraction, detail), `log` (each line of swift's
output) and `done`. They stop on {"cancel": true} on stdin or SIGTERM: swift's process group is killed and the
manifest and Package.resolved are put back. One mutating command per project at a time (`busy` otherwise).

    add      check, add, fetch, products, link, validate
    update   update, changes          resolve   resolve, changes

Errors are {code, message, hint?, diagnostics?, …}; diagnostics have the build diagnostics' shape
(category 'package', diagnostics.py parses swift's output).

Modules: commands (handlers, flags, JSON out), project (project folder, lock, backup, cached dump-package,
checkouts, products, the list), swift (swift/git runs, progress lines, errors), add (add, add-product),
update (update, resolve, remove), manifest (dump-package and Package.resolved as data), manifest_edit (the remove
edit through a small Swift lexer), demo_mode (IOSSTUDIO_DEMO).
"""
from iosstudio.features.packages.commands import GROUPS, demo

__all__ = ["GROUPS", "demo"]
