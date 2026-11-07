import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../common"
import "../../lib/Model.js" as Model

// App data › Stores: the app's SwiftData / Core Data / SQLite files, read-only. Pick a store and a
// table, see its rows 50 at a time. Core Data's own bookkeeping tables (Z_METADATA, Z_PRIMARYKEY, …)
// are hidden until asked for. Daemon: appdata.stores, appdata.rows.
ColumnLayout {
  id: root
  required property var view                 // DeviceAppDataView
  readonly property var app: view.app
  readonly property var svc: view.svc
  property bool active: false
  readonly property int pageSize: 50

  readonly property string bundleId: view.bundleId
  property string status: "unknown"           // unknown | loading | ready | error
  property var stores: []
  property var error: null
  property string loadedFor: ""
  property string storePath: ""
  property string tableName: ""
  property bool showMeta: false

  property string rowsStatus: "unknown"
  property var page: null                     // appdata.rows reply
  property var rowsError: null

  readonly property var store: {
    for (var i = 0; i < stores.length; i++) if (stores[i].path === storePath) return stores[i]
    return stores.length ? stores[0] : null
  }
  readonly property var allTables: store ? store.tables || [] : []
  readonly property int metaCount: allTables.filter(function(t) { return t.meta }).length
  readonly property var tables: allTables.filter(function(t) { return root.showMeta || !t.meta })
  readonly property var table: {
    for (var i = 0; i < allTables.length; i++) if (allTables[i].name === tableName) return allTables[i]
    return null
  }
  readonly property bool wide: width > Style.space(820)

  spacing: Style.spacing.lg

  onActiveChanged: if (active && loadedFor !== bundleId) load()
  onBundleIdChanged: { stores = []; status = "unknown"; page = null; tableName = ""; storePath = ""; loadedFor = ""; if (active) load() }

  function kindLabel(k) { return k === "swiftdata" ? "SwiftData" : k === "coredata" ? "Core Data" : "SQLite" }
  function tableTitle(t) { return t.entity || t.name }

  function load() {
    if (!root.bundleId) return
    var bid = root.bundleId
    root.status = "loading"
    root.error = null
    root.svc.request("appdata.stores", { bundleId: bid }, function(ok, d) {
      if (bid !== root.bundleId) return
      root.loadedFor = bid
      if (ok) {
        root.stores = root.view.norm(d.stores || [])
        root.status = "ready"
        if (!root.stores.some(function(s) { return s.path === root.storePath })) { root.storePath = root.stores.length ? root.stores[0].path : ""; root.tableName = "" }
        // keep the open table when it's still there, else open the first one with rows
        if (root.table) root.openTable(root.table.name, root.page ? root.page.offset : 0)
        else root._openFirst()
      } else if (d && d.code === "no-store") { root.stores = []; root.status = "ready" }
      else { root.error = d; root.status = "error" }
    }, 60000)
  }

  function _openFirst() {
    var ts = root.tables
    var pick = ts.filter(function(t) { return t.rows > 0 })[0] || ts[0]
    if (pick) root.openTable(pick.name, 0)
    else { root.tableName = ""; root.page = null }
  }

  function chooseStore(path) {
    root.storePath = path
    root.tableName = ""
    root.page = null
    root._openFirst()
  }

  function openTable(name, offset) {
    if (!root.store) return
    var bid = root.bundleId, st = root.store.path
    if (name !== root.tableName) root.page = null
    root.tableName = name
    root.rowsStatus = "loading"
    root.rowsError = null
    root.svc.request("appdata.rows", { bundleId: bid, store: st, table: name, offset: Math.max(0, offset), limit: root.pageSize }, function(ok, d) {
      if (bid !== root.bundleId || name !== root.tableName) return
      if (ok) { root.page = root.view.norm(d); root.rowsStatus = "ready" }
      else { root.rowsError = d; root.rowsStatus = "error" }
    }, 60000)
  }

  // ---- states
  Skeleton { Layout.fillWidth: true; visible: root.status === "loading" && root.stores.length === 0; lines: 5; rowHeight: Style.space(40); gap: Style.spacing.md }
  ErrorState {
    Layout.fillWidth: true
    visible: root.status === "error"
    app: root.app
    error: root.error
    title: root.error && root.error.code === "not-developer" ? "Only your own builds expose their stores" : "Couldn't read the app's stores"
    onRetry: root.load()
  }
  EmptyState {
    Layout.fillWidth: true
    visible: root.status === "ready" && root.stores.length === 0
    compact: true
    glyph: Model.Glyph.database
    title: "No database yet"
    text: "Run " + root.view.appName + " and use it, then look again."
  }

  // ---- store picker. A Flow, so on a short line the facts move under the picker whole instead of being cut.
  Flow {
    id: storeRow
    Layout.fillWidth: true
    visible: root.status !== "error" && root.stores.length > 0
    // side by side, the facts start on the table panel's edge (the grid below uses columnSpacing huge)
    spacing: storePicker.visible && root.wide ? Style.spacing.huge : Style.spacing.lg
    readonly property real lineH: storePicker.implicitHeight
    Dropdown {
      id: storePicker
      visible: root.stores.length > 1
      showLabel: false
      width: Math.min(Style.space(280), storeRow.width)
      value: root.store ? root.store.path : ""
      options: root.stores.map(function(s) { return { value: s.path, label: s.name } })
      onChanged: function(v) { root.chooseStore(v) }
    }
    Row {
      id: storeName
      visible: root.stores.length === 1
      height: storeRow.lineH
      spacing: Style.spacing.lg
      Icon { anchors.verticalCenter: parent.verticalCenter; glyph: Model.Glyph.database; size: Style.font.icon; color: Theme.dim }
      AppText { anchors.verticalCenter: parent.verticalCenter; text: root.store ? root.store.name : ""; strong: true }
    }
    AppText {
      id: storeLine
      visible: !!root.store
      height: storeRow.lineH
      verticalAlignment: Text.AlignVCenter
      readonly property string shortText: root.store ? root.kindLabel(root.store.kind) + "  ·  " + Model.formatBytes(root.store.size) + "  ·  " + Model.plural(root.allTables.length - root.metaCount, "table") : ""
      readonly property string fullText: root.store ? shortText + "  ·  " + root.store.path.substring(0, root.store.path.lastIndexOf("/")) : ""
      // beside the picker when it fits; the folder is the first thing to go
      readonly property real besides: storeRow.width - (storePicker.visible ? storePicker.width : storeName.implicitWidth) - storeRow.spacing
      text: fullMetrics.advanceWidth <= besides ? fullText : shortMetrics.advanceWidth <= besides ? shortText
            : fullMetrics.advanceWidth <= storeRow.width ? fullText : shortText
      TextMetrics { id: fullMetrics; font: storeLine.font; text: storeLine.fullText }
      TextMetrics { id: shortMetrics; font: storeLine.font; text: storeLine.shortText }
      size: "small"; tone: "faint"
    }
  }

  ErrorState {
    Layout.fillWidth: true
    visible: root.status === "ready" && !!root.store && !!root.store.error
    compact: true
    app: root.app
    error: root.store ? root.store.error : null
    title: "Couldn't read " + (root.store ? root.store.name : "this store")
    onRetry: root.load()
  }

  // ---- tables + rows
  GridLayout {
    Layout.fillWidth: true
    visible: root.status !== "error" && root.stores.length > 0 && !(root.store && root.store.error)
    columns: root.wide ? 2 : 1
    columnSpacing: Style.spacing.huge
    rowSpacing: Style.spacing.lg

    ColumnLayout {
      Layout.preferredWidth: root.wide ? Style.space(280) : -1
      Layout.fillWidth: !root.wide
      Layout.alignment: Qt.AlignTop
      spacing: Style.spacing.xxs
      Repeater {
        model: root.tables
        delegate: ListRow {
          id: trow
          required property var modelData
          Layout.fillWidth: true
          property bool hot: false
          current: root.tableName === modelData.name
          hasCursor: hot && !current
          onHovered: function(h) { trow.hot = h }
          glyph: Model.Glyph.table
          glyphColor: modelData.meta ? Theme.faint : Theme.dim
          title: root.tableTitle(modelData)
          subtitle: modelData.entity && modelData.entity !== modelData.name ? modelData.name : ""
          meta: modelData.rows === null || modelData.rows === undefined ? "" : modelData.rows === 1 ? "1 row" : Number(modelData.rows).toLocaleString(Qt.locale(), "f", 0) + " rows"
          opacity: modelData.meta ? 0.7 : 1
          onClicked: root.openTable(modelData.name, 0)
        }
      }
      // Core Data's bookkeeping tables, under the list they join
      RowLayout {
        Layout.fillWidth: true
        Layout.topMargin: Style.spacing.md
        visible: root.metaCount > 0
        spacing: Style.spacing.md
        ToggleSwitch {
          Layout.leftMargin: cursorRing ? -cursorPad : 0
          checked: root.showMeta
          onToggled: { root.showMeta = !checked; if (!root.showMeta && root.table && root.table.meta) root._openFirst() }
        }
        AppText { Layout.fillWidth: true; text: "Show internal tables (" + root.metaCount + ")"; size: "small"; tone: "dim" }
      }
      QuietNote {
        Layout.fillWidth: true
        visible: root.tables.length === 0
        glyph: Model.Glyph.table
        title: "No tables of your own"
        text: "This store only has internal tables so far."
      }
    }

    ColumnLayout {
      Layout.fillWidth: true
      Layout.alignment: Qt.AlignTop
      spacing: Style.spacing.lg
      visible: !!root.table

      RowLayout {
        Layout.fillWidth: true
        spacing: Style.spacing.lg
        AppText { text: root.table ? root.tableTitle(root.table) : ""; strong: true; elide: Text.ElideRight; Layout.maximumWidth: Style.space(260) }
        AppText {
          Layout.fillWidth: true
          text: {
            var p = root.page
            if (!p || !p.total) return p ? "No rows" : ""
            var a = p.offset + 1, b = p.offset + p.rows.length
            return "Rows " + a + "–" + b + " of " + Number(p.total).toLocaleString(Qt.locale(), "f", 0)
          }
          size: "small"; tone: "faint"; elide: Text.ElideRight
        }
        IconButton {
          glyph: Model.Glyph.chevronLeft
          tooltip: "Previous " + root.pageSize
          disabled: !root.page || root.page.offset === 0 || root.rowsStatus === "loading"
          onClicked: root.openTable(root.tableName, root.page.offset - root.pageSize)
        }
        IconButton {
          glyph: Model.Glyph.chevronRight
          tooltip: "Next " + root.pageSize
          disabled: !root.page || !root.page.more || root.rowsStatus === "loading"
          busy: root.rowsStatus === "loading" && !!root.page
          onClicked: root.openTable(root.tableName, root.page.offset + root.pageSize)
        }
      }

      Skeleton { Layout.fillWidth: true; visible: root.rowsStatus === "loading" && !root.page; lines: 6; rowHeight: Style.space(28); gap: Style.spacing.xs }
      ErrorState {
        Layout.fillWidth: true
        visible: root.rowsStatus === "error"
        compact: true
        app: root.app
        error: root.rowsError
        title: "Couldn't read this table"
        onRetry: root.openTable(root.tableName, root.page ? root.page.offset : 0)
      }
      DeviceAppDataGrid {
        Layout.fillWidth: true
        visible: root.rowsStatus !== "error" && !!root.page && root.page.rows.length > 0
        opacity: root.rowsStatus === "loading" ? 0.5 : 1
        // the store summary knows Core Data's own columns (Z_PK, Z_ENT, Z_OPT): mark them for a quieter header
        columns: {
          if (!root.page) return []
          var meta = {}
          var tc = root.table && root.table.columns ? root.table.columns : []
          for (var i = 0; i < tc.length; i++) if (tc[i].meta) meta[tc[i].name] = true
          return root.page.columns.map(function(c) { return Object.assign({}, c, { meta: !!meta[c.name] }) })
        }
        rows: root.page ? root.page.rows : []
        offset: root.page ? root.page.offset : 0
      }
      QuietNote {
        Layout.fillWidth: true
        visible: root.rowsStatus === "ready" && !!root.page && root.page.rows.length === 0
        glyph: Model.Glyph.table
        title: "This table is empty"
        text: root.view.appName + " hasn't saved anything here yet."
      }
    }
  }

}
