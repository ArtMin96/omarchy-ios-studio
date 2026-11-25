import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../common"
import "../../lib/Model.js" as Model

// One row of the unit tests sheet's results (ProjectTestsSheet; projects slice). kind:
//   failed       error glyph · test name · suite (· library when the project uses both) · the assertion's message ·
//                file:line (relative to the project); a click opens the file at the line
//   skipped      a quiet minus · name · suite · "skipped", the reason under it (muted) when the test gave one
//   passedGroup  "12 passed" with a chevron: a click shows or hides the passed tests below it
//   passed       indented under the group: check · name · suite, its time on the right
// Insets, glyph column and gap are BuildDiagnosticRow's, so these rows line up with the compile problems and the
// host blockers the sheet shows with it.
//   TestResultRow { kind: "failed"; test: t; projectPath: p.path; showLibrary: true; onClicked: … }
CursorSurface {
  id: root

  property string kind: "passed"
  property var test: ({})
  property int count: 0                    // passedGroup: how many passed
  property bool open: false                // passedGroup: the passed tests show
  property string projectPath: ""
  property bool showLibrary: false

  readonly property bool failed: kind === "failed"
  readonly property bool group: kind === "passedGroup"
  readonly property bool clickable: group || (failed && !!test.file)
  readonly property real iconColumn: Style.font.iconLarge * 1.4
  readonly property string relFile: {
    var f = String(test.file || "")
    if (projectPath && f.indexOf(projectPath + "/") === 0) return f.substring(projectPath.length + 1)
    return f.split("/").slice(-3).join("/")
  }
  readonly property string location: failed && test.file ? relFile + (test.line ? ":" + test.line : "") : ""

  signal clicked()
  signal hovered(bool h)

  FontMetrics { id: nameMetrics; font: nameText.font }

  implicitHeight: row.implicitHeight + Style.spacing.lg * 2
  implicitWidth: Style.space(320)

  RowLayout {
    id: row
    anchors.fill: parent
    anchors.topMargin: Style.spacing.lg
    anchors.bottomMargin: Style.spacing.lg
    // passed tests sit under the group's text, so they read as its contents
    anchors.leftMargin: Style.spacing.rowPaddingX + (root.kind === "passed" ? root.iconColumn + Style.spacing.xl : 0)
    anchors.rightMargin: Style.spacing.md
    spacing: Style.spacing.xl

    // glyph centred on the first line's box
    Item {
      Layout.alignment: Qt.AlignTop
      Layout.preferredWidth: root.iconColumn
      Layout.preferredHeight: nameMetrics.height
      Icon {
        anchors.centerIn: parent
        glyph: root.failed ? Model.Glyph.error : root.kind === "skipped" ? Model.Glyph.minus : Model.Glyph.checkCircle
        color: root.failed ? Theme.error : root.kind === "skipped" ? Theme.faint : Theme.success
        size: Style.font.icon
      }
    }

    ColumnLayout {
      Layout.fillWidth: true
      Layout.alignment: Qt.AlignTop
      spacing: Style.spacing.xs

      // name · suite · library · time: the suite gives way (elides) first, the name keeps up to 160 px
      RowLayout {
        Layout.fillWidth: true
        spacing: Style.spacing.md
        AppText {
          id: nameText
          Layout.fillWidth: true
          Layout.maximumWidth: implicitWidth + 1
          Layout.minimumWidth: Math.min(implicitWidth + 1, Style.space(160))
          text: root.group ? root.count + " passed" : String(root.test.name || root.test.id || "")
          tone: root.kind === "skipped" ? "dim" : "primary"
          strong: root.failed
          elide: Text.ElideRight
        }
        AppText {
          Layout.fillWidth: true
          Layout.maximumWidth: implicitWidth + 1
          Layout.minimumWidth: 0
          Layout.horizontalStretchFactor: 100
          visible: !root.group && text !== ""
          text: String(root.test.suite || "") + (root.kind === "skipped" ? " · skipped" : "")
          tone: "faint"; size: "small"; elide: Text.ElideRight
        }
        Badge {
          Layout.alignment: Qt.AlignVCenter
          visible: root.showLibrary && !root.group && !!root.test.library
          level: "info"
          text: Model.testLibraryLabel(root.test.library)
        }
        Item { Layout.fillWidth: true }
      }
      // a failure's assertion, a skipped test's reason (XCTSkip's message, Swift Testing's .disabled comment)
      AppText {
        Layout.fillWidth: true
        visible: (root.failed || root.kind === "skipped") && text !== ""
        text: String(root.test.message || "")
        tone: root.failed ? "dim" : "faint"; size: "small"; wrapMode: Text.Wrap; maximumLineCount: root.failed ? 4 : 2
        elide: Text.ElideRight
      }
      AppText {
        Layout.fillWidth: true
        visible: root.location !== ""
        text: root.location
        tone: "faint"; size: "small"; elide: Text.ElideMiddle
      }
    }

    // a passed test's time, in one column on the right
    AppText {
      Layout.alignment: Qt.AlignVCenter
      visible: root.kind === "passed" && text !== ""
      text: Model.testDuration(root.test.duration)
      tone: "faint"; size: "small"
    }
    // Same size and right inset as BuildDiagnosticRow's open-in-editor glyph.
    Icon {
      Layout.alignment: Qt.AlignVCenter
      visible: root.clickable
      glyph: root.group ? (root.open ? Model.Glyph.chevronUp : Model.Glyph.chevronDown) : Model.Glyph.openExternal
      size: Style.font.bodySmall
      color: root.group ? Theme.dim : (root.hot ? Theme.dim : "transparent")
    }
  }

  readonly property bool hot: hoverH.hovered || hasCursor
  HoverHandler { id: hoverH; cursorShape: root.clickable ? Qt.PointingHandCursor : Qt.ArrowCursor; onHoveredChanged: root.hovered(hovered) }
  TapHandler { onTapped: if (root.clickable) root.clicked() }
}
