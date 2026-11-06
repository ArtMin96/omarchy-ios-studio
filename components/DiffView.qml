import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../Model.js" as Model

// A unified diff, read-only (projects slice: ProjectFormatSheet). Monospace lines with old and new
// line numbers, removed lines in the theme's error colour and added ones in its success colour (on a
// faint tint of the same), each file as a header row (file glyph + path, from the ---/+++ pair) and
// each hunk as an accent "@@ … @@" band. Trailing spaces on changed lines show as faint dots, so a
// whitespace-only change is visible. Scrolls on its own (both ways; lines don't wrap) once it is
// taller than maxHeight.
//   DiffView { Layout.fillWidth: true; text: result.diff; maxHeight: Style.space(360) }
Rectangle {
  id: root

  property string text: ""
  property real maxHeight: Style.space(360)
  property int maxLines: 5000
  readonly property var rows: parse(text)
  readonly property int fileCount: rows.filter(function(r) { return r.k === "file" }).length

  // Widest line numbers and text, in characters (the font is monospace).
  readonly property int numDigits: {
    var m = 1
    for (var i = 0; i < rows.length; i++) m = Math.max(m, String(rows[i].o || "").length, String(rows[i].n || "").length)
    return m
  }
  readonly property int maxChars: {
    var m = 0
    for (var i = 0; i < rows.length; i++) m = Math.max(m, rows[i].t.length + (rows[i].k === "file" ? 3 : 0))
    return m
  }

  FontMetrics { id: fm; font.family: Style.font.family; font.pixelSize: Style.font.bodySmall }
  readonly property real cw: fm.advanceWidth("0")
  readonly property real lineH: Math.ceil(fm.height * 1.25)
  readonly property real padX: Style.spacing.lg
  readonly property real gutterW: Math.ceil(numDigits * cw) + Style.spacing.md * 2
  readonly property real markW: Math.ceil(cw * 2)
  readonly property real textX: gutterW * 2 + markW
  readonly property real contentW: Math.max(list.width, textX + Math.ceil(maxChars * cw) + padX * 2)

  // "--- a/x" + "+++ b/x" → one file row; "@@ -a,b +c,d @@ ctx" → a hunk row; then +, -, context.
  function parse(src) {
    var lines = String(src || "").split("\n")
    if (lines.length && lines[lines.length - 1] === "") lines.pop()
    var out = [], o = 0, n = 0
    for (var i = 0; i < lines.length && out.length < root.maxLines; i++) {
      var l = lines[i].replace(/\t/g, "    ").replace(/\r$/, "")
      if (/^diff --git |^index [0-9a-f]+\.\.|^(new|deleted) file mode /.test(l)) continue
      if (l.indexOf("--- ") === 0 && i + 1 < lines.length && lines[i + 1].indexOf("+++ ") === 0) {
        var b = lines[i + 1].substring(4).replace(/\t.*$/, "")
        var a = l.substring(4).replace(/\t.*$/, "")
        var path = b === "/dev/null" ? a : b
        out.push({ k: "file", t: path.replace(/^[ab]\//, ""), o: "", n: "" })
        i++
        continue
      }
      var h = l.match(/^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@(.*)$/)
      if (h) {
        o = parseInt(h[1]); n = parseInt(h[2])
        out.push({ k: "hunk", t: l.substring(0, l.length - h[3].length), ctx: h[3].trim(), o: "", n: "" })
        continue
      }
      var c = l.charAt(0)
      if (c === "+") out.push({ k: "add", t: l.substring(1), o: "", n: String(n++) })
      else if (c === "-") out.push({ k: "del", t: l.substring(1), o: String(o++), n: "" })
      else if (c === " " || l === "") out.push({ k: "ctx", t: l.substring(1), o: String(o++), n: String(n++) })
      else out.push({ k: "meta", t: l, o: "", n: "" })
    }
    return out
  }

  radius: Style.cornerRadius
  color: Theme.surface
  border.width: Style.normalBorderWidth
  border.color: Theme.hairline
  implicitHeight: Math.min(list.contentHeight, maxHeight) + border.width * 2
  implicitWidth: Style.space(420)
  clip: true

  // A wheel the list can't use any more (at its top or bottom edge) stops here instead of
  // scrolling the page behind the sheet. Only while the diff scrolls at all.
  MouseArea {
    anchors.fill: parent
    enabled: list.contentHeight > list.height || list.contentWidth > list.width
    acceptedButtons: Qt.NoButton
    onWheel: function(wheel) { wheel.accepted = true }
  }

  ListView {
    id: list
    anchors.fill: parent
    anchors.margins: root.border.width
    clip: true
    model: root.rows
    contentWidth: root.contentW
    flickableDirection: Flickable.AutoFlickIfNeeded
    boundsBehavior: Flickable.StopAtBounds
    ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
    ScrollBar.horizontal: ScrollBar { policy: ScrollBar.AsNeeded }

    delegate: Rectangle {
      id: line
      required property var modelData
      required property int index
      readonly property string k: modelData.k
      readonly property bool changed: k === "add" || k === "del"
      // Changed lines: the text, then its trailing spaces as dots.
      readonly property string body: changed ? modelData.t.replace(/\s+$/, "") : modelData.t
      readonly property int trailing: changed ? modelData.t.length - body.length : 0
      readonly property color tint: k === "add" ? Theme.success : k === "del" ? Theme.error : Theme.fg

      width: root.contentW
      height: k === "file" ? root.lineH + Style.spacing.md * 2 : root.lineH
      color: k === "add" ? Util.alpha(Theme.success, 0.14)
             : k === "del" ? Util.alpha(Theme.error, 0.14)
             : k === "hunk" ? Util.alpha(Commons.Color.accent, 0.07)
             : k === "file" ? Theme.surfaceRaised : "transparent"

      // a hairline between files
      Rectangle { visible: line.k === "file" && line.index > 0; anchors.top: parent.top; width: parent.width; height: Style.normalBorderWidth; color: Theme.hairline }

      // ---- file header
      Row {
        visible: line.k === "file"
        x: list.contentX + root.padX     // the path stays in view while scrolled sideways
        anchors.verticalCenter: parent.verticalCenter
        spacing: Style.spacing.md
        Icon { anchors.verticalCenter: parent.verticalCenter; glyph: Model.Glyph.fileCode; size: Style.font.bodySmall; color: Theme.dim }
        AppText { anchors.verticalCenter: parent.verticalCenter; text: line.modelData.t; size: "small"; strong: true }
      }

      // ---- numbered lines and hunk bands
      Item {
        visible: line.k !== "file"
        anchors.fill: parent
        Row {
          x: root.textX
          anchors.verticalCenter: parent.verticalCenter
          AppText {
            text: line.k === "hunk" ? line.modelData.t : line.body
            size: "small"
            tone: line.k === "hunk" ? "accent" : line.k === "meta" ? "faint" : line.changed ? "inherit" : "dim"
            color: line.tint
          }
          AppText {
            visible: line.trailing > 0
            text: "·".repeat(Math.max(0, line.trailing))
            size: "small"; tone: "inherit"; color: Util.alpha(line.tint, 0.55)
          }
          AppText {
            visible: line.k === "hunk" && !!line.modelData.ctx
            text: " " + (line.modelData.ctx || "")
            size: "small"; tone: "faint"
          }
        }
        // Line numbers and +/- stay put while the text scrolls sideways (opaque, over the text).
        Item {
          x: list.contentX
          width: root.textX - Style.spacing.xs
          height: parent.height
          Rectangle { anchors.fill: parent; color: Commons.Color.popups.background }
          Rectangle { anchors.fill: parent; color: Theme.surface }
          Rectangle { anchors.fill: parent; color: line.color }
          Rectangle { visible: line.changed; width: Style.space(2); height: parent.height; color: line.tint }
          AppText {
            x: 0; width: root.gutterW - Style.spacing.md
            anchors.verticalCenter: parent.verticalCenter
            horizontalAlignment: Text.AlignRight
            text: line.modelData.o; size: "small"; tone: "faint"
          }
          AppText {
            x: root.gutterW; width: root.gutterW - Style.spacing.md
            anchors.verticalCenter: parent.verticalCenter
            horizontalAlignment: Text.AlignRight
            text: line.modelData.n; size: "small"; tone: "faint"
          }
          AppText {
            x: root.gutterW * 2 + Style.spacing.xs
            anchors.verticalCenter: parent.verticalCenter
            visible: line.changed
            text: line.k === "add" ? "+" : "−"
            size: "small"; tone: "inherit"; color: line.tint; strong: true
          }
        }
      }
    }
  }

  AppText {
    anchors.centerIn: parent
    visible: root.rows.length === 0
    text: "No changes"
    tone: "faint"
    size: "small"
  }
}
