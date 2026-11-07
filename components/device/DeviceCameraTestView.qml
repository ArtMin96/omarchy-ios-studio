import QtQuick
import QtQuick.Layouts
import QtQuick.Dialogs
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../common"
import "../../lib/Model.js" as Model

// Files › Camera test: run one of your apps' camera code on chosen images/videos instead of the
// camera. The daemon (cameratest.run) clears and fills Documents/CameraTestInput, relaunches the app
// with the launch arguments, waits for Documents/CameraTestOutput/done.json, copies the outputs here
// and checks for crash reports. The app side is a small hook: docs/camera-testing.md.
ColumnLayout {
  id: root
  required property var app
  readonly property var svc: app.device
  property bool active: false

  readonly property var devApps: svc.apps.filter(function(a) { return a.developer })
  property string bundleId: app.setting("cameraTestApp", "")
  property var inputs: app.setting("cameraTestInputs", []) || []
  property string argsText: app.setting("cameraTestArgs", "-cameraTestMode 1")
  property string envText: app.setting("cameraTestEnv", "")
  property int timeoutS: app.setting("cameraTestTimeout", 60)

  property bool running: false
  property string testId: ""
  property var steps: []                // [{id, title, state, detail}]
  property var result: null
  property var error: null
  property var transfer: null
  property string doneText: ""

  readonly property var stepTitles: [["check", "Check the app"], ["clear", "Clear the test folders"], ["push", "Copy the test files"],
                                     ["launch", "Launch the app"], ["wait", "Wait for results"], ["pull", "Copy the results"],
                                     ["crashes", "Look for crash reports"]]
  readonly property var current: {
    for (var i = 0; i < devApps.length; i++) if (devApps[i].bundleId === bundleId) return devApps[i]
    return null
  }
  readonly property var images: result ? result.outputs.filter(function(p) { return /\.(png|jpe?g|gif|webp|bmp)$/i.test(p) }) : []

  spacing: Style.spacing.xl

  onActiveChanged: if (active) _ensure()
  onDevAppsChanged: if (active) _ensure()
  function _ensure() {
    if (root.svc.connected && root.svc.appsStatus === "unknown") root.svc.refreshApps()
    if (!root.current && root.devApps.length) root.bundleId = root.devApps[0].bundleId
  }

  Connections {
    target: root.svc
    function onDaemonEvent(name, data) {
      if (!data || !root.running) return
      if (name === "cameratest" && data.testId === root.testId) root._step(data.step, data.state, data.detail)
      else if (name === "transfer" && data.id === root.testId) root.transfer = data.state === "running" ? data : null
    }
  }

  function _blankSteps() { return root.stepTitles.map(function(t) { return { id: t[0], title: t[1], state: "pending", detail: "" } }) }
  function _step(id, state, detail) {
    root.steps = root.steps.map(function(s) { return s.id === id ? Object.assign({}, s, { state: state, detail: detail || s.detail }) : s })
  }

  function addInputs(paths) {
    var cur = root.inputs.slice()
    for (var i = 0; i < paths.length; i++) if (cur.indexOf(paths[i]) < 0) cur.push(paths[i])
    root.inputs = cur
    root.app.setSetting("cameraTestInputs", cur)
  }
  function removeInput(p) {
    root.inputs = root.inputs.filter(function(x) { return x !== p })
    root.app.setSetting("cameraTestInputs", root.inputs)
  }

  function run() {
    if (!root.current || !root.inputs.length || root.running) return
    root.app.setSetting("cameraTestApp", root.bundleId)
    root.app.setSetting("cameraTestArgs", argsField.text)
    root.app.setSetting("cameraTestEnv", envField.text)
    root.app.setSetting("cameraTestTimeout", timeoutField.value)
    root.testId = "ct" + Date.now()
    root.running = true
    root.result = null
    root.error = null
    root.doneText = ""
    root.steps = root._blankSteps()
    root.svc.request("cameratest.run", { bundleId: root.bundleId, inputs: root.inputs, args: argsField.text, env: envField.text,
                                         timeout: timeoutField.value, testId: root.testId }, function(ok, d) {
      root.running = false
      root.transfer = null
      if (!ok) {
        root.error = d
        if (d.code === "cancelled") root.app.toast("Camera test stopped", "info")
        return
      }
      root.result = d
      if (d.steps) root.steps = d.steps
      root.doneText = d.done !== null && d.done !== undefined ? JSON.stringify(d.done, null, 2) : (d.doneRaw || "")
      revealTimer.restart()
      root.app.notify("Camera test finished", d.ok ? "done.json is ready" : "No results from the app", Model.Glyph.camera)
    }, (timeoutField.value + 600) * 1000)
  }
  Timer { id: revealTimer; interval: 250; onTriggered: root.revealResults() }   // after the results are laid out
  // Scroll the page so the results start at the top of the view.
  function revealResults() {
    for (var pp = root.parent; pp; pp = pp.parent) {
      if (pp.flickable && pp.flickable.contentItem) {
        var f = pp.flickable
        var y = resultsCol.mapToItem(f.contentItem, 0, 0).y - Style.spacing.huge
        f.contentY = Math.max(0, Math.min(y, f.contentHeight - f.height))
        return
      }
    }
  }
  function cancel() { root.svc.request("transfer.cancel", { transferId: root.testId }, null) }
  function open(p) { Qt.openUrlExternally("file://" + p) }
  function docs() { Qt.openUrlExternally("file://" + root.app.repoPath + "/docs/camera-testing.md") }

  FileDialog {
    id: pick
    title: "Choose test images or videos"
    fileMode: FileDialog.OpenFiles
    nameFilters: ["Images and videos (*.jpg *.jpeg *.png *.heic *.heif *.mov *.mp4 *.m4v)", "All files (*)"]
    currentFolder: "file://" + root.app.home + "/Pictures"
    onAccepted: root.addInputs(selectedFiles.map(function(u) { return decodeURIComponent(String(u).replace(/^file:\/\//, "")) }))
  }

  // ---- intro
  RowLayout {
    Layout.fillWidth: true
    spacing: Style.spacing.lg
    AppText {
      Layout.fillWidth: true
      wrapMode: Text.WordWrap; tone: "dim"
      text: "Feed your app's camera code with images and videos from this computer instead of the camera, and get its results back. The app needs a small test hook."
    }
    ActionButton { text: "How to add the hook"; glyph: Model.Glyph.help; kind: "ghost"; compact: true; onClicked: root.docs() }
  }

  EmptyState {
    Layout.fillWidth: true
    visible: root.svc.appsStatus === "ready" && root.devApps.length === 0
    compact: true
    glyph: Model.Glyph.camera
    title: "None of your apps is on this iPhone"
    text: "Camera tests run on your own apps. Build one in Projects and run it on the iPhone first."
    actionText: "Open Projects"; actionGlyph: Model.Glyph.hammer
    onAction: root.app.nav("projects")
  }

  // ---- form
  GridLayout {
    Layout.fillWidth: true
    visible: root.devApps.length > 0
    columns: width > Style.space(620) ? 2 : 1
    columnSpacing: Style.spacing.huge
    rowSpacing: Style.spacing.lg

    AppText { text: "App"; tone: "dim"; size: "small" }
    Dropdown {
      showLabel: false
      Layout.preferredWidth: Style.space(300)
      enabled: !root.running
      value: root.bundleId
      options: root.devApps.map(function(a) { return { value: a.bundleId, label: a.name } })
      onChanged: function(v) { root.bundleId = v; root.app.setSetting("cameraTestApp", v) }
    }

    AppText { text: "Test files"; tone: "dim"; size: "small"; Layout.alignment: Qt.AlignTop; Layout.topMargin: Style.spacing.md }
    ColumnLayout {
      Layout.fillWidth: true
      spacing: Style.spacing.xxs
      Item {
        Layout.fillWidth: true
        implicitHeight: inputList.implicitHeight
        ColumnLayout {
          id: inputList
          anchors.left: parent.left; anchors.right: parent.right
          spacing: Style.spacing.xxs
          Repeater {
            model: root.inputs
            delegate: ListRow {
              id: irow
              required property var modelData
              Layout.fillWidth: true
              glyph: /\.(mov|mp4|m4v)$/i.test(modelData) ? Model.Glyph.video : Model.Glyph.image
              title: modelData.substring(modelData.lastIndexOf("/") + 1)
              subtitle: modelData.indexOf(root.app.home) === 0 ? "~" + modelData.substring(root.app.home.length, modelData.lastIndexOf("/")) : modelData.substring(0, modelData.lastIndexOf("/"))
              trailing: [ IconButton { glyph: Model.Glyph.close; tooltip: "Remove"; enabled: !root.running; onClicked: root.removeInput(irow.modelData) } ]
            }
          }
        }
        DropArea {
          id: inDrop
          anchors.fill: parent
          enabled: !root.running
          onEntered: function(ev) { if (!ev.hasUrls) ev.accepted = false }
          onDropped: function(ev) {
            var files = []
            for (var i = 0; i < ev.urls.length; i++) { var u = String(ev.urls[i]); if (u.indexOf("file://") === 0) files.push(decodeURIComponent(u.substring(7))) }
            root.addInputs(files)
          }
        }
      }
      RowLayout {
        spacing: Style.spacing.lg
        ActionButton { text: "Add images or videos…"; glyph: Model.Glyph.plus; enabled: !root.running; onClicked: pick.open() }
        AppText { visible: root.inputs.length === 0; text: "or drop them here"; tone: "faint"; size: "small" }
        ActionButton { visible: root.inputs.length > 1; text: "Clear"; kind: "ghost"; compact: true; enabled: !root.running; onClicked: { root.inputs = []; root.app.setSetting("cameraTestInputs", []) } }
      }
    }

    AppText { text: "Launch arguments"; tone: "dim"; size: "small" }
    TextField { id: argsField; Layout.fillWidth: true; text: root.argsText; enabled: !root.running; placeholderText: "-cameraTestMode 1" }

    AppText { text: "Environment"; tone: "dim"; size: "small" }
    TextField { id: envField; Layout.fillWidth: true; text: root.envText; enabled: !root.running; placeholderText: "Optional: KEY=value, OTHER=value" }

    AppText { text: "Give up after"; tone: "dim"; size: "small" }
    RowLayout {
      spacing: Style.spacing.md
      NumberField { id: timeoutField; from: 5; to: 600; stepSize: 5; value: root.timeoutS; enabled: !root.running }
      AppText { text: "seconds"; tone: "dim"; size: "small" }
    }
  }

  RowLayout {
    Layout.fillWidth: true
    visible: root.devApps.length > 0
    spacing: Style.spacing.lg
    ActionButton {
      text: root.running ? "Running…" : "Run test"
      glyph: Model.Glyph.play; kind: "primary"
      busy: root.running
      disabled: !root.current || root.inputs.length === 0 || !root.svc.connected
      tooltip: !root.svc.connected ? "Connect your iPhone first" : (root.inputs.length ? "" : "Add at least one test file")
      onClicked: root.run()
    }
    ActionButton { visible: root.running; text: "Cancel"; kind: "ghost"; glyph: Model.Glyph.close; onClicked: root.cancel() }
    AppText {
      Layout.fillWidth: true
      visible: !root.running && !!root.current
      text: "Relaunches " + (root.current ? root.current.name : "the app") + " on the iPhone. Keep it unlocked."
      tone: "faint"; size: "small"; elide: Text.ElideRight
    }
  }

  // ---- progress
  SectionHeader { Layout.fillWidth: true; visible: root.steps.length > 0; text: "Run"; detail: root.result ? root.result.seconds + " s" : "" }
  ColumnLayout {
    Layout.fillWidth: true
    visible: root.steps.length > 0
    spacing: Style.spacing.sm
    Repeater {
      model: root.steps
      delegate: RowLayout {
        id: srow
        required property var modelData
        required property int index
        readonly property string st: modelData.state
        Layout.fillWidth: true
        spacing: Style.spacing.lg
        Item {
          implicitWidth: Style.font.iconLarge * 1.4; implicitHeight: Style.font.iconLarge * 1.4
          Spinner { anchors.centerIn: parent; visible: srow.st === "running"; running: visible; size: Style.font.icon; color: Commons.Color.accent }
          Icon {
            anchors.centerIn: parent
            visible: srow.st !== "running"
            glyph: srow.st === "done" ? Model.Glyph.checkCircle : (srow.st === "failed" ? Model.Glyph.error : Model.Glyph.circle)
            size: Style.font.icon
            color: srow.st === "done" ? Theme.success : (srow.st === "failed" ? Theme.error : Theme.faint)
          }
        }
        AppText { text: srow.modelData.title; tone: srow.st === "pending" ? "faint" : "primary"; strong: srow.st === "running" }
        AppText {
          Layout.fillWidth: true
          text: srow.st === "running" && root.transfer && root.transfer.file ? root.transfer.file + (root.transfer.total ? "  ·  " + Math.round(100 * root.transfer.bytes / root.transfer.total) + "%" : "") : (srow.modelData.detail || "")
          tone: srow.st === "failed" ? "error" : "faint"; size: "small"; elide: Text.ElideRight
        }
      }
    }
  }
  ErrorState {
    Layout.fillWidth: true
    visible: !!root.error && root.error.code !== "cancelled"
    app: root.app
    error: root.error
    title: "The camera test couldn't run"
    retryText: "Try again"
    onRetry: root.run()
  }

  // ---- results
  ColumnLayout {
    id: resultsCol
    Layout.fillWidth: true
    visible: !!root.result
    spacing: Style.spacing.xl

    RowLayout {
      Layout.fillWidth: true
      spacing: Style.spacing.lg
      StatusPill {
        level: !root.result ? "info" : (root.result.newCrashes.length ? "error" : (root.result.ok ? "ok" : "warn"))
        text: !root.result ? "" : (root.result.newCrashes.length ? "Crashed" : (root.result.ok ? "Results ready" : (root.result.timedOut ? "Timed out" : "No results")))
      }
      AppText {
        Layout.fillWidth: true
        wrapMode: Text.WordWrap
        text: {
          var r = root.result
          if (!r) return ""
          if (r.done !== null && r.done !== undefined) {
            var n = r.done && r.done.results ? r.done.results.length : null
            return (n !== null ? n + (n === 1 ? " result" : " results") : "done.json") + " from " + (root.current ? root.current.name : "the app") + "."
          }
          if (r.doneRaw) return "The app wrote done.json, but it isn't valid JSON."
          if (r.exited) return "The app closed before writing done.json."
          return "No done.json after " + r.timeout + " seconds. Is the test hook in the app, and is -cameraTestMode in the launch arguments?"
        }
      }
      ActionButton { text: "Open folder"; glyph: Model.Glyph.folderOpen; kind: "ghost"; compact: true; visible: !!root.result; onClicked: root.open(root.result.outputDir) }
    }

    ErrorState {
      Layout.fillWidth: true
      visible: !!root.result && root.result.newCrashes.length > 0
      banner: true
      app: root.app
      error: root.result && root.result.newCrashes.length ? { code: "crash", message: "The app crashed during the test (" + root.result.newCrashes[0].file + ").",
                                                              fix: { label: "Open crash report", page: "logs", tab: "crashes" } } : null
    }

    SectionHeader { Layout.fillWidth: true; visible: root.doneText !== ""; text: "done.json" }
    Rectangle {
      Layout.fillWidth: true
      visible: root.doneText !== ""
      implicitHeight: Math.min(doneEdit.implicitHeight, Style.space(320)) + Style.spacing.lg * 2
      radius: Style.cornerRadius
      color: Theme.surface
      border.width: Style.normalBorderWidth
      border.color: Theme.hairline
      Flickable {
        id: doneFlick
        anchors.fill: parent
        anchors.margins: Style.spacing.lg
        clip: true
        contentWidth: width
        contentHeight: doneEdit.implicitHeight
        boundsBehavior: Flickable.StopAtBounds
        TextEdit {
          id: doneEdit
          width: doneFlick.width
          text: root.doneText
          readOnly: true; selectByMouse: true
          wrapMode: TextEdit.WrapAnywhere
          font.family: Style.font.family; font.pixelSize: Style.font.bodySmall
          color: Commons.Color.foreground
          selectionColor: Util.alpha(Commons.Color.accent, 0.35)
        }
      }
    }

    SectionHeader { Layout.fillWidth: true; visible: root.images.length > 0; text: "Output images"; detail: String(root.images.length) }
    Flow {
      Layout.fillWidth: true
      visible: root.images.length > 0
      spacing: Style.spacing.md
      Repeater {
        model: root.images
        delegate: Rectangle {
          required property var modelData
          width: Style.space(180); height: Style.space(135)
          radius: Style.cornerRadius
          color: Theme.surfaceRaised
          clip: true
          Image { anchors.fill: parent; source: "file://" + modelData; fillMode: Image.PreserveAspectFit; asynchronous: true; sourceSize.width: width * 2 }
          HoverHandler { id: ih; cursorShape: Qt.PointingHandCursor }
          TapHandler { onTapped: root.open(modelData) }
          PanelToolTip { visible: ih.hovered; text: modelData.substring(modelData.lastIndexOf("/") + 1) + "  ·  click to open" }
        }
      }
    }

    SectionHeader { Layout.fillWidth: true; text: "App log during the test"; detail: root.result ? String(root.result.logCount) : "" }
    LogView {
      Layout.fillWidth: true
      Layout.preferredHeight: Style.space(240)
      showToolbar: false
      showMeta: false
      lines: root.result ? root.result.logLines : []
      emptyText: "Nothing logged by the app."
    }
  }
}
