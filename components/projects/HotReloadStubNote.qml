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
//   HotReloadStubNote { files: app.hotReload.importedBy(project); busy: entry.action === "cleaning"; onCleanUp: app.hotReload.cleanUp(project) }
// Its glyph sits where HotReloadBar's does (the same inset and glyph column), so the two line up on a card.
RowLayout {
  id: root
  property var files: []
  property bool busy: false

  signal cleanUp()

  spacing: Style.spacing.lg
  Layout.leftMargin: Style.spacing.xl
  Layout.rightMargin: Style.spacing.md

  function names() { return Model.hotReloadFileNames(root.files) }

  FontMetrics { id: metrics; font: note.font }
  Item {
    Layout.preferredWidth: Math.ceil(Style.font.body * 1.4)
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
