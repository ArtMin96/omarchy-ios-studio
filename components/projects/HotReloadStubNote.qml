import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import "../common"
import "../../lib/Model.js" as Model

// A quiet line on a project card whose hot reload is off while some views still `import HotReload` (mode
// stub): they keep building against a no-op copy in .iosstudio/hot-reload. Lists the files when this window
// turned it off, or `hotreload status` reports it in stub mode (both carry importedBy).
// Clean up runs `hotreload disable` again: once no file imports HotReload, the block and the copy go.
//   HotReloadStubNote { files: app.hotReload.importedBy(project); busy: …; onCleanUp: app.hotReload.setEnabled(project, false) }
RowLayout {
  id: root
  property var files: []
  property bool busy: false

  signal cleanUp()

  spacing: Style.spacing.lg

  function names() {
    var l = (root.files || []).map(function(f) { var s = String(f); return s.substring(s.lastIndexOf("/") + 1) })
    if (l.length <= 3) return l.join(", ")
    return l.slice(0, 3).join(", ") + " and " + (l.length - 3) + " more"
  }

  FontMetrics { id: metrics; font: note.font }
  Item {
    Layout.preferredWidth: Style.font.bodySmall
    Layout.preferredHeight: metrics.height
    Layout.alignment: Qt.AlignTop
    Icon { anchors.centerIn: parent; glyph: Model.Glyph.hotReload; size: Style.font.bodySmall; color: Theme.faint }
  }
  AppText {
    id: note
    Layout.fillWidth: true
    Layout.preferredWidth: 0
    size: "small"
    tone: "faint"
    wrapMode: Text.Wrap
    text: (root.files || []).length
          ? "Hot reload is off. " + root.names() + (root.files.length === 1 ? " still imports" : " still import")
            + " HotReload, so the app builds against a no-op copy of it. Once the imports are gone, Clean up removes it."
          : "Hot reload is off. Some views still import HotReload, so the app builds against a no-op copy of it. "
            + "Once the imports are gone, Clean up removes it."
  }
  ActionButton {
    Layout.alignment: Qt.AlignTop
    text: "Clean up"
    kind: "ghost"
    compact: true
    busy: root.busy
    tooltip: "Take the hot reload lines out of Package.swift and remove .iosstudio/hot-reload, if no file imports HotReload anymore"
    onClicked: root.cleanUp()
  }
}
