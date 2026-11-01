import QtQuick
import qs.Commons
import qs.Commons as Commons

// Indeterminate activity arc.
//   Spinner { size: Style.font.heading; running: service.status === "loading" }
Item {
  id: root
  property real size: Style.font.heading
  property color color: Commons.Color.accent
  property bool running: true
  property real lineWidth: Math.max(1.5, size / 9)

  implicitWidth: size
  implicitHeight: size
  visible: running

  Canvas {
    id: canvas
    anchors.fill: parent
    antialiasing: true
    onPaint: {
      var ctx = getContext("2d")
      ctx.reset()
      var r = Math.min(width, height) / 2 - root.lineWidth
      ctx.lineWidth = root.lineWidth
      ctx.lineCap = "round"
      ctx.strokeStyle = Qt.rgba(root.color.r, root.color.g, root.color.b, 0.18)
      ctx.beginPath(); ctx.arc(width / 2, height / 2, r, 0, Math.PI * 2); ctx.stroke()
      ctx.strokeStyle = root.color
      ctx.beginPath(); ctx.arc(width / 2, height / 2, r, -Math.PI / 2, Math.PI * 0.35); ctx.stroke()
    }
    Connections { target: root; function onColorChanged() { canvas.requestPaint() } }
    RotationAnimation on rotation {
      running: root.running && root.visible
      from: 0; to: 360; duration: 900
      loops: Animation.Infinite
    }
  }
}
