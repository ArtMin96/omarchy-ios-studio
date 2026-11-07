import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui

// The card primitive: a quiet raised surface with an optional header.
//   Card {
//     title: "Apple ID"; glyph: Model.Glyph.apple; subtitle: "Free team"
//     actions: [ IconButton { glyph: Model.Glyph.refresh; onClicked: … } ]
//     AppText { text: "…" }                 // body content (a ColumnLayout)
//   }
// `hasCursor`/`clickable` turn it into a large hit target (Home cards).
BorderSurface {
  id: root

  property string title: ""
  property string subtitle: ""
  property string glyph: ""
  property color glyphColor: Theme.dim
  property bool hasCursor: false
  property bool clickable: false
  property bool accentEdge: false        // accent rule on the left edge (highlight a card)
  property color edgeColor: Commons.Color.accent
  property real contentSpacing: Style.spacing.lg
  property alias actions: actionsRow.data
  default property alias content: body.data
  readonly property bool hot: hasCursor || (clickable && hover.hovered)

  signal clicked()

  radius: Style.cornerRadius
  color: hot ? Theme.surfaceRaised : Theme.surface
  borderSpec: Border.flat(hot ? Util.alpha(Commons.Color.foreground, 0.22) : Theme.hairline, Style.normalBorderWidth)
  padding: Style.spacing.panelPadding
  implicitWidth: Style.space(280)
  implicitHeight: col.implicitHeight + contentTopInset + contentBottomInset

  Behavior on color { ColorAnimation { duration: 140 } }

  Rectangle {
    visible: root.accentEdge
    anchors.left: parent.left
    anchors.top: parent.top
    anchors.bottom: parent.bottom
    width: Style.space(2)
    color: root.edgeColor
  }

  ColumnLayout {
    id: col
    anchors.fill: parent
    anchors.topMargin: root.contentTopInset
    anchors.bottomMargin: root.contentBottomInset
    anchors.leftMargin: root.contentLeftInset
    anchors.rightMargin: root.contentRightInset
    spacing: root.contentSpacing

    RowLayout {
      Layout.fillWidth: true
      visible: root.title !== "" || actionsRow.children.length > 0
      spacing: Style.spacing.lg

      // The glyph shares the title's line (centred on it, not on title + subtitle);
      // the subtitle starts on the card's content edge, like the body below it.
      ColumnLayout {
        Layout.fillWidth: true
        spacing: 0
        RowLayout {
          Layout.fillWidth: true
          spacing: Style.spacing.lg
          Icon {
            id: glyphIcon
            visible: root.glyph !== ""
            glyph: root.glyph
            color: root.glyphColor
            size: Style.font.icon
          }
          AppText {
            Layout.fillWidth: true
            text: root.title
            size: "title"
            strong: true
            elide: Text.ElideRight
          }
        }
        AppText {
          Layout.fillWidth: true
          visible: root.subtitle !== ""
          text: root.subtitle
          size: "small"
          tone: "dim"
          elide: Text.ElideRight
        }
      }
      Row {
        id: actionsRow
        Layout.alignment: Qt.AlignVCenter
        spacing: Style.spacing.xs
      }
    }

    ColumnLayout {
      id: body
      Layout.fillWidth: true
      Layout.fillHeight: true
      spacing: root.contentSpacing
    }
  }

  HoverHandler { id: hover; enabled: root.clickable; cursorShape: Qt.PointingHandCursor }
  TapHandler { enabled: root.clickable; onTapped: root.clicked() }
}
