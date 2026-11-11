import QtQuick
import QtTest
import "../../lib/Model.js" as Model

// Model.hexColor: the app icon sheet's background field. Run by tests/qml/test_qml_core.py.
TestCase {
  name: "ModelHexColor"

  function test_sixDigits() {
    compare(Model.hexColor("#1E90FF"), "#1e90ff")
    compare(Model.hexColor("1e90ff"), "#1e90ff")
    compare(Model.hexColor("  #ABCDEF "), "#abcdef")
  }

  function test_threeDigitsExpand() {
    compare(Model.hexColor("#fff"), "#ffffff")
    compare(Model.hexColor("0a3"), "#00aa33")
  }

  function test_rejectsOthers() {
    compare(Model.hexColor(""), "")
    compare(Model.hexColor(null), "")
    compare(Model.hexColor(undefined), "")
    compare(Model.hexColor("#12345"), "")
    compare(Model.hexColor("#1234567"), "")
    compare(Model.hexColor("#ggg"), "")
    compare(Model.hexColor("white"), "")
    compare(Model.hexColor("##ffffff"), "")
  }
}
