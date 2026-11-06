import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../Model.js" as Model

// A project's Run button with its run preset menu, as one split control (projects slice):
//   [ ▶ Run | ▾ ]
// The main part runs (with the selected preset, see ProjectService.run); the slim chevron part opens
// a menu: "Default (no preset)", the project's presets (a check on the selected one, its language or
// a short summary beside it) and "Edit presets…". Picking one stores it through the helper
// (`projects presets --select`), so Run uses it from then on, also after a restart.
//   RunSplitButton { project: p; svc: app.projects; disabled: …; tooltip: "…"
//                    onRun: …; onEditRequested: … }
// `disabled` (with `tooltip` saying why) only blocks the Run part: the menu stays usable.
// Menu keys: j/k or Up/Down move, Enter picks, Esc closes.
Item {
  id: root

  property var project: ({})
  property var svc: null
  property bool disabled: false
  property string tooltip: ""

  readonly property var presets: svc ? svc.presetsOf(project) : (project.runPresets || [])
  readonly property string selected: svc ? svc.selectedPreset(project) : (project.runPreset || "")
  readonly property bool popupOpen: menu.opened

  // Menu rows: { kind: "default" | "preset" | "edit", name, detail }
  readonly property var rows: {
    var r = [{ kind: "default", name: "Default (no preset)", detail: "" }]
    for (var i = 0; i < presets.length; i++) {
      var p = presets[i]
      r.push({ kind: "preset", name: p.name, detail: p.language ? Model.languageLabel(p.language) + " · " + p.language : Model.presetSummary(p) })
    }
    r.push({ kind: "edit", name: presets.length ? "Edit presets…" : "New preset…", detail: "" })
    return r
  }

  // Menu sizing: grows to its widest row up to menuMaxWidth; past that the details elide first.
  readonly property real menuMaxWidth: Style.space(420)
  readonly property real detailGap: Style.spacing.xxl
  readonly property real nameKeep: Style.space(120)
  readonly property real menuNatural: {
    var m = 0
    for (var i = 0; i < rowRepeater.count; i++) { var it = rowRepeater.itemAt(i); if (it) m = Math.max(m, it.natural) }
    return m
  }

  signal run()
  signal editRequested()

  function openMenu() { menu.open() }
  function closeMenu() { menu.close() }

  function pick(i) {
    var r = root.rows[i]
    if (!r) return
    menu.close()
    if (r.kind === "edit") { root.editRequested(); return }
    var name = r.kind === "preset" ? r.name : ""
    if (name !== root.selected && root.svc) root.svc.selectPreset(root.project, name)
  }

  function rowIsCurrent(r) {
    return (r.kind === "default" && root.selected === "") || (r.kind === "preset" && r.name === root.selected)
  }

  implicitWidth: runPart.implicitWidth + divider.width + menuPart.implicitWidth
  implicitHeight: runPart.implicitHeight

  FontMetrics { id: bodyMetrics; font.family: Style.font.family; font.pixelSize: Style.font.body }

  // The primary wash and border of ActionButton kind "primary", shared by both parts.
  Rectangle {
    anchors.fill: parent
    radius: Style.cornerRadius
    color: Util.alpha(Commons.Color.accent, (runPart.hot && !root.disabled) || menuPart.hot || menu.opened ? 0.30 : 0.22)
    border.width: Style.normalBorderWidth
    border.color: Util.alpha(Commons.Color.accent, 0.85)
    Behavior on color { ColorAnimation { duration: 120 } }
  }

  RowLayout {
    anchors.fill: parent
    spacing: 0

    Button {
      id: runPart
      Layout.fillHeight: true
      text: "Run"
      iconText: Model.Glyph.play
      tooltipText: Host.covers(root, Host.top) ? "" : root.tooltip
      foreground: Commons.Color.foreground
      background: "transparent"
      fontSize: Style.font.body
      iconSize: Style.font.icon
      // ActionButton's (non-compact) padding, so the split button is as tall as every other button.
      verticalPadding: Style.spacing.controlPaddingY
      horizontalPadding: Style.spacing.controlPaddingX + Style.spacing.xs
      opacity: root.disabled ? 0.45 : 1
      onClicked: if (!root.disabled) root.run()
      // disabled: swallow presses (no click, no press animation) while hover still shows the tooltip.
      MouseArea { anchors.fill: parent; visible: root.disabled; cursorShape: Qt.ArrowCursor }
    }

    Rectangle {
      id: divider
      Layout.fillHeight: true
      Layout.topMargin: Style.spacing.sm
      Layout.bottomMargin: Style.spacing.sm
      implicitWidth: Style.normalBorderWidth
      color: Util.alpha(Commons.Color.accent, 0.55)
    }

    Button {
      id: menuPart
      Layout.fillHeight: true
      iconText: Model.Glyph.chevronDown
      tooltipText: menu.opened || Host.covers(root, Host.top) ? "" : (root.selected !== "" ? "Run preset: " + root.selected + ". Change it, or edit presets" : "Pick a run preset: launch arguments, environment and language")
      foreground: Commons.Color.foreground
      background: "transparent"
      iconSize: Style.font.icon
      verticalPadding: Style.spacing.controlPaddingY
      horizontalPadding: Style.spacing.md
      onClicked: menu.opened ? menu.close() : menu.open()
    }
  }

  Popup {
    id: menu
    // Right-aligned under the button (it sits at the right edge of the card), wide enough for the rows.
    // Rounded up: a Text laid out a fraction narrower than its implicit width elides.
    width: Math.max(root.width, Math.min(root.menuMaxWidth, Math.ceil(root.menuNatural + leftPadding + rightPadding) + 1))
    x: root.width - width
    y: root.height + Style.spacing.xxs
    padding: Style.spacing.xs
    closePolicy: Popup.CloseOnPressOutsideParent | Popup.CloseOnEscape
    focus: true
    property int cursor: 0

    background: BorderSurface {
      color: Commons.Color.popups.background
      borderSpec: Border.surfaceSpec("popups", "border", Commons.Color.accent, Style.normalBorderWidth)
      radius: Style.cornerRadius
    }

    onOpened: {
      var at = 0
      for (var i = 0; i < root.rows.length; i++) if (root.rowIsCurrent(root.rows[i])) at = i
      menu.cursor = at
      menuCol.forceActiveFocus()
    }

    contentItem: ColumnLayout {
      id: menuCol
      spacing: 0
      focus: true
      Keys.onPressed: function(event) {
        var a = Model.keyAction(event)
        if (a === "down") { menu.cursor = Math.min(root.rows.length - 1, menu.cursor + 1); event.accepted = true }
        else if (a === "up") { menu.cursor = Math.max(0, menu.cursor - 1); event.accepted = true }
        else if (a === "activate") { root.pick(menu.cursor); event.accepted = true }
        else if (event.key === Qt.Key_Escape) { menu.close(); event.accepted = true }
      }

      Repeater {
        id: rowRepeater
        model: root.rows
        delegate: ColumnLayout {
          id: menuRow
          required property var modelData
          required property int index
          readonly property bool isCurrent: root.rowIsCurrent(modelData)
          readonly property bool hot: menu.cursor === index
          // The width this row needs to show everything (the menu grows to the widest, up to its cap).
          readonly property real natural: Style.spacing.controlPaddingX * 2 + Style.font.icon + Style.spacing.md + nameText.implicitWidth
                                          + (rowLayout.hasDetail ? root.detailGap + detailText.implicitWidth : 0)
          Layout.fillWidth: true
          spacing: 0

          // A hairline above "Edit presets…" sets it apart from the choices.
          Rectangle {
            Layout.fillWidth: true
            Layout.topMargin: Style.spacing.xs
            Layout.bottomMargin: Style.spacing.xs
            visible: menuRow.modelData.kind === "edit"
            implicitHeight: Style.normalBorderWidth
            color: Theme.hairline
          }

          Rectangle {
            Layout.fillWidth: true
            implicitHeight: Math.max(Style.spacing.popupRowHeight, rowLayout.implicitHeight + Style.spacing.sm * 2)
            radius: Style.cornerRadius
            color: menuRow.hot ? Style.hoverFillFor(Commons.Color.foreground, Commons.Color.accent) : "transparent"

            // Columns: glyph, name, then the detail right-aligned in its own column, at least
            // `detailGap` from the name. When the menu is at its widest, the detail elides first and
            // the name only once it is down to `nameKeep`.
            RowLayout {
              id: rowLayout
              anchors.fill: parent
              anchors.leftMargin: Style.spacing.controlPaddingX
              anchors.rightMargin: Style.spacing.controlPaddingX
              spacing: 0
              readonly property bool hasDetail: menuRow.modelData.detail !== ""
              readonly property real textRoom: width - glyphBox.width - Style.spacing.md
              readonly property real detailWidth: hasDetail
                ? Math.max(0, Math.min(detailText.implicitWidth, textRoom - root.detailGap - Math.min(nameText.implicitWidth, root.nameKeep)))
                : 0
              readonly property real nameWidth: Math.max(0, Math.min(nameText.implicitWidth, textRoom - (hasDetail ? root.detailGap + detailWidth : 0)))

              // The glyph box is one text line tall, so the glyph centres on the label's line.
              Item {
                id: glyphBox
                Layout.alignment: Qt.AlignVCenter
                Layout.preferredWidth: Style.font.icon
                Layout.rightMargin: Style.spacing.md
                implicitHeight: bodyMetrics.height
                Icon {
                  anchors.centerIn: parent
                  glyph: menuRow.modelData.kind === "edit" ? (root.presets.length ? Model.Glyph.edit : Model.Glyph.plus)
                         : (menuRow.isCurrent ? Model.Glyph.check : "")
                  size: Style.font.icon
                  color: menuRow.modelData.kind === "edit" ? Theme.dim : Commons.Color.accent
                }
              }
              AppText {
                id: nameText
                Layout.alignment: Qt.AlignVCenter
                Layout.preferredWidth: rowLayout.nameWidth
                text: menuRow.modelData.name
                strong: menuRow.isCurrent
                tone: menuRow.modelData.kind === "default" && !menuRow.isCurrent ? "dim" : "primary"
                elide: Text.ElideRight
              }
              Item { Layout.fillWidth: true; Layout.minimumWidth: rowLayout.hasDetail ? root.detailGap : 0 }
              AppText {
                id: detailText
                Layout.alignment: Qt.AlignVCenter
                Layout.preferredWidth: rowLayout.detailWidth
                visible: rowLayout.hasDetail
                text: menuRow.modelData.detail
                size: "small"
                tone: "faint"
                horizontalAlignment: Text.AlignRight
                elide: Text.ElideRight
              }
            }

            HoverHandler { cursorShape: Qt.PointingHandCursor; onHoveredChanged: if (hovered) menu.cursor = menuRow.index }
            TapHandler { onTapped: root.pick(menuRow.index) }
          }
        }
      }
    }
  }
}
