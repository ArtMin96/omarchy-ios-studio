import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../common"
import "../../lib/Model.js" as Model

// "New app" sheet (projects slice): name, template gallery, location, bundle ID.
//   ProjectNewSheet { id: newSheet; app: page.app; svc: page.app.projects; onCreated: function(p) { … } }
//   newSheet.open()
Sheet {
  id: root

  property var app: null
  property var svc: null
  property string template: "swiftui-app"
  property bool working: false
  property var error: null
  property bool bundleEdited: false

  signal created(var project)

  title: "New iPhone app"
  subtitle: "Starts from a small SwiftUI project that builds on Linux."
  glyph: Model.Glyph.sparkles
  preferredWidth: Style.space(640)
  busy: working

  readonly property string cleanName: nameField.text.trim()
  readonly property bool nameValid: /^[A-Za-z][A-Za-z0-9_]{0,48}$/.test(cleanName)
  readonly property string nameHint: {
    if (cleanName === "") return "Letters and numbers, starting with a letter. For example: WeatherBuddy"
    if (/\s/.test(cleanName)) return "No spaces. Try " + cleanName.replace(/\s+(\w)/g, function(m, c) { return c.toUpperCase() })
    if (!/^[A-Za-z]/.test(cleanName)) return "Start with a letter."
    if (!nameValid) return "Use only letters, numbers and _."
    return "The folder " + (dirField.text.trim() || "~/Developer").replace(/\/$/, "") + "/" + cleanName + " will be created."
  }
  readonly property string suggestedBundle: (svc ? svc.bundlePrefix : "com.example") + "." + cleanName.replace(/_/g, "-")

  function templateGlyph(g) {
    return { phone: Model.Glyph.phone, tabs: Model.Glyph.apps, list: Model.Glyph.logs }[g] || Model.Glyph.sparkles
  }

  function reset() {
    nameField.text = ""
    root.error = null
    root.working = false
    root.bundleEdited = false
    bundleField.text = ""
    var def = (svc && svc.templates.length) ? svc.templates[0].id : "swiftui-app"
    root.template = def
    dirField.text = svc && svc.defaultDir ? svc.defaultDir.replace(app.home, "~") : "~/Developer"
  }

  function submit() {
    if (!root.nameValid || root.working) return
    root.working = true
    root.error = null
    var dir = dirField.text.trim().replace(/^~(?=\/|$)/, app.home)
    var bundle = bundleField.text.trim() || root.suggestedBundle
    svc.create({ name: root.cleanName, template: root.template, dir: dir, bundleId: bundle }, function(p) {
      root.working = false
      root.close()
      root.created(p)
    }, function(e) {
      root.working = false
      root.error = e
    })
  }

  onShown: { reset(); nameField.forceActiveFocus() }

  // ---------------------------------------------------------------- name
  ColumnLayout {
    Layout.fillWidth: true
    spacing: Style.spacing.sm
    AppText { text: "App name"; strong: true }
    TextField {
      id: nameField
      Layout.fillWidth: true
      placeholderText: "MyFirstApp"
      onAccepted: root.submit()
      onTextChanged: if (!root.bundleEdited) bundleField.text = ""
    }
    AppText { Layout.fillWidth: true; text: root.nameHint; size: "small"; tone: root.cleanName !== "" && !root.nameValid ? "warn" : "faint"; wrapMode: Text.WordWrap }
  }

  // ---------------------------------------------------------------- templates
  ColumnLayout {
    Layout.fillWidth: true
    spacing: Style.spacing.sm
    AppText { text: "Start from"; strong: true }
    GridLayout {
      Layout.fillWidth: true
      columns: 3
      columnSpacing: Style.spacing.md
      rowSpacing: Style.spacing.md
      Repeater {
        model: root.svc ? root.svc.templates : []
        delegate: BorderSurface {
          id: tile
          required property var modelData
          readonly property bool selected: root.template === modelData.id
          Layout.fillWidth: true
          Layout.preferredWidth: 1
          Layout.fillHeight: true
          radius: Style.cornerRadius
          padding: Style.spacing.xl
          color: selected ? Util.alpha(Commons.Color.accent, 0.12) : (tileHover.hovered ? Theme.surfaceRaised : Theme.surface)
          borderSpec: Border.flat(selected ? Commons.Color.accent : Theme.hairline, Style.normalBorderWidth)
          implicitHeight: tileCol.implicitHeight + contentTopInset + contentBottomInset
          Behavior on color { ColorAnimation { duration: 120 } }
          ColumnLayout {
            id: tileCol
            anchors.fill: parent
            anchors.topMargin: tile.contentTopInset
            anchors.bottomMargin: tile.contentBottomInset
            anchors.leftMargin: tile.contentLeftInset
            anchors.rightMargin: tile.contentRightInset
            spacing: Style.spacing.sm
            RowLayout {
              Layout.fillWidth: true
              Icon { glyph: root.templateGlyph(tile.modelData.glyph); size: Style.font.iconLarge; color: tile.selected ? Commons.Color.accent : Theme.dim }
              Item { Layout.fillWidth: true }
              Icon { visible: tile.selected; glyph: Model.Glyph.checkCircle; size: Style.font.icon; color: Commons.Color.accent }
            }
            AppText { Layout.fillWidth: true; text: tile.modelData.title; strong: true; elide: Text.ElideRight }
            AppText { Layout.fillWidth: true; text: tile.modelData.description; size: "small"; tone: "dim"; wrapMode: Text.WordWrap }
            Item { Layout.fillHeight: true }
          }
          HoverHandler { id: tileHover; cursorShape: Qt.PointingHandCursor }
          TapHandler { onTapped: root.template = tile.modelData.id }
        }
      }
    }
  }

  // ---------------------------------------------------------------- location + bundle id
  ColumnLayout {
    Layout.fillWidth: true
    spacing: Style.spacing.sm
    AppText { text: "Location"; strong: true }
    RowLayout {
      Layout.fillWidth: true
      spacing: Style.spacing.md
      TextField { id: dirField; Layout.fillWidth: true; placeholderText: "~/Developer" }
      ActionButton {
        text: "Browse…"
        glyph: Model.Glyph.folder
        onClicked: root.svc.pickFolder(dirField.text.trim().replace(/^~(?=\/|$)/, root.app.home), function(d) {
          if (d.path) dirField.text = d.path.replace(root.app.home, "~")
        })
      }
    }
  }

  ColumnLayout {
    Layout.fillWidth: true
    spacing: Style.spacing.sm
    AppText { text: "Bundle ID"; strong: true }
    TextField {
      id: bundleField
      Layout.fillWidth: true
      placeholderText: root.cleanName ? root.suggestedBundle : "com.example.MyFirstApp"
      onTextEdited: root.bundleEdited = text !== ""
    }
    AppText {
      Layout.fillWidth: true
      text: "Identifies the app on your iPhone. Free Apple IDs can register up to 10 new IDs a week, so keep the one you pick."
      size: "small"; tone: "faint"; wrapMode: Text.WordWrap
    }
  }

  ErrorState {
    Layout.fillWidth: true
    visible: root.error !== null
    app: root.app
    banner: true
    error: root.error
    showRetry: false
  }

  footer: [
    ActionButton { text: "Cancel"; kind: "ghost"; enabled: !root.working; onClicked: root.close() },
    ActionButton { text: "Create app"; glyph: Model.Glyph.plus; kind: "primary"; enabled: root.nameValid; busy: root.working; onClicked: root.submit() }
  ]
}
