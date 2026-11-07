import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons

// Standard page scaffold: comfortable margins, a PageHeader, and a vertically
// scrolling ColumnLayout for content. Content is width-capped for readability
// on wide windows.
//   PageFrame {
//     title: "Projects"; subtitle: "Build and run your apps"
//     actions: [ ActionButton { … } ]
//     Card { Layout.fillWidth: true; … }       // children go into the ColumnLayout
//   }
// Set `scrollable: false` when the content manages its own scrolling (LogView,
// big lists) — the content area then fills the remaining height.
Item {
  id: root
  property string title: ""
  property string subtitle: ""
  property string glyph: ""
  property alias actions: header.actions
  property alias status: header.status
  property bool scrollable: true
  property real maxContentWidth: Style.space(980)
  property real spacing: Style.spacing.huge
  default property alias content: body.data
  readonly property alias flickable: flick

  function scrollToItem(item) {
    if (!item || !root.scrollable) return
    var p = item.mapToItem(body, 0, 0)
    var top = p.y, bottom = p.y + item.height
    if (top < flick.contentY) flick.contentY = Math.max(0, top - Style.spacing.lg)
    else if (bottom > flick.contentY + flick.height) flick.contentY = bottom - flick.height + Style.spacing.lg
  }

  readonly property real _margin: Style.spacing.huge + Style.spacing.xxl
  readonly property real _colWidth: Math.min(width - _margin * 2, maxContentWidth)

  PageHeader {
    id: header
    x: (root.width - root._colWidth) / 2
    y: root._margin
    width: root._colWidth
    title: root.title
    subtitle: root.subtitle
    glyph: root.glyph
  }

  Flickable {
    id: flick
    anchors.top: header.bottom
    anchors.topMargin: root.spacing
    anchors.left: parent.left
    anchors.right: parent.right
    anchors.bottom: parent.bottom
    clip: true
    interactive: root.scrollable
    contentWidth: width
    contentHeight: root.scrollable ? body.implicitHeight + root._margin : height
    boundsBehavior: Flickable.StopAtBounds
    ScrollBar.vertical: ScrollBar { policy: root.scrollable && flick.contentHeight > flick.height ? ScrollBar.AsNeeded : ScrollBar.AlwaysOff }

    ColumnLayout {
      id: body
      x: (root.width - root._colWidth) / 2
      width: root._colWidth
      height: root.scrollable ? implicitHeight : flick.height - root._margin
      spacing: root.spacing
    }
  }
}
