import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../Model.js" as Model

// App navigation (scaffold-internal). Badges come from each service's `summary`:
// summary.badge (short text) or summary.level ("warn"/"error" → dot).
// While a watch (compile on save, app.watch) runs, Projects also shows a small eye: faint while
// watching, accent while compiling, red after a failed compile; its tooltip names the project.
Rectangle {
  id: root
  property var app: null
  property string current: "home"
  // Icons only (narrow / tiled windows): labels become tooltips, badges become dots.
  property bool compact: false
  signal selected(string key)

  color: Util.alpha(Commons.Color.foreground, Theme.isDark ? 0.025 : 0.04)
  implicitWidth: compact ? Style.space(60) : Style.space(212)

  Rectangle { anchors.right: parent.right; width: Style.spacing.hairline; height: parent.height; color: Theme.hairline }

  ColumnLayout {
    anchors.fill: parent
    anchors.topMargin: Style.spacing.huge + Style.spacing.md
    anchors.bottomMargin: Style.spacing.xxl
    anchors.leftMargin: root.compact ? Style.spacing.md : Style.spacing.xl
    anchors.rightMargin: (root.compact ? Style.spacing.md : Style.spacing.xl) + Style.spacing.hairline
    spacing: Style.spacing.xxs

    // wordmark
    RowLayout {
      Layout.leftMargin: root.compact ? 0 : Style.spacing.lg
      Layout.alignment: root.compact ? Qt.AlignHCenter : Qt.AlignLeft
      Layout.fillWidth: !root.compact
      Layout.bottomMargin: Style.spacing.huge + Style.spacing.md
      spacing: Style.spacing.lg
      Rectangle {
        implicitWidth: Style.font.heading * 1.9
        implicitHeight: implicitWidth
        radius: Style.cornerRadius > 0 ? Math.max(Style.cornerRadius, width * 0.28) : 0
        color: Util.alpha(Commons.Color.accent, 0.16)
        border.width: Style.normalBorderWidth
        border.color: Util.alpha(Commons.Color.accent, 0.5)
        Icon { anchors.centerIn: parent; glyph: Model.Glyph.phone; color: Commons.Color.accent; size: Style.font.heading }
      }
      ColumnLayout {
        visible: !root.compact
        spacing: 0
        AppText { text: "iOS Studio"; size: "title"; strong: true }
        AppText { text: "for Omarchy"; size: "caption"; tone: "faint" }
      }
    }

    Repeater {
      model: Model.PAGES
      delegate: Item {
        id: item
        required property var modelData
        required property int index
        readonly property bool isCurrent: root.current === modelData.key
        readonly property var svc: root.app && modelData.service ? root.app[modelData.service] : null
        readonly property var summary: svc && svc.summary ? svc.summary : null
        readonly property string level: summary && summary.level ? String(summary.level) : ""
        readonly property string badge: summary && summary.badge ? String(summary.badge) : ""
        readonly property bool busy: svc ? svc.status === "loading" : false
        readonly property var watchState: modelData.key === "projects" && root.app && root.app.watch ? root.app.watch.current : null
        // A failed compile on save already turns the eye red: no second red marker for it.
        readonly property bool hideBadgeForWatch: item.watchState !== null && item.summary !== null && item.summary.title === "Last build failed"
        readonly property color watchTint: {
          var w = item.watchState
          if (!w) return Theme.faint
          if (w.state === "compiling" || w.state === "starting" || w.state === "running") return Commons.Color.accent
          if (w.last && !w.last.ok) return Commons.Color.urgent
          return Theme.faint
        }
        readonly property string watchTip: {
          var w = item.watchState
          if (!w || !root.app) return ""
          var l = root.app.watch.label(w)
          return "Watching " + w.name + (l ? " · " + l.text.replace(/^Watching · /, "").replace(/^Watching for changes$/, "no changes yet") : "")
        }

        Layout.fillWidth: true
        implicitHeight: Style.space(34)
        // a little air before Settings
        Layout.topMargin: modelData.key === "settings" ? Style.spacing.lg : 0

        Rectangle {
          anchors.fill: parent
          radius: Style.cornerRadius
          color: item.isCurrent ? Util.alpha(Commons.Color.foreground, 0.08) : (hover.hovered ? Style.hoverFill : "transparent")
          Behavior on color { ColorAnimation { duration: 110 } }
        }
        Rectangle {
          anchors.left: parent.left
          anchors.verticalCenter: parent.verticalCenter
          width: Style.space(2)
          height: parent.height * (item.isCurrent ? 0.6 : 0)
          color: Commons.Color.accent
          Behavior on height { NumberAnimation { duration: 160; easing.type: Easing.OutCubic } }
        }

        RowLayout {
          visible: !root.compact
          anchors.fill: parent
          anchors.leftMargin: Style.spacing.xl
          anchors.rightMargin: Style.spacing.lg
          spacing: Style.spacing.xl
          Icon {
            glyph: item.modelData.glyph
            size: Style.font.icon
            color: item.isCurrent ? Commons.Color.accent : (hover.hovered ? Commons.Color.foreground : Theme.dim)
            Layout.preferredWidth: Style.font.icon * 1.3
          }
          AppText {
            Layout.fillWidth: true
            text: item.modelData.label
            tone: item.isCurrent || hover.hovered ? "primary" : "dim"
            strong: item.isCurrent
            elide: Text.ElideRight
          }
          Icon {
            visible: item.watchState !== null
            glyph: Model.Glyph.eye
            size: Style.font.bodySmall
            color: item.watchTint
            HoverHandler { id: watchHover }
            PanelToolTip { visible: watchHover.hovered && item.watchTip !== ""; text: item.watchTip }
          }
          Spinner { visible: item.busy; running: item.busy; size: Style.font.bodySmall }
          Badge {
            visible: !item.busy && !item.hideBadgeForWatch && (item.badge !== "" || item.level === "warn" || item.level === "error")
            level: item.level === "" ? "info" : item.level
            text: item.badge
          }
          AppText {
            visible: hover.hovered && item.watchState === null && item.badge === "" && !item.busy && item.level !== "warn" && item.level !== "error"
            text: String(item.index + 1)
            size: "caption"
            tone: "faint"
          }
        }
        // compact: centred icon with a status dot
        Item {
          visible: root.compact
          anchors.fill: parent
          Icon {
            anchors.centerIn: parent
            glyph: item.modelData.glyph
            size: Style.font.icon
            color: item.isCurrent ? Commons.Color.accent : (hover.hovered ? Commons.Color.foreground : Theme.dim)
          }
          Badge {
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.margins: Style.spacing.xs
            visible: !item.busy && !item.hideBadgeForWatch && (item.level === "warn" || item.level === "error")
            level: item.level
          }
          // watch mode: a small eye in the bottom corner
          Icon {
            anchors.right: parent.right
            anchors.bottom: parent.bottom
            anchors.margins: Style.spacing.xs
            visible: item.watchState !== null
            glyph: Model.Glyph.eye
            size: Style.font.caption
            color: item.watchTint
          }
        }
        PanelToolTip {
          visible: root.compact && hover.hovered
          text: item.modelData.label + "  " + (item.index + 1) + (item.watchTip !== "" ? "  ·  " + item.watchTip : "")
        }
        HoverHandler { id: hover; cursorShape: Qt.PointingHandCursor }
        TapHandler { onTapped: root.selected(item.modelData.key) }
      }
    }

    Item { Layout.fillHeight: true }

    // footer: shortcut hint
    RowLayout {
      Layout.fillWidth: !root.compact
      Layout.alignment: root.compact ? Qt.AlignHCenter : Qt.AlignLeft
      Layout.leftMargin: root.compact ? 0 : Style.spacing.lg
      spacing: Style.spacing.md
      Rectangle {
        implicitWidth: k.implicitWidth + Style.spacing.md * 2
        implicitHeight: k.implicitHeight + Style.spacing.xxs * 2
        radius: Style.cornerRadius
        color: "transparent"
        border.width: Style.normalBorderWidth
        border.color: Theme.hairline
        AppText { id: k; anchors.centerIn: parent; text: "?"; size: "caption"; tone: "dim" }
      }
      AppText { visible: !root.compact; text: "Keyboard shortcuts"; size: "caption"; tone: footHover.hovered ? "primary" : "faint" }
      HoverHandler { id: footHover; cursorShape: Qt.PointingHandCursor }
      TapHandler { onTapped: if (root.app) root.app.shortcutsOpen = !root.app.shortcutsOpen }
    }
  }
}
