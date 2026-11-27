import QtQuick
import QtTest
import "../../lib/Model.js" as Model

// Toolchain › Updates helpers in lib/Model.js: the status line, which rows/notices show, the reinstall list for
// going back to Tested, versions, error texts, the interrupted and failed-update notes and the toast. Run by
// tests/qml/test_qml_core.py.
TestCase {
  name: "ToolchainUpdates"

  readonly property real now: 1795731484000          // ms
  readonly property string nb: "\u00a0"

  function c(id, state, extra) {
    var o = { id: id, name: id, installed: "1.0", tested: "1.0", latest: "1.1", target: "1.0", state: state,
              untested: false, applicable: true, blockedReason: null, restarts: [] }
    for (var k in extra || {}) o[k] = extra[k]
    return o
  }
  function st(components, extra) {
    var o = { channel: "tested", checkedAt: (now - 3 * 3600 * 1000) / 1000, nextCheckAt: (now + 21 * 3600 * 1000) / 1000,
              due: false, error: null, count: 0, components: components || [], notices: [] }
    for (var k in extra || {}) o[k] = extra[k]
    return o
  }
  function plain(s) { return s.split(nb).join(" ") }

  function test_shortAgo() {
    compare(Model.shortAgo(now - 20 * 1000, now), "just now")
    compare(Model.shortAgo(now - 5 * 60 * 1000, now), "5 min ago")
    compare(Model.shortAgo(now - 3 * 3600 * 1000 - 59000, now), "3 h ago")
    compare(Model.shortAgo(now - 30 * 3600 * 1000, now), "yesterday")
    compare(Model.shortAgo(now - 4 * 86400 * 1000, now), "4 days ago")
    compare(Model.shortAgo((now - 120 * 1000) / 1000, now), "2 min ago")     // seconds epoch, as the helper writes
    compare(Model.shortAgo(null, now), "")
    compare(Model.shortAgo(now + 60000, now), "just now")                   // clock skew never reads "in …"
  }

  function test_lineUpToDateKeepsPhrasesTogether() {
    var l = Model.updatesLine(st([c("ipsw", "current")]), false, now)
    compare(l.level, "ok")
    compare(plain(l.text), "Up to date · checked 3 h ago")
    // the only breakable spaces sit around the dot
    compare(l.text, "Up" + nb + "to" + nb + "date · checked" + nb + "3" + nb + "h" + nb + "ago")
  }

  function test_lineOtherStates() {
    compare(Model.updatesLine(st([]), true, now), { text: "Checking for updates…", level: "info", tip: "" })
    compare(Model.updatesLine(null, false, now).text, "")
    var withRow = Model.updatesLine(st([c("ipsw", "update")]), false, now)
    compare(withRow.level, "info")
    compare(plain(withRow.text), "Checked 3 h ago")
    compare(plain(Model.updatesLine(st([], { checkedAt: null }), false, now).text), "Not checked yet")
    // an interrupted update is never "up to date" (its note says what to do)
    compare(plain(Model.updatesLine(st([], { interrupted: { component: "ipsw", since: 1 } }), false, now).text), "Checked 3 h ago")
    var err = Model.updatesLine(st([], { error: { code: "rate_limited", message: "GitHub is busy. Try again in 20 minutes." } }), false, now)
    compare(err.level, "warn")
    compare(plain(err.text), "Couldn't check: GitHub is busy. Try again in 20 minutes · checked 3 h ago")
    compare(err.tip, "GitHub is busy. Try again in 20 minutes.")
    // the helper's codes read as a few plain words, the full message in the tooltip
    var limited = Model.updatesLine(st([], { error: { code: "rate-limited", message: "GitHub asked iOS Studio to wait; it checks again after 14:05." } }), false, now)
    compare(plain(limited.text), "GitHub asked iOS Studio to wait · checked 3 h ago")
    compare(limited.tip, "GitHub asked iOS Studio to wait; it checks again after 14:05.")
    compare(plain(Model.updatesLine(st([], { error: { code: "partial", message: "x" } }), false, now).text), "Some sources didn't answer · checked 3 h ago")
    compare(plain(Model.updatesLine(st([], { error: { code: "network", message: "x" } }), false, now).text), "Couldn't reach GitHub or PyPI · checked 3 h ago")
  }

  function test_errorTexts() {
    compare(Model.updateErrorText({ code: "busy", message: "A toolchain update is running. Wait for it to finish, then try again." }),
            "Another toolchain update is running. Try again when it's done.")
    compare(Model.updateErrorText({ code: "iphone-busy", message: "The iPhone connection is in use (debugging). Stop that first." }),
            "The iPhone connection is in use (debugging). Stop that first.")
    compare(Model.updateErrorText({ code: "iphone-busy" }), "The iPhone connection is in use. Stop debugging, screen mirroring or hot reload first.")
    compare(Model.updateErrorText({ code: "blocked", message: "Needs the iOS 26.2 SDK." }), "Needs the iOS 26.2 SDK.")
    compare(Model.updateErrorText({ code: "verify-failed" }), "The new version didn't pass its check. The previous version is back.")
    compare(Model.updateErrorText({ code: "cancelled", message: "Cancelled." }), "Update cancelled. The previous version is back.")
    compare(Model.updateErrorText({ code: "network", message: "x" }), "Couldn't reach GitHub or PyPI.")
    compare(Model.updateErrorText({ code: "internal", message: "Something unexpected went wrong." }), "Something unexpected went wrong.")
    compare(Model.updateErrorText(null), "Something went wrong.")
  }

  function test_failureNote() {
    compare(Model.updateFailureNote({ id: "ipsw", name: "ipsw", error: { code: "verify-failed",
              message: "The new ipsw reports version 3.1.700, not 3.1.752. The previous version is back." } }),
            { title: "Updating ipsw failed and was put back", text: "The new ipsw reports version 3.1.700, not 3.1.752." })
    compare(Model.updateFailureNote({ id: "x", name: "xtool", error: { code: "verify-failed" } }).text,
            "The new version didn't pass its check.")
    compare(Model.updateFailureNote(null), null)
  }

  function test_interrupted() {
    var s = st([c("ipsw", "current", { name: "ipsw" })], { interrupted: { component: "ipsw", since: now / 1000 - 600 } })
    var n = Model.updateInterrupted(s)
    compare(n.id, "ipsw")
    compare(n.title, "An update of ipsw was interrupted")
    verify(n.text.indexOf("Put back") >= 0)
    compare(Model.updateInterrupted(st([], { interrupted: { component: "rcodesign" } })).title, "An update of rcodesign was interrupted")
    compare(Model.updateInterrupted(st([])), null)
    compare(Model.updateInterrupted(st([], { interrupted: null })), null)
    compare(Model.updateInterrupted(null), null)
  }

  function test_rowsAndBlocked() {
    var s = st([c("a", "current"), c("b", "update"), c("c", "newer-than-tested"), c("d", "blocked", { applicable: false }),
                c("e", "update", { applicable: false }), c("f", "unknown")])
    compare(Model.updateRows(s).map(function(x) { return x.id }), ["b"])   // newer-than-tested is current on latest
    s.channel = "latest"
    compare(Model.updateRows(s).map(function(x) { return x.id }), ["b"])
    compare(Model.updateBlocked(s).map(function(x) { return x.id }), ["d"])
    compare(Model.updateRows(null), [])
    compare(Model.updateBlocked({}), [])
  }

  function test_reinstallList() {
    var s = st([c("ipsw", "current", { installed: "3.1.752", tested: "3.1.743", target: "3.1.752" }),
                c("apple-dev", "current", { installed: "0123456789abcdef0123456789abcdef01234567", tested: "9f8352fc353d9d4f4112a454a81eb5769812387a" }),
                c("rcodesign", "current"), c("swift", "unknown", { installed: null })], { channel: "latest" })
    compare(Model.updatesReinstall(s), [
      { id: "ipsw", name: "ipsw", from: "3.1.752", to: "3.1.743" },
      { id: "apple-dev", name: "apple-dev", from: "0123456", to: "9f8352f" }
    ])
    compare(Model.updatesReinstall(null), [])
  }

  function test_noticesStayQuietWhenEverythingIsCurrent() {
    var info = { id: "xtool-upstream", level: "info", message: "xtool 1.25.0 is out upstream.", fix: null }
    var warn = { id: "sdk", level: "warn", message: "A newer iOS SDK is out.", fix: null }
    var fix = { id: "xcode", level: "info", message: "Xcode 27.2 is out.", fix: { label: "Open Setup", page: "setup", step: "xcode" } }
    var current = st([c("ipsw", "current")], { notices: [info, warn, fix] })
    compare(Model.updateNotices(current).map(function(n) { return n.id }), ["sdk", "xcode"])
    var busy = st([c("ipsw", "update")], { notices: [info, warn, fix] })
    compare(Model.updateNotices(busy).map(function(n) { return n.id }), ["xtool-upstream", "sdk", "xcode"])
    compare(Model.updateNotices(st([], { notices: [{ id: "x", level: "warn", message: "" }] })), [])
  }

  function test_due() {
    verify(!Model.updatesDue(st([]), now))
    verify(Model.updatesDue(st([], { due: true }), now))
    verify(Model.updatesDue(st([]), now + 22 * 3600 * 1000))     // nextCheckAt passed since the status was read
    verify(!Model.updatesDue(st([], { nextCheckAt: null }), now))
    verify(!Model.updatesDue(null, now))
  }

  function test_versions() {
    compare(Model.updateVersion("9f8352fc353d9d4f4112a454a81eb5769812387a"), "9f8352f")
    compare(Model.updateVersion("3.1.752"), "3.1.752")
    compare(Model.updateVersion(null), "")
    compare(Model.updateVersions(c("ipsw", "update", { installed: "3.1.743", target: "3.1.752" })), "3.1.743 → 3.1.752")
    // on tested, the helper reports a newer untested install as an update back to the tested pin
    var back = c("ipsw", "update", { installed: "3.1.752", tested: "3.1.743", latest: "3.1.752", target: "3.1.743" })
    var tested = st([back])
    verify(Model.updateIsBack(tested, back))
    compare(Model.updateVersions(back, tested), "3.1.752 → tested 3.1.743")
    verify(!Model.updateIsBack(st([back], { channel: "latest" }), back))
    var forward = c("ipsw", "update", { installed: "3.1.700", tested: "3.1.743", latest: "3.1.752", target: "3.1.743" })
    verify(!Model.updateIsBack(st([forward]), forward))
    compare(Model.updateVersions(forward, st([forward])), "3.1.700 → 3.1.743")
    compare(Model.updateVersions(null), "")
  }

  function test_glyphs() {
    compare(Model.toolchainGlyph("xtool"), Model.Glyph.hammer)
    compare(Model.toolchainGlyph("pymobiledevice3"), Model.Glyph.phone)
    compare(Model.toolchainGlyph("apple-dev"), Model.Glyph.folder)
    compare(Model.toolchainGlyph("ipsw"), Model.Glyph.wrench)
  }

  function test_doneText() {
    var status = st([c("ipsw", "current", { name: "ipsw", installed: "3.1.752" })])
    compare(Model.updatesDoneText({ applied: ["ipsw"], status: status }), "ipsw updated to 3.1.752")
    compare(Model.updatesDoneText({ applied: ["ipsw", "rcodesign", "xtool"] }), "3 tools updated")
    compare(Model.updatesDoneText({ applied: [] }), "Nothing to update")
    compare(Model.updatesDoneText(null), "Nothing to update")
    compare(Model.updatesDoneText({ applied: ["gone"], status: status }), "gone updated")
    compare(Model.updatesDoneText({ applied: [], rolledBack: ["ipsw"], status: status }), "The previous ipsw is back")
  }
}
