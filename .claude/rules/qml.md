---
paths:
  - "**/*.qml"
  - "lib/**/*.js"
---
# QML rules

APIs: [docs/components.md](../../docs/components.md). Wiring: [docs/development.md#qml-wiring](../../docs/development.md#qml-wiring).

## Data

- Services own data, pages show it. Data that two pages need lives in a service in `services/` as a
  `Resource` property; a page gets `required property var app` and reads `app.<service>`, never calls the
  helper for data a service already has.
- Backend data goes through `core/Resource.qml` + `components/common/StateView.qml`, not hand-rolled
  `status` strings with Skeleton/ErrorState/EmptyState. Fetch with `app.device.fetcher(cmd, args)` or
  `app.cli.fetcher(argv)`; `args`/`argv` may be functions read at each load.
- Refresh after an action with `resource.load(true)` (it queues one more fetch if one is running) or
  `invalidate()`; `reset()` when the device switches. Don't add re-run flags.
- Every async surface has loading, empty and error states; errors show the plain `message` and its fix button.

## Shared helpers (use them, don't re-implement)

- `app.toastError(err, prefix)` as the error callback: `app.cli.call(argv, "", ok, app.toastError)`.
- `app.copyText(text, label)`, `app.openPath(path)`, `app.showInFolder(file)`.
- `app.confirm({...})` for every destructive action (Cancel is the default).
- `Util.fileUrl(path)` (qs.Commons) for `Image.source`, never `"file://" + path`.
- `Model.clock(sec)` / `Model.elapsedSeconds(t)` for timers; `Model.formatBytes`, `relativeTime`,
  `errorMessage`, `plural` and the rest of `lib/Model.js` before writing a new formatter.
- `CommandCheck.check(name)` / `CommandCheck.found[name]` to ask whether a program is on PATH.

## Performance

- Pages and tabs load lazily (`Loader`, asynchronous). A view that does work takes `property bool active`
  and gates on it: `Resource { active: root.active && … }`, `Timer { running: root.active && … }`. Hidden
  views make no requests and run no timers.
- Long or unbounded lists are `ListView`s (virtualized), not `Repeater`s. `Repeater` is fine for short,
  fixed lists (a handful of rows).
- Streaming buffers change in place (`push`/`splice`, `ListModel.append` of a batch), never copied per
  update, and have a cap (see `components/common/LogView.qml`). Batch frequent events before they hit the model.
- Nothing animates unseen: an `Icon` with `spinning` turns only while it's visible and the window isn't
  hidden; bind a StatusPill's `level: "busy"` to work that is running (never leave it on a hidden pill), and
  gate your own animations and timers on `visible`. A hidden spinner keeps the whole window redrawing.

## Files and imports

- Imports between repo folders are relative so qmllint can follow them: pages import `"../components/common"`;
  `components/<area>/` imports `"../common"` and `"../../core"`; `lib/Model.js` as `Model` with the right `../`.
- A new folder must be imported from a file `shell.qml` already reaches, or its types are "not a type" at runtime.
- A widget used by a second area moves to `components/common/`. Check new component names against
  `/usr/lib/qt6/qml/QtQuick/Controls/Basic/` (hence `Theme`, `FilterField`, `ProgressTrack`, `PageTabs`).

## Look and detail

- Sizes from `Style.font.*`, spacing from `Style.spacing.*` / `Style.space(px)`, radius `Style.cornerRadius`,
  colours from `Commons.Color.*` or `Theme.*` (`Theme.levelColor(level)` for ok/warn/error/info/busy).
  No hard-coded pixels or hex colours.
- Icons sit vertically centred with their text; gaps and indents use the same tokens as neighbouring rows;
  text wraps or elides cleanly at narrow widths.
- No clutter: fold new controls into existing ones (the split Run button, a ⋮ menu) before adding buttons.
- Check the view in demo mode (`IOSSTUDIO_DEMO=ready qs -n -p .` with private XDG folders, see CLAUDE.md)
  with zoomed screenshots, and review them yourself before calling it done.

## Checks

- `/usr/lib/qt6/bin/qmllint path/to/File.qml`: must exit 0; read the warnings for your file and don't add
  new ones (unresolved `qs.Commons`/`qs.Ui` warnings are expected without the symlinks).
- `.venv/bin/pytest tests/qml` when you touch `core/Resource.qml` or `lib/Model.js`; add a `tst_*.qml` there
  for new logic in them.
