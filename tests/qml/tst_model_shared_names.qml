import QtQuick
import QtTest
import "../../lib/Model.js" as Model

// Model.sharedNames / Model.tildePath: two listed apps with one name (a copy of an app in another folder) are
// told apart by their folder on the Projects page. Run by tests/qml/test_qml_core.py.
TestCase {
  name: "ModelSharedNames"

  function test_sharedNames() {
    var items = [
      { name: "CrashLab", path: "/home/me/Projects/CrashLab" },
      { name: "WeatherGlance", path: "/home/me/Developer/WeatherGlance" },
      { name: "CrashLab", path: "/home/me/Projects/ios-studio-wt/crashlab-hr-test" },
    ]
    compare(Model.sharedNames(items), { CrashLab: true })
    compare(Model.sharedNames(items)["WeatherGlance"], undefined)
  }

  function test_sharedNamesThreeCopiesAndOddInput() {
    compare(Model.sharedNames([{ name: "A", path: "/a" }, { name: "A", path: "/b" }, { name: "A", path: "/c" }]), { A: true })
    compare(Model.sharedNames([]), {})
    compare(Model.sharedNames(null), {})
    compare(Model.sharedNames([null, {}, { name: "", path: "/a" }, { name: "", path: "/b" }]), {})
    // names that are also Object members aren't mistaken for a second copy
    compare(Model.sharedNames([{ name: "constructor", path: "/a" }, { name: "toString", path: "/b" }]), {})
    compare(Model.sharedNames([{ name: "constructor", path: "/a" }, { name: "constructor", path: "/b" }]), { constructor: true })
  }

  // Build history (Logs › Build logs, Projects › Recent builds): many builds of one app are one folder.
  function test_sharedNamesBuildHistory() {
    var builds = [
      { name: "CrashLab", path: "/home/me/Projects/CrashLab" },
      { name: "CrashLab", path: "/home/me/Projects/CrashLab" },
      { name: "WeatherGlance", path: "/home/me/Developer/WeatherGlance" },
      { name: "WeatherGlance", path: "/home/me/Developer/WeatherGlance" },
    ]
    compare(Model.sharedNames(builds), {})
    builds.push({ name: "CrashLab", path: "/home/me/Projects/ios-studio-wt/crashlab-hr-test" })
    compare(Model.sharedNames(builds), { CrashLab: true })
  }

  function test_tildePath() {
    compare(Model.tildePath("/home/me/Projects/ios-studio-wt/crashlab-hr-test", "/home/me"), "~/Projects/ios-studio-wt/crashlab-hr-test")
    compare(Model.tildePath("/home/me", "/home/me/"), "~")
    compare(Model.tildePath("/home/meadow/App", "/home/me"), "/home/meadow/App")
    compare(Model.tildePath("/tmp/App", "/home/me"), "/tmp/App")
    compare(Model.tildePath("/tmp/App", ""), "/tmp/App")
    compare(Model.tildePath(undefined, "/home/me"), "")
  }
}
