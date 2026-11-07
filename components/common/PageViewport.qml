import QtQuick
import qs.Commons

// Virtualizes a ListView / GridView that is as tall as its content inside a scrolling page (PageFrame's
// Flickable): the view doesn't scroll itself, so it would create every row; with these display margins
// it only creates the rows inside the page's viewport, plus a screen of slack each way.
//   ListView {
//     id: list
//     Layout.fillWidth: true; Layout.preferredHeight: contentHeight; interactive: false; reuseItems: true
//     PageViewport { id: viewport; view: list }
//     displayMarginBeginning: viewport.marginBeginning; displayMarginEnd: viewport.marginEnd
//   }
// reveal(y0, y1) scrolls the page so that band of the view (view coordinates) is on screen.
QtObject {
  id: root
  required property Flickable view
  property Flickable page: null             // found on completion: the nearest Flickable above the view
  property real top: 0                      // the view's y in the page's content
  readonly property real slack: page ? page.height : 0
  readonly property real marginBeginning: page ? -Math.max(0, page.contentY - top - slack) : 0
  readonly property real marginEnd: page ? -Math.max(0, top + view.height - page.contentY - page.height - slack) : 0

  function place() {
    if (root.page && root.view.visible) root.top = root.view.mapToItem(root.page.contentItem, 0, 0).y
  }
  function reveal(y0, y1) {
    var p = root.page
    if (!p) return
    root.place()
    var a = root.top + y0, b = root.top + y1
    if (a < p.contentY) p.contentY = Math.max(0, a - Style.spacing.lg)
    else if (b > p.contentY + p.height) p.contentY = b - p.height + Style.spacing.lg
  }

  // Anything above the view that moves it changes the page's content height or comes with a scroll;
  // mapToItem is cheap, so re-place on those instead of watching every ancestor.
  property Connections _view: Connections {
    target: root.view
    function onYChanged() { root.place() }
    function onHeightChanged() { root.place() }
    function onVisibleChanged() { root.place() }
  }
  property Connections _page: Connections {
    target: root.page
    function onContentYChanged() { root.place() }
    function onContentHeightChanged() { root.place() }
    function onHeightChanged() { root.place() }
  }

  Component.onCompleted: {
    var p = root.view.parent
    while (p && !(p instanceof Flickable)) p = p.parent
    root.page = p
    root.place()
  }
}
