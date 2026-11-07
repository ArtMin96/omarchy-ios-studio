import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import "../../lib/Model.js" as Model

// Plain-language error with a one-click fix. Feed it the helper's error object
// as-is ({code, message, fix?: {label, page, step?, action?}, detail?}).
//   ErrorState { app: root.app; error: svc.error; onRetry: svc.refresh() }
//   ErrorState { app: root.app; message: "Couldn't reach your iPhone."; compact: true }
// The fix button opens fix.url when present, else calls app.nav(fix.page, fix) — so a fix with step: "swift"
// lands on Setup with that step opened. `banner: true` renders a slim inline
// strip for use above content instead of replacing it.
Item {
  id: root
  property var app: null
  property var error: null
  property string title: "Something needs attention"
  property string message: Model.errorMessage(error)
  property bool compact: false
  property bool banner: false
  property bool showRetry: true
  property string retryText: "Try again"
  readonly property var fix: error && error.fix ? error.fix : null
  readonly property string detail: error && error.detail ? String(error.detail) : ""
  property bool _showDetail: false

  signal retry()
  signal fixed(var fix)

  function runFix() {
    if (!root.fix) return
    root.fixed(root.fix)
    if (root.fix.url) Qt.openUrlExternally(root.fix.url)
    else if (root.app && root.fix.page) root.app.nav(root.fix.page, root.fix)
  }

  implicitHeight: banner ? bannerBox.implicitHeight : (col.implicitHeight + (compact ? Style.spacing.xxl : Style.spacing.huge * 2) * 2)
  implicitWidth: banner ? bannerBox.implicitWidth : col.implicitWidth

  // ----- banner
  Rectangle {
    id: bannerBox
    visible: root.banner
    anchors.left: parent.left
    anchors.right: parent.right
    implicitHeight: bannerRow.implicitHeight + Style.spacing.lg * 2
    radius: Style.cornerRadius
    color: Util.alpha(Theme.errorTint, 0.16)
    border.width: Style.normalBorderWidth
    border.color: Util.alpha(Theme.errorTint, 0.5)
    RowLayout {
      id: bannerRow
      anchors.fill: parent
      anchors.margins: Style.spacing.lg
      anchors.leftMargin: Style.spacing.xxl
      spacing: Style.spacing.lg
      // the glyph centred on the message's first line, not on the whole block (one line: all centred)
      FontMetrics { id: bannerMetrics; font: bannerText.font }
      Item {
        Layout.alignment: bannerText.lineCount > 1 ? Qt.AlignTop : Qt.AlignVCenter
        Layout.minimumWidth: implicitWidth
        implicitWidth: bannerIcon.implicitWidth
        implicitHeight: bannerMetrics.height
        Icon { id: bannerIcon; anchors.centerIn: parent; glyph: Model.Glyph.error; color: Commons.Color.urgent }
      }
      // The message takes what the buttons leave (preferredWidth 0: a long unbroken path or id never
      // pushes the row wider) and wraps anywhere when a word doesn't fit; the buttons never shrink (ActionButton's minimumWidth).
      AppText {
        id: bannerText
        Layout.fillWidth: true
        Layout.preferredWidth: 0
        Layout.alignment: lineCount > 1 ? Qt.AlignTop : Qt.AlignVCenter
        text: root.message
        wrapMode: Text.Wrap
      }
      ActionButton { visible: !!root.fix; text: root.fix ? root.fix.label : ""; kind: "primary"; compact: true; onClicked: root.runFix() }
      ActionButton { visible: root.showRetry && !root.fix; text: root.retryText; glyph: Model.Glyph.refresh; kind: "ghost"; compact: true; onClicked: root.retry() }
    }
  }

  // ----- full
  ColumnLayout {
    id: col
    visible: !root.banner
    anchors.centerIn: parent
    width: Math.min(parent.width - Style.spacing.huge * 2, Style.space(460))
    spacing: Style.spacing.lg

    Rectangle {
      Layout.alignment: Qt.AlignHCenter
      Layout.bottomMargin: Style.spacing.sm
      implicitWidth: (root.compact ? Style.font.display : Style.font.displayLarge) * 2
      implicitHeight: implicitWidth
      radius: Style.cornerRadius > 0 ? width / 2 : 0
      color: Util.alpha(Theme.errorTint, 0.16)
      border.width: Style.normalBorderWidth
      border.color: Util.alpha(Theme.errorTint, 0.45)
      Icon { anchors.centerIn: parent; glyph: Model.Glyph.warn; size: root.compact ? Style.font.display : Style.font.displayLarge; color: Commons.Color.urgent }
    }
    AppText { Layout.fillWidth: true; horizontalAlignment: Text.AlignHCenter; text: root.title; size: root.compact ? "title" : "heading"; strong: true; wrapMode: Text.Wrap }
    AppText { Layout.fillWidth: true; horizontalAlignment: Text.AlignHCenter; text: root.message; tone: "dim"; wrapMode: Text.Wrap; lineHeight: 1.25 }
    Row {
      Layout.alignment: Qt.AlignHCenter
      Layout.topMargin: Style.spacing.md
      spacing: Style.spacing.lg
      ActionButton { visible: !!root.fix; text: root.fix ? root.fix.label : ""; glyph: Model.Glyph.wrench; kind: "primary"; onClicked: root.runFix() }
      ActionButton { visible: root.showRetry; text: root.retryText; glyph: Model.Glyph.refresh; kind: root.fix ? "ghost" : "secondary"; onClicked: root.retry() }
    }
    AppText {
      Layout.alignment: Qt.AlignHCenter
      visible: root.detail !== ""
      text: root._showDetail ? "Hide details" : "Show details"
      size: "small"; tone: "faint"
      font.underline: detailHover.hovered
      HoverHandler { id: detailHover; cursorShape: Qt.PointingHandCursor }
      TapHandler { onTapped: root._showDetail = !root._showDetail }
    }
    Rectangle {
      Layout.fillWidth: true
      visible: root._showDetail && root.detail !== ""
      implicitHeight: detailText.implicitHeight + Style.spacing.lg * 2
      radius: Style.cornerRadius
      color: Theme.surface
      border.width: Style.normalBorderWidth
      border.color: Theme.hairline
      TextEdit {
        id: detailText
        anchors.fill: parent
        anchors.margins: Style.spacing.lg
        text: root.detail
        readOnly: true
        selectByMouse: true
        wrapMode: TextEdit.WrapAnywhere
        color: Theme.dim
        font.family: Style.font.family
        font.pixelSize: Style.font.caption
        textFormat: TextEdit.PlainText
      }
    }
  }
}
