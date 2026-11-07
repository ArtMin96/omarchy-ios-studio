import QtQuick
import QtTest
import "../../lib/Model.js" as Model

// lib/Model.js xdgHome(): AppState's config/state/cache folders follow the XDG variables the same way the
// helper does (helper/iosstudio/core/appdirs.py: an unset or empty variable means the default).
TestCase {
  name: "XdgHome"

  function test_setVariableWins() {
    compare(Model.xdgHome("/tmp/x/state", "/home/me", ".local/state"), "/tmp/x/state")
  }
  function test_unsetOrEmptyMeansDefault() {
    compare(Model.xdgHome(undefined, "/home/me", ".local/state"), "/home/me/.local/state")
    compare(Model.xdgHome(null, "/home/me", ".config"), "/home/me/.config")
    compare(Model.xdgHome("", "/home/me", ".cache"), "/home/me/.cache")
  }
}
