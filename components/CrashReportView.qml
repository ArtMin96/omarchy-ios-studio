import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell.Io
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../Model.js" as Model

// One crash report (logs slice): a plain-language summary card first, then the crashed
// thread and the full report. Save copies the .ips to ~/Downloads; Copy puts it on the clipboard.
Item {
  id: root
  required property var app
  property var item: null
  readonly property var svc: app.logs
  readonly property var summary: item ? svc.summaryFor(item) : null
  readonly property string path: item && svc.localPaths[item.name] ? svc.localPaths[item.name] : ""
  readonly property bool loading: !!item && !!svc.pulling[item.name]
  property var loadError: null

  function load(c) {
    root.loadError = null
    if (!c) return
    svc.pullCrash(c, function(ok, d) { if (!ok) root.loadError = d })
  }

  function save() {
    if (path === "") return
    var dest = svc.downloadsDir + "/" + path.split("/").pop()
    svc.saveCopy(path, dest, function(ok) { root.app.toast(ok ? "Saved to " + svc.prettyPath(dest) : "Couldn't save the report", ok ? "ok" : "error", { timeout: 6000 }) })
  }
  function copy() {
    if (path === "") return
    svc.copyFile(path, function(ok) { root.app.toast(ok ? "Copied the full report" : "Couldn't copy", ok ? "ok" : "error") })
  }

  // Plain-language reading of the exception, for people who don't read crash logs.
  function explain(s, c) {
    if (c && c.kind === "jetsam") return "iOS closed apps to free memory. If your app was among them, it was using more memory than the phone could spare."
    if (c && c.kind === "resource") return "The app used more CPU, memory or disk than iOS allows in the background, so iOS recorded it (the app may have kept running)."
    if (c && c.kind === "hang") return "The app stopped responding for a while."
    if (!s) return ""
    var t = String(s.exceptionType || ""), sig = String(s.signal || ""), why = String(s.terminationReason || "")
    if (/8badf00d/i.test(why)) return "iOS's watchdog closed the app because it took too long to launch or stopped responding (usually slow work on the main thread)."
    if (t === "EXC_BREAKPOINT" || sig === "SIGTRAP") return "A Swift runtime check failed: usually force-unwrapping a nil value (!), an array index out of range, or a fatalError()."
    if (t === "EXC_BAD_ACCESS") return "The app touched memory it shouldn't have, such as an object that was already freed or an unsafe pointer."
    if (t === "EXC_CRASH" && sig === "SIGABRT") return "The app stopped itself on purpose: an uncaught exception, a failed assertion, or a framework that found a programming error."
    if (t === "EXC_CRASH" && sig === "SIGKILL") return "iOS force-closed the app (for example a code-signing problem or the system needed the resources)."
    if (t === "EXC_RESOURCE") return "The app went over an iOS resource limit (CPU, memory or wake-ups)."
    if (t === "EXC_GUARD") return "The app misused a protected system resource, such as closing a file it doesn't own."
    if (t === "EXC_BAD_INSTRUCTION" || sig === "SIGILL") return "The app ran into an illegal instruction, usually a failed Swift runtime check."
    return ""
  }

  readonly property string ownImage: item ? (item.process || "") : ""

  // Crash symbolication (helper/iosstudio/features/crash_symbols.py): file:line for the app's frames, from the build that crashed.
  readonly property var sym: summary && summary.symbolication ? summary.symbolication : null
  readonly property string symLevel: !sym ? "" : (sym.status === "full" ? "ok" : (sym.status === "not-needed" ? "" : "warn"))
  readonly property string symNote: {
    if (!sym || sym.status === "not-needed") return ""
    if (sym.status === "full") return "File and line numbers come from the build that crashed. Click a frame to open it."
    return [sym.message || "", sym.hint || ""].filter(function(s) { return s !== "" }).join(" ")
  }
  function openFrame(info) {
    if (!info || !info.file || !root.app.projects) return
    var project = root.sym && root.sym.project ? root.sym.project : ""
    var file = info.path || info.file
    if (project === "") { root.app.toast("Couldn't tell which project this build came from", "warn"); return }
    root.app.projects.openInEditor({ path: project }, file, info.line, info.column)
  }

  property string reportText: ""
  onPathChanged: reportText = ""
  FileView {
    id: file
    path: root.path
    printErrors: false
    onLoaded: root.reportText = text()
    onLoadFailed: root.reportText = "The report file couldn't be read."
  }

  EmptyState {
    anchors.centerIn: parent
    width: Math.min(parent.width, Style.space(420))
    visible: !root.item
    glyph: Model.Glyph.crash
    title: "Pick a report"
    text: "Choose a crash on the left to see what happened."
    compact: true
  }

  Flickable {
    id: flick
    anchors.fill: parent
    visible: !!root.item
    clip: true
    contentWidth: width
    contentHeight: col.implicitHeight + Style.spacing.huge
    boundsBehavior: Flickable.StopAtBounds
    ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

    ColumnLayout {
      id: col
      width: flick.width - Style.spacing.xl
      spacing: Style.spacing.huge

      // ---- header
      RowLayout {
        Layout.fillWidth: true
        spacing: Style.spacing.xl
        ColumnLayout {
          Layout.fillWidth: true
          spacing: Style.spacing.xs
          AppText {
            Layout.fillWidth: true
            text: root.item ? (root.item.kind === "jetsam" ? "Out of memory" : (root.item.process || root.item.file)) : ""
            size: "title"; strong: true; elide: Text.ElideRight
          }
          AppText {
            Layout.fillWidth: true
            text: root.item ? [svc.kindLabel(root.item.kind), new Date(root.item.ts * 1000).toLocaleString(Qt.locale(), "dddd d MMMM, HH:mm:ss"), Model.relativeTime(root.item.ts)].join(" · ") : ""
            size: "small"; tone: "dim"; elide: Text.ElideRight
          }
        }
        ActionButton { text: "Copy"; glyph: Model.Glyph.copy; enabled: root.path !== ""; onClicked: root.copy() }
        ActionButton { text: "Save"; glyph: Model.Glyph.save; enabled: root.path !== ""; onClicked: root.save() }
      }

      LoadingState {
        Layout.fillWidth: true
        visible: root.loading && !root.summary
        text: "Copying the report from your iPhone…"
      }
      ErrorState {
        Layout.fillWidth: true
        visible: !!root.loadError && !root.summary
        app: root.app
        error: root.loadError
        banner: true
        onRetry: root.load(root.item)
      }

      // ---- summary card
      Card {
        Layout.fillWidth: true
        visible: !!root.summary
        title: root.summary && root.summary.exceptionType ? root.summary.exceptionType + (root.summary.signal ? " (" + root.summary.signal + ")" : "") : svc.kindLabel(root.item ? root.item.kind : "")
        glyph: Model.Glyph.crash
        glyphColor: Theme.error
        accentEdge: false
        contentSpacing: Style.spacing.xl

        AppText {
          Layout.fillWidth: true
          visible: text !== ""
          text: root.explain(root.summary, root.item)
          wrapMode: Text.WordWrap
        }
        KeyValueList {
          Layout.fillWidth: true
          labelWidth: Style.space(140)
          rows: {
            var s = root.summary || {}, r = []
            if (s.terminationReason) r.push({ label: "Reason", value: s.terminationReason })
            if (s.bundleId) r.push({ label: "App", value: s.bundleId, copy: true })
            if (s.appVersion) r.push({ label: "Version", value: s.appVersion })
            if (s.osVersion) r.push({ label: "iOS", value: s.osVersion })
            if (s.date) r.push({ label: "When", value: s.date })
            if (s.crashedThread !== null && s.crashedThread !== undefined) r.push({ label: "Crashed thread", value: String(s.crashedThread) })
            if (s.incident) r.push({ label: "Incident", value: s.incident, copy: true })
            return r
          }
          onCopied: function(v) { root.app.toast("Copied") }
        }
      }

      // ---- crashed thread
      ColumnLayout {
        Layout.fillWidth: true
        visible: !!root.summary && !!root.summary.frames && root.summary.frames.length > 0
        spacing: Style.spacing.md
        SectionHeader { Layout.fillWidth: true; text: "Where it crashed"; detail: root.summary && root.summary.frames ? "first " + root.summary.frames.length + " frames" : "" }
        Rectangle {
          Layout.fillWidth: true
          implicitHeight: frames.implicitHeight + Style.spacing.xl * 2
          radius: Style.cornerRadius
          color: Util.alpha(Commons.Color.background, Theme.isDark ? 0.5 : 0.3)
          border.width: Style.normalBorderWidth
          border.color: Theme.hairline
          Column {
            id: frames
            x: Style.spacing.xl; y: Style.spacing.xl
            width: parent.width - Style.spacing.xl * 2
            spacing: Style.spacing.xs
            Repeater {
              model: root.summary && root.summary.frames ? root.summary.frames : []
              delegate: CrashFrameRow {
                required property var modelData
                required property int index
                width: frames.width
                text: String(modelData)
                info: root.sym && root.sym.frames ? (root.sym.frames[index] || null) : null
                mine: root.ownImage !== "" && String(modelData).indexOf(root.ownImage) >= 0
                onClicked: root.openFrame(info)
              }
            }
          }
        }
        // symbolication status: says why app frames have no file:line, and what fixes it
        RowLayout {
          Layout.fillWidth: true
          visible: root.symNote !== ""
          spacing: Style.spacing.md
          Icon {
            Layout.alignment: Qt.AlignTop
            glyph: root.symLevel === "ok" ? Model.Glyph.info : Model.Glyph.warn
            color: root.symLevel === "ok" ? Theme.faint : Theme.warn
            size: Style.font.bodySmall
          }
          AppText {
            Layout.fillWidth: true
            text: root.symNote
            size: "small"; tone: "dim"; wrapMode: Text.WordWrap
          }
        }
      }

      // ---- full report
      ColumnLayout {
        Layout.fillWidth: true
        visible: root.path !== ""
        spacing: Style.spacing.md
        SectionHeader { Layout.fillWidth: true; text: "Full report"; detail: root.path !== "" ? root.path.split("/").pop() : "" }
        Rectangle {
          Layout.fillWidth: true
          implicitHeight: body.implicitHeight + Style.spacing.xl * 2
          radius: Style.cornerRadius
          color: Util.alpha(Commons.Color.background, Theme.isDark ? 0.5 : 0.3)
          border.width: Style.normalBorderWidth
          border.color: Theme.hairline
          TextEdit {
            id: body
            x: Style.spacing.xl; y: Style.spacing.xl
            width: parent.width - Style.spacing.xl * 2
            readOnly: true
            selectByMouse: true
            textFormat: TextEdit.PlainText
            wrapMode: TextEdit.WrapAnywhere
            text: root.reportText !== "" ? root.reportText : "Loading…"
            color: Theme.dim
            selectionColor: Style.selectionFill
            selectedTextColor: Commons.Color.foreground
            font.family: Style.font.family
            font.pixelSize: Style.font.caption
          }
        }
      }
    }
  }
}
