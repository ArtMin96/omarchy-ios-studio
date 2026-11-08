pragma Singleton
import QtQuick

// Window-level overlay registry. shell.qml sets `layer` to the Item that
// covers the whole window; Sheet/ConfirmHost reparent into it and push
// themselves on `stack` while open so global keys know an overlay owns input.
QtObject {
  id: root

  property Item layer: null
  property var stack: []
  readonly property int depth: stack.length
  readonly property var top: stack.length ? stack[stack.length - 1] : null

  function push(item) {
    var s = root.stack.slice()
    var i = s.indexOf(item)
    if (i >= 0) s.splice(i, 1)
    s.push(item)
    root.stack = s
  }

  function pop(item) {
    var s = root.stack.slice()
    var i = s.indexOf(item)
    if (i >= 0) { s.splice(i, 1); root.stack = s }
  }

  // True when an overlay is open and `item` isn't inside it: the item is under the dim layer, so
  // its tooltip shouldn't show (the pointer hasn't moved, so its hover never ends). Pass `top` too
  // so a binding re-evaluates when the stack changes: Host.covers(root, Host.top).
  function covers(item, top) {
    var t = top === undefined ? root.top : top
    if (!t || !item) return false
    for (var p = item; p; p = p.parent) if (p === t) return false
    return true
  }

  // Close the top overlay. Returns true when something was closed.
  function closeTop() {
    var t = root.top
    if (!t) return false
    if (typeof t.dismiss === "function") t.dismiss()
    else if (typeof t.close === "function") t.close()
    else root.pop(t)
    return true
  }
}
