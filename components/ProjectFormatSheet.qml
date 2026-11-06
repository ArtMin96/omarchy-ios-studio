import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../Model.js" as Model

// "Format project…" sheet (projects slice): what swift-format would change in a project, before it
// changes anything. Opening it runs the dry run (`format --project P`, ProjectService.formatPreview):
// a summary ("3 files would change", the style used: the project's .swift-format or the default),
// files swift-format couldn't read, then the diff (DiffView). Apply (`format --apply`) writes the files,
// toasts and closes; style warnings on screen are checked again quietly. Loading, "Already formatted"
// and error states (with the helper's hint, e.g. how to get swift-format).
//   ProjectFormatSheet { id: formatSheet; app: page.app; svc: page.svc }
//   formatSheet.openFor(project)
Sheet {
  id: root

  property var app: null
  property var svc: null
  property var project: ({})
  property string phase: "idle"          // loading | ready | error | applying
  property var result: null              // the dry run's answer
  property var error: null               // the dry run failed
  property var applyError: null          // apply failed; the diff stays
  property int _ticket: 0                // drops answers for a project the sheet no longer shows

  readonly property var changed: result && result.files_changed ? result.files_changed : []
  readonly property int changedCount: result ? (result.files_changed_total !== undefined ? result.files_changed_total : changed.length) : 0
  readonly property var fileErrors: result && result.errors ? result.errors : []
  readonly property bool loading: phase === "loading"
  readonly property bool applying: phase === "applying"
  readonly property string applyBlocker: loading ? "Still checking what would change"
                                         : phase === "error" ? "swift-format didn't run; nothing to apply"
                                         : changedCount === 0 ? "Nothing to change: the project is already formatted"
                                         : ""

  title: "Format " + (project && project.name ? project.name : "project")
  subtitle: "swift-format shows its changes first. Nothing is written until you apply them."
  glyph: Model.Glyph.format
  preferredWidth: Style.space(820)
  busy: applying                         // loading can still be cancelled

  function openFor(p) {
    root.project = p || {}
    root.open()
    root.check()
  }

  function check() {
    var t = ++root._ticket
    root.phase = "loading"
    root.result = null
    root.error = null
    root.applyError = null
    svc.formatPreview(root.project, function(d) {
      if (t !== root._ticket) return
      root.result = d
      root.phase = "ready"
    }, function(e) {
      if (t !== root._ticket) return
      root.error = e
      root.phase = "error"
    })
  }

  function apply() {
    if (root.applyBlocker !== "" || root.applying) return
    var t = ++root._ticket
    root.phase = "applying"
    root.applyError = null
    svc.formatApply(root.project, function(d) {
      if (t !== root._ticket) return
      root.phase = "ready"
      root.close()
    }, function(e) {
      if (t !== root._ticket) return
      root.applyError = e
      root.phase = "ready"
    })
  }

  // Answers that arrive after the sheet closed are dropped.
  onClosed: { root._ticket++; if (root.phase === "loading") root.phase = "idle" }

  function errorText(e) {
    return Model.errorMessage(e) + (e && e.hint ? " " + e.hint : "")
  }

  function summaryDetail() {
    var r = root.result
    if (!r) return ""
    var parts = []
    if (r.files_checked !== undefined) parts.push(Model.plural(r.files_checked, "Swift file") + " checked")
    parts.push(r.config_path ? "Style: " + r.config_path : "Default style (no .swift-format)")
    if (r.tool_version) parts.push("swift-format " + String(r.tool_version).replace(/^swift-format\s*/i, ""))
    return parts.join(" · ")
  }

  // ---- loading
  LoadingState {
    Layout.fillWidth: true
    Layout.preferredHeight: Style.space(200)
    visible: root.loading
    text: "Running swift-format…"
    detail: "Reading every Swift file. Nothing changes yet."
  }

  // ---- dry run failed
  ErrorState {
    Layout.fillWidth: true
    visible: root.phase === "error"
    compact: true
    app: root.app
    title: "Couldn't run swift-format"
    error: root.error
    message: root.errorText(root.error)
    onRetry: root.check()
    onFixed: root.close()            // the fix opens another page
  }

  // ---- nothing to change
  EmptyState {
    Layout.fillWidth: true
    Layout.topMargin: Style.spacing.lg
    Layout.bottomMargin: Style.spacing.lg
    visible: (root.phase === "ready" || root.applying) && !!root.result && root.changedCount === 0
    compact: true
    level: "ok"
    glyph: Model.Glyph.checkCircle
    title: "Already formatted"
    text: root.result ? "swift-format wouldn't change any of the " + Model.plural(root.result.files_checked || 0, "Swift file") + ".\n"
                        + (root.result.config_path ? "Style: " + root.result.config_path : "Default style (no .swift-format)") : ""
  }

  // ---- what would change
  ColumnLayout {
    Layout.fillWidth: true
    visible: (root.phase === "ready" || root.applying) && !!root.result && root.changedCount > 0
    spacing: Style.spacing.xl

    ErrorState {
      Layout.fillWidth: true
      visible: !!root.applyError
      banner: true
      app: root.app
      error: root.applyError
      message: root.errorText(root.applyError)
      retryText: "Try again"
      onRetry: root.apply()
    }

    // summary: glyph centred on the headline's line, detail under it. The glyph column and gap are the
    // Sheet header's (display-size glyph, xxl), so the summary text starts on the title's left edge.
    RowLayout {
      Layout.fillWidth: true
      spacing: Style.spacing.xxl
      FontMetrics { id: headMetrics; font: headText.font }
      Item {
        Layout.alignment: Qt.AlignTop
        Layout.preferredWidth: Style.font.display
        Layout.preferredHeight: headMetrics.height
        Icon { anchors.centerIn: parent; glyph: Model.Glyph.edit; size: Style.font.iconLarge; color: Commons.Color.accent }
      }
      ColumnLayout {
        Layout.fillWidth: true
        spacing: Style.spacing.xxs
        AppText { id: headText; Layout.fillWidth: true; text: Model.plural(root.changedCount, "file") + " would change"; strong: true; elide: Text.ElideRight }
        AppText { Layout.fillWidth: true; text: root.summaryDetail(); tone: "dim"; size: "small"; wrapMode: Text.WordWrap }
      }
    }

    // files swift-format couldn't read (skipped)
    Rectangle {
      Layout.fillWidth: true
      visible: root.fileErrors.length > 0
      radius: Style.cornerRadius
      color: Theme.levelFill("warn")
      border.width: Style.normalBorderWidth
      border.color: Util.alpha(Theme.warn, 0.35)
      implicitHeight: errCol.implicitHeight + Style.spacing.lg * 2
      ColumnLayout {
        id: errCol
        anchors.fill: parent
        anchors.margins: Style.spacing.lg
        anchors.leftMargin: Style.spacing.xxl
        spacing: Style.spacing.xs
        RowLayout {
          Layout.fillWidth: true
          spacing: Style.spacing.lg
          Icon { glyph: Model.Glyph.warn; color: Theme.warn; size: Style.font.icon }
          AppText {
            Layout.fillWidth: true
            text: Model.plural(root.fileErrors.length, "file") + " couldn't be read and stay as they are"
            wrapMode: Text.WordWrap
          }
        }
        Repeater {
          model: root.fileErrors.slice(0, 5)
          delegate: AppText {
            required property var modelData
            Layout.fillWidth: true
            Layout.leftMargin: Style.font.icon + Style.spacing.lg
            text: (modelData.file || "") + (modelData.message ? ": " + modelData.message : "")
            tone: "dim"; size: "small"; elide: Text.ElideMiddle
          }
        }
        AppText {
          Layout.fillWidth: true
          Layout.leftMargin: Style.font.icon + Style.spacing.lg
          visible: root.fileErrors.length > 5
          text: "and " + (root.fileErrors.length - 5) + " more"
          tone: "faint"; size: "small"
        }
      }
    }

    DiffView {
      Layout.fillWidth: true
      text: root.result && root.result.diff ? root.result.diff : ""
      maxHeight: Style.space(380)
    }

    // the diff was cut: where the whole one is
    RowLayout {
      Layout.fillWidth: true
      visible: !!root.result && root.result.truncated === true
      spacing: Style.spacing.lg
      Icon { glyph: Model.Glyph.info; size: Style.font.bodySmall; color: Theme.faint }
      AppText { text: "Showing the start of a long diff."; tone: "faint"; size: "small" }
      // where the whole one is (elides in the middle, so the file name stays)
      AppText {
        Layout.fillWidth: true
        text: String(root.result && root.result.full_output_path ? root.result.full_output_path : "").replace(root.app ? root.app.home : "", "~")
        tone: "faint"; size: "small"; elide: Text.ElideMiddle
      }
      ActionButton {
        visible: !!(root.result && root.result.full_output_path)
        text: "Open"
        glyph: Model.Glyph.openExternal
        kind: "ghost"
        compact: true
        tooltip: "Open the whole diff in your editor"
        onClicked: {
          var f = String(root.result.full_output_path)
          root.svc.openInEditor({ path: f.substring(0, f.lastIndexOf("/")) || "/" }, f)
        }
      }
    }
  }

  footer: [
    ActionButton {
      text: root.phase === "ready" && root.changedCount === 0 ? "Close" : "Cancel"
      kind: "ghost"
      enabled: !root.applying
      onClicked: root.close()
    },
    ActionButton {
      text: root.changedCount > 0 ? "Apply to " + Model.plural(root.changedCount, "file") : "Apply"
      glyph: Model.Glyph.format
      kind: "primary"
      busy: root.applying
      disabled: root.applyBlocker !== ""
      tooltip: root.applyBlocker !== "" ? root.applyBlocker : "Write swift-format's changes to these files"
      onClicked: root.apply()
    }
  ]
}
