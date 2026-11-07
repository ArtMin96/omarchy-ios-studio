import QtQuick
import QtTest
import "../../lib/Model.js" as Model

// lib/Model.js helpers behind DeviceService's cold open (the iPhone seen last, shown until discovery has
// finished) and its appsRunning event. Run by tests/qml/test_qml_core.py through qmltestrunner.
TestCase {
  name: "DeviceColdOpen"

  readonly property string stateText: JSON.stringify({
    udid: "U1", name: "Alex's iPhone", model: "iPhone 17 Pro", productType: "iPhone18,1", iosVersion: "27.2",
    build: "24B115", connection: "connected", transport: "wifi", wifiPaired: true, usbPaired: false, updatedAt: 1
  })

  function test_knownDeviceFromStateFile() {
    var d = Model.knownDevice(stateText)
    compare(d.udid, "U1")
    compare(d.name, "Alex's iPhone")
    compare(d.ios, "27.2")
    compare(d.transport, null)            // not connected yet, whatever the file says
    compare(d.state, "connecting")
    verify(d.active && d.known)
  }

  function test_knownDeviceNeedsAPairedIPhone() {
    compare(Model.knownDevice(""), null)
    compare(Model.knownDevice("not json"), null)
    compare(Model.knownDevice("null"), null)
    compare(Model.knownDevice(JSON.stringify({ udid: "U1", wifiPaired: false, usbPaired: false })), null)
    compare(Model.knownDevice(JSON.stringify({ name: "x", wifiPaired: true })), null)
  }

  function test_mergeKeepsKnownFieldsTheCacheLacks() {
    var known = Model.knownDevice(stateText)
    var cached = [{ udid: "U2", name: "Other" },
                  { udid: "U1", name: "", model: "iPhone 17 Pro Max", ios: "27.3", state: "idle", active: false, usb: true }]
    var cur = Model.mergeKnownDevice(known, cached)
    compare(cur.name, "Alex's iPhone")    // blank in the cache
    compare(cur.model, "iPhone 17 Pro Max")
    compare(cur.ios, "27.3")
    compare(cur.usb, true)
    compare(cur.state, "connecting")      // never the cache's idle/inactive
    verify(cur.active && cur.known)
    compare(known.model, "iPhone 17 Pro") // the input isn't changed
    compare(Model.mergeKnownDevice(known, []).name, "Alex's iPhone")
  }

  function test_patchRunningCopiesOnlyChangedApps() {
    var a = { bundleId: "a", running: false, pid: null, icon: "/a.png" }
    var b = { bundleId: "b", running: true, pid: 7 }
    var c = { bundleId: "c", running: false, pid: null }
    var list = [a, b, c]
    var out = Model.patchRunning(list, { a: 42, b: null, zz: 3 })
    verify(out !== null && out !== list)
    compare(out.length, 3)
    compare(out[0].running, true); compare(out[0].pid, 42); compare(out[0].icon, "/a.png")
    compare(out[1].running, false); compare(out[1].pid, null)
    verify(out[2] === c)                  // unchanged: the same object
    compare(a.running, false)             // the old list isn't changed
  }

  function test_patchRunningNoChangeIsNull() {
    var list = [{ bundleId: "a", running: true, pid: 5 }, { bundleId: "b", running: false, pid: null }]
    compare(Model.patchRunning(list, { a: 5, b: null }), null)
    compare(Model.patchRunning(list, {}), null)
    compare(Model.patchRunning(list, null), null)
    compare(Model.patchRunning([], { a: 1 }), null)
    // a new pid for a running app is a change
    compare(Model.patchRunning(list, { a: 6 })[0].pid, 6)
  }
}
