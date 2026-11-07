import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../../lib/Model.js" as Model

// Filter box with a magnifier and a clear button. Esc clears, then gives
// focus back to the page (the global `/` key calls `focusField()`).
//   FilterField { id: search; placeholder: "Filter apps"; onTextChanged: list.filter = text }
//   function focusSearch() { search.focusField() }      // page hook for `/`
Item {
  id: root
  property alias text: input.text
  property string placeholder: "Search"
  property alias field: input
  readonly property bool editing: input.activeFocus

  signal accepted(string text)
  signal cleared()

  function focusField() { input.forceActiveFocus(); input.selectAll() }
  function clear() { input.text = ""; root.cleared() }

  implicitWidth: Style.space(260)
  implicitHeight: input.implicitHeight

  TextField {
    id: input
    anchors.fill: parent
    placeholderText: root.placeholder
    leftPadding: Style.spacing.controlPaddingX + Style.font.icon + Style.spacing.md
    rightPadding: Style.spacing.controlPaddingX + (input.text !== "" ? Style.font.icon + Style.spacing.md : 0)
    onAccepted: root.accepted(input.text)
    Keys.onEscapePressed: function(event) {
      if (input.text !== "") { root.clear(); event.accepted = true }
      else { input.focus = false; event.accepted = false }
    }
  }
  Icon {
    anchors.left: parent.left
    anchors.leftMargin: Style.spacing.controlPaddingX
    anchors.verticalCenter: parent.verticalCenter
    glyph: Model.Glyph.search
    size: Style.font.icon
    color: input.activeFocus ? Commons.Color.accent : Theme.faint
  }
  Icon {
    visible: input.text !== ""
    anchors.right: parent.right
    anchors.rightMargin: Style.spacing.controlPaddingX
    anchors.verticalCenter: parent.verticalCenter
    glyph: Model.Glyph.close
    size: Style.font.icon
    color: clearHover.hovered ? Commons.Color.foreground : Theme.faint
    HoverHandler { id: clearHover; cursorShape: Qt.PointingHandCursor }
    TapHandler { onTapped: root.clear() }
  }
}
