pragma ComponentBehavior: Bound

import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "Model.js" as Model

// iOS Studio's Omarchy bar button (the plugin's only entry point). A click opens iOS Studio, or brings its window to
// the front, through its own launcher. When iOS Studio isn't installed, a panel asks first, runs install.sh from
// this plugin folder, then opens iOS Studio on its Setup page; a failure says why and offers Try again.
// Nothing runs until a click: no polling, no status badge.
Panel {
  id: root
  moduleName: "io.github.artmin96.ios-studio"
  manageIpc: false

  readonly property string helper: Model.localPath(Qt.resolvedUrl("app.sh"))
  // "confirm" asks to install, "installing" runs install.sh, "failed" says why.
  property string stage: "confirm"
  property string reason: ""
  property var output: []
  // The highlighted button: 0 Cancel/Close, 1 Install/Try again.
  property int choice: 1

  readonly property color foreground: bar ? bar.foreground : Commons.Color.foreground
  readonly property color dim: Qt.darker(foreground, 1.55)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property string glyph: String.fromCodePoint(0xF011C)   // the phone glyph iOS Studio's sidebar shows

  Commons.Translations { id: strings; directory: Qt.resolvedUrl("i18n") }

  function activate() {
    if (installer.running || opened) { toggle(); return }
    if (!status.running) status.running = true
  }

  function install() {
    if (installer.running) return
    stage = "installing"
    reason = ""
    output = []
    installer.running = true
  }

  function choose(index) {
    if (stage === "installing") return
    if (index === 1) install()
    else close()
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  onOpenedChanged: if (opened) {
    choice = 1
    Qt.callLater(function() { if (panel.loadedContent) panel.loadedContent.forceActiveFocus() })
  }

  Process {
    id: status
    command: [root.helper, "status"]
    stdout: StdioCollector { id: statusOut }
    onExited: function(exitCode) {
      if (exitCode === 0 && statusOut.text.trim() === "installed") {
        Quickshell.execDetached([root.helper, "open"])
      } else {
        root.stage = "confirm"
        root.open()
      }
    }
  }

  Process {
    id: installer
    command: [root.helper, "install"]
    stdout: SplitParser { onRead: data => Model.keepLine(root.output, data) }
    onExited: function(exitCode) {
      if (exitCode === 0) {
        root.stage = "confirm"
        root.close()
        Quickshell.execDetached([root.helper, "open", "setup"])
      } else {
        root.reason = Model.failureReason(root.output, exitCode)
        root.stage = "failed"
        root.open()
      }
    }
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.glyph
    tooltipText: "iOS Studio"
    onPressed: function(buttonCode) {
      if (buttonCode === Qt.LeftButton) root.activate()
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: panel.loadedContent
    contentWidth: panel.fittedContentWidth(Style.space(340))
    contentHeight: panel.fittedContentHeight(panel.loadedContentHeight, Style.space(320))

    content: PanelKeyCatcher {
      implicitHeight: column.implicitHeight
      anchors.fill: parent
      onMoveRequested: function(dx, dy) { if (dx !== 0) root.choice = dx > 0 ? 1 : 0 }
      onActivateRequested: root.choose(root.choice)
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }

      Column {
        id: column
        width: parent.width
        spacing: Style.space(14)

        PanelHero {
          width: parent.width
          title: "iOS Studio"
          meta: root.stage === "installing" ? strings.t("installing")
            : root.stage === "failed" ? strings.t("failed") : strings.t("notInstalled")
          foreground: root.foreground
          fontFamily: root.fontFamily
          iconComponent: Component {
            Text {
              textFormat: Text.PlainText
              text: root.glyph
              color: root.stage === "failed" ? Commons.Color.urgent : Commons.Color.accent
              font.family: root.fontFamily
              font.pixelSize: Style.font.display
            }
          }
        }

        Text {
          width: parent.width
          textFormat: Text.PlainText
          text: root.stage === "installing" ? strings.t("installingDetail")
            : root.stage === "failed" ? strings.t("failedDetail") : strings.t("askInstall")
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          wrapMode: Text.WordWrap
        }

        Text {
          width: parent.width
          visible: text !== ""
          textFormat: Text.PlainText   // install.sh output: never rich text
          text: root.stage === "failed" ? root.reason : root.stage === "confirm" ? strings.t("askDetail") : ""
          color: root.stage === "failed" ? Commons.Color.urgent : root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.bodySmall
          wrapMode: Text.WordWrap
          maximumLineCount: 4
          elide: Text.ElideRight
        }

        Row {
          anchors.right: parent.right
          visible: root.stage !== "installing"
          spacing: Style.space(8)

          Repeater {
            model: root.stage === "failed" ? [strings.t("close"), strings.t("retry")] : [strings.t("cancel"), strings.t("install")]

            Button {
              required property int index
              required property string modelData
              text: modelData
              bordered: true
              hasCursor: root.choice === index
              foreground: root.foreground
              fontFamily: root.fontFamily
              onHovered: function(isHovered) { if (isHovered) root.choice = index }
              onClicked: root.choose(index)
            }
          }
        }
      }
    }
  }
}
