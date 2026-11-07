import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons

// Centered spinner + message for whole-area loads. For lists prefer
// <Skeleton> so the shape is preserved.
//   LoadingState { text: "Asking Apple for your certificates…"; detail: "This can take ~10 seconds." }
Item {
  id: root
  property string text: "Loading…"
  property string detail: ""

  implicitHeight: col.implicitHeight + Style.spacing.huge * 2
  implicitWidth: col.implicitWidth

  ColumnLayout {
    id: col
    anchors.centerIn: parent
    width: Math.min(parent.width - Style.spacing.huge * 2, Style.space(420))
    spacing: Style.spacing.lg
    Spinner { Layout.alignment: Qt.AlignHCenter; size: Style.font.display }
    AppText { Layout.fillWidth: true; horizontalAlignment: Text.AlignHCenter; text: root.text; size: "subtitle"; wrapMode: Text.WordWrap }
    AppText { Layout.fillWidth: true; visible: root.detail !== ""; horizontalAlignment: Text.AlignHCenter; text: root.detail; tone: "dim"; size: "small"; wrapMode: Text.WordWrap }
  }
}
