import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../Model.js" as Model

// Provisioning profiles installed on the iPhone (read-only). Free-team profiles last 7 days.
ColumnLayout {
  id: root
  required property var app
  readonly property var svc: app.device
  readonly property var active: svc.profiles.filter(function(p) { return !p.expired })
  readonly property var expired: svc.profiles.filter(function(p) { return p.expired })
  property bool showExpired: false

  spacing: Style.spacing.lg

  AppText {
    Layout.fillWidth: true
    wrapMode: Text.WordWrap
    tone: "dim"
    text: "A provisioning profile lets one of your apps run on this iPhone. With a free Apple ID each one lasts 7 days — when it runs out the app stops opening until you run it again from Projects."
  }

  Skeleton { Layout.fillWidth: true; visible: root.svc.profilesStatus === "loading" && root.svc.profiles.length === 0; lines: 3; rowHeight: Style.space(40) }
  ErrorState {
    Layout.fillWidth: true
    visible: root.svc.profilesStatus === "error" && root.svc.profiles.length === 0
    app: root.app; error: root.svc.profilesError; title: "Couldn't read the profiles"
    onRetry: root.svc.refreshProfiles()
  }
  EmptyState {
    Layout.fillWidth: true
    visible: root.svc.profilesStatus === "ready" && root.svc.profiles.length === 0
    compact: true; glyph: Model.Glyph.signature
    title: "No profiles on this iPhone"
    text: "They're installed automatically when you run one of your apps from Projects."
  }

  SectionHeader {
    Layout.fillWidth: true
    visible: root.active.length > 0
    text: "Active"; detail: String(root.active.length)
    trailing: [ IconButton { glyph: Model.Glyph.refresh; tooltip: "Reload"; busy: root.svc.profilesStatus === "loading"; onClicked: root.svc.refreshProfiles() } ]
  }
  Repeater {
    model: root.active
    delegate: profileRow
  }

  RowLayout {
    Layout.fillWidth: true
    visible: root.expired.length > 0
    SectionHeader { Layout.fillWidth: true; text: "Expired"; detail: String(root.expired.length) }
    ActionButton { text: root.showExpired ? "Hide" : "Show"; kind: "ghost"; compact: true; onClicked: root.showExpired = !root.showExpired }
  }
  Repeater {
    model: root.showExpired ? root.expired : []
    delegate: profileRow
  }

  Component {
    id: profileRow
    ListRow {
      required property var modelData
      readonly property var exp: Model.expiryStatus(modelData.expires)
      Layout.fillWidth: true
      glyph: Model.Glyph.signature
      glyphColor: Theme.levelColor(exp.level)
      title: {
        var id = modelData.appId || ""
        var dot = id.indexOf(".")
        return dot > 0 ? id.substring(dot + 1) : (modelData.name || id)
      }
      subtitle: "Installed " + Model.relativeTime(modelData.created) + " · " + (modelData.teamName ? modelData.teamName + " · " : "") + "Team " + modelData.team
      trailing: [
        StatusPill { level: exp.level; text: exp.text; anchors.verticalCenter: parent ? parent.verticalCenter : undefined },
        IconButton { glyph: Model.Glyph.copy; tooltip: "Copy profile UUID"; onClicked: { Util.execArgv(["wl-copy", modelData.uuid]); root.app.toast("Copied the profile UUID") } }
      ]
    }
  }
}
