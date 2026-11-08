import QtQuick
import qs.Commons
import qs.Commons as Commons
import "../../lib/Model.js" as Model

// In-page tabs with an accent underline.
//   PageTabs {
//     tabs: [ { key: "live", label: "Live log", glyph: Model.Glyph.console },
//             { key: "crashes", label: "Crashes", glyph: Model.Glyph.crash, count: 3, level: "warn" } ]
//     current: page.tab
//     onSelected: function(key) { page.tab = key }
//   }
//
// Narrow windows: when the tabs don't fit, their padding tightens first; if they still don't fit, the
// strip scrolls sideways (mouse wheel or drag) with faded edges, and the current tab (also when the page
// changes it from the keyboard) is scrolled into view. Labels never truncate.
Item {
  id: root
  property var tabs: []
  property string current: ""
  property int cursorIndex: -1
  signal selected(string key)

  readonly property int fullPad: Style.spacing.xxl
  readonly property int tightPad: Style.spacing.lg
  // Sum of the tab contents (padding-independent), so the fit test doesn't depend on the padding it picks.
  readonly property real contentSum: {
    var w = 0
    for (var i = 0; i < measure.count; i++) {
      var it = measure.itemAt(i)
      if (it) w += it.implicitWidth
    }
    return w + Math.max(0, measure.count - 1) * row.spacing
  }
  readonly property int pad: width <= 0 || contentSum + measure.count * fullPad * 2 <= width ? fullPad : tightPad
  readonly property bool overflowing: flick.contentWidth > flick.width + 1

  implicitHeight: Style.space(34)
  implicitWidth: contentSum + measure.count * fullPad * 2

  // Transparent copies of the tab contents, measured only.
  Row {
    opacity: 0
    enabled: false
    Repeater {
      id: measure
      model: root.tabs
      delegate: TabContent {
        required property var modelData
        tabData: modelData
        selected: root.current === modelData.key
      }
    }
  }

  Rectangle {
    anchors.left: parent.left; anchors.right: parent.right; anchors.bottom: parent.bottom
    height: Style.spacing.hairline
    color: Theme.hairline
  }

  Flickable {
    id: flick
    anchors.fill: parent
    contentWidth: row.width
    contentHeight: height
    flickableDirection: Flickable.HorizontalFlick
    boundsBehavior: Flickable.StopAtBounds
    interactive: root.overflowing
    clip: true
    Behavior on contentX { id: scrollAnim; enabled: false; NumberAnimation { duration: 180; easing.type: Easing.OutCubic } }

    Row {
      id: row
      height: flick.height
      spacing: Style.spacing.xs

      Repeater {
        id: tabRepeater
        model: root.tabs
        delegate: Item {
          id: tab
          required property var modelData
          required property int index
          readonly property bool isCurrent: root.current === modelData.key
          readonly property bool hot: hover.hovered || root.cursorIndex === index
          width: content.implicitWidth + root.pad * 2
          height: row.height

          Rectangle {
            anchors.fill: parent
            anchors.bottomMargin: Style.space(2)
            radius: Style.cornerRadius
            color: tab.hot ? Style.hoverFill : "transparent"
            Behavior on color { ColorAnimation { duration: 100 } }
          }
          TabContent {
            id: content
            anchors.centerIn: parent
            tabData: tab.modelData
            selected: tab.isCurrent
          }
          Rectangle {
            anchors.left: parent.left; anchors.right: parent.right; anchors.bottom: parent.bottom
            anchors.leftMargin: Style.spacing.md; anchors.rightMargin: Style.spacing.md
            height: Style.space(2)
            color: Commons.Color.accent
            opacity: tab.isCurrent ? 1 : 0
            Behavior on opacity { NumberAnimation { duration: 140 } }
          }
          HoverHandler { id: hover; cursorShape: Qt.PointingHandCursor }
          TapHandler { onTapped: root.selected(tab.modelData.key) }
        }
      }
    }

    // The wheel scrolls the strip sideways only while it overflows; at either end it goes on to the page.
    WheelHandler {
      enabled: root.overflowing
      acceptedDevices: PointerDevice.Mouse | PointerDevice.TouchPad
      onWheel: function(event) {
        var d = event.angleDelta.x !== 0 ? event.angleDelta.x : event.angleDelta.y
        var max = flick.contentWidth - flick.width
        var x = Math.max(0, Math.min(max, flick.contentX - d / 2))
        if (x === flick.contentX) { event.accepted = false; return }
        scrollAnim.enabled = false
        flick.contentX = x
      }
    }
  }

  // Faded edges show there is more to scroll to.
  Rectangle {
    anchors.left: parent.left; anchors.top: parent.top; anchors.bottom: parent.bottom
    anchors.bottomMargin: Style.spacing.hairline
    width: Style.space(48)
    visible: root.overflowing && flick.contentX > 1
    gradient: Gradient {
      orientation: Gradient.Horizontal
      GradientStop { position: 0.0; color: Theme.bg }
      GradientStop { position: 1.0; color: Util.alpha(Theme.bg, 0) }
    }
  }
  Rectangle {
    anchors.right: parent.right; anchors.top: parent.top; anchors.bottom: parent.bottom
    anchors.bottomMargin: Style.spacing.hairline
    width: Style.space(48)
    visible: root.overflowing && flick.contentX < flick.contentWidth - flick.width - 1
    gradient: Gradient {
      orientation: Gradient.Horizontal
      GradientStop { position: 0.0; color: Util.alpha(Theme.bg, 0) }
      GradientStop { position: 1.0; color: Theme.bg }
    }
  }

  // Keep the current tab, plus a sliver of its neighbours so they stay discoverable, in view.
  function revealCurrent(animate) {
    if (!root.overflowing) { scrollAnim.enabled = false; flick.contentX = 0; return }
    var idx = -1
    for (var i = 0; i < root.tabs.length; i++) if (root.tabs[i].key === root.current) idx = i
    var it = idx >= 0 ? tabRepeater.itemAt(idx) : null
    if (!it) return
    var margin = Style.space(40)
    var x = flick.contentX
    if (it.x - margin < x) x = it.x - margin
    else if (it.x + it.width + margin > x + flick.width) x = it.x + it.width + margin - flick.width
    x = Math.max(0, Math.min(flick.contentWidth - flick.width, x))
    scrollAnim.enabled = animate
    flick.contentX = x
  }
  onCurrentChanged: Qt.callLater(revealCurrent, true)
  onWidthChanged: Qt.callLater(revealCurrent, false)
  onTabsChanged: Qt.callLater(revealCurrent, false)
  onPadChanged: Qt.callLater(revealCurrent, false)

  component TabContent: Row {
    id: tc
    property var tabData: ({})
    property bool selected: false
    spacing: Style.spacing.md
    Icon { visible: !!tc.tabData.glyph; glyph: tc.tabData.glyph || ""; size: Style.font.iconSmall; color: tc.selected ? Commons.Color.foreground : Theme.dim; anchors.verticalCenter: parent.verticalCenter }
    AppText { text: tc.tabData.label || ""; size: "body"; strong: tc.selected; tone: tc.selected ? "primary" : "dim"; anchors.verticalCenter: parent.verticalCenter }
    Badge {
      visible: tc.tabData.count !== undefined && tc.tabData.count !== null && tc.tabData.count !== ""
      text: String(tc.tabData.count === undefined ? "" : tc.tabData.count)
      level: tc.tabData.level || "info"
      anchors.verticalCenter: parent.verticalCenter
    }
  }
}
