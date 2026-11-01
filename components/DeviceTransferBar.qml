import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import "../Model.js" as Model

// Progress of one running file transfer (a daemon `transfer` event), with a Cancel button.
//   DeviceTransferBar { Layout.fillWidth: true; transfer: root.transfer; verb: "Importing"; onCancel: root.cancel() }
RowLayout {
  id: root
  property var transfer: null        // {bytes, total, files, count, file, state}
  property string verb: "Copying"
  signal cancel()

  readonly property real fraction: transfer && transfer.total ? Math.min(1, transfer.bytes / transfer.total) : 0
  visible: !!transfer
  spacing: Style.spacing.xl

  ProgressTrack {
    Layout.fillWidth: true
    indeterminate: !root.transfer || !root.transfer.total
    value: root.fraction
    label: {
      var t = root.transfer
      if (!t) return ""
      var s = root.verb + (t.file ? " " + t.file : "…")
      if (t.count > 1) s += "  ·  " + Math.min(t.files + 1, t.count) + " of " + t.count
      return s
    }
    trailing: root.transfer && root.transfer.total ? Model.formatBytes(root.transfer.bytes) + " of " + Model.formatBytes(root.transfer.total) : ""
  }
  ActionButton { text: "Cancel"; kind: "ghost"; compact: true; glyph: Model.Glyph.close; onClicked: root.cancel() }
}
