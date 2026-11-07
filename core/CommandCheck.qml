pragma Singleton
import QtQuick
import Quickshell
import Quickshell.Io

// Whether a program is on PATH (`command -v`), shared by every view. check(name) runs one probe at a
// time per name; a program that was found stays found for the session, a missing one is probed again
// on the next check (it may have been installed meanwhile). Call check() when the view that needs
// the answer is shown, not on create, so hidden views spawn nothing.
//   readonly property bool hasUxplay: CommandCheck.found.uxplay === true
//   onActiveChanged: if (active) CommandCheck.check("uxplay")
Singleton {
  id: root

  property var found: ({})          // name -> true | false; absent until the first probe finishes
  property var _running: ({})       // name -> true while a probe runs

  function check(name) {
    if (root.found[name] === true || root._running[name]) return
    var r = Object.assign({}, root._running); r[name] = true; root._running = r
    probe.createObject(root, { name: name })
  }

  function _done(name, ok) {
    var f = Object.assign({}, root.found); f[name] = ok; root.found = f
    var r = Object.assign({}, root._running); delete r[name]; root._running = r
  }

  Component {
    id: probe
    Process {
      id: p
      property string name: ""
      // the name is an argument, never part of the script
      command: ["sh", "-c", "command -v \"$1\" >/dev/null", "sh", name]
      running: true
      onExited: function(code) { root._done(p.name, code === 0); p.destroy() }
    }
  }
}
