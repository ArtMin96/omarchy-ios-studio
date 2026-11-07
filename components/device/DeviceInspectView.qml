import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../common"
import "../../core"
import "../../lib/Model.js" as Model

// The Screen tab's Elements and Audit panes, beside the phone (DeviceScreenView picks the pane).
// Elements: the app on screen as VoiceOver reads it (deviced `ui.tree`), with a filter; a row's
// Press taps that element on the real iPhone (`ui.press`), says what changed and reads the screen
// again. Audit: Apple's accessibility audit (`ui.audit`), issues grouped by type; an issue whose
// `details.elementId` is in the last read links to that row (showElements asks for the pane).
// `highlight`/`space` are the cursor row's frame and the screen size in points, for the phone's
// outline (DevicePhoneFrame.highlight); without a known screen size no outline is drawn. Each read
// also takes a screenshot (its size gives `space` when ui.tree has no `screen`), so the outline
// matches what's shown. A `no-app-in-front` error reads as the empty state. j/k move, Enter presses or opens the issue's element (handleKey, from
// DevicePage), / focuses the filter (focusFilter).
//   DeviceInspectView { app: root.app; section: "elements"; onDevModeHelp: …; onShowElements: … }
ColumnLayout {
  id: root
  required property var app
  readonly property var svc: app.device
  property string section: "elements"     // "elements" | "audit"
  signal devModeHelp()

  // Why nothing can be read now ("" = ready). The page hides the whole tab while disconnected.
  readonly property bool devModeOff: svc.info.developerMode === false
  readonly property string blocked: !svc.connected ? "Connect your iPhone to inspect its screen."
                                  : (devModeOff ? "Reading the screen needs Developer Mode on the iPhone." : "")

  // ---------------------------------------------------------------- elements
  // The last ui.tree result; read on demand (Read the screen, Refresh, after a press). No app in front
  // reads as an empty screen.
  Resource {
    id: treeRes
    fetch: function(done, fail) {
      root.svc.request("ui.tree", {}, function(ok, d) {
        if (ok) done(d)
        else if (root.noApp(d)) done({ elements: [], count: 0 })
        else fail(d)
      })
    }
    onSucceeded: function(d) {
      if (d.screen && d.screen.w > 0 && d.screen.h > 0) root.knownScreen = d.screen
      root.cursor = Math.min(root.cursor, root.shown.length - 1)
    }
  }
  // the last read's outcome: idle | loading (a read is on its way) | ready | error (the last one failed;
  // the elements read before stay listed)
  readonly property string treeStatus: treeRes.busy ? "loading" : treeRes.status === "idle" ? "idle" : treeRes.error ? "error" : "ready"
  readonly property var tree: treeRes.data
  readonly property var treeError: treeRes.error
  property int cursor: -1
  property real _since: 0                 // when the current read started (ms)
  property real _now: Date.now()
  Timer { interval: 500; repeat: true; running: root.treeStatus === "loading" || root.auditStatus === "loading"; onTriggered: root._now = Date.now() }
  readonly property int waited: Math.max(0, Math.round((_now - _since) / 1000))

  readonly property var elements: tree && tree.elements ? tree.elements : []
  readonly property var shown: {
    var q = filterField.text
    return elements.filter(function(e) {
      return Model.matches([e.label, e.value, e.role, e.identifier, e.hint].join(" "), q)
    })
  }
  // The walk stopped early: at the limit (truncated) or before it reached the end (complete false).
  readonly property bool partial: !!tree && (tree.truncated === true || tree.complete === false)
  readonly property string partialReason: {
    if (!tree) return ""
    var by = tree.stoppedBy || (tree.truncated ? "max" : "")
    if (by === "max") return "The screen has more than that. Filter to find an element, or scroll the app and read it again."
    if (by === "timeout") return "Reading stopped after " + Math.round((tree.elapsedMs || 0) / 1000) + " s. Read it again once the app is still."
    if (by === "no-focus") return "VoiceOver's focus couldn't move further on this screen. Close any alert or keyboard and read it again."
    if (by === "foreign") return "Stopped: the focus left the app (an alert may be open)."
    return "The screen changed while it was read. Read it again once the app is still."
  }

  // Some iOS versions (27.3) report no element positions at all: say so instead of outlining nothing.
  readonly property bool noPositions: elements.length > 0 && elements.every(function(e) { return !e.frame })

  // The app that was read: its name from the app list, else the bundle id (without xtool's
  // "XTL-<TEAM>." prefix).
  function appName(bundleId) {
    var id = String(bundleId || "")
    if (id === "") return ""
    var strip = function(b) { return String(b || "").replace(/^XTL-[^.]+\./, "") }
    var plain = strip(id)
    var apps = root.svc.apps || []
    for (var i = 0; i < apps.length; i++)
      if (strip(apps[i].bundleId) === plain) return apps[i].name || plain
    return plain
  }

  // ui.tree / ui.audit found no app in front: shown like an empty screen, not as an error.
  function noApp(err) { return !!err && err.code === "no-app-in-front" }

  // Audit issues name their element by `details.elementId`, an id from the last read.
  function issueElementId(it) { return it && it.details && it.details.elementId ? String(it.details.elementId) : "" }
  function elementIndex(id) {
    if (!id) return -1
    for (var i = 0; i < elements.length; i++) if (elements[i].id === id) return i
    return -1
  }
  // Elements pane with that element under the cursor (an audit issue's "Show in Elements").
  signal showElements()
  function revealElement(id) {
    var i = elementIndex(id)
    if (i < 0) return
    filterField.clear()
    root.showElements()
    root.cursor = i
    Qt.callLater(function() { list.positionViewAtIndex(i, ListView.Center) })
  }

  // The whole screen in points, for the outline: ui.tree's `screen` when it has one, else the
  // screenshot's pixel size over the iPhone's scale (every iPhone is @3x but the 750/828 px wide
  // @2x ones; a smaller image is already in points). Unknown = no outline, never a misplaced one.
  property var knownScreen: null
  property var shotSize: null             // {w, h} in pixels, from the last screenshot taken here
  readonly property var space: {
    if (knownScreen) return knownScreen
    if (!shotSize || !(shotSize.w > 0) || !(shotSize.h > 0)) return null
    var scale = shotSize.w < 640 ? 1 : (shotSize.w <= 828 ? 2 : 3)
    return { w: shotSize.w / scale, h: shotSize.h / scale }
  }
  readonly property var highlight: {
    if (section === "audit") {
      var it = auditCursor >= 0 && auditCursor < auditRows.length ? auditRows[auditCursor] : null
      if (!it) return null
      if (it.element && it.element.frame) return it.element.frame
      var linked = elementIndex(issueElementId(it))
      return linked >= 0 ? elements[linked].frame || null : null
    }
    return cursor >= 0 && cursor < shown.length ? shown[cursor].frame || null : null
  }

  // A screenshot for the phone (unless the live view runs) whose size gives `space`.
  function shoot() {
    if (root.svc.mirroring && root.space) return
    root.svc.screenshot(function(ok, d) { if (ok && d.width > 0 && d.height > 0) root.shotSize = { w: d.width, h: d.height } })
  }

  function refresh() {
    if (root.blocked !== "" || treeRes.busy) return
    root._since = Date.now(); root._now = root._since
    root.shoot()
    treeRes.load(true)
  }

  // ---------------------------------------------------------------- press
  property string pressingId: ""
  property string pressingLabel: ""
  property var pressResult: null          // { label, summary }
  property var pressError: null

  function labelOf(e) { return e && (e.label || e.identifier || e.value) ? String(e.label || e.identifier || e.value) : "the element" }

  // One short line per kind of change:
  //   Added: “Welcome”, “Sign out”
  //   Removed: “Sign in”
  //   Changed: Volume 40 % → 60 %
  function changeSummary(c) {
    if (!c) return ""
    var lines = []
    var list = function(a) {
      a = (a || []).filter(function(s) { return s !== null && s !== undefined && String(s) !== "" })
      return a.slice(0, 2).map(function(s) { return "“" + s + "”" }).join(", ") + (a.length > 2 ? " and " + (a.length - 2) + " more" : "")
    }
    if (c.added && c.added.length) lines.push("Added: " + list(c.added))
    if (c.removed && c.removed.length) lines.push("Removed: " + list(c.removed))
    var cv = c.changedValues || []
    if (cv.length) lines.push("Changed: " + (cv[0].label || "a value") + " " + (cv[0].from || "empty") + " → " + (cv[0].to || "empty")
                              + (cv.length > 1 ? " and " + (cv.length - 1) + " more" : ""))
    return lines.length ? lines.join("\n") : "Nothing on the screen changed."
  }

  function press(e) {
    if (!e || root.pressingId !== "" || root.blocked !== "") return
    root.pressingId = e.id
    root.pressingLabel = root.labelOf(e)
    root.pressError = null
    root.pressResult = null
    root.svc.request("ui.press", { id: e.id }, function(ok, d) {
      root.pressingId = ""
      if (!ok && root.noApp(d)) { root.refresh(); return }   // the empty state says what to do
      if (!ok) { root.pressError = d; return }
      root.pressResult = { label: root.labelOf(d.pressed || e), summary: root.changeSummary(d.changes) }
      root.refresh()
    })
  }

  // ---------------------------------------------------------------- audit
  // The last ui.audit result, run on demand; { noApp: true } when no app was in front.
  Resource {
    id: auditRes
    fetch: function(done, fail) {
      root.svc.request("ui.audit", {}, function(ok, d) {
        if (ok) done(d)
        else if (root.noApp(d)) done({ noApp: true })
        else fail(d)
      })
    }
    onSucceeded: root.auditCursor = -1
  }
  // idle | loading | ready | no-app | error (the last run failed)
  readonly property string auditStatus: auditRes.busy ? "loading" : auditRes.status === "idle" ? "idle"
                                      : auditRes.error ? "error" : (auditRes.data && auditRes.data.noApp ? "no-app" : "ready")
  readonly property var audit: auditRes.data && !auditRes.data.noApp ? auditRes.data : null
  readonly property var auditError: auditRes.error
  property int auditCursor: -1
  property real _auditSince: 0
  readonly property int auditWaited: Math.max(0, Math.round((_now - _auditSince) / 1000))

  readonly property var typeInfo: ({
    "contrast": { label: "Contrast", glyph: Model.Glyph.contrast },
    "dynamic-type": { label: "Dynamic Type", glyph: Model.Glyph.textSize },
    "clipped-text": { label: "Clipped text", glyph: Model.Glyph.clipped },
    "hit-region": { label: "Small hit area", glyph: Model.Glyph.hitArea },
    "missing-label": { label: "Missing label", glyph: Model.Glyph.label }
  })
  function humanize(s) {
    s = String(s || "").replace(/^(UIAccessibility)?AuditType/, "").replace(/([a-z])([A-Z])/g, "$1 $2").replace(/[-_]+/g, " ").trim()
    return s ? s.charAt(0).toUpperCase() + s.slice(1).toLowerCase() : "Other"
  }
  function typeLabel(t) { return typeInfo[t] ? typeInfo[t].label : humanize(t) }
  function typeGlyph(t) { return typeInfo[t] ? typeInfo[t].glyph : Model.Glyph.warn }

  // Issues grouped by type, the biggest group first; `offset` is the group's first index in auditRows.
  readonly property var auditGroups: {
    var issues = audit && audit.issues ? audit.issues : []
    var byType = {}, order = []
    for (var i = 0; i < issues.length; i++) {
      var t = issues[i].type || issues[i].rawType || "other"
      if (!byType[t]) { byType[t] = []; order.push(t) }
      byType[t].push(issues[i])
    }
    order.sort(function(a, b) { return byType[b].length - byType[a].length })
    var out = [], off = 0
    for (var k = 0; k < order.length; k++) {
      out.push({ type: order[k], label: typeLabel(order[k]), glyph: typeGlyph(order[k]), issues: byType[order[k]], offset: off })
      off += byType[order[k]].length
    }
    return out
  }
  readonly property var auditRows: {
    var r = []
    for (var i = 0; i < auditGroups.length; i++) r = r.concat(auditGroups[i].issues)
    return r
  }
  readonly property var unsupportedLabels: (audit && audit.unsupported ? audit.unsupported : []).map(function(t) { return root.typeLabel(t) })

  function runAudit() {
    if (root.blocked !== "" || auditRes.busy) return
    root._auditSince = Date.now(); root._now = root._auditSince
    root.shoot()
    auditRes.load(true)
  }

  // ---------------------------------------------------------------- keys (DevicePage.handleKey)
  function handleKey(a) {
    var elementsPane = root.section === "elements"
    var n = elementsPane ? root.shown.length : root.auditRows.length
    var cur = elementsPane ? root.cursor : root.auditCursor
    var next = cur
    if (a === "down") next = Math.min(n - 1, cur + 1)
    else if (a === "up") next = Math.max(0, cur - 1)
    else if (a === "home") next = 0
    else if (a === "end") next = n - 1
    else if (a === "activate" && cur >= 0 && cur < n) {
      if (elementsPane) root.press(root.shown[cur])
      else root.revealElement(root.issueElementId(root.auditRows[cur]))
      return true
    }
    else return false
    if (n === 0) return false
    if (elementsPane) { root.cursor = next; list.positionViewAtIndex(next, ListView.Contain) }
    else { root.auditCursor = next; root.revealIssue(next) }
    return true
  }
  function focusFilter() { if (root.section === "elements" && root.tree) filterField.focusField() }

  // The height beside the phone (DeviceScreenView sets it). Lists scroll inside what's left of it
  // so the phone stays next to them.
  property real availableHeight: Style.space(480)
  function listRoom(pane, list, below) {
    return Math.max(Style.space(220), root.availableHeight - pane.y - list.y - below)
  }

  spacing: Style.spacing.lg

  // ================================================================ blocked
  EmptyState {
    Layout.fillWidth: true
    visible: root.blocked !== ""
    compact: true
    level: "warn"
    glyph: root.devModeOff ? Model.Glyph.developer : Model.Glyph.wifiOff
    title: root.section === "audit" ? "Can't run the audit yet" : "Can't read the screen yet"
    text: root.blocked
    actionText: root.devModeOff ? "How to turn it on" : ""
    actionGlyph: Model.Glyph.help
    onAction: root.devModeHelp()
  }

  // ================================================================ elements
  ColumnLayout {
    id: elementsPane
    Layout.fillWidth: true
    visible: root.blocked === "" && root.section === "elements"
    spacing: Style.spacing.lg

    // first visit: nothing read yet
    EmptyState {
      Layout.fillWidth: true
      visible: root.treeStatus === "idle"
      compact: true
      glyph: Model.Glyph.inspect
      title: "See what's on the screen"
      text: "Lists the buttons, texts and other elements of the app that's open on your iPhone, in the order VoiceOver reads them."
      actionText: "Read the screen"; actionGlyph: Model.Glyph.inspect
      onAction: root.refresh()
    }

    // toolbar: filter + read again
    RowLayout {
      Layout.fillWidth: true
      visible: root.elements.length > 0
      spacing: Style.spacing.lg
      FilterField {
        id: filterField
        Layout.fillWidth: true
        placeholder: "Filter " + root.elements.length + " elements by label, value, role or identifier"
        onTextChanged: root.cursor = -1
      }
      IconButton {
        glyph: Model.Glyph.refresh
        tooltip: "Read the screen again"
        busy: root.treeStatus === "loading"
        onClicked: root.refresh()
      }
    }

    // reading (with nothing listed yet; over a list the toolbar's spinner says it)
    LoadingState {
      Layout.fillWidth: true
      visible: root.treeStatus === "loading" && root.elements.length === 0
      text: "Reading the screen…"
      detail: "Going through every element on the iPhone takes a few seconds" + (root.waited >= 2 ? " (" + root.waited + " s so far)." : ".")
    }

    ErrorState {
      Layout.fillWidth: true
      visible: root.treeStatus === "error"
      banner: root.elements.length > 0
      compact: true
      app: root.app
      title: "Couldn't read the screen"
      error: root.treeError
      onRetry: root.refresh()
    }

    // a press takes several seconds on the phone (the tap, then a fresh read for the diff)
    InspectNote {
      Layout.fillWidth: true
      visible: root.pressingId !== ""
      busy: true
      title: "Pressing “" + root.pressingLabel + "”…"
      text: "Tapping it on your iPhone, then reading the screen again to see what changed."
    }

    // what the last press did
    InspectNote {
      Layout.fillWidth: true
      visible: !!root.pressResult
      glyph: Model.Glyph.tap
      glyphColor: Theme.levelColor("ok")
      title: root.pressResult ? "Pressed “" + root.pressResult.label + "”" : ""
      text: root.pressResult ? root.pressResult.summary : ""
      dismissable: true
      onDismissed: root.pressResult = null
    }
    ErrorState {
      Layout.fillWidth: true
      visible: !!root.pressError
      banner: true
      app: root.app
      error: root.pressError
      title: "Couldn't press it"
      // Ids last until the screen changes: a stale id needs a fresh read, as do the other misses
      // (no-element, ambiguous-label, wrong-app, press-failed, ax-unavailable).
      retryText: "Refresh"
      onRetry: { root.pressError = null; root.refresh() }
    }

    // stopped early
    InspectNote {
      Layout.fillWidth: true
      visible: root.partial && root.treeStatus !== "error"
      glyph: Model.Glyph.warn
      glyphColor: Theme.levelColor("warn")
      readonly property int n: root.tree ? (root.tree.count || root.elements.length) : 0
      title: "Partial: stopped after " + n + (n === 1 ? " element" : " elements")
      text: root.partialReason
    }

    // nothing on screen
    EmptyState {
      Layout.fillWidth: true
      visible: root.treeStatus === "ready" && root.elements.length === 0
      compact: true
      glyph: Model.Glyph.apps
      title: "No app on the screen"
      text: "Open an app on your iPhone, then press Refresh."
      actionText: "Refresh"; actionGlyph: Model.Glyph.refresh
      onAction: root.refresh()
    }
    EmptyState {
      Layout.fillWidth: true
      visible: root.elements.length > 0 && root.shown.length === 0
      compact: true
      glyph: Model.Glyph.filter
      title: "No element matches “" + filterField.text + "”"
      text: "The filter looks at labels, values, roles and identifiers."
      actionText: "Clear filter"; actionGlyph: Model.Glyph.close
      onAction: filterField.clear()
    }

    ListView {
      id: list
      Layout.fillWidth: true
      // room for the count line under it
      Layout.preferredHeight: Math.min(contentHeight, root.listRoom(elementsPane, list, listFooter.implicitHeight + Style.spacing.lg))
      visible: root.shown.length > 0
      clip: true
      spacing: Style.spacing.xxs
      boundsBehavior: Flickable.StopAtBounds
      ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
      model: root.shown
      delegate: DeviceInspectRow {
        id: erow
        required property var modelData
        required property int index
        readonly property var el: modelData
        width: ListView.view.width
        hasCursor: root.cursor === index
        glyph: root.roleGlyph(el)
        title: el.label ? String(el.label) : "No label"
        titleMuted: !el.label
        value: el.value !== null && el.value !== undefined && String(el.value) !== "" && el.value !== el.label ? String(el.value) : ""
        secondary: {
          var p = [root.roleName(el.role)]
          if (el.identifier) p.push(el.identifier)
          if (el.enabled === false) p.push("disabled")
          return p.join(" · ")
        }
        opacity: el.enabled === false ? 0.7 : 1
        pressable: true
        pressBusy: root.pressingId === el.id
        pressEnabled: root.pressingId === "" && root.treeStatus !== "loading"
        pressTooltip: root.treeStatus === "loading" ? "Wait for the screen to be read" : "Press on your iPhone (taps the real screen)"
        onHovered: function(h) { if (h) root.cursor = index }
        onPressRequested: root.press(el)
      }
    }

    // which app was read, how many elements and how long it took; the keys; missing positions
    ColumnLayout {
      id: listFooter
      Layout.fillWidth: true
      visible: root.shown.length > 0
      spacing: Style.spacing.xxs
      RowLayout {
        Layout.fillWidth: true
        spacing: Style.spacing.lg
        AppText {
          Layout.fillWidth: true
          text: {
            var app = root.tree ? root.appName(root.tree.bundleId) : ""
            return (app ? app + " · " : "")
                   + (root.shown.length < root.elements.length ? root.shown.length + " of " : "") + root.elements.length + (root.elements.length === 1 ? " element" : " elements")
                   + (root.tree && root.tree.elapsedMs ? ", read in " + (root.tree.elapsedMs / 1000).toFixed(1) + " s" : "")
          }
          size: "caption"; tone: "dim"
          elide: Text.ElideRight
        }
        AppText { text: "Enter presses on your iPhone"; size: "caption"; tone: "faint" }
      }
      AppText {
        Layout.fillWidth: true
        visible: root.noPositions
        text: "This iOS version doesn't report element positions, so rows aren't outlined on the phone. Audit issues are."
        size: "caption"; tone: "faint"
        wrapMode: Text.WordWrap
      }
    }
  }

  // ================================================================ audit
  ColumnLayout {
    id: auditPane
    Layout.fillWidth: true
    visible: root.blocked === "" && root.section === "audit"
    spacing: Style.spacing.lg

    EmptyState {
      Layout.fillWidth: true
      visible: root.auditStatus === "idle"
      compact: true
      glyph: Model.Glyph.accessibility
      title: "Check the screen's accessibility"
      text: "Apple's audit looks for low contrast, text that doesn't grow with Dynamic Type, clipped text, small hit areas and missing labels in the app that's open."
      actionText: "Run audit"; actionGlyph: Model.Glyph.play
      onAction: root.runAudit()
    }

    EmptyState {
      Layout.fillWidth: true
      visible: root.auditStatus === "no-app"
      compact: true
      glyph: Model.Glyph.apps
      title: "No app on the screen"
      text: "Open an app on your iPhone, then press Refresh."
      actionText: "Refresh"; actionGlyph: Model.Glyph.refresh
      onAction: root.runAudit()
    }

    LoadingState {
      Layout.fillWidth: true
      visible: root.auditStatus === "loading"
      text: "Running the accessibility audit…"
      detail: "Keep the app open on the iPhone" + (root.auditWaited >= 2 ? " (" + root.auditWaited + " s so far)." : ".")
    }

    ErrorState {
      Layout.fillWidth: true
      visible: root.auditStatus === "error"
      compact: true
      app: root.app
      title: "Couldn't run the audit"
      error: root.auditError
      onRetry: root.runAudit()
    }

    // summary + run again
    RowLayout {
      Layout.fillWidth: true
      visible: root.auditStatus === "ready"
      spacing: Style.spacing.lg
      AppText {
        Layout.fillWidth: true
        text: root.auditRows.length === 0 ? "No problems found"
              : root.auditRows.length + (root.auditRows.length === 1 ? " issue" : " issues") + " in " + root.auditGroups.length + (root.auditGroups.length === 1 ? " category" : " categories")
        strong: true
        elide: Text.ElideRight
      }
      IconButton { glyph: Model.Glyph.refresh; tooltip: "Run the audit again"; onClicked: root.runAudit() }
    }

    EmptyState {
      Layout.fillWidth: true
      visible: root.auditStatus === "ready" && root.auditRows.length === 0
      compact: true
      level: "ok"
      glyph: Model.Glyph.checkCircle
      title: "Nothing to fix on this screen"
      text: "Open other screens of the app and run the audit again."
    }

    Flickable {
      id: issuesFlick
      Layout.fillWidth: true
      // room for the notes under it
      Layout.preferredHeight: Math.min(contentHeight, root.listRoom(auditPane, issuesFlick, auditNotes.implicitHeight > 0 ? auditNotes.implicitHeight + Style.spacing.lg : 0))
      visible: root.auditStatus === "ready" && root.auditRows.length > 0
      clip: true
      contentWidth: width
      contentHeight: groups.implicitHeight
      boundsBehavior: Flickable.StopAtBounds
      ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

      ColumnLayout {
        id: groups
        width: issuesFlick.width
        spacing: Style.spacing.lg
        Repeater {
          model: root.auditGroups
          delegate: ColumnLayout {
            id: group
            required property var modelData
            Layout.fillWidth: true
            spacing: Style.spacing.xxs
            SectionHeader { Layout.fillWidth: true; text: group.modelData.label; detail: String(group.modelData.issues.length) }
            Repeater {
              model: group.modelData.issues
              delegate: DeviceInspectRow {
                required property var modelData
                required property int index
                readonly property int flat: group.modelData.offset + index
                Layout.fillWidth: true
                hasCursor: root.auditCursor === flat
                glyph: group.modelData.glyph
                glyphColor: Theme.levelColor("warn")
                title: modelData.message || root.typeLabel(group.modelData.type)
                titleLines: 3
                secondary: {
                  var lbl = modelData.element && modelData.element.label ? modelData.element.label
                          : (linkedId !== "" ? root.elements[root.elementIndex(linkedId)].label : "")
                  if (lbl) return "“" + lbl + "”"
                  return modelData.element || linkedId !== "" ? "An element without a label" : "The whole screen"
                }
                readonly property string linkedId: root.elementIndex(root.issueElementId(modelData)) >= 0 ? root.issueElementId(modelData) : ""
                clickable: linkedId !== ""
                clickTooltip: "Show in Elements"
                onHovered: function(h) { if (h) root.auditCursor = flat }
                onClicked: root.revealElement(linkedId)
              }
            }
          }
        }
      }
    }

    // stopped early / types this iOS can't check
    ColumnLayout {
      id: auditNotes
      Layout.fillWidth: true
      visible: root.auditStatus === "ready" && !!root.audit && (root.audit.timedOut === true || root.unsupportedLabels.length > 0)
      spacing: Style.spacing.lg
      InspectNote {
        Layout.fillWidth: true
        visible: root.auditStatus === "ready" && !!root.audit && root.audit.timedOut === true
        glyph: Model.Glyph.warn
        glyphColor: Theme.levelColor("warn")
        title: "Partial results"
        text: "The audit stopped at its time limit, so this screen may have more issues. Run it again to check the rest."
      }

      InspectNote {
        Layout.fillWidth: true
        visible: root.auditStatus === "ready" && root.unsupportedLabels.length > 0
        glyph: Model.Glyph.info
        glyphColor: Theme.faint
        title: ""
        text: "Not available on this iPhone's iOS: " + root.unsupportedLabels.join(", ") + "."
      }
    }
  }

  // Scroll the issues list so row `i` is visible.
  function revealIssue(i) {
    for (var g = 0; g < groups.children.length; g++) {
      var kids = groups.children[g].children || []
      for (var k = 0; k < kids.length; k++) {
        if (kids[k].flat !== i) continue
        var p = kids[k].mapToItem(groups, 0, 0)
        if (p.y < issuesFlick.contentY) issuesFlick.contentY = p.y
        else if (p.y + kids[k].height > issuesFlick.contentY + issuesFlick.height) issuesFlick.contentY = p.y + kids[k].height - issuesFlick.height
        return
      }
    }
  }

  // ---------------------------------------------------------------- roles
  function roleName(r) {
    var s = String(r || "").replace(/^AX/, "").replace(/([a-z])([A-Z])/g, "$1 $2").replace(/[-_]+/g, " ").trim()
    return s ? s.charAt(0).toUpperCase() + s.slice(1).toLowerCase() : "Element"
  }
  function roleGlyph(e) {
    var s = (String(e.role || "") + " " + (e.traits || []).join(" ")).toLowerCase()
    var G = Model.Glyph
    if (/search|text ?field|secure|text ?view|textentry|keyboard/.test(s)) return G.textbox
    if (/switch|toggle|checkbox/.test(s)) return G.toggle
    if (/slider|adjustable|stepper|picker/.test(s)) return G.slider
    if (/link/.test(s)) return G.linkVariant
    if (/tab/.test(s)) return G.tab
    if (/button/.test(s)) return G.tap
    if (/header|heading/.test(s)) return G.header
    if (/image|icon/.test(s)) return G.image
    if (/cell|row|list|table|collection/.test(s)) return G.list
    if (/text|label/.test(s)) return G.text
    return G.element
  }

  // A slim note with the glyph on its first line (press results, partial reads, unsupported types).
  component InspectNote: Item {
    id: note
    property string glyph: ""
    property color glyphColor: Theme.dim
    property string title: ""
    property string text: ""
    property bool dismissable: false
    property bool busy: false             // a spinner in place of the glyph
    signal dismissed()
    FontMetrics { id: noteMetrics; font: noteFirst.font }
    implicitHeight: noteRow.implicitHeight + Style.spacing.md * 2
    RowLayout {
      id: noteRow
      anchors.fill: parent
      anchors.topMargin: Style.spacing.md
      anchors.bottomMargin: Style.spacing.md
      anchors.leftMargin: Style.spacing.rowPaddingX
      anchors.rightMargin: Style.spacing.md
      spacing: Style.spacing.xl
      Item {
        Layout.alignment: Qt.AlignTop
        Layout.preferredWidth: Style.font.iconLarge * 1.4
        Layout.preferredHeight: noteMetrics.height
        Icon { anchors.centerIn: parent; visible: !note.busy; glyph: note.glyph; color: note.glyphColor; size: Style.font.icon }
        Spinner { anchors.centerIn: parent; visible: note.busy; running: note.busy; size: Style.font.icon }
      }
      ColumnLayout {
        Layout.fillWidth: true
        Layout.alignment: Qt.AlignTop
        spacing: Style.spacing.xxs
        AppText {
          id: noteFirst
          Layout.fillWidth: true
          text: note.title !== "" ? note.title : note.text
          tone: note.title !== "" ? "primary" : "faint"
          size: note.title !== "" ? "body" : "small"
          wrapMode: Text.WordWrap
        }
        AppText {
          Layout.fillWidth: true
          visible: note.title !== "" && note.text !== ""
          text: note.text
          size: "small"; tone: "dim"
          wrapMode: Text.WordWrap; maximumLineCount: 4; elide: Text.ElideRight
        }
      }
      // Centred on the first line like the glyph (the button is taller than the line).
      Item {
        Layout.alignment: Qt.AlignTop
        Layout.preferredWidth: dismissButton.implicitWidth
        Layout.preferredHeight: noteMetrics.height
        visible: note.dismissable
        IconButton {
          id: dismissButton
          anchors.centerIn: parent
          glyph: Model.Glyph.close
          tooltip: "Dismiss"
          onClicked: note.dismissed()
        }
      }
    }
  }
}
