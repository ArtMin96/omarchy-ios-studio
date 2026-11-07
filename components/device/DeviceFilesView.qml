import QtQuick
import QtQuick.Layouts
import qs.Commons
import "../common"

// iPhone › Files: the camera roll and the camera test loop, one at a time. (An app's own files and saved
// data are in iPhone › Apps › the app.)
//   DeviceFilesView { app: page.app; active: page.tab === "files" }
ColumnLayout {
  id: root
  required property var app
  property bool active: false
  // "apps" and "data" were parts of this tab before; they live in the app details now
  property string section: app.setting("filesSection", "media") === "camera" ? "camera" : "media"

  spacing: Style.spacing.xl

  function show(s) { root.section = s; root.app.setSetting("filesSection", s) }

  ButtonGroupLite {
    options: [{ value: "media", label: "Photos & videos" }, { value: "camera", label: "Camera test" }]
    value: root.section
    onPicked: function(v) { root.show(v) }
  }

  DeviceMediaView {
    Layout.fillWidth: true
    visible: root.section === "media"
    active: root.active && visible
    app: root.app
  }
  DeviceCameraTestView {
    Layout.fillWidth: true
    visible: root.section === "camera"
    active: root.active && visible
    app: root.app
  }
}
