import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Commons as Commons

// Commons.Color doesn't watch the theme files (the shell gets an IPC push
// from omarchy-theme-set instead). `omarchy theme set` rewrites theme.name
// after swapping the theme dir, so watching that file is enough. The
// theme-set hook installed by install.sh calls `applyTheme` over IPC as a
// belt-and-braces path.
Item {
  id: root

  signal applied()

  // These are Omarchy internals (Commons.Color's FileViews, Style.scheduleRefresh), not an API:
  // check each one, so an Omarchy update that renames them can't break theme reloads.
  function _reload(file) { if (file && typeof file.reload === "function") file.reload() }
  function apply() {
    root._reload(Commons.Color.colorsFile)
    root._reload(Commons.Color.shellFile)
    if (typeof Style.scheduleRefresh === "function") Style.scheduleRefresh()
    root.applied()
  }

  FileView {
    path: Quickshell.env("HOME") + "/.local/state/omarchy/current/theme.name"
    watchChanges: true
    printErrors: false
    onFileChanged: { reload(); debounce.restart() }
  }

  // The theme dir is replaced with `mv`; give it a beat to settle.
  Timer { id: debounce; interval: 150; onTriggered: root.apply() }
}
