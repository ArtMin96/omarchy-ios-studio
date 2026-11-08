import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../common"
import "../../core"
import "../../lib/Model.js" as Model

// "Packages…" sheet (projects slice): the Swift packages a project depends on (`packages`, helper/iosstudio/features/packages/).
// One sheet, several views:
//   list     the declared packages (name, URL or path, requirement, resolved version, who uses them) with Update
//            and Remove per row; Add package… and Update all above; a warning with Resolve when some aren't
//            fetched yet. Update and resolve run inline (progress line, Cancel), then "2 packages updated: …".
//   add      URL, version rule (Up to next major/minor, Exact, Branch, Revision), version, target.
//   job      the add's steps as they stream (Adding → Fetching → Reading products → Linking), cancellable.
//   products the package has several libraries: pick which to link (`packages add-product`).
//   added    what was added, the Package.swift diff and Check build (a compile-only build in the build panel).
//   remove   the dry run's diff (DiffView) and "Remove <name>"; an edit that wouldn't parse is refused.
// Rows' actions are disabled (tooltip says why) while an update, resolve or remove runs.
//   ProjectPackagesSheet { id: packagesSheet; app: page.app; svc: page.svc; onCheckBuild: function(p) { … } }
//   packagesSheet.openFor(project)
Sheet {
  id: root

  property var app: null
  property var svc: null
  property var project: ({})
  property string view: "list"            // list | add | job | products | added | remove

  // ---- list: the `packages list` answer
  Resource {
    id: listRes
    fetch: (done, fail) => root.svc.packagesList(root.project, done, fail)
    onSucceeded: function(d) {
      if (root.target === "" || root.targets.map(function(x) { return x.name }).indexOf(root.target) < 0)
        root.target = d.app_target || (root.targets.length ? root.targets[0].name : "")
      root._loadAgainIfAsked()
    }
    onFailed: root._loadAgainIfAsked()
  }
  readonly property var list: listRes.data
  property bool _loadAgain: false         // load() came while one ran: that one may predate the change
  property int _ticket: 0                 // drops dry-run answers for a removal the sheet no longer shows
  readonly property var packages: list && list.packages ? list.packages : []
  readonly property var targets: list && list.targets ? list.targets : []
  readonly property var unresolved: list && list.unresolved ? list.unresolved : []
  readonly property bool needsResolve: unresolved.length > 0 || (!!list && packages.length > 0 && list.resolved_file === false)

  // ---- streamed jobs: add, update, resolve (one at a time)
  // {kind, title, pkg, steps: [{id, title, state, detail}], state: running|done|failed|cancelled, detail, data, error}
  property var job: null
  readonly property bool jobRunning: !!job && job.state === "running"
  property var notice: null               // update/resolve finished: {kind, ok, changes, error, demo}

  // ---- add form
  property string rule: "from"
  property string target: ""
  property bool urlTouched: false
  property bool addTried: false
  readonly property string urlText: urlField.text.trim()
  readonly property string versionText: versionField.text.trim()
  readonly property bool isLocal: /^(\/|~|\.\.?\/)/.test(urlText)
  readonly property string urlProblem: {
    var u = root.urlText
    if (u === "") return "Paste the package's Git URL."
    if (!root.isLocal && !/^(https?:\/\/|ssh:\/\/|git@)[^\s]+$/.test(u)) return "Use an https:// or git@ URL, or a local folder path."
    var have = root._sameUrl(u)
    if (have) return "This project already uses it as " + root.pkgName(have) + "."
    return ""
  }
  readonly property string versionProblem: {
    if (root.isLocal) return ""
    var v = root.versionText
    if (root.rule === "branch") return v === "" ? "Enter a branch name." : (/\s/.test(v) ? "A branch name has no spaces." : "")
    if (root.rule === "revision") return /^[0-9a-fA-F]{7,40}$/.test(v) ? "" : "Enter a commit hash (7 to 40 hex characters)."
    if (v === "") return ""                    // the newest release (the helper looks it up)
    return /^v?\d+\.\d+\.\d+([-+][0-9A-Za-z.-]+)?$/.test(v) ? "" : "Use a version with three numbers, like 1.2.0."
  }
  readonly property string addBlocker: urlProblem !== "" ? urlProblem : versionProblem !== "" ? versionProblem
                                       : root.jobRunning ? "Wait for the running " + root.job.kind + " to finish" : ""

  // ---- products picker
  property var products: []               // [{name, type}]
  property var picked: ({})               // name -> true
  property bool linking: false
  property var linkError: null
  readonly property var pickedNames: products.map(function(p) { return p.name }).filter(function(n) { return root.picked[n] === true })

  // ---- added
  property var added: null                // {package, products: [names], target, diff, demo}

  // ---- remove
  property var removing: null             // the package row being removed
  property string removePhase: "idle"     // loading | ready | invalid | error | removing
  property var removeData: null
  property var removeError: null

  readonly property string lockReason: root.jobRunning ? "Wait for the running " + root.job.kind + " to finish"
                                       : root.removePhase === "removing" ? "Wait for the removal to finish" : ""

  signal checkBuild(var project)

  title: "Packages · " + (project && project.name ? project.name : "project")
  subtitle: view === "add" ? "Paste the package's Git URL. iOS Studio adds, fetches and links it."
          : view === "remove" && removePhase === "invalid" ? "This is the edit it would make, so you can make it by hand."
          : view === "remove" ? "Shows the change to Package.swift first. Nothing is written until you remove it."
          : view === "job" && job && job.state === "failed" ? "Adding " + (job.pkg || "the package") + " didn't work. Back changes the URL or version."
          : view === "job" && job && job.state === "cancelled" ? "Adding " + (job.pkg || "the package") + " was cancelled."
          : view === "job" ? "Adding " + (job && job.pkg ? job.pkg : "the package") + ". Cancel puts Package.swift back as it was."
          : view === "products" ? (job && job.pkg ? job.pkg : "The package") + " has " + products.length
                                  + " libraries. Pick the ones " + (target || "your app") + " imports."
          : view === "added" ? "Package.swift has the package now. Check build makes sure it builds for iPhone."
          : "Requirements from Package.swift, versions from Package.resolved."
  glyph: Model.Glyph.packages
  preferredWidth: Style.space(760)
  busy: root.jobRunning || root.linking || root.removePhase === "removing"

  // ---------------------------------------------------------------- helpers
  function pkgId(p) { return p ? (p.identity || p.name || "") : "" }
  function pkgName(p) { return p ? (p.name || p.identity || "") : "" }
  function pkgWhere(p) {
    var w = p ? (p.url || p.path || "") : ""
    var home = root.app && root.app.home ? root.app.home : ""
    return home && w.indexOf(home) === 0 ? "~" + w.substring(home.length) : w
  }
  // a package identity ("confetti-swiftui") as the list shows it ("ConfettiSwiftUI")
  function nameOf(id) {
    for (var i = 0; i < root.packages.length; i++) if (root.packages[i].identity === id) return root.pkgName(root.packages[i])
    return id
  }
  function reqText(p) {
    var r = p && p.requirement ? p.requirement : null
    if (!r) return ""
    if (r.text) return r.text
    if (r.rule === "local") return "local"
    if (r.rule === "branch") return "branch " + (r.branch || "")
    if (r.rule === "revision") return "revision " + String(r.revision || "").substring(0, 7)
    if (r.rule === "range") return (r.lower || "") + " ..< " + (r.upper || "")
    return (r.rule === "upToNextMinor" ? "up to next minor from " : r.rule === "exact" ? "exactly " : "from ") + (r.version || "")
  }
  function resolvedText(p) {
    var r = p ? p.resolved : null
    if (!r) return ""
    if (r.version) return r.version
    var rev = String(r.revision || "").substring(0, 7)
    return r.branch ? r.branch + (rev ? " @ " + rev : "") : rev
  }
  function usedText(p) {
    var by = p && p.used_by ? p.used_by : []
    var prods = p && p.products_used ? p.products_used : []
    if (by.length === 0) return ""
    return "Used by " + by.join(", ") + (prods.length ? " · " + prods.join(", ") : "")
  }
  function _normUrl(u) { return String(u || "").trim().toLowerCase().replace(/\/+$/, "").replace(/\.git$/, "").replace(/^git@([^:]+):/, "https://$1/").replace(/^ssh:\/\/git@/, "https://") }
  function _sameUrl(u) {
    var n = root._normUrl(u)
    for (var i = 0; i < root.packages.length; i++) {
      var p = root.packages[i]
      if (root._normUrl(p.url || p.path) === n) return p
    }
    return null
  }
  function errorText(e) { return Model.errorMessage(e) + (e && e.hint ? " " + e.hint : "") }
  function stepGlyph(state) {
    return { done: Model.Glyph.checkCircle, failed: Model.Glyph.error, running: Model.Glyph.sync, skipped: Model.Glyph.minus }[state] || Model.Glyph.circle
  }
  function stepColor(state) {
    return { done: Theme.success, failed: Theme.error, running: Commons.Color.accent }[state] || Theme.faint
  }
  function changeLine(c) {
    var from = c.from || "", to = c.to || ""
    return (c.identity || c.name || "") + (from && to ? " " + from + " → " + to : to ? " " + to + " (new)" : from ? " " + from + " (gone)" : "")
  }

  // ---------------------------------------------------------------- open / list
  function openFor(p) {
    if (root.jobRunning && root.project && p && root.project.path !== p.path) {
      root.app.toast("Packages for " + root.project.name + " are still updating. Wait for it or cancel it first.", "warn")
      root.open()
      return
    }
    if (!root.jobRunning) {
      root.project = p || {}
      root.view = "list"
      root.notice = null
      root.job = null
      listRes.reset()                     // never show another project's packages while loading
    }
    root.open()
    root.load()
  }

  function load() { if (!listRes.load(true)) root._loadAgain = true }
  function _loadAgainIfAsked() { if (root._loadAgain) { root._loadAgain = false; Qt.callLater(root.load) } }

  onClosed: { root._ticket++; if (!root.jobRunning) root.view = "list" }

  // ---------------------------------------------------------------- add
  function startAdd() {
    root.view = "add"
    root.urlTouched = false
    root.addTried = false
    root.rule = "from"
    urlField.text = ""
    versionField.text = ""
    Qt.callLater(function() { urlField.forceActiveFocus() })
  }

  function submitAdd() {
    root.addTried = true
    if (root.addBlocker !== "") return
    var argv = ["packages", "add", "--project", root.project.path, "--url", root.urlText]
    if (!root.isLocal) {
      argv.push("--rule", root.rule)
      if (root.versionText !== "") argv.push("--version", root.versionText.replace(/^v(?=\d)/, ""))
    }
    if (root.target) argv.push("--target", root.target)
    var name = root.urlText.replace(/\/+$/, "").replace(/\.git$/, "").split(/[\/:]/).pop()
    root._startJob("add", argv, name)        // the helper sends its steps (check, add, fetch, products, link, validate)
    root.view = "job"
  }

  // ---------------------------------------------------------------- update / resolve
  function update(p) {
    var argv = ["packages", "update", "--project", root.project.path]
    if (p) argv.push("--package", root.pkgId(p))
    root._startJob("update", argv, p ? root.pkgName(p) : "")
  }
  function resolve() { root._startJob("resolve", ["packages", "resolve", "--project", root.project.path], "") }

  // ---------------------------------------------------------------- streamed job
  function _startJob(kind, argv, pkg) {
    if (root.jobRunning) return
    root.notice = null
    root.job = { kind: kind, pkg: pkg, steps: [], state: "running", detail: "Starting…", fraction: -1, data: null, error: null,
                 started: Date.now() }
    jobStream.start(argv)
  }
  function _patchJob(fields) { root.job = Object.assign({}, root.job, fields) }
  function _setStep(id, state, detail, title) {
    var steps = root.job.steps.slice(), found = false
    for (var i = 0; i < steps.length; i++) {
      if (steps[i].id !== id) {
        // a later step started: the earlier running ones are done
        if (state === "running" && steps[i].state === "running") steps[i] = Object.assign({}, steps[i], { state: "done" })
        continue
      }
      steps[i] = Object.assign({}, steps[i], { state: state, detail: detail || steps[i].detail, title: title || steps[i].title })
      found = true
    }
    if (!found) steps.push({ id: id, title: title || id, state: state, detail: detail || "" })
    root._patchJob({ steps: steps })
  }
  function _onEvent(ev) {
    if (!root.jobRunning) return
    if (ev.event === "step") {
      root._setStep(ev.id || ev.step, ev.state || "running", ev.detail, ev.title)
      if (ev.detail && (ev.state || "running") === "running") root._patchJob({ detail: ev.detail })
    } else if (ev.event === "progress") {
      if (ev.step) root._setStep(ev.step, "running", ev.detail, ev.title)
      root._patchJob({ detail: ev.detail || ev.text || root.job.detail,
                       fraction: typeof ev.fraction === "number" ? Math.max(root.job.fraction, ev.fraction) : root.job.fraction })
    } else if (ev.event === undefined && ev.ok !== undefined) {
      root._onDone(ev.ok === true, ev.data || {}, ev.error || null)     // a plain {ok, data} answer
    }
  }
  function _onDone(ok, data, error) {
    if (!root.jobRunning) return
    cancelKill.stop()
    var cancelled = !ok && (root._cancelling || (error && error.code === "cancelled"))
    root._cancelling = false
    var steps = root.job.steps.map(function(s) {
      if (s.state === "running") return Object.assign({}, s, { state: ok ? "done" : "failed" })
      return s
    })
    root._patchJob({ state: ok ? "done" : cancelled ? "cancelled" : "failed", steps: steps, data: data, error: error })
    var kind = root.job.kind
    if (ok) svc.packagesChanged(root.project)
    if (kind === "add") {
      if (ok && data.needs_product) {
        root.products = (data.products || []).map(function(x) { return typeof x === "string" ? { name: x } : x })
        var pre = {}
        root.picked = pre
        root.linkError = null
        root.view = "products"
      } else if (ok) {
        root.added = { "package": data["package"] || null, products: data.products_added || (data.product_added ? [data.product_added] : []),
                       target: data.target || root.target, diff: data.diff || "", demo: data.demo === true }
        root.view = "added"
      }
      root.load()
      return
    }
    // update / resolve: the list shows the result
    root.notice = { kind: kind, ok: ok, cancelled: cancelled, changes: (data && data.changes) || [], error: error,
                    demo: !!(data && data.demo), pkg: root.job.pkg }
    if (ok) root.load()
  }
  property bool _cancelling: false
  function cancelJob() {
    if (!root.jobRunning) return
    root._cancelling = true
    root._patchJob({ detail: "Cancelling…" })
    jobStream.write({ cancel: true })
    cancelKill.restart()          // the helper stops swift and restores Package.resolved; force it if it doesn't exit
  }
  Timer { id: cancelKill; interval: 3000; onTriggered: if (jobStream.running) jobStream.stop() }

  Stream {
    id: jobStream
    program: [root.app ? root.app.helperPath : ""]
    onEvent: function(ev) { root._onEvent(ev) }
    onFinished: function(ok, data, error) { root._onDone(ok, data, error) }
    onExited: function(code) {
      Qt.callLater(function() {
        if (!root.jobRunning || jobStream.running) return
        if (root._cancelling) root._onDone(false, {}, { code: "cancelled", message: "Cancelled." })
        else root._onDone(false, {}, { code: "exited", message: "It stopped without reporting a result (exit code " + code + ")." })
      })
    }
  }

  // ---------------------------------------------------------------- products
  function togglePick(name) {
    var p = Object.assign({}, root.picked)
    p[name] = !(p[name] === true)
    root.picked = p
  }
  function linkPicked() {
    var names = root.pickedNames
    if (names.length === 0 || root.linking) return
    var data = root.job && root.job.data ? root.job.data : {}
    var id = data.identity || root.pkgId(data["package"]) || root.job.pkg
    root.linking = true
    root.linkError = null
    svc.packagesAddProduct(root.project, id, names, data.target || root.target, function(d) {
      root.linking = false
      root.added = { "package": d["package"] || data["package"] || null, products: d.products_added || names,
                     target: d.target || data.target || root.target, diff: d.diff || "", demo: d.demo === true }
      root.view = "added"
      svc.packagesChanged(root.project)
      root.load()
    }, function(e) {
      root.linking = false
      root.linkError = e
    })
  }

  // ---------------------------------------------------------------- remove
  function startRemove(p) {
    var t = ++root._ticket
    root.removing = p
    root.removeData = null
    root.removeError = null
    root.removePhase = "loading"
    root.view = "remove"
    svc.packagesRemove(root.project, root.pkgId(p), false, function(d) {
      if (t !== root._ticket) return
      root.removeData = d
      root.removePhase = d && d.valid === false ? "invalid" : "ready"
    }, function(e) {
      if (t !== root._ticket) return
      root.removeError = e
      root.removeData = e && e.diff ? e : null          // edit-invalid carries the diff and would_remove
      root.removePhase = e && e.code === "edit-invalid" ? "invalid" : "error"
    })
  }
  function confirmRemove() {
    if (root.removePhase !== "ready") return
    var p = root.removing
    root.removePhase = "removing"
    root.removeError = null
    svc.packagesRemove(root.project, root.pkgId(p), true, function(d) {
      root.removePhase = "idle"
      root.app.toast(d && d.demo ? "Demo mode: " + root.pkgName(p) + " would be removed, nothing was written"
                                 : "Removed " + root.pkgName(p) + " from " + (root.project.name || "the project"), d && d.demo ? "info" : "ok")
      svc.packagesChanged(root.project)
      root.view = "list"
      root.load()
    }, function(e) {
      root.removeError = e
      if (e && e.code === "edit-invalid") { if (e.diff) root.removeData = e; root.removePhase = "invalid" }
      else root.removePhase = "ready"
    })
  }
  readonly property var removeTargetDeps: {
    var w = root.removeData && root.removeData.would_remove ? root.removeData.would_remove : null
    return w && w.target_deps ? w.target_deps : []
  }
  function targetDepText(d) {
    if (typeof d === "string") return d
    return (d.target || "") + (d.product ? " → " + d.product : "")
  }

  // The Sheet header's glyph, measured: the glyph columns of "Added …" and "Remove …" are this wide, so their
  // text starts exactly on the sheet title's left edge. Never shown (takes no room in the body).
  Icon { id: headerGlyph; visible: false; glyph: root.glyph; size: Style.font.display }

  // ================================================================ list view
  LoadingState {
    Layout.fillWidth: true
    Layout.preferredHeight: Style.space(200)
    visible: root.view === "list" && listRes.status === "loading"
    text: "Reading Package.swift…"
    detail: "The first look at a project takes a few seconds."
  }

  ErrorState {
    Layout.fillWidth: true
    visible: root.view === "list" && listRes.status === "error"
    compact: true
    app: root.app
    title: "Couldn't read the packages"
    error: listRes.error
    message: root.errorText(listRes.error)
    onRetry: root.load()
    onFixed: root.close()
  }

  EmptyState {
    Layout.fillWidth: true
    visible: root.view === "list" && listRes.status === "ready" && root.packages.length === 0
    compact: true
    glyph: Model.Glyph.packages
    title: "No packages yet"
    text: "Add one by its Git URL. iOS Studio adds it to Package.swift, fetches it and links it to "
          + (root.targets.length ? root.targets[0].name : "your app") + "."
    actionText: "Add package…"
    actionGlyph: Model.Glyph.plus
    onAction: root.startAdd()
  }

  // One column system for the whole sheet: block content (fields, diffs, banners) spans the body; glyphs sit
  // in the header glyph's column (0 … glyphW) and text starts on the title's edge (textX); buttons end on the
  // body's right edge. Rows and status lines have no fill or border of their own, so nothing has to be
  // inset from an edge the header doesn't have.
  readonly property real glyphW: headerGlyph.implicitWidth
  readonly property real textX: glyphW + Style.spacing.xxl

  // count + actions: the count starts on the body's left edge, like the header glyph and the rows' glyphs,
  // so it heads the whole list (glyph column included) instead of only its text column
  RowLayout {
    Layout.fillWidth: true
    // a compact ghost button's label ends xs further right than an IconButton's glyph: this puts "Add package…"
    // on the same right edge as the header's × and the rows' bin (measured: both end at body right − 12)
    Layout.rightMargin: Style.spacing.xs
    visible: root.view === "list" && listRes.status === "ready" && root.packages.length > 0
    spacing: Style.spacing.md
    AppText { Layout.fillWidth: true; text: Model.plural(root.packages.length, "package"); strong: true; elide: Text.ElideRight }
    ActionButton {
      text: "Update all"; glyph: Model.Glyph.sync; kind: "ghost"; compact: true
      busy: root.jobRunning && root.job.kind === "update" && !root.job.pkg
      disabled: root.lockReason !== ""
      tooltip: root.lockReason !== "" ? root.lockReason : "Fetch the newest versions the requirements allow (swift package update)"
      onClicked: root.update(null)
    }
    ActionButton { text: "Add package…"; glyph: Model.Glyph.plus; kind: "ghost"; compact: true; onClicked: root.startAdd() }
  }

  // update / resolve running: the latest progress line, and Cancel
  SheetGlyphRow {
    Layout.fillWidth: true
    visible: root.view === "list" && root.jobRunning && root.job.kind !== "add"
    glyphWidth: root.glyphW
    spinner: root.jobRunning
    title: !root.job ? "" : root.job.kind === "resolve" ? "Fetching the packages…"
           : root.job.pkg ? "Updating " + root.job.pkg + "…" : "Updating all packages…"
    AppText { Layout.fillWidth: true; text: root.job ? root.job.detail || "" : ""; tone: "dim"; size: "small"; elide: Text.ElideMiddle }
    trailing: [ ActionButton { Layout.rightMargin: Style.spacing.xs; text: "Cancel"; kind: "ghost"; compact: true; tooltip: "Stop it. Package.resolved stays as it was."; onClicked: root.cancelJob() } ]
  }

  // update / resolve finished
  SheetGlyphRow {
    id: noticeBox
    readonly property string level: !root.notice ? "" : root.notice.ok ? "ok" : root.notice.cancelled ? "info" : "error"
    readonly property var changes: root.notice ? root.notice.changes : []
    Layout.fillWidth: true
    visible: root.view === "list" && !!root.notice && listRes.status !== "loading"
    glyphWidth: root.glyphW
    glyph: level === "ok" ? Model.Glyph.checkCircle : level === "error" ? Model.Glyph.error : Model.Glyph.info
    glyphColor: Theme.levelColor(level)
    titleWraps: true
    title: {
      var n = root.notice
      if (!n) return ""
      var what = n.kind === "resolve" ? "Fetching the packages" : n.pkg ? "Updating " + n.pkg : "The update"
      if (n.cancelled) return what + " was cancelled. Nothing changed."
      if (!n.ok) return (n.kind === "resolve" ? "Couldn't fetch the packages: " : "Couldn't update: ") + root.errorText(n.error)
      if (noticeBox.changes.length === 0) return n.kind === "resolve" ? "All packages are fetched." : "Everything is up to date."
      return Model.plural(noticeBox.changes.length, "package") + (n.kind === "resolve" ? " fetched" : " updated")
             + (n.demo ? " (demo mode, nothing was written)" : "")
    }
    trailingAlignment: Qt.AlignTop
    Repeater {
      model: noticeBox.changes.slice(0, 8)
      delegate: AppText {
        required property var modelData
        Layout.fillWidth: true
        text: root.changeLine(modelData)
        tone: "dim"; size: "small"; elide: Text.ElideRight
      }
    }
    AppText {
      Layout.fillWidth: true
      visible: noticeBox.changes.length > 8
      text: "and " + (noticeBox.changes.length - 8) + " more"
      tone: "faint"; size: "small"
    }
    trailing: [ IconButton { glyph: Model.Glyph.close; tooltip: "Dismiss"; onClicked: root.notice = null } ]
  }

  // some declared packages aren't in Package.resolved yet
  SheetGlyphRow {
    Layout.fillWidth: true
    visible: root.view === "list" && listRes.status === "ready" && root.needsResolve && !(root.jobRunning && root.job.kind === "resolve")
    glyphWidth: root.glyphW
    glyph: Model.Glyph.warn; glyphColor: Theme.warn
    titleWraps: true; titleTone: "warn"
    title: root.unresolved.length ? "Unresolved: " + Model.plural(root.unresolved.length, "package") + " not fetched yet"
                                  : "Unresolved: there's no Package.resolved yet"
    AppText {
      Layout.fillWidth: true
      text: root.unresolved.length ? root.unresolved.map(root.nameOf).join(", ") : "Resolve fetches every package and pins its version."
      tone: "dim"; size: "small"; elide: Text.ElideRight
    }
    trailing: [
      ActionButton {
        text: "Resolve"; glyph: Model.Glyph.download; kind: "secondary"; compact: true
        disabled: root.lockReason !== ""
        tooltip: root.lockReason !== "" ? root.lockReason : "Fetch the packages and write Package.resolved (swift package resolve)"
        onClicked: root.resolve()
      }
    ]
  }

  // the packages: like ListRow, the requirement, version and buttons are centred on the whole row
  ColumnLayout {
    Layout.fillWidth: true
    visible: root.view === "list" && listRes.status === "ready" && root.packages.length > 0
    spacing: 0

    Repeater {
      model: root.packages
      delegate: Item {
        id: pkgRow
        required property var modelData
        required property int index
        readonly property var req: modelData.requirement || {}
        readonly property bool local: modelData.kind === "local" || req.rule === "local"
        readonly property string resolved: root.resolvedText(modelData)
        readonly property bool fetched: modelData.resolved !== null && modelData.resolved !== undefined
        readonly property bool updatingThis: root.jobRunning && root.job.kind === "update" && root.job.pkg === root.pkgName(modelData)
        Layout.fillWidth: true
        implicitHeight: Math.max(textCol.implicitHeight, rowRight.implicitHeight) + Style.spacing.lg * 2

        // hairline between rows
        Rectangle { visible: pkgRow.index > 0; x: root.textX; width: parent.width - x; height: Style.normalBorderWidth; color: Theme.hairline }

        // glyph centred on the name's line
        FontMetrics { id: nameMetrics; font: nameText.font }
        Item {
          x: 0
          y: textCol.y
          width: root.glyphW
          height: Math.max(nameMetrics.height, nameRow.height)
          Icon {
            anchors.centerIn: parent
            glyph: pkgRow.local ? Model.Glyph.folder : Model.Glyph.packages
            size: Style.font.iconLarge
            color: Theme.dim
          }
        }

        // Placed by hand rather than in a RowLayout: the text column gets exactly what's left before the right
        // cluster, so a long name or URL elides instead of pushing the buttons out.
        ColumnLayout {
          id: textCol
          x: root.textX
          width: Math.max(0, pkgRow.width - x - rowRight.width - Style.spacing.xxl)
          anchors.verticalCenter: parent.verticalCenter
          spacing: Style.spacing.xxs

          Item {
            id: nameRow
            Layout.fillWidth: true
            implicitHeight: Math.max(nameText.implicitHeight, localBadge.implicitHeight)
            Row {
              anchors.left: parent.left
              anchors.verticalCenter: parent.verticalCenter
              spacing: Style.spacing.md
              AppText {
                id: nameText
                anchors.verticalCenter: parent.verticalCenter
                width: Math.min(implicitWidth, Math.max(Style.space(60), nameRow.width - (localBadge.visible ? localBadge.implicitWidth + Style.spacing.md : 0)))
                text: root.pkgName(pkgRow.modelData)
                strong: true
                elide: Text.ElideRight
              }
              Badge { id: localBadge; anchors.verticalCenter: parent.verticalCenter; visible: pkgRow.local; text: "local"; level: "info" }
            }
          }
          AppText {
            Layout.fillWidth: true
            Layout.minimumWidth: 0
            text: root.pkgWhere(pkgRow.modelData)
            tone: "dim"; size: "small"; elide: Text.ElideMiddle
          }
          AppText {
            Layout.fillWidth: true
            Layout.minimumWidth: 0
            text: root.usedText(pkgRow.modelData) || "Not linked to a target yet"
            tone: root.usedText(pkgRow.modelData) ? "faint" : "warn"
            size: "small"; elide: Text.ElideRight
          }
        }

        // requirement, resolved version, Update, Remove: centred on the row, the last button on the body's edge
        Row {
          id: rowRight
          anchors.right: parent.right
          anchors.verticalCenter: parent.verticalCenter
          spacing: Style.spacing.md
          AppText {
            id: reqText
            anchors.verticalCenter: parent.verticalCenter
            visible: text !== ""
            width: Math.min(implicitWidth, pkgRow.width * 0.3)
            // a revision pin names only the rule; its commit is in the badge
            text: pkgRow.local ? "" : pkgRow.req.rule === "revision" ? "revision" : root.reqText(pkgRow.modelData)
            tone: "dim"; size: "small"; elide: Text.ElideRight
          }
          // What's resolved, without repeating the requirement: branch and revision show the short commit only;
          // an exact version that resolved to itself shows no badge. Local packages show neither.
          Badge {
            readonly property var res: pkgRow.modelData.resolved || {}
            readonly property string shortRev: String(res.revision || "").substring(0, 7)
            readonly property bool sameExact: pkgRow.req.rule === "exact" && pkgRow.fetched && res.version === pkgRow.req.version
            anchors.verticalCenter: parent.verticalCenter
            visible: !pkgRow.local && !sameExact
            text: !pkgRow.fetched ? "not fetched"
                  : pkgRow.req.rule === "branch" || pkgRow.req.rule === "revision" ? shortRev
                  : pkgRow.resolved
            level: pkgRow.fetched ? "ok" : "warn"
          }
          Row {
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.spacing.xxs
            IconButton {
              glyph: Model.Glyph.sync
              busy: pkgRow.updatingThis
              disabled: root.lockReason !== "" || pkgRow.local
              tooltip: pkgRow.local ? "A local package is used as it is on disk; there's nothing to update"
                       : root.lockReason !== "" ? root.lockReason : "Update " + root.pkgName(pkgRow.modelData) + " to the newest version allowed"
              onClicked: if (!disabled) root.update(pkgRow.modelData)
            }
            IconButton {
              glyph: Model.Glyph.trash
              danger: true
              disabled: root.lockReason !== ""
              tooltip: root.lockReason !== "" ? root.lockReason : "Remove " + root.pkgName(pkgRow.modelData) + "… (shows the change first)"
              onClicked: if (!disabled) root.startRemove(pkgRow.modelData)
            }
          }
        }
      }
    }
  }

  // ================================================================ add form
  ColumnLayout {
    Layout.fillWidth: true
    visible: root.view === "add"
    spacing: Style.spacing.sm
    AppText { text: "Package URL"; strong: true }
    TextField {
      id: urlField
      Layout.fillWidth: true
      placeholderTextColor: Theme.faint
      placeholderText: "https://github.com/apple/swift-collections.git"
      onTextEdited: root.urlTouched = true
      onAccepted: root.submitAdd()
    }
    AppText {
      Layout.fillWidth: true
      readonly property bool showProblem: (root.urlTouched || root.addTried) && root.urlProblem !== "" && root.urlText !== "" || (root.addTried && root.urlProblem !== "")
      text: showProblem ? root.urlProblem : root.isLocal ? "A local package: used as it is on disk, with no version." : "A Git URL (https:// or git@), or a local folder path."
      size: "small"; tone: showProblem ? "warn" : "faint"; wrapMode: Text.Wrap
    }
  }

  GridLayout {
    Layout.fillWidth: true
    visible: root.view === "add" && !root.isLocal
    columns: width >= Style.space(560) ? 2 : 1          // the body's own width, not the window's
    columnSpacing: Style.spacing.xl
    rowSpacing: Style.spacing.xl

    ColumnLayout {
      Layout.fillWidth: true
      Layout.preferredWidth: 1
      Layout.alignment: Qt.AlignTop
      spacing: Style.spacing.sm
      AppText { text: "Version rule"; strong: true }
      Dropdown {
        Layout.fillWidth: true
        showLabel: false
        rowHeight: versionField.implicitHeight      // as tall as the Version field beside it
        value: root.rule
        options: [
          { value: "from", label: "Up to next major" },
          { value: "upToNextMinor", label: "Up to next minor" },
          { value: "exact", label: "Exact version" },
          { value: "branch", label: "Branch" },
          { value: "revision", label: "Revision" }
        ]
        onChanged: function(v) { root.rule = v; versionField.text = ""; Qt.callLater(function() { versionField.forceActiveFocus() }) }
      }
      AppText {
        Layout.fillWidth: true
        text: root.rule === "from" ? "Newer releases until the next major version."
              : root.rule === "upToNextMinor" ? "Bug fixes only, until the next minor version."
              : root.rule === "exact" ? "This version only; Update keeps it."
              : root.rule === "branch" ? "The newest commit on a branch."
              : "One commit, pinned."
        size: "small"; tone: "faint"; wrapMode: Text.Wrap
      }
    }

    ColumnLayout {
      Layout.fillWidth: true
      Layout.preferredWidth: 1
      Layout.alignment: Qt.AlignTop
      spacing: Style.spacing.sm
      AppText { text: root.rule === "branch" ? "Branch" : root.rule === "revision" ? "Commit" : "Version"; strong: true }
      TextField {
        id: versionField
        Layout.fillWidth: true
        placeholderTextColor: Theme.faint
        placeholderText: root.rule === "branch" ? "e.g. main" : root.rule === "revision" ? "e.g. 3f2c1ab" : "newest release"
        onAccepted: root.submitAdd()
      }
      AppText {
        Layout.fillWidth: true
        readonly property bool showProblem: root.addTried && root.versionProblem !== "" || (root.versionText !== "" && root.versionProblem !== "")
        text: showProblem ? root.versionProblem : root.rule === "branch" || root.rule === "revision" ? "Pinned in Package.resolved when it's fetched." : "A tag like 1.2.0. Empty: the newest."
        size: "small"; tone: showProblem ? "warn" : "faint"; wrapMode: Text.Wrap
      }
    }
  }

  ColumnLayout {
    Layout.fillWidth: true
    visible: root.view === "add" && root.targets.length > 0     // none known: the helper links the app's target
    spacing: Style.spacing.sm
    AppText { text: "Add to target"; strong: true }
    Dropdown {
      Layout.fillWidth: true
      showLabel: false
      rowHeight: urlField.implicitHeight
      value: root.target
      options: root.targets.map(function(t) { return { value: t.name, label: t.name + (t.app ? " (the app)" : t.type === "test" ? " (tests)" : "") } })
      onChanged: function(v) { root.target = v }
    }
    AppText {
      Layout.fillWidth: true
      text: "The package's library is linked to this target, so its code can import it."
      size: "small"; tone: "faint"; wrapMode: Text.Wrap
    }
  }

  // ================================================================ add progress
  ColumnLayout {
    Layout.fillWidth: true
    visible: root.view === "job" && !!root.job
    spacing: Style.spacing.xl

    ColumnLayout {
      Layout.fillWidth: true
      spacing: Style.spacing.sm
      Repeater {
        model: root.job ? root.job.steps : []
        // the step glyph in the header glyph's column, the title on the title's edge
        delegate: RowLayout {
          required property var modelData
          Layout.fillWidth: true
          spacing: Style.spacing.xxl
          Item {
            Layout.preferredWidth: root.glyphW
            Layout.preferredHeight: stepIcon.implicitHeight
            Icon {
              id: stepIcon
              anchors.centerIn: parent
              glyph: root.stepGlyph(modelData.state)
              color: root.stepColor(modelData.state)
              spinning: modelData.state === "running"
              size: Style.font.icon
            }
          }
          AppText {
            text: modelData.title
            tone: modelData.state === "pending" || modelData.state === "skipped" ? "faint" : "primary"
            strong: modelData.state === "running"
          }
          // running: the latest progress line; finished: what the step found ("swift-collections from 1.1.0")
          AppText {
            Layout.fillWidth: true
            text: modelData.detail && modelData.detail !== modelData.title ? modelData.detail : ""
            tone: modelData.state === "running" ? "dim" : "faint"; size: "small"; elide: Text.ElideRight
          }
        }
      }
    }

    ErrorState {
      Layout.fillWidth: true
      visible: !!root.job && root.job.state === "failed"
      banner: true
      app: root.app
      error: root.job ? root.job.error : null
      message: root.job && root.job.error ? root.errorText(root.job.error) + " Package.swift is unchanged." : ""
      retryText: "Try again"
      // a URL or version the helper refused won't work a second time: Back is the way
      showRetry: !!root.job && !!root.job.error && ["bad-url", "bad-rule", "bad-version", "already-added", "no-versions", "usage", "unknown-target"].indexOf(root.job.error.code) < 0
      onRetry: root.submitAdd()
    }
    // swift's own errors behind a failed add (package diagnostics), first few
    Repeater {
      model: root.job && root.job.state === "failed" && root.job.error && root.job.error.diagnostics
             ? root.job.error.diagnostics.slice(0, 4) : []
      delegate: AppText {
        required property var modelData
        Layout.fillWidth: true
        text: modelData.message || ""
        tone: "dim"; size: "small"; elide: Text.ElideRight
      }
    }
    AppText {
      Layout.fillWidth: true
      visible: !!root.job && root.job.state === "cancelled"
      text: "Cancelled. Package.swift and Package.resolved are back as they were."
      tone: "dim"; wrapMode: Text.Wrap
    }
  }

  // ================================================================ products
  ColumnLayout {
    Layout.fillWidth: true
    visible: root.view === "products"
    spacing: Style.spacing.sm

    // A checklist: the checkbox in the header glyph's column, the name on the title's edge. Checked shows only in
    // the checkbox; hover brightens the row's checkbox and name, like a link.
    Repeater {
      model: root.products
      delegate: Item {
        id: pickRow
        required property var modelData
        required property int index
        readonly property bool on: root.picked[modelData.name] === true
        Layout.fillWidth: true
        implicitHeight: pickText.implicitHeight + Style.spacing.lg * 2
        Rectangle { visible: pickRow.index > 0; x: root.textX; width: parent.width - x; height: Style.normalBorderWidth; color: Theme.hairline }
        Item {
          width: root.glyphW
          height: parent.height
          Icon {
            anchors.centerIn: parent
            glyph: pickRow.on ? Model.Glyph.checkboxOn : Model.Glyph.checkboxOff
            size: Style.font.iconLarge
            color: pickRow.on ? Commons.Color.accent : pickHover.hovered ? Commons.Color.foreground : Theme.dim
          }
        }
        AppText {
          id: pickText
          x: root.textX
          width: parent.width - x
          anchors.verticalCenter: parent.verticalCenter
          text: pickRow.modelData.name + (pickRow.modelData.type ? "  (" + pickRow.modelData.type + ")" : "")
          tone: pickRow.on || pickHover.hovered ? "primary" : "dim"
          elide: Text.ElideRight
        }
        // a MouseArea, not a TapHandler: a TapHandler's passive grab lets the click reach the page behind the sheet
        MouseArea {
          id: pickHover
          readonly property bool hovered: containsMouse
          anchors.fill: parent
          hoverEnabled: true
          cursorShape: Qt.PointingHandCursor
          onClicked: root.togglePick(pickRow.modelData.name)
        }
      }
    }
    ErrorState {
      Layout.fillWidth: true
      Layout.topMargin: Style.spacing.sm
      visible: !!root.linkError
      banner: true
      app: root.app
      error: root.linkError
      message: root.errorText(root.linkError)
      retryText: "Try again"
      onRetry: root.linkPicked()
    }
  }

  // ================================================================ added
  ColumnLayout {
    Layout.fillWidth: true
    visible: root.view === "added" && !!root.added
    spacing: Style.spacing.xl

    // glyph centred on the headline's line; the text starts on the sheet title's left edge
    SheetGlyphRow {
      Layout.fillWidth: true
      glyphWidth: root.glyphW
      glyph: Model.Glyph.checkCircle; glyphColor: Theme.success; glyphSize: Style.font.iconLarge
      titleStrong: true
      title: "Added " + (root.added && root.added["package"] ? root.pkgName(root.added["package"]) : (root.job ? root.job.pkg : "the package"))
      // one fact per line, so none breaks mid-phrase
      Repeater {
        model: {
          var a = root.added
          if (!a) return []
          var lines = []
          if (a.products.length) lines.push(a.products.join(", ") + (a.products.length > 1 ? " are" : " is") + " linked to " + a.target)
          else lines.push("No library linked yet")
          var p = a["package"]
          if (p) {
            var req = root.reqText(p), res = root.resolvedText(p)
            if (req || res) lines.push((req ? "Requirement " + req : "") + (req && res ? " · " : "") + (res ? "resolved " + res : ""))
          }
          if (a.demo) lines.push("Demo mode: nothing was written")
          return lines
        }
        delegate: AppText {
          required property string modelData
          Layout.fillWidth: true
          text: modelData
          tone: "dim"; size: "small"; elide: Text.ElideRight
        }
      }
    }

    DiffView {
      Layout.fillWidth: true
      visible: !!root.added && root.added.diff !== ""
      text: root.added ? root.added.diff : ""
      maxHeight: Style.space(260)
    }
  }

  // ================================================================ remove
  LoadingState {
    Layout.fillWidth: true
    Layout.preferredHeight: Style.space(200)
    visible: root.view === "remove" && root.removePhase === "loading"
    text: "Working out the change…"
    detail: "Checks that Package.swift still reads correctly without it."
  }

  ErrorState {
    Layout.fillWidth: true
    visible: root.view === "remove" && root.removePhase === "error"
    compact: true
    app: root.app
    title: "Couldn't prepare the removal"
    error: root.removeError
    message: root.errorText(root.removeError)
    onRetry: root.startRemove(root.removing)
  }

  ColumnLayout {
    Layout.fillWidth: true
    visible: root.view === "remove" && (root.removePhase === "ready" || root.removePhase === "removing" || root.removePhase === "invalid")
    spacing: Style.spacing.xl

    ErrorState {
      Layout.fillWidth: true
      visible: root.removePhase === "invalid"
      banner: true
      showRetry: false
      app: root.app
      error: root.removeError
      // the helper's reason ("… can't be done safely: …") and hint ("Nothing was changed. Edit Package.swift by hand.")
      message: root.removeError && root.removeError.message ? root.errorText(root.removeError)
               : "Removing " + root.pkgName(root.removing) + " can't be done safely: Package.swift wouldn't read afterwards. Nothing was changed."
    }
    ErrorState {
      Layout.fillWidth: true
      visible: root.removePhase === "ready" && !!root.removeError
      banner: true
      app: root.app
      error: root.removeError
      message: root.errorText(root.removeError)
      onRetry: root.confirmRemove()
    }

    SheetGlyphRow {
      Layout.fillWidth: true
      visible: root.removePhase !== "invalid"
      glyphWidth: root.glyphW
      glyph: Model.Glyph.trash; glyphColor: Commons.Color.urgent; glyphSize: Style.font.iconLarge
      titleStrong: true
      title: "Remove " + root.pkgName(root.removing) + " from Package.swift"
      AppText {
        Layout.fillWidth: true
        tone: "dim"; size: "small"; wrapMode: Text.Wrap
        text: root.removeTargetDeps.length
              ? "Also unlinks it from " + root.removeTargetDeps.map(root.targetDepText).join(", ") + ". Code that imports it stops compiling."
              : "No target uses it."
      }
    }

    DiffView {
      Layout.fillWidth: true
      text: root.removeData && root.removeData.diff ? root.removeData.diff : ""
      maxHeight: Style.space(320)
    }
  }

  // ================================================================ footer
  footer: [
    // list
    ActionButton { visible: root.view === "list"; text: "Close"; kind: "ghost"; onClicked: root.close() },
    // add form
    ActionButton { visible: root.view === "add"; text: "Back"; kind: "ghost"; onClicked: root.view = "list" },
    ActionButton {
      visible: root.view === "add"
      text: "Add package"; glyph: Model.Glyph.plus; kind: "primary"
      disabled: root.addBlocker !== ""
      tooltip: root.addBlocker !== "" ? root.addBlocker : "Add it to Package.swift, fetch it and link it to " + (root.target || "the app")
      onClicked: root.submitAdd()
    },
    // add progress
    ActionButton {
      visible: root.view === "job"
      text: root.jobRunning ? "Cancel" : "Back"
      kind: "ghost"
      onClicked: root.jobRunning ? root.cancelJob() : (root.view = root.job && root.job.state === "failed" || root.job && root.job.state === "cancelled" ? "add" : "list")
    },
    // products
    ActionButton { visible: root.view === "products"; text: "Not now"; kind: "ghost"; enabled: !root.linking; tooltip: "Keep the package without linking it. You can link it later."; onClicked: root.view = "list" },
    ActionButton {
      visible: root.view === "products"
      text: root.pickedNames.length > 1 ? "Link " + root.pickedNames.length + " libraries" : "Link library"
      glyph: Model.Glyph.link; kind: "primary"
      busy: root.linking
      disabled: root.pickedNames.length === 0
      tooltip: root.pickedNames.length === 0 ? "Pick at least one library" : "Link to " + (root.target || "the app")
      onClicked: root.linkPicked()
    },
    // added
    ActionButton { visible: root.view === "added"; text: "Done"; kind: "ghost"; onClicked: root.view = "list" },
    ActionButton {
      visible: root.view === "added"
      readonly property string blocker: !root.svc ? "" : root.svc.building ? "Another build is running"
                                        : root.svc.blocked(root.project) ? "Allow its generated xtool.yml in Build settings first" : ""
      text: "Check build"; glyph: Model.Glyph.hammer; kind: "primary"
      disabled: blocker !== ""
      tooltip: blocker !== "" ? blocker : "Compile for iPhone without signing or installing; the build panel shows it"
      onClicked: { root.close(); root.checkBuild(root.project) }
    },
    // remove
    ActionButton { visible: root.view === "remove"; text: "Back"; kind: "ghost"; enabled: root.removePhase !== "removing"; onClicked: root.view = "list" },
    ActionButton {
      visible: root.view === "remove" && root.removePhase !== "invalid" && root.removePhase !== "error"
      text: "Remove " + root.pkgName(root.removing)
      glyph: Model.Glyph.trash; kind: "danger"
      busy: root.removePhase === "removing"
      disabled: root.removePhase !== "ready" && root.removePhase !== "removing"
      tooltip: root.removePhase === "loading" ? "Still working out the change" : "Write this change to Package.swift"
      onClicked: root.confirmRemove()
    }
  ]
}
