import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.components
import ".."
import "../Model.js" as Model

// Apple ID / developer team state (account slice).
//   status    "unknown" | "loading" | "ready" | "needs-setup" | "error"
//   auth      `account status` result: {signedIn, mode, appleId, teamId, tokenExpiry{text,ts,iso}, hasLocalSigningKey}
//   details   `account details` result: {sections{teams,certificates,identifiers,devices,profiles}, summary{team,free,limits,counts,health}, fetchedAt, stale}
//   flow      sign-in state machine, driven by `account login` (see AccountSignInSheet)
Item {
  id: root
  required property var app

  property string status: "unknown"
  property var error: null
  property var auth: null
  property var details: null
  property bool detailsLoading: false
  property var detailsError: null
  property bool signingOut: false
  property real now: Date.now()

  readonly property bool signedIn: !!(auth && auth.signedIn)
  readonly property bool keyMode: !!(auth && auth.mode === "key")
  readonly property var info: details && details.summary ? details.summary : ({})
  readonly property var team: info.team || null
  readonly property bool free: info.free === true
  readonly property var health: info.health || null
  readonly property var sections: details && details.sections ? details.sections : ({})
  readonly property real fetchedAtMs: details && details.fetchedAt ? details.fetchedAt * 1000 : 0

  // Soonest active profile expiry (ms) — the signing countdown everyone shows.
  readonly property real signingExpiresMs: health && health.expires && health.expires.ts ? health.expires.ts * 1000 : 0
  readonly property var signingExpiry: signingExpiresMs > 0 ? expiryFor(signingExpiresMs) : null

  // { title, detail, level, badge } for Home and the sidebar.
  readonly property var summary: {
    if (status === "needs-setup") return { title: "Apple ID", detail: "Install xtool first, then sign in.", level: "warn", badge: "" }
    if (status === "error" && !auth) return { title: "Apple ID", detail: Model.errorMessage(error), level: "error", badge: "" }
    if (!auth) return { title: "Apple ID", detail: "Checking…", level: "info", badge: "" }
    if (!signedIn) return { title: "Not signed in", detail: "Sign in with your Apple ID to put apps on your iPhone.", level: "warn", badge: "" }
    var who = auth.appleId || "API key"
    if (!signingExpiry) return { title: "Signed in", detail: who + (detailsLoading && !details ? " · checking signing…" : ""), level: "ok", badge: "" }
    var e = signingExpiry
    return {
      title: "Signed in",
      detail: who + " · signing " + e.text,
      level: e.level,
      badge: e.level === "ok" ? "" : (e.hours <= 0 ? "!" : (e.hours < 48 ? Math.max(1, Math.round(e.hours)) + "h" : e.days + "d")),
      expiresAt: signingExpiresMs
    }
  }

  // Short copy the projects slice (or anyone) can show next to xtool's revoke prompt.
  readonly property string revokeExplanation:
    "Apple only lets this Apple ID keep one signing certificate per computer, and the secret key for it lives on the computer that made it. " +
    "iOS Studio on this computer doesn't have that key, so it needs a new certificate. Replacing it stops apps you signed on your other computer " +
    "from opening until you re-install them from there. Apps you sign here will keep working."

  // ------------------------------------------------------------ expiry helper
  // Like Model.expiryStatus but with "less than 2 days" = warn and hours exposed.
  function expiryFor(ms) {
    var hours = (ms - root.now) / 3600000
    var e = Model.expiryStatus(ms, root.now)
    var lv = hours <= 0 ? "error" : (hours < 48 ? "warn" : "ok")
    return { level: lv, text: e.text, days: Math.max(0, Math.floor(hours / 24)), hours: hours }
  }

  // ------------------------------------------------------------ refresh
  function refresh() {
    if (root.status !== "ready") root.status = "loading"
    app.cli.call(["account", "status"], "", function(data) {
      var wasSignedIn = root.signedIn
      root.auth = data
      root.error = null
      root.status = "ready"
      if (data.signedIn && data.mode !== "key") {
        if (!root.details || !wasSignedIn) loadCached(true)
        else refreshDetails()
      } else {
        root.details = null
      }
    }, function(err) {
      root.error = err
      root.status = err && err.code === "missing-tool" ? "needs-setup" : "error"
    }, { timeout: 30000 })
  }

  // Instant cached read, then a background refresh when it's stale or empty.
  function loadCached(thenRefresh) {
    app.cli.call(["account", "details", "--cached"], "", function(data) {
      if (!data.empty && data.status && root.auth && data.status.teamId === root.auth.teamId) root.details = data
      if (thenRefresh && (data.empty || data.stale)) refreshDetails()
    }, function(err) { if (thenRefresh) refreshDetails() })
  }

  function refreshDetails() {
    if (root.detailsLoading || !root.signedIn || root.keyMode) return
    root.detailsLoading = true
    app.cli.call(["account", "details"], "", function(data) {
      root.detailsLoading = false
      root.details = data
      root.detailsError = null
      root.now = Date.now()
    }, function(err) {
      root.detailsLoading = false
      root.detailsError = err
      if (err && err.code === "not-signed-in") root.refresh()
    }, { timeout: 180000 })
  }

  function signOut() {
    root.signingOut = true
    app.cli.call(["account", "logout"], "", function() {
      root.signingOut = false
      root.details = null
      root.auth = { signedIn: false, mode: null }
      app.toast("Signed out of your Apple ID", "ok")
      root.refresh()
    }, function(err) {
      root.signingOut = false
      app.toast(Model.errorMessage(err), "error")
    })
  }

  // ------------------------------------------------------------ sign-in flow
  // phase: idle | starting | working | password | code | team | text | done | failed
  property var flow: ({ phase: "idle" })
  property bool signInRequested: false        // AccountPage opens its sheet when this flips on
  signal signInSucceeded(var status)
  signal promptArrived(var prompt)

  readonly property bool signingIn: flow.phase !== "idle" && flow.phase !== "done" && flow.phase !== "failed"

  // From anywhere: jump to the Apple ID page with the sign-in sheet open.
  // (Pages that host their own AccountSignInSheet can call its begin() instead.)
  function requestSignIn() {
    app.nav("account", { action: "signin" })
  }

  function _patch(o) { root.flow = Object.assign({}, root.flow, o) }

  function startSignIn(email, mode) {
    var argv = ["account", "login", "--mode", mode || "password"]
    if (email) argv.push("--email", email)
    root.flow = { phase: "starting", mode: mode || "password", email: email || "", notices: [], step: "Starting sign-in", prompt: null, error: null }
    login.start(argv)
  }

  function answer(value) {
    var p = root.flow.prompt
    if (!p) return
    login.write({ prompt: p.id, value: String(value) })
    _patch({ phase: "working", prompt: null, error: null, step: p.kind === "code" ? "Checking the code with Apple" : (p.kind === "team" ? "Finishing up" : "Checking your Apple ID with Apple") })
  }

  function cancelSignIn() {
    var p = root.flow.prompt
    if (login.running) {
      if (p) login.write({ prompt: p.id, cancel: true })
      killLater.restart()
    }
    root.flow = { phase: "idle" }
  }

  function resetFlow() { if (!root.signingIn) root.flow = { phase: "idle" } }

  Timer { id: killLater; interval: 1500; onTriggered: if (login.running) login.stop() }

  Stream {
    id: login
    program: [root.app.helperPath]
    onEvent: function(ev) {
      if (root.flow.phase === "idle") return
      if (ev.event === "prompt") {
        root._patch({ phase: ev.kind === "text" ? "text" : ev.kind, prompt: ev })
        root.promptArrived(ev)
      } else if (ev.event === "step") {
        if (ev.state === "running") root._patch({ step: ev.title })
      } else if (ev.event === "notice") {
        var n = (root.flow.notices || []).slice()
        n.push(ev)
        root._patch({ notices: n, manualCode: root.flow.manualCode || ev.id === "code-manual" })
      }
    }
    onFinished: function(ok, data, error) {
      if (root.flow.phase === "idle") return
      if (ok) {
        root._patch({ phase: "done", prompt: null, error: null, result: data.status || null })
        if (data.status) root.auth = data.status
        root.details = null
        root.status = "ready"
        root.signInSucceeded(data.status || {})
        root.refreshDetails()
        Qt.callLater(root.refresh)
      } else {
        root._patch({ phase: error && error.code === "cancelled" ? "idle" : "failed", prompt: null, error: error })
      }
    }
  }

  // ------------------------------------------------------------ timers
  Timer { interval: 60000; running: true; repeat: true; onTriggered: root.now = Date.now() }
  // Re-check Apple every 30 minutes while the app is open (cheap status, cached details).
  Timer { interval: 30 * 60000; running: root.signedIn; repeat: true; onTriggered: root.refreshDetails() }

  // Notify once when signing is about to run out.
  property string _notifiedFor: ""
  onSummaryChanged: {
    if (summary.level === "warn" || summary.level === "error") {
      var key = String(root.signingExpiresMs) + summary.level
      if (root.signingExpiresMs > 0 && key !== root._notifiedFor) {
        root._notifiedFor = key
        app.notify(summary.level === "error" ? "App signing has expired" : "App signing expires soon",
                   "Open iOS Studio and renew your apps to keep them opening on your iPhone.", Model.Glyph.signature)
      }
    }
  }

  Component.onCompleted: Qt.callLater(root.refresh)
}
