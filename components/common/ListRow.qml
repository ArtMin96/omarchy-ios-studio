import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui

// A selectable list row on the shared cursor model: glyph, title, subtitle,
// meta text and trailing controls. Visuals come only from `hasCursor`/`current`
// (bind both from the page's cursor state; `hovered` lets mouse move the cursor).
//   ListRow {
//     glyph: Model.Glyph.apps; title: app.name; subtitle: app.bundleId; meta: "1.2.0"
//     hasCursor: page.cursor === index
//     onHovered: function(h) { if (h) page.cursor = index }
//     onClicked: page.open(index)
//     trailing: [ IconButton { glyph: Model.Glyph.play; tooltip: "Launch" } ]
//   }
CursorSurface {
  id: root
  property string glyph: ""
  property color glyphColor: Theme.dim
  property string title: ""
  property string subtitle: ""
  property string meta: ""
  property string badgeLevel: ""        // shows a StatusPill-coloured dot before the meta
  property alias trailing: trail.data
  property alias leading: lead.data

  signal clicked()
  signal doubleClicked()
  signal hovered(bool isHovered)

  // Narrow rows put the meta text under the subtitle so the title keeps its room.
  readonly property bool narrow: width > 0 && width < Style.space(560)

  implicitHeight: Math.max(Style.space(44), row.implicitHeight + Style.spacing.lg * 2)
  implicitWidth: Style.space(320)

  RowLayout {
    id: row
    anchors.fill: parent
    anchors.leftMargin: Style.spacing.rowPaddingX
    anchors.rightMargin: Style.spacing.md
    spacing: Style.spacing.xxl

    Item { id: lead; implicitWidth: childrenRect.width; implicitHeight: childrenRect.height; visible: children.length > 0 }
    Icon {
      visible: root.glyph !== ""
      glyph: root.glyph
      color: root.hasCursor ? Commons.Color.foreground : root.glyphColor
      size: Style.font.iconLarge
      Layout.preferredWidth: Style.font.iconLarge * 1.4
    }
    ColumnLayout {
      Layout.fillWidth: true
      spacing: Style.spacing.xxs
      AppText { Layout.fillWidth: true; text: root.title; elide: Text.ElideRight; strong: root.current }
      AppText { Layout.fillWidth: true; visible: root.subtitle !== ""; text: root.subtitle; size: "small"; tone: "dim"; elide: Text.ElideRight }
      AppText { Layout.fillWidth: true; visible: root.narrow && root.meta !== ""; text: root.meta; size: "small"; tone: "faint"; elide: Text.ElideRight }
    }
    Badge { visible: root.badgeLevel !== ""; level: root.badgeLevel }
    AppText { visible: !root.narrow && root.meta !== ""; text: root.meta; size: "small"; tone: "faint"; elide: Text.ElideRight; Layout.maximumWidth: root.width * 0.35 }
    Row { id: trail; spacing: Style.spacing.xxs; Layout.alignment: Qt.AlignVCenter }
  }

  MouseArea {
    anchors.fill: parent
    z: -1
    hoverEnabled: true
    cursorShape: Qt.PointingHandCursor
    onContainsMouseChanged: root.hovered(containsMouse)
    onClicked: root.clicked()
    onDoubleClicked: root.doubleClicked()
  }
}
