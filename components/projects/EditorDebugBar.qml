import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../common"
import "../../core"
import "../../lib/Model.js" as Model

// Debug in editor status on a project card (issue #14), shown while a bridge runs and briefly after it ends,
// whether or not the card is expanded. Fed by EditorDebugService:
//   EditorDebugBar { app: …; entry: app.editorDebug.forProject(project); onStop: …; onDismiss: …; onRetry: … }
// One slim row: the state (spinner while it starts or stops, the bug glyph while it runs) with what to do in
// the editor, then Stop (or, after the end, a close button). Under it, the helper's notices (no debug symbols,
// no local build), one quiet line each. An error shows its message, its fix button and Try again.
Rectangle {
  id: root

  property var app: null
  property var entry: null
  readonly property var info: entry ? Model.editorDebugLabel(entry) : null
  readonly property string _lv: info ? Model.level(info.level) : "info"
  readonly property bool isError: _lv === "error"
  readonly property var fix: isError && entry.error && entry.error.fix ? entry.error.fix : null

  signal stop()
  signal dismiss()
  signal retry()

  visible: info !== null
  implicitHeight: col.implicitHeight + Style.spacing.md * 2
  radius: Style.cornerRadius
  color: isError ? Util.alpha(Theme.errorTint, 0.12)
       : _lv === "ok" ? Util.alpha(Theme.levelColor("ok"), 0.08)
       : Theme.surfaceRaised
  border.width: Style.normalBorderWidth
  border.color: isError ? Util.alpha(Theme.errorTint, 0.45)
              : _lv === "ok" ? Util.alpha(Theme.levelColor("ok"), 0.30)
              : Theme.hairline
  Behavior on color { ColorAnimation { duration: 160 } }

  function runFix() {
    if (!root.fix || !root.app) return
    if (root.fix.url) Qt.openUrlExternally(root.fix.url)
    else if (root.fix.page) root.app.nav(root.fix.page, root.fix)
  }

  readonly property bool stackButtons: isError && width < Style.space(600)
  readonly property real glyphColumn: Math.ceil(Style.font.body * 1.4)

  ColumnLayout {
    id: col
    anchors.fill: parent
    anchors.margins: Style.spacing.md
    anchors.leftMargin: Style.spacing.xl
    spacing: Style.spacing.sm

    RowLayout {
      Layout.fillWidth: true
      spacing: Style.spacing.lg

      // The glyph sits on the first line of the text (centred with it when it's one line).
      FontMetrics { id: lineMetrics; font: mainText.font }
      Item {
        Layout.preferredWidth: root.glyphColumn
        Layout.preferredHeight: lineMetrics.height
        Layout.alignment: textCol.implicitHeight > lineMetrics.height * 1.5 ? Qt.AlignTop : Qt.AlignVCenter
        Icon {
          anchors.centerIn: parent
          size: Style.font.body
          glyph: !root.entry ? "" : root.isError ? Model.Glyph.error
                 : root._lv === "busy" ? Model.Glyph.sync
                 : root.entry.state === "ended" ? Model.Glyph.info
                 : Model.Glyph.bug
          spinning: root._lv === "busy"
          color: root.isError ? Commons.Color.urgent
                 : root._lv === "ok" ? Theme.levelColor("ok")
                 : root._lv === "busy" ? Theme.accent
                 : root.entry && root.entry.state === "ready" ? Theme.accent
                 : Theme.faint
        }
      }

      ColumnLayout {
        id: textCol
        Layout.fillWidth: true
        Layout.preferredWidth: 0
        Layout.alignment: Qt.AlignVCenter
        spacing: Style.spacing.xxs
        HoverHandler { id: textHover }
        PanelToolTip {
          visible: textHover.hovered && !!root.info && root.info.tip !== "" && !Host.covers(textCol, Host.top)
          text: root.info ? root.info.tip : ""
        }
        AppText {
          id: mainText
          Layout.fillWidth: true
          text: root.info ? root.info.text : ""
          tone: root.isError ? "primary" : root.entry && root.entry.state === "ended" ? "dim" : "primary"
          wrapMode: Text.Wrap
        }
        AppText {
          Layout.fillWidth: true
          visible: text !== ""
          text: root.info ? root.info.detail : ""
          size: "small"
          tone: "dim"
          wrapMode: Text.Wrap
        }
      }

      ActionButton {
        Layout.alignment: Qt.AlignVCenter
        visible: !!root.fix && !root.stackButtons
        text: root.fix ? root.fix.label : ""
        kind: "primary"
        compact: true
        onClicked: root.runFix()
      }
      ActionButton {
        Layout.alignment: Qt.AlignVCenter
        visible: root.isError && !root.stackButtons
        text: "Try again"
        glyph: Model.Glyph.refresh
        kind: "ghost"
        compact: true
        onClicked: root.retry()
      }
      ActionButton {
        Layout.alignment: Qt.AlignVCenter
        visible: !!root.info && root.info.live
        text: "Stop"
        glyph: Model.Glyph.stop
        kind: "ghost"
        compact: true
        busy: !!root.entry && root.entry.state === "stopping"
        tooltip: root.entry && root.entry.state === "attached"
                 ? "Stop debugging in the editor. Your editor's debugging session ends too."
                 : "Stop debugging in the editor"
        onClicked: root.stop()
      }
      IconButton {
        Layout.alignment: Qt.AlignVCenter
        visible: !!root.info && !root.info.live
        glyph: Model.Glyph.close
        tooltip: "Dismiss"
        onClicked: root.dismiss()
      }
    }

    // Narrow: an error's buttons go under its message, so the message keeps the width.
    RowLayout {
      Layout.fillWidth: true
      Layout.leftMargin: root.glyphColumn + Style.spacing.lg
      visible: root.stackButtons
      spacing: Style.spacing.md
      ActionButton {
        visible: !!root.fix
        text: root.fix ? root.fix.label : ""
        kind: "primary"
        compact: true
        onClicked: root.runFix()
      }
      ActionButton { text: "Try again"; glyph: Model.Glyph.refresh; kind: "ghost"; compact: true; onClicked: root.retry() }
      Item { Layout.fillWidth: true }
    }

    // The helper's notices: one quiet line each, under the text (same indent).
    Repeater {
      model: root.entry && !root.isError ? root.entry.notices : []
      delegate: RowLayout {
        id: note
        required property var modelData
        Layout.fillWidth: true
        spacing: Style.spacing.lg
        FontMetrics { id: noteMetrics; font: noteText.font }
        Item {
          Layout.preferredWidth: root.glyphColumn
          Layout.preferredHeight: noteMetrics.height
          Layout.alignment: Qt.AlignTop
          Icon {
            anchors.centerIn: parent
            size: Style.font.bodySmall
            glyph: Model.levelGlyph(note.modelData.level)
            color: Model.level(note.modelData.level) === "warn" ? Theme.levelColor("warn") : Theme.faint
          }
        }
        AppText {
          id: noteText
          Layout.fillWidth: true
          Layout.preferredWidth: 0
          text: note.modelData.message
          size: "small"
          tone: "dim"
          wrapMode: Text.Wrap
        }
      }
    }
  }

}
