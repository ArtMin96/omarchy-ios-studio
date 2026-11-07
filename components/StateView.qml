import QtQuick
import QtQuick.Layouts
import qs.Commons

// The loading / error / empty / content switch for one Resource (../Resource.qml), so a view doesn't
// wire Skeleton + ErrorState + EmptyState by hand. Children are the content, shown once there's data.
//
//   StateView {
//     Layout.fillWidth: true
//     app: root.app; resource: conditions
//     errorTitle: "Couldn't read the conditions"
//     emptyGlyph: Model.Glyph.speed; emptyTitle: "No conditions offered"; emptyText: "…"
//     skeletonLines: 6; skeletonRowHeight: Style.space(44)
//     Repeater { model: conditions.data.groups; delegate: … }      // laid out in a ColumnLayout
//   }
//
// Data already loaded stays on screen while it reloads and when a reload fails (the Resource keeps it).
// `isEmpty(data)` defaults to "null or an empty array"; give your own for objects. Retry calls
// `retry()` when set, else resource.load(true).
ColumnLayout {
  id: root

  property var app: null
  property var resource: null
  property var isEmpty: function(d) { return d === null || d === undefined || (Array.isArray(d) && d.length === 0) }
  property var retry: null

  property string errorTitle: "Couldn't load this"
  property string emptyGlyph: ""
  property string emptyTitle: "Nothing here yet"
  property string emptyText: ""
  property string emptyActionText: ""
  property string emptyActionGlyph: ""
  property bool compact: true               // the empty state
  property bool errorCompact: false
  property int skeletonLines: 3
  property real skeletonRowHeight: Style.space(40)
  property real skeletonGap: Style.spacing.md
  property alias contentSpacing: body.spacing

  default property alias content: body.data

  signal emptyAction()

  // "loading" | "error" | "empty" | "ready"
  readonly property string phase: {
    var r = root.resource
    if (!r || r.status === "idle" || r.status === "loading") return "loading"
    if (r.status === "error") return "error"
    return root.isEmpty(r.data) ? "empty" : "ready"
  }

  spacing: 0

  Skeleton {
    Layout.fillWidth: true
    visible: root.phase === "loading"
    lines: root.skeletonLines; rowHeight: root.skeletonRowHeight; gap: root.skeletonGap
  }
  ErrorState {
    Layout.fillWidth: true
    visible: root.phase === "error"
    app: root.app
    error: root.resource ? root.resource.error : null
    title: root.errorTitle
    compact: root.errorCompact
    onRetry: if (typeof root.retry === "function") root.retry(); else if (root.resource) root.resource.load(true)
  }
  EmptyState {
    Layout.fillWidth: true
    visible: root.phase === "empty"
    compact: root.compact
    glyph: root.emptyGlyph
    title: root.emptyTitle
    text: root.emptyText
    actionText: root.emptyActionText
    actionGlyph: root.emptyActionGlyph
    onAction: root.emptyAction()
  }
  ColumnLayout {
    id: body
    Layout.fillWidth: true
    visible: root.phase === "ready"
    spacing: Style.spacing.lg
  }
}
