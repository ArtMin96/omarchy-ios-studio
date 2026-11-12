import QtQuick
import QtTest
import "../../lib/Model.js" as Model

// Model.installProgressText / installWaitMessage: while the iPhone's installer is silent the device daemon's
// installProgress carries a `message`, shown instead of "Installing… 0%" (the Run bar, the app data sheets).
// Run by tests/qml/test_qml_core.py.
TestCase {
  name: "ModelInstallProgress"

  readonly property string waitText: "Waiting for your iPhone. If CrashLab is open on it, close it"

  function test_text() {
    compare(Model.installProgressText({ status: "uploading", percent: 0 }), "Sending the app to your iPhone over Wi-Fi")
    compare(Model.installProgressText({ status: "installing", percent: 40 }), "Installing on your iPhone… 40%")
    compare(Model.installProgressText({ status: "installing", percent: 0, message: waitText }), waitText)
    // the installer answered again: back to the percent
    compare(Model.installProgressText({ status: "installing", percent: 55 }), "Installing on your iPhone… 55%")
    compare(Model.installProgressText({ status: "done", percent: 100, message: waitText }), "Installed")
    compare(Model.installProgressText({ status: "installing" }), "Installing on your iPhone… 0%")
    compare(Model.installProgressText(null), "Installing on your iPhone… 0%")
  }

  function test_waitMessage() {
    compare(Model.installWaitMessage({ status: "installing", percent: 0, message: waitText }), waitText)
    compare(Model.installWaitMessage({ status: "installing", percent: 20 }), "")
    compare(Model.installWaitMessage({ status: "done", message: waitText }), "")
    compare(Model.installWaitMessage(null), "")
  }
}
