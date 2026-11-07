import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../common"
import "../../lib/Model.js" as Model

// One installed editor in Settings › Code editor (EditorSection): name, state, what it still needs, and
// Set up. Set up shows the dry run under the row first; Apply runs it, then the row lists what it did.
//   EditorSetupRow { section: root; editor: e; plan: root.plans[e.id] || null; first: index === 0
//                    slotWidth: probe.implicitWidth
//                    onSetupRequested: root.plan(e); onApplyRequested: root.apply(e); onCancelRequested: root.cancel(e) }
// editor = an `editor status` entry; plan = { state: loading|ready|error|running|done, result, error }.
Item {
  id: root

  property var section: null
  property var editor: ({})
  property var plan: null
  property bool first: false
  property real slotWidth: 0

  signal setupRequested()
  signal applyRequested()
  signal cancelRequested()

  readonly property string planState: plan ? plan.state : ""
  readonly property var result: plan && plan.result ? plan.result : null
  readonly property bool done: planState === "done"
  readonly property bool open: planState === "ready" || planState === "running" || done
  readonly property var changes: section ? section.changes(result) : []
  readonly property var userChanges: changes.filter(function(a) { return a.kind !== "backup" && root.section && root.section.editorConfig(a) })
  readonly property var backups: changes.filter(function(a) { return a.kind === "backup" })
  readonly property string edState: done && result && result.state ? result.state : (editor.state || "")
  // Neovim: LazyVim (Omarchy's default) is set up here; plain Neovim is set up by hand from the docs snippet.
  readonly property bool manualNvim: editor.id === "neovim" && editor.lazyvim === false && edState !== "ready"
  readonly property string displayName: editor.name || editor.id || ""
  readonly property bool canSetUp: edState === "needs-setup" && !manualNvim
  // Human-only steps left (state "partial", e.g. installing Zed's Swift extension): from the setup answer
  // once it ran, else from the status.
  readonly property var notes: (done && result && result.notes ? result.notes : (editor.notes || [])).filter(function(n) { return !!n })
  // The line under the name. While the dry run is open the action rows say which files, so it's hidden then.
  readonly property string noteText: {
    if (open) return ""
    if (manualNvim) return "Your Neovim config isn't LazyVim, so iOS Studio doesn't change it. Add the sourcekit-lsp snippet from the docs to it."
    if (edState === "partial" && notes.length) return ""        // the steps show below, one per line
    if (edState === "ready") return "Completion and errors work in every xtool project you open in " + (editor.name || "it") + "."
    if (edState === "needs-setup") return "Set up shows what it changes before it changes anything."
    return editor.note ? String(editor.note) : ""
  }
  readonly property string nextText: result && result.next ? (Array.isArray(result.next) ? result.next.join(" ") : String(result.next)) : ""

  Layout.fillWidth: true
  implicitHeight: col.implicitHeight + Style.spacing.lg * 2

  Rectangle {
    visible: !root.first
    anchors.top: parent.top
    width: parent.width
    height: Style.spacing.hairline
    color: Theme.hairline
  }

  ColumnLayout {
    id: col
    anchors.fill: parent
    anchors.topMargin: Style.spacing.lg
    anchors.bottomMargin: Style.spacing.lg
    spacing: Style.spacing.lg

    RowLayout {
      Layout.fillWidth: true
      spacing: Style.spacing.xxl

      Rectangle {
        Layout.alignment: Qt.AlignTop
        implicitWidth: Style.space(32); implicitHeight: Style.space(32)
        radius: Style.cornerRadius
        color: root.edState === "ready" ? Theme.levelFill("ok") : Theme.surface
        border.width: Style.normalBorderWidth
        border.color: root.edState === "ready" ? Util.alpha(Theme.success, 0.35) : Theme.hairline
        Icon {
          anchors.centerIn: parent
          glyph: Model.Glyph.fileCode
          size: Style.font.icon
          color: root.edState === "ready" ? Theme.success : Commons.Color.foreground
        }
      }

      ColumnLayout {
        Layout.fillWidth: true
        Layout.alignment: Qt.AlignVCenter
        spacing: Style.spacing.xxs
        RowLayout {
          Layout.fillWidth: true
          spacing: Style.spacing.lg
          AppText { text: root.displayName; elide: Text.ElideRight; Layout.maximumWidth: implicitWidth + 1; Layout.fillWidth: true }
          StatusPill {
            quiet: root.edState !== "ready"
            showGlyph: root.edState === "ready" || root.edState === "partial"
            level: root.manualNvim ? "info" : (root.section ? root.section.stateLevel(root.edState) : "info")
            text: root.manualNvim ? "Set up by hand" : (root.section ? root.section.stateText(root.edState) : "")
          }
          Item { Layout.fillWidth: true }
        }
        AppText {
          Layout.fillWidth: true
          visible: root.noteText !== ""
          text: root.noteText
          size: "small"; tone: "dim"; wrapMode: Text.WordWrap
        }
        Repeater {
          model: root.edState === "partial" && !root.manualNvim ? root.notes : []
          delegate: AppText {
            required property var modelData
            Layout.fillWidth: true
            text: root.section ? root.section.tidy(modelData) : String(modelData)
            size: "small"; tone: "warn"; wrapMode: Text.WordWrap
          }
        }
      }

      // Fixed-width slot, so the rows' buttons line up with or without one.
      Item {
        Layout.alignment: Qt.AlignVCenter
        implicitWidth: root.slotWidth
        implicitHeight: setupBtn.implicitHeight
        ActionButton {
          id: setupBtn
          anchors.fill: parent
          visible: root.canSetUp && !root.open
          compact: true
          text: "Set up…"
          glyph: Model.Glyph.editorSetup
          busy: root.planState === "loading"
          tooltip: "See what it changes first. Nothing changes until you press Apply."
          onClicked: root.setupRequested()
        }
        ActionButton {
          anchors.fill: parent
          visible: root.manualNvim
          compact: true
          kind: "ghost"
          text: "Snippet"
          glyph: Model.Glyph.openExternal
          tooltip: "Open the Neovim snippet in docs/features/editor-setup.md"
          onClicked: if (root.section) root.section.openDocs("neovim")
        }
      }
    }

    // ---- the dry run, or what it did
    ColumnLayout {
      Layout.fillWidth: true
      Layout.leftMargin: Style.space(32) + Style.spacing.xxl
      visible: root.open || root.planState === "error"
      spacing: Style.spacing.md

      ColumnLayout {
        Layout.fillWidth: true
        spacing: Style.spacing.sm
        Repeater {
          model: root.result ? (root.result.actions || []) : []
          delegate: RowLayout {
            id: act
            required property var modelData
            readonly property bool skip: modelData.kind === "skip"
            Layout.fillWidth: true
            spacing: Style.spacing.lg
            AppText {
              Layout.preferredWidth: Style.space(80)
              Layout.alignment: Qt.AlignTop
              text: root.section ? root.section.kindLabel(act.modelData.kind, root.done && act.modelData.done !== false) : act.modelData.kind
              size: "small"
              tone: act.skip ? "faint" : (root.done ? "ok" : (root.section && root.section.editorConfig(act.modelData) ? "warn" : "dim"))
            }
            // The file (once, here), then what it does and any values of yours it keeps.
            ColumnLayout {
              Layout.fillWidth: true
              spacing: 0
              AppText {
                Layout.fillWidth: true
                text: root.section ? root.section.shortPath(act.modelData.path) : String(act.modelData.path || "")
                size: "small"; mono: true
                tone: act.skip ? "faint" : "primary"
                elide: Text.ElideMiddle
              }
              AppText {
                Layout.fillWidth: true
                visible: !!act.modelData.detail
                text: root.section ? root.section.tidy(act.modelData.detail) : String(act.modelData.detail || "")
                size: "small"; tone: "faint"; wrapMode: Text.WordWrap
              }
              Repeater {
                model: act.modelData.kept || []
                delegate: AppText {
                  required property var modelData
                  Layout.fillWidth: true
                  text: "Keeps your " + modelData.key + ": " + JSON.stringify(modelData.yours) + " (iOS Studio would use " + JSON.stringify(modelData.ours) + ")"
                  size: "small"; tone: "faint"; wrapMode: Text.WordWrap; elide: Text.ElideRight; maximumLineCount: 2
                }
              }
            }
          }
        }
      }

      // Plain words before Apply when it changes the user's own config.
      AppText {
        Layout.fillWidth: true
        visible: !root.done && root.userChanges.length > 0
        text: "Changes your " + root.displayName + " config for every project."
              + (root.backups.length ? " A copy of the current file is saved first." : "")
        size: "small"; tone: "warn"; wrapMode: Text.WordWrap
      }
      AppText {
        Layout.fillWidth: true
        visible: !root.done && root.planState === "ready" && root.changes.length === 0
        text: "Nothing to change."
        size: "small"; tone: "faint"
      }
      AppText {
        Layout.fillWidth: true
        visible: root.planState === "error"
        text: root.plan && root.plan.error ? Model.errorMessage(root.plan.error) : ""
        size: "small"; tone: "error"; wrapMode: Text.WordWrap
      }
      AppText {
        Layout.fillWidth: true
        visible: root.done && root.nextText !== ""
        text: "Next: " + (root.section ? root.section.tidy(root.nextText) : root.nextText)
        size: "small"; wrapMode: Text.WordWrap
      }

      Row {
        visible: !root.done
        spacing: Style.spacing.md
        ActionButton {
          visible: root.changes.length > 0
          compact: true
          kind: "primary"
          text: root.userChanges.length > 0 ? "Change " + (root.editor.name || "") + " config" : "Apply"
          glyph: Model.Glyph.check
          busy: root.planState === "running"
          onClicked: root.applyRequested()
        }
        ActionButton { compact: true; kind: "ghost"; text: root.planState === "error" ? "Close" : "Cancel"; enabled: root.planState !== "running"; onClicked: root.cancelRequested() }
      }
    }
  }
}
