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

  function apply() {
    Commons.Color.colorsFile.reload()
    Commons.Color.shellFile.reload()
    Style.scheduleRefresh()
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
