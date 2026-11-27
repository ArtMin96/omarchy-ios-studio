import QtQuick
import QtTest
import "../../plugin/Model.js" as Model

// plugin/Model.js: the iOS Studio bar button's output tail and failure reason.
TestCase {
  name: "PluginInstall"

  function test_keepLine_caps_the_tail() {
    var lines = []
    for (var i = 0; i < Model.MAX_LINES + 5; i++) Model.keepLine(lines, "line " + i)
    compare(lines.length, Model.MAX_LINES)
    compare(lines[0], "line 5")
    compare(lines[lines.length - 1], "line " + (Model.MAX_LINES + 4))
  }

  function test_failureReason_is_the_last_line() {
    compare(Model.failureReason(["Installing iOS Studio", "Quickshell (qs) not found", "  "], 1), "Quickshell (qs) not found")
    compare(Model.failureReason([], 1), "")
    compare(Model.failureReason(["", " "], 1), "")
  }

  function test_failureReason_cuts_long_lines() {
    var long = new Array(400).join("x")
    var reason = Model.failureReason([long], 1)
    compare(reason.length, Model.MAX_CHARS)
    verify(reason.endsWith("…"))
  }

  function test_failureReason_names_a_timeout() {
    verify(Model.failureReason(["Copying iOS Studio"], 124).indexOf("5 minutes") >= 0)
  }

  function test_markup_stays_text() {
    compare(Model.failureReason(["<img src=\"https://example.invalid/t\">"], 1), "<img src=\"https://example.invalid/t\">")
  }

  function test_localPath_decodes() {
    compare(Model.localPath("file:///home/a%20b/plugin/app.sh"), "/home/a b/plugin/app.sh")
  }
}
