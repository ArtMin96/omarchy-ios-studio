import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../common"
import "../../lib/Model.js" as Model

// Style warnings of one project in the build drawer's Problems tab (projects slice): the result of
// `format-lint` (swift-format, plus SwiftLint when it's installed) from ProjectService.lintFor(path).
//   StyleWarningsSection { app: root.app; svc: root.svc; path: root.stylePath }
// A "Style" section header with the count, Hide/Show (the `showStyleWarnings` setting, remembered)
// and Check again; under it the tools and config used and when, then one BuildDiagnosticRow per
// warning (a click opens the file at the line). Loading, error, "No style warnings" and hidden
// states. Warnings that formatting fixes (the helper gives them a hint) lose that per-row hint; one
// line says how many there are, with "Format project…" (formatRequested()). Style warnings never count
// as build problems: nothing here changes the build's result.
ColumnLayout {
  id: root

  property var app: null
  property var svc: null
  property string path: ""

  readonly property var entry: svc && path ? svc.lintFor(path) : null
  readonly property string lintState: entry ? entry.state : "none"
  readonly property var result: entry && entry.result ? entry.result : null
  // Rows without the per-row "Format project … fixes this" hint (the fixable line below says it once).
  readonly property var items: (result && result.diagnostics ? result.diagnostics : []).map(function(d) {
    return d && d.hint && /^Format project/.test(d.hint) ? Object.assign({}, d, { hint: "" }) : d
  })
  readonly property int fixable: (result && result.diagnostics ? result.diagnostics : []).filter(function(d) { return d && d.hint && /^Format project/.test(d.hint) }).length
  readonly property int total: result ? (result.total !== undefined ? result.total : items.length) : 0
  readonly property bool loading: lintState === "loading"
  readonly property bool failed: lintState === "error"
  readonly property bool shown: app ? app.setting("showStyleWarnings", true) !== false : true
  // What the Problems tab counts: the warnings on screen.
  readonly property int visibleCount: shown && !failed ? total : 0

  signal formatRequested()
  signal rowHovered()                  // a warning row took the hover (the panel drops its own row cursor)

  // The hovered row gets the same highlight as the build's problem rows (CursorSurface's cursor).
  property int _hoverIndex: -1

  // tools, config, when: separate items, so a narrow drawer wraps between them, never inside one
  function toolsParts() {
    var r = root.result
    if (!r) return []
    var tools = (r.tools || []).map(function(t) { return t === "swiftlint" ? "SwiftLint" : t })
    if (!tools.length) tools = ["swift-format"]
    var parts = [tools.join(" + ")]
    parts.push(r.config_path ? String(r.config_path) : "default style")
    if (root.entry && root.entry.at) parts.push("checked " + Model.relativeTime(root.entry.at))
    return parts
  }

  function countText() {
    if (root.failed) return ""
    if (root.loading && !root.result) return "checking…"
    if (!root.result || root.total === 0) return ""     // "No style warnings" says it under the header
    return root.shown ? String(root.total) : root.total + " hidden"
  }

  visible: entry !== null
  spacing: Style.spacing.xs

  SectionHeader {
    Layout.fillWidth: true
    Layout.topMargin: Style.spacing.lg
    Layout.bottomMargin: Style.spacing.xs
    text: "Style"
    detail: root.countText()
    trailing: [
      ActionButton {
        visible: root.total > 0 && !root.failed
        text: root.shown ? "Hide" : "Show"
        glyph: root.shown ? Model.Glyph.eyeOff : Model.Glyph.eye
        kind: "ghost"
        compact: true
        tooltip: root.shown ? "Hide style warnings (remembered)" : "Show style warnings again"
        onClicked: if (root.app) root.app.setSetting("showStyleWarnings", !root.shown)
      },
      ActionButton {
        text: root.failed ? "Try again" : "Check again"
        glyph: Model.Glyph.refresh
        kind: "ghost"
        compact: true
        busy: root.loading
        tooltip: root.loading ? "Checking the style…" : "Run swift-format lint" + (root.result && root.result.swiftlint ? " and SwiftLint" : "") + " again"
        onClicked: if (root.svc) root.svc.lint({ path: root.path })
      }
    ]
  }

  // Small faint items joined by " · " that wrap as whole items (the meta line under the header).
  component MetaFlow: Flow {
    id: flow
    property var parts: []
    spacing: 0                            // Flow spaces rows too; the gap between items is the item's padding
    Repeater {
      model: flow.parts
      delegate: AppText {
        id: part
        required property string modelData
        required property int index
        FontMetrics { id: partMetrics; font: part.font }
        text: modelData + (index < flow.parts.length - 1 ? " ·" : "")
        rightPadding: index < flow.parts.length - 1 ? partMetrics.averageCharacterWidth : 0   // one space
        tone: "faint"; size: "small"
        // one item wider than the line (a long config path) elides instead of overflowing
        width: Math.min(implicitWidth, flow.width)
        elide: Text.ElideMiddle
      }
    }
  }

  // One status line (loading, error, clean, hidden, notes) on the rows' glyph column and text edge.
  component StatusLine: RowLayout {
    id: line
    property string glyph: ""
    property color glyphColor: Theme.faint
    property bool spinning: false
    property string title: ""
    property string detail: ""
    property var detailParts: []          // instead of detail: items that wrap whole (MetaFlow)
    property string titleTone: "dim"
    Layout.fillWidth: true
    Layout.leftMargin: Style.spacing.rowPaddingX
    Layout.rightMargin: Style.spacing.md
    Layout.topMargin: Style.spacing.sm
    Layout.bottomMargin: Style.spacing.sm
    spacing: Style.spacing.xl
    FontMetrics { id: lineMetrics; font: lineTitle.font }
    Item {
      Layout.alignment: Qt.AlignTop
      Layout.preferredWidth: Style.font.iconLarge * 1.4
      Layout.preferredHeight: lineMetrics.height
      Spinner { anchors.centerIn: parent; visible: line.spinning; running: visible; size: Style.font.icon }
      Icon { anchors.centerIn: parent; visible: !line.spinning; glyph: line.glyph; color: line.glyphColor; size: Style.font.icon }
    }
    ColumnLayout {
      Layout.fillWidth: true
      Layout.alignment: Qt.AlignTop
      spacing: Style.spacing.xxs
      AppText { id: lineTitle; Layout.fillWidth: true; text: line.title; tone: line.titleTone; wrapMode: Text.Wrap }
      AppText { Layout.fillWidth: true; visible: line.detail !== ""; text: line.detail; size: "small"; tone: "faint"; wrapMode: Text.Wrap }
      MetaFlow { Layout.fillWidth: true; visible: line.detailParts.length > 0; parts: line.detailParts }
    }
  }

  // tools, config, when (the clean state says it in its own line)
  MetaFlow {
    Layout.fillWidth: true
    Layout.leftMargin: Style.spacing.rowPaddingX + Style.font.iconLarge * 1.4 + Style.spacing.xl
    Layout.rightMargin: Style.spacing.md
    visible: !!root.result && !root.failed && root.total > 0
    parts: root.toolsParts()
  }

  // how many of them formatting fixes, with the way to do it
  RowLayout {
    Layout.fillWidth: true
    Layout.leftMargin: Style.spacing.rowPaddingX
    visible: root.shown && !root.failed && root.fixable > 0
    spacing: Style.spacing.xl
    Item {
      Layout.preferredWidth: Style.font.iconLarge * 1.4
      Layout.preferredHeight: fixText.implicitHeight
      Icon { anchors.centerIn: parent; glyph: Model.Glyph.format; color: Commons.Color.accent; size: Style.font.icon }
    }
    AppText {
      id: fixText
      Layout.fillWidth: true
      text: (root.fixable === root.total ? (root.total === 1 ? "Formatting fixes it" : "Formatting fixes all of them")
                                         : "Formatting fixes " + root.fixable + " of these") + "."
      tone: "dim"; size: "small"; wrapMode: Text.Wrap
    }
    ActionButton {
      text: "Format project…"
      glyph: Model.Glyph.format
      kind: "ghost"
      compact: true
      tooltip: "See what swift-format would change, then apply it"
      onClicked: root.formatRequested()
    }
  }

  StatusLine {
    visible: root.loading && !root.result
    spinning: true
    title: "Checking the code style…"
    detail: "swift-format reads every Swift file. This takes a few seconds."
  }
  StatusLine {
    visible: root.failed
    glyph: Model.Glyph.error
    glyphColor: Theme.error
    titleTone: "primary"
    title: "Couldn't check the code style"
    detail: root.failed ? Model.errorMessage(root.entry.error) + (root.entry.error && root.entry.error.hint ? " " + root.entry.error.hint : "") : ""
  }
  StatusLine {
    visible: !!root.result && !root.failed && root.total === 0
    glyph: Model.Glyph.checkCircle
    glyphColor: Theme.success
    title: "No style warnings"
    detailParts: root.toolsParts()
  }
  StatusLine {
    visible: !!root.result && !root.failed && root.total > 0 && !root.shown
    glyph: Model.Glyph.eyeOff
    title: Model.plural(root.total, "style warning") + " hidden"
    detail: "They never affect a build. Show brings them back."
  }

  Repeater {
    model: root.shown && !root.failed ? root.items : []
    delegate: BuildDiagnosticRow {
      required property var modelData
      required property int index
      Layout.fillWidth: true
      diag: modelData
      projectPath: root.path
      showCategory: false          // the section header says "Style"
      hasCursor: root._hoverIndex === index
      onHovered: function(h) {
        if (h) { root._hoverIndex = index; root.rowHovered() }
        else if (root._hoverIndex === index) root._hoverIndex = -1
      }
      onClicked: if (root.svc) root.svc.openInEditor({ path: root.path }, modelData.file, modelData.line, modelData.col)
    }
  }

  AppText {
    Layout.fillWidth: true
    Layout.leftMargin: Style.spacing.rowPaddingX + Style.font.iconLarge * 1.4 + Style.spacing.xl
    Layout.rightMargin: Style.spacing.md
    visible: root.shown && !root.failed && !!root.result && (root.result.truncated === true || root.result.timed_out === true)
    text: root.result && root.result.timed_out ? "The check stopped after its time limit; these are the files it got to."
          : "Showing " + root.items.length + " of " + root.total + ". Fix these and check again to see the rest."
    tone: "faint"; size: "small"; wrapMode: Text.Wrap
  }
}
