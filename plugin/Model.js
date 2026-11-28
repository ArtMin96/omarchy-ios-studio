.pragma library
// Pure helpers for the iOS Studio bar button (BarWidget.qml), tested in tests/qml/tst_plugin_install.qml.

var MAX_LINES = 20
var MAX_CHARS = 240

// Keeps the last MAX_LINES lines of install.sh's output (changed in place, so a chatty run stays small).
function keepLine(lines, line) {
  lines.push(String(line))
  if (lines.length > MAX_LINES) lines.splice(0, lines.length - MAX_LINES)
  return lines
}

// The reason to show when install.sh fails: its last non-empty line, cut to MAX_CHARS. A timeout (exit 124 from
// timeout(1)) says so instead. "" when there's nothing to add to the plain "couldn't be installed".
function failureReason(lines, exitCode) {
  if (exitCode === 124) return "It took longer than 5 minutes and was stopped."
  for (var i = lines.length - 1; i >= 0; i--) {
    var line = String(lines[i]).trim()
    if (line === "") continue
    return line.length > MAX_CHARS ? line.slice(0, MAX_CHARS - 1) + "…" : line
  }
  return ""
}

// What a click does after `app.sh status`: "open" iOS Studio, offer an "update" (the app folder is behind the
// plugin folder) or offer to "install" it (missing, or the check itself failed).
function statusAction(exitCode, text) {
  if (exitCode !== 0) return "install"
  var s = String(text).trim()
  if (s === "installed") return "open"
  if (s === "outdated") return "update"
  return "install"
}

// A file:// URL from Qt.resolvedUrl as a path for Process.command (spaces and other characters decoded).
function localPath(url) {
  return decodeURIComponent(String(url).replace(/^file:\/\//, ""))
}
