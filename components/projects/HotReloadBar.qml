import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../common"
import "../../core"
import "../../lib/Model.js" as Model

// Hot reload on a project card (issue #13), shown while the project's Hot reload switch is on (also when the
// card is collapsed), and while it turns on or off. Fed by HotReloadService:
//   HotReloadBar { app: …; project: modelData; svc: app.hotReload; startBlocker: "Connect your iPhone first." }
// One slim strip: the flash glyph, the state as a StatusPill ("Building 35%", "Watching", "Reloaded", "Paused",
// "Reload failed", "Stopped"), the line that says what happened ("Reloaded ContentView.swift · 1.6 s · 12:04:31
// · 5 s ago", updated every second while the window is focused), then the buttons: the error's fix, Pause or
// Resume, Stop or Start. A control that can't be used now stays visible, dimmed, with the reason in its tooltip.
// Under it: the build's progress, a failed reload's blockers (≤ 3, file:line opens the editor) or its first
// compiler error, and the session's notice (one quiet line, with its fix as a link-style button).
// On a narrow bar (below 540 px, 780 px with an error's fix) the buttons drop under the text and the places.
Rectangle {
  id: root

  property var app: null
  property var project: ({})
  property var svc: null                    // app.hotReload
  property string startBlocker: ""          // why Start can't run now ("" = it can)

  property double now: Date.now()
  readonly property var entry: svc ? svc.forProject(project) : null
  readonly property var setting: svc ? svc.setting(project) : null
  readonly property var info: Model.hotReloadLabel(entry, setting, now)
  readonly property string _lv: info ? info.level : "info"
  readonly property bool isError: _lv === "error"
  readonly property var fix: info && info.error && info.error.fix && info.error.fix.label ? info.error.fix : null
  readonly property bool canStop: !!info && info.live && info.state !== "stopping"
  readonly property bool showStart: !!info && !info.live && ["enabling", "disabling", "starting"].indexOf(info.state) < 0
  // A fix that only starts it again makes the Start button say the same thing twice: the fix wins.
  readonly property bool fixIsStart: !!fix && (fix.action === "hot-reload-restart" || fix.action === "hot-reload-start")
  readonly property bool showPause: !!info && info.live && info.state !== "stopping"
  readonly property bool canDismiss: !!info && !info.live && (info.state === "stopped" || info.state === "error")

  visible: info !== null
  implicitHeight: col.implicitHeight + Style.spacing.md * 2
  radius: Style.cornerRadius
  color: isError ? Util.alpha(Theme.errorTint, 0.12)
       : info && info.state === "reloaded" ? Util.alpha(Theme.levelColor("ok"), 0.08)
       : Theme.surfaceRaised
  border.width: Style.normalBorderWidth
  border.color: isError ? Util.alpha(Theme.errorTint, 0.45)
              : info && info.state === "reloaded" ? Util.alpha(Theme.levelColor("ok"), 0.30)
              : Theme.hairline
  Behavior on color { ColorAnimation { duration: 160 } }

  // "5 s ago" moves on while it shows; nothing ticks while the window is in the background.
  Timer {
    interval: 1000
    repeat: true
    running: root.visible && !!root.app && root.app.windowFocused && !!root.info
             && (root.info.state === "watching" || root.info.state === "reloaded" || root.info.note !== null)
    onTriggered: root.now = Date.now()
  }
  onEntryChanged: now = Date.now()

  function runFix(f) { if (f && root.svc) root.svc.runFix(root.project, f) }
  function openAt(file, line) {
    if (!root.app || !file) return
    root.app.projects.openInEditor(root.project, String(file).charAt(0) === "/" ? file : root.project.path + "/" + file, line)
  }

  // An error has its fix next to Pause and Stop: it needs more room before the buttons go under the text.
  readonly property bool stackButtons: width < Style.space(isError || (info && info.note && info.note.fix) ? 780 : 540)
  readonly property real glyphColumn: Math.ceil(Style.font.body * 1.4)
  readonly property real textIndent: glyphColumn + Style.spacing.lg

  ColumnLayout {
    id: col
    anchors.fill: parent
    anchors.margins: Style.spacing.md
    anchors.leftMargin: Style.spacing.xl
    spacing: Style.spacing.sm

    RowLayout {
      Layout.fillWidth: true
      spacing: Style.spacing.lg

      // The glyph and the pill sit on the first line of the text (centred with it when it's one line).
      FontMetrics { id: lineMetrics; font: mainText.font }
      readonly property bool multiLine: textCol.implicitHeight > Math.max(lineMetrics.height, pill.implicitHeight) * 1.5
      Item {
        Layout.preferredWidth: root.glyphColumn
        Layout.preferredHeight: Math.max(lineMetrics.height, pill.implicitHeight)
        Layout.alignment: parent.multiLine ? Qt.AlignTop : Qt.AlignVCenter
        Icon {
          anchors.centerIn: parent
          size: Style.font.body
          glyph: Model.Glyph.hotReload
          color: root.isError ? Commons.Color.urgent
                 : root.info && root.info.live ? Theme.accent
                 : Theme.faint
        }
      }
      // The pill's slot is as wide as "Reloading" at least, so the text doesn't shift on every save.
      StatusPill { id: pillRef; visible: false; level: "busy"; text: "Reloading" }
      Item {
        Layout.preferredWidth: Math.max(pill.implicitWidth, pillRef.implicitWidth)
        Layout.preferredHeight: Math.max(lineMetrics.height, pill.implicitHeight)
        Layout.alignment: parent.multiLine ? Qt.AlignTop : Qt.AlignVCenter
        StatusPill {
          id: pill
          anchors.left: parent.left
          anchors.verticalCenter: parent.verticalCenter
          level: root.info ? root.info.level : "info"
          glyph: root.info ? root.info.pillGlyph : ""
          text: root.info ? root.info.pill : ""
        }
      }

      ColumnLayout {
        id: textCol
        Layout.fillWidth: true
        Layout.preferredWidth: 0
        Layout.alignment: Qt.AlignVCenter
        spacing: Style.spacing.xxs
        AppText {
          id: mainText
          Layout.fillWidth: true
          text: root.info ? root.info.text : ""
          tone: root.info && root.info.state === "stopped" && !root.isError ? "dim" : "primary"
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

      Loader {
        Layout.alignment: Qt.AlignVCenter
        active: !root.stackButtons
        visible: active
        sourceComponent: buttons
      }
    }

    // The app's build, before the first reload
    ProgressTrack {
      Layout.fillWidth: true
      Layout.leftMargin: root.textIndent
      visible: !!root.info && root.info.progress >= 0
      value: root.info ? Math.max(0, root.info.progress) : 0
      indeterminate: !!root.info && root.info.indeterminate
    }

    // A failed reload: what blocks it (file:line opens it in the editor), or the compiler's first error
    Repeater {
      model: root.info ? root.info.blockers : []
      delegate: RowLayout {
        id: blk
        required property var modelData
        Layout.fillWidth: true
        Layout.leftMargin: root.textIndent
        spacing: Style.spacing.md
        AppText {
          Layout.alignment: Qt.AlignTop
          visible: blk.modelData.where !== ""
          text: blk.modelData.where
          size: "small"
          tone: whereHover.hovered ? "accent" : "primary"
          font.underline: whereHover.hovered
          HoverHandler { id: whereHover; cursorShape: Qt.PointingHandCursor }
          TapHandler { onTapped: root.openAt(blk.modelData.file, blk.modelData.line) }
        }
        AppText {
          Layout.fillWidth: true
          Layout.preferredWidth: 0
          visible: text !== ""
          text: blk.modelData.text
          size: "small"
          tone: "dim"
          wrapMode: Text.Wrap
        }
      }
    }
    AppText {
      Layout.fillWidth: true
      Layout.leftMargin: root.textIndent
      visible: !!root.info && root.info.moreBlockers > 0
      text: root.info ? "and " + root.info.moreBlockers + " more" : ""
      size: "small"
      tone: "faint"
    }
    AppText {
      Layout.fillWidth: true
      Layout.leftMargin: root.textIndent
      visible: text !== ""
      text: root.info ? root.info.diagnostic : ""
      size: "small"
      tone: "dim"
      mono: true
      elide: Text.ElideRight
    }

    // Narrow: the buttons go under the text (and a failed reload's places), so the text keeps the width.
    Loader {
      Layout.leftMargin: root.textIndent
      Layout.topMargin: Style.spacing.xs
      active: root.stackButtons && !!root.info
      visible: active
      sourceComponent: buttons
    }

    // The session's notice (watch mode stopped, Package.swift changed: press Run, the app didn't open…)
    RowLayout {
      Layout.fillWidth: true
      visible: !!root.info && root.info.note !== null
      spacing: Style.spacing.lg
      FontMetrics { id: noteMetrics; font: noteText.font }
      Item {
        Layout.preferredWidth: root.glyphColumn
        Layout.preferredHeight: noteMetrics.height
        Layout.alignment: Qt.AlignTop
        Icon {
          anchors.centerIn: parent
          size: Style.font.bodySmall
          glyph: root.info && root.info.note ? Model.levelGlyph(root.info.note.level) : ""
          color: root.info && root.info.note && root.info.note.level === "warn" ? Theme.levelColor("warn") : Theme.faint
        }
      }
      AppText {
        id: noteText
        Layout.fillWidth: true
        Layout.preferredWidth: 0
        text: root.info && root.info.note ? root.info.note.text : ""
        size: "small"
        tone: "dim"
        wrapMode: Text.Wrap
      }
      ActionButton {
        Layout.alignment: Qt.AlignTop
        visible: !!(root.info && root.info.note && root.info.note.fix && root.info.note.fix.label)
        text: visible ? root.info.note.fix.label : ""
        kind: "ghost"
        compact: true
        onClicked: root.runFix(root.info.note.fix)
      }
    }
  }

  Component {
    id: buttons
    RowLayout {
      spacing: Style.spacing.md
      ActionButton {
        visible: !!root.fix
        text: root.fix ? root.fix.label : ""
        glyph: root.fixIsStart ? Model.Glyph.refresh : ""
        kind: "primary"
        compact: true
        disabled: root.fixIsStart && !root.info.live && root.startBlocker !== ""
        tooltip: disabled ? root.startBlocker : ""
        onClicked: root.runFix(root.fix)
      }
      ActionButton {
        visible: root.showPause
        text: root.info && root.info.canResume ? "Resume" : "Pause"
        glyph: root.info && root.info.canResume ? Model.Glyph.play : Model.Glyph.pause
        kind: "ghost"
        compact: true
        busy: !!root.entry && (root.entry.action === "pausing" || root.entry.action === "resuming")
        disabled: !!root.info && !root.info.canPause && !root.info.canResume
        tooltip: !root.info ? ""
                 : root.info.canResume ? "Apply the saves made while paused, then reload each save again"
                 : root.info.canPause ? "Keep the app as it is: saves wait until you resume, no rebuild needed"
                 : root.info.pauseTip
        onClicked: root.info.canResume ? root.svc.resume(root.project) : root.svc.pause(root.project)
      }
      ActionButton {
        visible: root.showPause || (root.info && root.info.state === "stopping")
        text: "Stop"
        glyph: Model.Glyph.stop
        kind: "ghost"
        compact: true
        busy: !!root.info && root.info.state === "stopping"
        tooltip: "Stop hot reload. The app keeps running with the last reload; the switch stays on, so Start begins again."
        onClicked: root.svc.stop(root.project)
      }
      ActionButton {
        visible: root.showStart && !(root.fixIsStart)
        text: "Start"
        glyph: Model.Glyph.play
        kind: root.isError ? "ghost" : "secondary"
        compact: true
        disabled: root.startBlocker !== ""
        tooltip: root.startBlocker !== "" ? root.startBlocker
                 : "Build and open the app once, then reload each save on your iPhone"
        onClicked: root.svc.start(root.project)
      }
      IconButton {
        visible: root.canDismiss
        glyph: Model.Glyph.close
        tooltip: "Dismiss"
        onClicked: root.svc.dismiss(root.project)
      }
    }
  }
}
