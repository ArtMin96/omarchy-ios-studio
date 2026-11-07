import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../Model.js" as Model

// App data › Stores: one page of a table as a grid. The column header stays on top and the row
// numbers on the left while the cells scroll both ways; long cells are cut with … and show in full
// in a tooltip. Column widths follow the header and the page's longest cells, within limits.
//   DeviceAppDataGrid { Layout.fillWidth: true; columns: page.columns; rows: page.rows; offset: page.offset }
// columns: [{name, type}]; rows: arrays in column order (objects keyed by column name work too).
Rectangle {
  id: root
  property var columns: []
  property var rows: []
  property int offset: 0
  property real maxHeight: Style.space(440)

  readonly property real padX: Style.spacing.lg
  readonly property real rowH: Math.ceil(fm.height) + Style.spacing.md * 2
  readonly property real headH: Math.ceil(fm.height) + Math.ceil(capFm.height) + Style.spacing.md * 2 + Style.spacing.xxs
  readonly property real numW: Math.ceil(fm.advanceWidth(String(offset + rows.length))) + padX * 2
  readonly property var widths: {
    var out = []
    var lo = Style.space(56), hi = Style.space(300)
    for (var c = 0; c < columns.length; c++) {
      var w = Math.max(fm.advanceWidth(columns[c].name), capFm.advanceWidth(columns[c].type || ""))
      for (var r = 0; r < rows.length; r++) w = Math.max(w, fm.advanceWidth(root.cellText(root.cell(rows[r], c)).substring(0, 48)))
      out.push(Math.ceil(Math.min(hi, Math.max(lo, w)) + padX * 2))
    }
    return out
  }
  readonly property real contentW: numW + widths.reduce(function(a, b) { return a + b }, 0)
  readonly property real barLane: Style.spacing.sm + Style.spacing.xxs * 2

  radius: Style.cornerRadius
  color: "transparent"
  border.width: Style.normalBorderWidth
  border.color: Theme.hairline
  clip: true
  implicitWidth: Style.space(420)
  implicitHeight: Math.min(headH + rows.length * rowH, maxHeight) + (contentW > width - border.width * 2 ? barLane : 0) + border.width * 2

  FontMetrics { id: fm; font.family: Style.font.family; font.pixelSize: Style.font.bodySmall }
  FontMetrics { id: capFm; font.family: Style.font.family; font.pixelSize: Style.font.caption }

  function cell(row, c) {
    if (!row) return null
    var v = row[c]
    return v !== undefined ? v : row[root.columns[c].name]
  }
  // cells as helper/app_data.py's cell() sends them: plain values, or {"$type": blob|text|date|float, …}
  function cellText(v) {
    if (v === null || v === undefined) return "NULL"
    if (typeof v === "object") {
      switch (v["$type"]) {
      case "blob": return "‹" + Model.formatBytes(v.size) + "› " + String(v.hex || "").replace(/(..)(?!$)/g, "$1 ") + (v.truncated ? " …" : "")
      case "text": return String(v.preview || "").replace(/\n/g, " ⏎ ") + " …"
      case "date": { var d = new Date(v.iso); return isNaN(d.getTime()) ? String(v.iso) : Qt.formatDateTime(d, "yyyy-MM-dd HH:mm:ss") }
      case "float": return String(v.value)
      default: return JSON.stringify(v)
      }
    }
    return String(v).replace(/\n/g, " ⏎ ")
  }
  function cellTip(v) {
    var t = root.cellText(v)
    if (v && typeof v === "object") {
      if (v["$type"] === "text") t = String(v.preview || "") + " …  (" + Number(v.length).toLocaleString(Qt.locale(), "f", 0) + " characters)"
      else if (v["$type"] === "date") t += "  (local time; stored as " + v.raw + ")"
      else if (v["$type"] === "blob") t = Model.formatBytes(v.size) + " of binary data, starting " + String(v.hex || "").replace(/(..)(?!$)/g, "$1 ")
    }
    return t.length > 800 ? t.substring(0, 800) + " …" : t
  }
  // a column of numbers (SQLite's numeric affinities; Core Data dates are numbers but show as text):
  // its header, type and cells are right-aligned
  function numericColumn(c) {
    var col = root.columns[c]
    return !!col && !col.date && /INT|REAL|FLOA|DOUB|NUM|DEC/i.test(col.type || "")
  }
  function numeric(v) { return typeof v === "number" || (!!v && typeof v === "object" && v["$type"] === "float") }
  function quiet(v) { return v === null || v === undefined || (typeof v === "object" && v["$type"] === "blob") }

  component GridScrollBar: ScrollBar {
    id: bar
    readonly property bool needed: size < 1
    policy: needed ? ScrollBar.AlwaysOn : ScrollBar.AlwaysOff
    padding: Style.spacing.xxs
    minimumSize: 0.08
    contentItem: Rectangle {
      implicitWidth: Style.spacing.sm
      implicitHeight: Style.spacing.sm
      radius: Style.cornerRadius
      color: bar.pressed ? Theme.dim : bar.hovered ? Util.alpha(Theme.fg, 0.45) : Util.alpha(Theme.fg, 0.22)
      Behavior on color { ColorAnimation { duration: 120 } }
    }
    background: Item {}
  }

  // the horizontal bar sits in its own lane under the rows, so it never covers the last one
  GridScrollBar {
    id: hbar
    orientation: Qt.Horizontal
    anchors.left: parent.left; anchors.right: parent.right; anchors.bottom: parent.bottom
    anchors.margins: root.border.width
    height: root.barLane
  }

  ListView {
    id: list
    anchors.fill: parent
    anchors.margins: root.border.width
    clip: true
    model: root.rows
    // a lane past the last column for the vertical bar, so it never covers a cell
    contentWidth: root.contentW + (vbar.needed ? root.barLane : 0)
    flickableDirection: Flickable.AutoFlickIfNeeded
    boundsBehavior: Flickable.StopAtBounds
    anchors.bottomMargin: root.border.width + (hbar.needed ? root.barLane : 0)
    headerPositioning: ListView.OverlayHeader
    ScrollBar.vertical: GridScrollBar { id: vbar; orientation: Qt.Vertical }
    ScrollBar.horizontal: hbar

    header: Rectangle {
      z: 3
      width: Math.max(list.contentWidth, list.width)
      height: root.headH
      color: Commons.Color.background
      Rectangle { anchors.fill: parent; color: Theme.surfaceRaised }
      Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: Style.normalBorderWidth; color: Theme.hairline }
      Row {
        x: root.numW
        height: parent.height
        Repeater {
          model: root.columns
          delegate: Item {
            required property var modelData
            required property int index
            width: root.widths[index] || 0
            height: parent.height
            Column {
              anchors.left: parent.left; anchors.right: parent.right
              anchors.leftMargin: root.padX; anchors.rightMargin: root.padX
              anchors.verticalCenter: parent.verticalCenter
              spacing: Style.spacing.xxs
              AppText { width: parent.width; text: modelData.name; size: "small"; strong: !modelData.meta; tone: modelData.meta ? "dim" : "primary"; elide: Text.ElideRight; horizontalAlignment: root.numericColumn(index) ? Text.AlignRight : Text.AlignLeft }
              AppText { width: parent.width; text: (modelData.type || "—").toLowerCase(); size: "caption"; tone: "faint"; elide: Text.ElideRight; horizontalAlignment: root.numericColumn(index) ? Text.AlignRight : Text.AlignLeft }
            }
            HoverHandler { id: headHover }
            PanelToolTip { visible: headHover.hovered && fm.advanceWidth(modelData.name) > (root.widths[index] || 0) - root.padX * 2; text: modelData.name + (modelData.type ? "  ·  " + modelData.type : "") }
          }
        }
      }
      // the "#" corner stays put while the header scrolls sideways
      Rectangle {
        x: list.contentX
        width: root.numW; height: parent.height - Style.normalBorderWidth
        color: Commons.Color.background
        Rectangle { anchors.fill: parent; color: Theme.surfaceRaised }
        AppText { anchors.right: parent.right; anchors.rightMargin: root.padX; anchors.verticalCenter: parent.verticalCenter; text: "#"; size: "small"; tone: "faint" }
      }
    }

    delegate: Rectangle {
      id: line
      required property var modelData
      required property int index
      width: Math.max(list.contentWidth, list.width)
      height: root.rowH
      color: index % 2 === 1 ? Theme.surface : "transparent"

      Row {
        x: root.numW
        height: parent.height
        Repeater {
          model: root.columns.length
          delegate: Item {
            id: cellItem
            required property int index
            readonly property var value: root.cell(line.modelData, index)
            width: root.widths[index] || 0
            height: parent.height
            AppText {
              id: cellText
              anchors.left: parent.left; anchors.right: parent.right
              anchors.leftMargin: root.padX; anchors.rightMargin: root.padX
              anchors.verticalCenter: parent.verticalCenter
              text: root.cellText(cellItem.value)
              size: "small"
              font.features: { "liga": 0, "calt": 0 }
              tone: root.quiet(cellItem.value) ? "faint" : "primary"
              horizontalAlignment: root.numericColumn(cellItem.index) || (root.numeric(cellItem.value) && !root.columns[cellItem.index].date) ? Text.AlignRight : Text.AlignLeft
              elide: Text.ElideRight
            }
            HoverHandler { id: cellHover }
            PanelToolTip {
              visible: cellHover.hovered && (cellText.truncated || (!!cellItem.value && typeof cellItem.value === "object" && cellItem.value["$type"] !== "float"))
              text: root.cellTip(cellItem.value)
            }
          }
        }
      }
      // row numbers stay put while the cells scroll sideways (opaque, over the cells)
      Rectangle {
        x: list.contentX
        width: root.numW; height: parent.height
        color: Commons.Color.background
        Rectangle { anchors.fill: parent; color: line.color }
        Rectangle { anchors.right: parent.right; width: Style.normalBorderWidth; height: parent.height; color: Theme.hairline }
        AppText { anchors.right: parent.right; anchors.rightMargin: root.padX; anchors.verticalCenter: parent.verticalCenter; text: String(root.offset + line.index + 1); size: "small"; tone: "faint" }
      }
    }
  }
}
