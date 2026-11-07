import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../components/account"
import "../components/common"
import "../lib/Model.js" as Model

// Apple ID: who you're signed in as, signing health, and what Apple has on file
// (certificates, profiles, App IDs, devices). Account slice.
PageFrame {
  id: page
  required property var app
  readonly property var svc: app.account

  title: "Apple ID"
  subtitle: svc.signedIn ? "The account iOS Studio signs your apps with." : "Sign in once so iOS Studio can put apps on your iPhone."
  glyph: ""
  status: StatusPill {
    visible: page.svc.signedIn && page.svc.team !== null
    level: page.svc.free ? "info" : "ok"
    showGlyph: false
    text: page.svc.free ? "Free account" : "Paid account"
  }
  actions: [
    AppText {
      Layout.alignment: Qt.AlignVCenter
      visible: page.svc.signedIn && page.svc.fetchedAtMs > 0
      text: page.svc.detailsLoading ? "Checking with Apple…" : "Updated " + Model.relativeTime(page.svc.fetchedAtMs, page.svc.now)
      size: "small"; tone: "faint"
    },
    ActionButton {
      visible: page.svc.signedIn
      text: "Refresh"; glyph: Model.Glyph.refresh; kind: "ghost"
      busy: page.svc.detailsLoading
      onClicked: page.refresh()
    }
  ]

  // ------------------------------------------------------------ page contract
  property int cursor: -1

  function activated(params) {
    if (params && (params.action === "signin" || params.action === "sign-in")) openSignIn()
    // fix deep links: {action: "certificates" | "identifiers" | "profiles" | "devices"} → scroll to that list
    var keys = ["profiles", "certificates", "identifiers", "devices"]
    var si = params ? keys.indexOf(String(params.action || "")) : -1
    if (si >= 0) Qt.callLater(function() { var it = sectionList.itemAt(si); if (it) page.scrollToItem(it) })
    if (page.svc.status === "unknown") page.svc.refresh()
  }
  function refresh() { page.svc.refresh(); if (page.svc.signedIn) page.svc.refreshDetails() }
  function openSignIn() { signInSheet.begin() }

  // flattened keyboard rows across all lists
  readonly property var rows: {
    var out = []
    var add = function(kind, items) { for (var i = 0; i < items.length; i++) out.push({ kind: kind, item: items[i] }) }
    add("profile", items("profiles"))
    add("certificate", items("certificates"))
    add("identifier", items("identifiers"))
    add("device", items("devices"))
    return out
  }
  function items(name) { var s = page.svc.sections[name]; return s && s.items ? s.items : [] }
  function rowIndex(kind, i) {
    var order = ["profile", "certificate", "identifier", "device"], names = ["profiles", "certificates", "identifiers", "devices"]
    var off = 0
    for (var k = 0; k < order.length && order[k] !== kind; k++) off += items(names[k]).length
    return off + i
  }
  function copyRow(r) {
    if (!r) return
    var v = "", what = ""
    if (r.kind === "profile") { v = r.item.displayBundleId || r.item.name; what = "the bundle ID" }
    else if (r.kind === "certificate") { v = r.item.serial; what = "the serial number" }
    else if (r.kind === "identifier") { v = r.item.displayBundleId || r.item.bundleId; what = "the bundle ID" }
    else if (r.kind === "device") { v = r.item.udid; what = "the UDID" }
    copy(v, what)
  }
  function copy(v, what) {
    if (v) page.app.copyText(v, what)
  }

  function handleKey(event) {
    if (!page.svc.signedIn || page.rows.length === 0) return false
    var a = Model.keyAction(event)
    if (a === "down") { page.cursor = Math.min(page.rows.length - 1, page.cursor + 1); return true }
    if (a === "up") { page.cursor = Math.max(0, page.cursor - 1); return true }
    if (a === "home") { page.cursor = 0; return true }
    if (a === "end") { page.cursor = page.rows.length - 1; return true }
    if ((a === "activate" || a === "copy") && page.cursor >= 0) { copyRow(page.rows[page.cursor]); return true }
    return false
  }

  function goBack() {
    if (page.cursor >= 0) { page.cursor = -1; return true }
    return false
  }

  Connections {
    target: page.svc
    function onSignInRequestedChanged() {
      if (page.svc.signInRequested) { page.svc.signInRequested = false; page.openSignIn() }
    }
  }

  AccountSignInSheet { id: signInSheet; app: page.app }

  // ================================================================ checking
  LoadingState {
    Layout.fillWidth: true
    Layout.topMargin: Style.spacing.huge
    visible: !page.svc.auth && (page.svc.status === "loading" || page.svc.status === "unknown")
    text: "Checking your Apple ID…"
  }

  ErrorState {
    Layout.fillWidth: true
    visible: !page.svc.auth && (page.svc.status === "error" || page.svc.status === "needs-setup")
    app: page.app
    title: page.svc.status === "needs-setup" ? "One thing to install first" : "Couldn't check your Apple ID"
    error: page.svc.error
    onRetry: page.svc.refresh()
  }

  // ================================================================ signed out
  BorderSurface {
    Layout.fillWidth: true
    visible: !!page.svc.auth && !page.svc.signedIn
    radius: Style.cornerRadius
    color: Util.alpha(Commons.Color.accent, 0.06)
    borderSpec: Border.flat(Util.alpha(Commons.Color.accent, 0.28), Style.normalBorderWidth)
    implicitHeight: outRow.implicitHeight + Style.spacing.panelPadding * 2

    RowLayout {
      id: outRow
      anchors.fill: parent
      anchors.margins: Style.spacing.panelPadding
      anchors.leftMargin: Style.spacing.panelPadding + Style.spacing.md
      spacing: Style.spacing.huge + Style.spacing.md

      Icon { Layout.alignment: Qt.AlignTop; glyph: Model.Glyph.apple; size: Style.font.displayLarge * 2; color: Commons.Color.accent }

      ColumnLayout {
        Layout.fillWidth: true
        spacing: Style.spacing.lg
        AppText { Layout.fillWidth: true; text: "Sign in with your Apple ID"; size: "title"; strong: true; wrapMode: Text.Wrap }
        AppText {
          Layout.fillWidth: true
          text: "iPhones only run apps signed by Apple. iOS Studio uses your Apple ID to get that signature, the same way Xcode does on a Mac. Your everyday Apple ID is fine: you don't need to pay for a developer account."
          tone: "dim"; wrapMode: Text.Wrap; lineHeight: 1.2
        }
        GridLayout {
          Layout.fillWidth: true
          Layout.topMargin: Style.spacing.sm
          columns: 2
          columnSpacing: Style.spacing.lg
          rowSpacing: Style.spacing.md
          Icon { glyph: Model.Glyph.timer; color: Theme.dim; size: Style.font.bodySmall }
          AppText { Layout.fillWidth: true; text: "With a free account, apps stay signed for 7 days. Renewing takes one click."; wrapMode: Text.Wrap }
          Icon { glyph: Model.Glyph.phone; color: Theme.dim; size: Style.font.bodySmall }
          AppText { Layout.fillWidth: true; text: "Up to 3 of your own apps on each iPhone at a time."; wrapMode: Text.Wrap }
          Icon { glyph: Model.Glyph.shieldCheck; color: Theme.dim; size: Style.font.bodySmall }
          AppText { Layout.fillWidth: true; text: "Your password goes straight to Apple, and you'll confirm with a code on your iPhone."; wrapMode: Text.Wrap }
        }
        RowLayout {
          Layout.topMargin: Style.spacing.md
          spacing: Style.spacing.md
          ActionButton { text: "Sign in"; glyph: Model.Glyph.arrowRight; kind: "primary"; onClicked: page.openSignIn() }
          ActionButton {
            text: "Create an Apple ID"; glyph: Model.Glyph.openExternal; kind: "ghost"
            onClicked: Qt.openUrlExternally("https://account.apple.com/account")
          }
        }
      }
    }
  }

  // ================================================================ signed in
  ColumnLayout {
    Layout.fillWidth: true
    visible: page.svc.signedIn
    spacing: Style.spacing.huge      // same gap between cards as the page itself

    // ---------------------------------------------------- identity + health
    GridLayout {
      Layout.fillWidth: true
      columns: page.width > Style.space(940) ? 2 : 1
      columnSpacing: Style.spacing.xxl
      rowSpacing: Style.spacing.huge

      // identity
      Card {
        Layout.fillWidth: true
        Layout.fillHeight: true
        Layout.preferredWidth: 3
        contentSpacing: Style.spacing.md

        RowLayout {
          Layout.fillWidth: true
          spacing: Style.spacing.xl
          Rectangle {
            Layout.alignment: Qt.AlignTop
            implicitWidth: Style.font.displayLarge * 2.2; implicitHeight: implicitWidth
            radius: width / 2
            color: Util.alpha(Commons.Color.accent, 0.12)
            AppText {
              anchors.centerIn: parent
              text: {
                var t = page.svc.team ? page.svc.team.name : (page.svc.auth && page.svc.auth.appleId ? page.svc.auth.appleId : "")
                var p = String(t).split(/[\s@.]+/).filter(function(s) { return s.length > 0 })
                return p.length === 0 ? "" : (p[0].charAt(0) + (p.length > 1 && page.svc.team ? p[1].charAt(0) : "")).toUpperCase()
              }
              size: "title"; strong: true; tone: "accent"
            }
            Icon { anchors.centerIn: parent; visible: !page.svc.team && !(page.svc.auth && page.svc.auth.appleId); glyph: Model.Glyph.apple; color: Commons.Color.accent; size: Style.font.heading }
          }
          ColumnLayout {
            Layout.fillWidth: true
            spacing: Style.spacing.xxs
            AppText {
              Layout.fillWidth: true
              text: page.svc.team ? page.svc.team.name : (page.svc.keyMode ? "App Store Connect API key" : (page.svc.detailsLoading ? "Loading your team…" : "Your team"))
              size: "heading"; strong: true; elide: Text.ElideRight
            }
            AppText {
              Layout.fillWidth: true
              text: page.svc.keyMode ? ("Key " + (page.svc.auth.keyId || "")) : (page.svc.auth && page.svc.auth.appleId ? page.svc.auth.appleId : "")
              tone: "dim"; elide: Text.ElideRight
            }
          }
        }

        Item { implicitHeight: Style.spacing.xs }

        InfoRow {
          Layout.fillWidth: true
          visible: !page.svc.keyMode
          label: "Team ID"; glyph: Model.Glyph.idCard
          value: page.svc.auth && page.svc.auth.teamId ? page.svc.auth.teamId : ""
          copyable: true
          onCopied: page.app.toast("Team ID copied", "ok")
        }
        InfoRow {
          Layout.fillWidth: true
          visible: !page.svc.keyMode
          label: "Membership"; glyph: Model.Glyph.briefcase
          value: page.svc.team && page.svc.team.memberships && page.svc.team.memberships.length
                 ? (page.svc.free ? "Free (Xcode free provisioning)" : page.svc.team.memberships[0].name)
                 : (page.svc.detailsLoading ? "Checking…" : "")
        }
        InfoRow {
          Layout.fillWidth: true
          visible: !page.svc.keyMode
          label: "Signed in until"; glyph: Model.Glyph.key
          readonly property var exp: page.svc.auth && page.svc.auth.tokenExpiry ? page.svc.auth.tokenExpiry : null
          value: exp ? (exp.ts ? new Date(exp.ts * 1000).toLocaleDateString(Qt.locale(), "d MMMM yyyy") + "  ·  " + Model.relativeTime(exp.ts * 1000, page.svc.now) : exp.text) : ""
          level: exp && exp.ts ? page.svc.expiryFor(exp.ts * 1000).level === "ok" ? "" : page.svc.expiryFor(exp.ts * 1000).level : ""
        }
        InfoRow {
          Layout.fillWidth: true
          visible: page.svc.keyMode
          label: "Issuer ID"; value: page.svc.auth && page.svc.auth.issuerId ? page.svc.auth.issuerId : ""; copyable: true
          onCopied: page.app.toast("Issuer ID copied", "ok")
        }
      }

      // signing health
      BorderSurface {
        id: healthCard
        Layout.fillWidth: true
        Layout.fillHeight: true
        Layout.preferredWidth: 2
        visible: !page.svc.keyMode
        readonly property var h: page.svc.health
        readonly property var e: page.svc.signingExpiry
        readonly property string lv: e ? e.level : (h ? Model.level(h.level) : "info")
        readonly property bool loading: !page.svc.details && page.svc.detailsLoading
        radius: Style.cornerRadius
        color: lv === "ok" || lv === "info" ? Theme.surface : Theme.levelFill(lv)
        borderSpec: Border.flat(lv === "ok" || lv === "info" ? Theme.hairline : Util.alpha(Theme.levelColor(lv), 0.45), Style.normalBorderWidth)
        implicitHeight: hCol.implicitHeight + Style.spacing.panelPadding * 2
        implicitWidth: Style.space(280)

        ColumnLayout {
          id: hCol
          anchors.fill: parent
          anchors.margins: Style.spacing.panelPadding
          spacing: Style.spacing.md

          // header styled like a Card title
          RowLayout {
            Layout.fillWidth: true
            spacing: Style.spacing.lg
            Icon { glyph: Model.Glyph.signature; color: healthCard.lv === "info" ? Theme.dim : Theme.levelColor(healthCard.lv); size: Style.font.icon }
            AppText { Layout.fillWidth: true; text: "App signing"; size: "title"; strong: true }
          }

          Skeleton { Layout.fillWidth: true; visible: healthCard.loading; lines: 3; rowHeight: Style.font.heading }

          // countdown
          RowLayout {
            Layout.fillWidth: true
            visible: !healthCard.loading && !!healthCard.e
            spacing: Style.spacing.md
            AppText {
              text: {
                var e = healthCard.e
                if (!e) return ""
                if (e.hours <= 0) return "Expired"
                if (e.hours < 48) return Math.max(1, Math.round(e.hours)) + ""
                return e.days + ""
              }
              size: "displayLarge"; strong: true
              tone: healthCard.lv === "ok" ? "primary" : healthCard.lv
            }
            AppText {
              Layout.alignment: Qt.AlignBaseline
              visible: healthCard.e && healthCard.e.hours > 0
              text: healthCard.e ? (healthCard.e.hours < 48 ? (Math.round(healthCard.e.hours) === 1 ? "hour left" : "hours left") : (healthCard.e.days === 1 ? "day left" : "days left")) : ""
              tone: "dim"
            }
          }
          ProgressTrack {
            Layout.fillWidth: true
            visible: !healthCard.loading && !!healthCard.e
            value: healthCard.e ? Model.clamp(healthCard.e.hours / ((page.svc.free ? 7 : 365) * 24), 0, 1) : 0
            level: healthCard.lv === "ok" ? "ok" : healthCard.lv
          }
          AppText {
            Layout.fillWidth: true
            visible: !healthCard.loading && page.svc.signingExpiresMs > 0
            text: (healthCard.e && healthCard.e.hours <= 0 ? "Expired " : "Expires ") + new Date(page.svc.signingExpiresMs).toLocaleString(Qt.locale(), "dddd d MMMM, HH:mm")
            size: "small"
          }
          AppText {
            Layout.fillWidth: true
            visible: !healthCard.loading
            text: healthCard.h ? healthCard.h.detail : (page.svc.detailsError ? "Couldn't check signing right now." : "")
            tone: "dim"; wrapMode: Text.Wrap; size: "small"
          }
          Item { Layout.fillHeight: true }
          ActionButton {
            visible: !healthCard.loading && !!healthCard.e
            text: "Renew now"; glyph: Model.Glyph.sync
            kind: healthCard.lv === "warn" || healthCard.lv === "error" ? "primary" : "secondary"
            onClicked: page.app.nav("projects", { action: "renew" })
          }
        }
      }
    }

    ErrorState {
      Layout.fillWidth: true
      visible: !!page.svc.detailsError && !page.svc.detailsLoading
      banner: true
      app: page.app
      error: page.svc.detailsError
      onRetry: page.svc.refreshDetails()
    }

    AppText {
      Layout.fillWidth: true
      visible: page.svc.keyMode
      text: "You're signed in with an App Store Connect API key. iOS Studio can build and upload with it, but certificates, profiles and devices are only listed for Apple ID sign-ins."
      tone: "dim"; wrapMode: Text.Wrap
    }

    // ---------------------------------------------------- free-account limits
    Card {
      Layout.fillWidth: true
      visible: page.svc.free
      title: "Free account limits"
      subtitle: "Apple's rules for Apple IDs without a paid developer membership."
      glyph: Model.Glyph.info
      contentSpacing: Style.spacing.lg

      GridLayout {
        Layout.fillWidth: true
        columns: page.width > Style.space(820) ? 3 : 1
        columnSpacing: Style.spacing.huge
        rowSpacing: Style.spacing.lg
        Repeater {
          model: {
            var c = page.svc.info.counts || {}, l = page.svc.info.limits || {}
            return [
              { label: "New App IDs, last 7 days", used: c.appIdsThisWeek || 0, max: l.appIdsPerWeek || 10,
                note: (c.appIds || 0) + " registered in total. Each new bundle ID uses one." },
              { label: "Registered devices", used: c.devices || 0, max: l.devices || 3,
                note: "iPhones and iPads your apps can run on." },
              { label: "Signing lasts", used: -1, max: l.profileDays || 7,
                note: "Apps stop opening after " + (l.profileDays || 7) + " days unless renewed." }
            ]
          }
          delegate: ColumnLayout {
            required property var modelData
            Layout.fillWidth: true
            Layout.preferredWidth: 1
            Layout.alignment: Qt.AlignTop
            spacing: Style.spacing.sm
            readonly property real frac: modelData.used < 0 ? 1 : modelData.used / Math.max(1, modelData.max)
            readonly property string lv: modelData.used < 0 ? "" : (frac >= 1 ? "error" : (frac >= 0.7 ? "warn" : ""))
            RowLayout {
              Layout.fillWidth: true
              AppText { Layout.fillWidth: true; text: modelData.label; tone: "dim"; size: "small"; elide: Text.ElideRight }
              AppText {
                text: modelData.used < 0 ? modelData.max + " days" : modelData.used + " of " + modelData.max
                strong: true; tone: parent.parent.lv === "" ? "primary" : parent.parent.lv
              }
            }
            ProgressTrack { Layout.fillWidth: true; visible: modelData.used >= 0; value: parent.frac; level: parent.lv }
            AppText { Layout.fillWidth: true; text: modelData.note; size: "small"; tone: "faint"; wrapMode: Text.Wrap }
          }
        }
      }
    }

    // ---------------------------------------------------- lists
    Repeater {
      id: sectionList
      model: page.svc.keyMode || (!page.svc.details && !page.svc.detailsLoading) ? [] : [
        { key: "profiles", kind: "profile", title: "Provisioning profiles", glyph: Model.Glyph.signature,
          about: "A profile lets one app run on your registered devices. Free profiles last 7 days.",
          empty: "No profiles yet. One is created the first time you run an app on your iPhone." },
        { key: "certificates", kind: "certificate", title: "Certificates", glyph: Model.Glyph.cert,
          about: "Your signing identity. Click a row to copy its serial number.",
          empty: "No certificates yet. iOS Studio creates one the first time it signs an app." },
        { key: "identifiers", kind: "identifier", title: "App IDs", glyph: Model.Glyph.apps,
          about: "Bundle IDs registered with Apple. Click one to copy it.",
          empty: "No App IDs yet. Each app you run gets one automatically." },
        { key: "devices", kind: "device", title: "Registered devices", glyph: Model.Glyph.phone,
          about: "Devices your apps are allowed to run on. Click one to copy its UDID.",
          empty: "No devices yet. Your iPhone is registered the first time you run an app on it." }
      ]
      delegate: Card {
        id: sec
        required property var modelData
        readonly property var section: page.svc.sections[modelData.key] || null
        readonly property var list: section && section.items ? section.items : []
        readonly property bool loading: !section && page.svc.detailsLoading
        Layout.fillWidth: true
        title: modelData.title
        glyph: modelData.glyph
        subtitle: sec.loading ? "Asking Apple…" : modelData.about
        contentSpacing: Style.spacing.xs
        actions: [
          StatusPill { visible: !!sec.section && sec.section.stale === true && !!sec.section.error; level: "warn"; text: "Showing saved copy"; quiet: true },
          AppText {
            visible: !!sec.section && sec.section.ok !== false && sec.list.length > 0
            text: sec.modelData.key === "devices" && page.svc.free ? sec.list.length + " of 3" : String(sec.list.length)
            tone: "dim"; strong: true
          }
        ]

        Skeleton { Layout.fillWidth: true; visible: sec.loading; lines: 2; rowHeight: Style.space(36); gap: Style.spacing.md }

        ErrorState {
          Layout.fillWidth: true
          visible: !!sec.section && !!sec.section.error && !sec.section.ok
          banner: true
          app: page.app
          error: sec.section ? sec.section.error : null
          onRetry: page.svc.refreshDetails()
        }

        AppText {
          Layout.fillWidth: true
          Layout.topMargin: Style.spacing.sm
          visible: !!sec.section && sec.section.ok && sec.list.length === 0
          text: modelData.empty
          tone: "faint"; wrapMode: Text.Wrap
        }

        Repeater {
          model: sec.list
          delegate: ListRow {
            id: row
            required property var modelData
            required property int index
            readonly property string kind: sec.modelData.kind
            readonly property int gi: page.rowIndex(kind, index)
            readonly property var it: modelData
            readonly property var exp: {
              var ts = it.expires && it.expires.ts ? it.expires.ts * 1000 : 0
              if (!ts) return null
              return kind === "profile" ? page.svc.expiryFor(ts) : Model.expiryStatus(ts, page.svc.now)
            }
            Layout.fillWidth: true
            // bleed into the card padding so the text lines up with the card's own content
            Layout.leftMargin: -Style.spacing.rowPaddingX
            Layout.rightMargin: -Style.spacing.md
            hasCursor: page.cursor === gi
            onHovered: function(h) { if (h) page.cursor = gi }
            onClicked: page.copyRow({ kind: kind, item: it })
            glyph: ""
            title: kind === "profile" ? (it.displayBundleId || it.name)
                 : kind === "certificate" ? (it.kindLabel || "Signing") + " certificate"
                 : kind === "identifier" ? (it.displayBundleId || it.bundleId)
                 : it.name
            subtitle: kind === "profile" ? it.name + "  ·  " + Model.plural(it.devicesCount || 0, "device") + (it.active ? "" : "  ·  " + String(it.state).toLowerCase())
                    : kind === "certificate" ? (it.commonName || it.name) + "  ·  serial " + it.serialShort
                    : kind === "identifier" ? it.name
                    : (it.model || "Device") + "  ·  " + it.udidShort + (it.added && it.added.ts ? "  ·  added " + Model.relativeTime(it.added.ts * 1000, page.svc.now) : "")
            trailing: [
              StatusPill {
                visible: !!row.exp
                level: row.exp ? row.exp.level : "info"
                text: row.exp ? row.exp.text : ""
                quiet: row.exp ? row.exp.level === "ok" : true
              },
              StatusPill {
                visible: row.kind === "device" && row.it.enabled === false
                level: "warn"; text: "Disabled"; quiet: true
              },
              Icon {
                anchors.verticalCenter: parent ? parent.verticalCenter : undefined
                glyph: Model.Glyph.copy
                color: row.hasCursor ? Theme.dim : "transparent"
                size: Style.font.bodySmall
              }
            ]
          }
        }
      }
    }

    AccountRevokeWarning { Layout.fillWidth: true; compact: true }

    // ---------------------------------------------------- sign out
    SectionHeader { Layout.fillWidth: true; Layout.topMargin: Style.spacing.md; text: "Sign out" }
    RowLayout {
      Layout.fillWidth: true
      spacing: Style.spacing.xxl
      AppText {
        Layout.fillWidth: true
        text: "Signing out removes your Apple ID from this computer. Apps already on your iPhone keep working until their signing runs out, and your certificate stays with Apple."
        tone: "dim"; wrapMode: Text.Wrap; size: "small"
      }
      ActionButton {
        text: "Sign out"; glyph: Model.Glyph.linkOff; kind: "danger"
        busy: page.svc.signingOut
        onClicked: page.app.confirm({
          title: "Sign out of your Apple ID?",
          message: "You'll need your password and a verification code to sign in again. Apps on your iPhone keep working until their signing expires.",
          confirmText: "Sign out",
          cancelText: "Stay signed in",
          destructive: true,
          glyph: Model.Glyph.apple,
          onConfirm: function() { page.svc.signOut() }
        })
      }
    }
    Item { implicitHeight: Style.spacing.xl }
  }
}
