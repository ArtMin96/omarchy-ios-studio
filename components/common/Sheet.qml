import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../../core"
import "../../lib/Model.js" as Model

// In-window modal panel for forms and focused tasks (sign-in, 2FA, pickers).
// Declare it anywhere in a page; it lifts itself into the window's overlay
// layer so the scrim covers the whole app. Esc / the close button / a scrim
// click (if `dismissible`) close it.
//   Sheet {
//     id: signIn
//     title: "Sign in to Apple ID"; subtitle: "Your password goes straight to Apple."; glyph: Model.Glyph.apple
//     TextField { Layout.fillWidth: true; placeholderText: "Apple ID email" }      // body (ColumnLayout)
//     footer: [
//       ActionButton { text: "Cancel"; kind: "ghost"; onClicked: signIn.close() },
//       ActionButton { text: "Sign in"; kind: "primary"; onClicked: … }
//     ]
//     onShown: emailField.forceActiveFocus()
//   }
//   signIn.open()
Item {
  id: root
  property string title: ""
  property string subtitle: ""
  property string glyph: ""
  property bool opened: false
  property bool dismissible: true
  property bool busy: false                 // shows a progress strip, blocks scrim dismissal
  property real preferredWidth: Style.space(480)
  default property alias content: body.data
  property alias footer: footerRow.data
  property Item _home: null

  // `opened` is the state; `shown()` / `closed()` are the transitions to hook.
  signal shown()
  signal closed()

  function open() { root.opened = true }
  function close() { root.opened = false }
  function dismiss() { if (root.dismissible && !root.busy) root.close() }

  z: 100
  visible: opacity > 0.001
  opacity: opened ? 1 : 0
  enabled: opened
  Behavior on opacity { NumberAnimation { duration: 140; easing.type: Easing.OutCubic } }

  Component.onCompleted: {
    if (Host.layer) { root._home = root.parent; root.parent = Host.layer }
  }
  // Explicit geometry instead of anchors: a Sheet declared inside a layout warns about anchors.
  x: 0
  y: 0
  width: parent ? parent.width : 0
  height: parent ? parent.height : 0

  onOpenedChanged: {
    if (opened) {
      Host.push(root)
      Qt.callLater(function() { scope.forceActiveFocus(); root.shown() })
    } else {
      Host.pop(root)
      root.closed()
    }
  }
  Component.onDestruction: Host.pop(root)

  Rectangle {
    anchors.fill: parent
    color: Theme.scrim
    // A wheel or hover over the scrim or the panel never reaches the page behind the sheet.
    MouseArea { anchors.fill: parent; hoverEnabled: true; onClicked: root.dismiss(); onWheel: function(wheel) { wheel.accepted = true } }
  }

  FocusScope {
    id: scope
    anchors.centerIn: parent
    width: Math.min(root.width - Style.spacing.huge * 4, root.preferredWidth)
    height: Math.min(root.height - Style.spacing.huge * 4, panel.implicitHeight)
    focus: root.opened
    Keys.onEscapePressed: function(event) { root.dismiss(); event.accepted = true }

    transform: Translate { y: root.opened ? 0 : Style.space(12); Behavior on y { NumberAnimation { duration: 160; easing.type: Easing.OutCubic } } }

    BorderSurface {
      id: panel
      anchors.fill: parent
      radius: Style.cornerRadius
      color: Commons.Color.popups.background
      borderSpec: Border.surfaceSpec("popups", "border", Commons.Color.accent, Style.normalBorderWidth)
      implicitHeight: layout.implicitHeight + Style.spacing.panelPadding * 2
      MouseArea { anchors.fill: parent; onWheel: function(wheel) { wheel.accepted = true } }   // swallow clicks and wheels

      ColumnLayout {
        id: layout
        anchors.fill: parent
        anchors.margins: Style.spacing.panelPadding
        spacing: Style.spacing.xxl

        RowLayout {
          Layout.fillWidth: true
          spacing: Style.spacing.xxl
          Icon { visible: root.glyph !== ""; glyph: root.glyph; size: Style.font.display; color: Commons.Color.accent; Layout.alignment: Qt.AlignTop }
          ColumnLayout {
            Layout.fillWidth: true
            spacing: Style.spacing.xs
            AppText { Layout.fillWidth: true; text: root.title; size: "heading"; strong: true; wrapMode: Text.WordWrap }
            AppText { Layout.fillWidth: true; visible: root.subtitle !== ""; text: root.subtitle; tone: "dim"; wrapMode: Text.WordWrap }
          }
          IconButton { Layout.alignment: Qt.AlignTop; glyph: Model.Glyph.close; tooltip: "Close (Esc)"; visible: root.dismissible; enabled: !root.busy; onClicked: root.dismiss() }
        }

        ProgressTrack { Layout.fillWidth: true; visible: root.busy; indeterminate: true; thickness: Style.space(2) }

        Flickable {
          Layout.fillWidth: true
          Layout.fillHeight: true
          Layout.preferredHeight: body.implicitHeight
          contentHeight: body.implicitHeight
          contentWidth: width
          clip: true
          boundsBehavior: Flickable.StopAtBounds
          ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
          ColumnLayout { id: body; width: parent.width; spacing: Style.spacing.xl }
        }

        RowLayout {
          Layout.fillWidth: true
          visible: footerRow.children.length > 0
          Item { Layout.fillWidth: true }
          Row { id: footerRow; spacing: Style.spacing.md }
        }
      }
    }
  }
}
