import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../common"
import "../../lib/Model.js" as Model

// Apple ID sign-in, reusable anywhere (AccountPage hosts one; Setup can embed its own):
//   AccountSignInSheet { id: signIn; app: page.app }      signIn.begin()   // or begin("me@icloud.com")
// Drives app.account's sign-in flow: email + password → 2FA code → team picker → done.
// The password lives only in this sheet's memory while it's open and goes to the
// helper over stdin when xtool asks for it.
Sheet {
  id: root
  required property var app
  readonly property var svc: app.account
  readonly property var flow: svc.flow || ({ phase: "idle" })
  readonly property string phase: flow.phase || "idle"
  readonly property var prompt: flow.prompt || null
  // what the body shows: form | working | code | team | done
  readonly property string view: {
    if (phase === "code") return "code"
    if (phase === "team") return "team"
    if (phase === "done") return "done"
    if (phase === "starting" || phase === "working" || phase === "password" || phase === "text") return "working"
    return "form"
  }
  property string mode: "password"        // password | key
  property string _password: ""
  property int teamPick: 0
  property bool showCodeHelp: false

  signal signedIn(var status)

  title: view === "done" ? "You're signed in" : (mode === "key" ? "Sign in with an API key" : "Sign in with your Apple ID")
  subtitle: view === "done" ? "" : view === "code" ? "Two-factor authentication"
          : view === "team" ? "Choose a team"
          : mode === "key" ? "For paid Apple Developer Program teams." : "A free Apple ID works. Your password goes only to Apple."
  glyph: view === "done" ? "" : Model.Glyph.apple
  preferredWidth: Style.space(500)
  busy: view === "working"
  dismissible: true

  function begin(email) {
    svc.resetFlow()
    if (email) emailField.text = email
    else if (emailField.text === "" && svc.auth && svc.auth.appleId) emailField.text = svc.auth.appleId
    codeField.text = ""
    root.showCodeHelp = false
    root.open()
  }

  function submit() {
    if (root.mode === "password") {
      var email = emailField.text.trim()
      if (email === "" || pwField.text === "") { localError.text = "Enter your Apple ID email and password."; return }
      if (email.indexOf("@") < 0) { localError.text = "Your Apple ID is the email address you use with Apple."; return }
      localError.text = ""
      root._password = pwField.text
      svc.startSignIn(email, "password")
    } else {
      if (keyIdField.text.trim() === "" || issuerField.text.trim() === "" || keyPathField.text.trim() === "") { localError.text = "Fill in all three fields."; return }
      localError.text = ""
      svc.startSignIn("", "key")
    }
  }

  function submitCode() {
    if (root.view !== "code") return
    var c = codeField.text.replace(/\D/g, "")
    if (c.length !== 6) return
    svc.answer(c)
  }

  function expandHome(p) { return p.replace(/^~(?=\/|$)/, root.app.home) }

  Connections {
    target: root.svc
    function onPromptArrived(p) {
      if (!root.opened) return
      if (p.kind === "password") { root.svc.answer(root._password) }
      else if (p.kind === "text") {
        var v = p.field === "email" ? emailField.text.trim()
              : p.field === "keyId" ? keyIdField.text.trim()
              : p.field === "issuerId" ? issuerField.text.trim()
              : p.field === "keyPath" ? root.expandHome(keyPathField.text.trim()) : ""
        root.svc.answer(v)
      } else if (p.kind === "code") {
        codeField.text = ""
        Qt.callLater(function() { codeField.forceActiveFocus() })
      } else if (p.kind === "team") {
        root.teamPick = 0
        codeField.focus = false      // let arrows/Enter reach the picker
      }
    }
    function onSignInSucceeded(status) {
      if (!root.opened) return
      root._password = ""
      pwField.text = ""
      root.signedIn(status)
      root.app.toast("Signed in as " + (status.appleId || "your Apple ID"), "ok")
      doneTimer.restart()
    }
  }
  Connections {
    target: root.svc
    function onFlowChanged() {
      // a failed code needs a fresh login (and a fresh code); keep the password for "Send a new code"
      if (root.phase === "failed") Qt.callLater(function() { if (root.view === "form") pwField.forceActiveFocus() })
    }
  }

  Timer { id: doneTimer; interval: 1800; onTriggered: root.close() }

  onShown: Qt.callLater(function() { if (root.view === "form") (emailField.text === "" ? emailField : pwField).forceActiveFocus() })
  onClosed: {
    doneTimer.stop()
    if (root.svc.signingIn) root.svc.cancelSignIn()
    root._password = ""
    pwField.text = ""
    codeField.text = ""
    root.svc.resetFlow()
  }

  // ================================================================ form
  ColumnLayout {
    Layout.fillWidth: true
    visible: root.view === "form"
    spacing: Style.spacing.lg

    ErrorState {
      Layout.fillWidth: true
      visible: root.phase === "failed" && root.flow.error && root.flow.error.code !== "cancelled"
      banner: true
      app: root.app
      error: root.flow.error
      showRetry: false
    }

    // ---- Apple ID + password
    ColumnLayout {
      Layout.fillWidth: true
      visible: root.mode === "password"
      spacing: Style.spacing.sm
      AppText { text: "Apple ID"; size: "small"; tone: "dim" }
      TextField {
        id: emailField
        Layout.fillWidth: true
        placeholderText: "name@example.com"
        inputMethodHints: Qt.ImhEmailCharactersOnly | Qt.ImhNoAutoUppercase
        onAccepted: pwField.forceActiveFocus()
        KeyNavigation.tab: pwField
      }
      AppText { Layout.topMargin: Style.spacing.sm; text: "Password"; size: "small"; tone: "dim" }
      TextField {
        id: pwField
        Layout.fillWidth: true
        password: true
        placeholderText: "Apple ID password"
        onAccepted: root.submit()
        KeyNavigation.tab: emailField
      }
    }

    // ---- API key
    ColumnLayout {
      Layout.fillWidth: true
      visible: root.mode === "key"
      spacing: Style.spacing.sm
      AppText { Layout.fillWidth: true; text: "Create a key in App Store Connect › Users and Access › Integrations, then download the .p8 file."; size: "small"; tone: "dim"; wrapMode: Text.Wrap }
      AppText { Layout.topMargin: Style.spacing.sm; text: "Key ID"; size: "small"; tone: "dim" }
      TextField { id: keyIdField; Layout.fillWidth: true; placeholderText: "2X9R4HXF34"; onAccepted: issuerField.forceActiveFocus() }
      AppText { text: "Issuer ID"; size: "small"; tone: "dim" }
      TextField { id: issuerField; Layout.fillWidth: true; placeholderText: "57246542-96fe-1a63-e053-0824d011072a"; onAccepted: keyPathField.forceActiveFocus() }
      AppText { text: "Key file"; size: "small"; tone: "dim" }
      TextField { id: keyPathField; Layout.fillWidth: true; placeholderText: "~/Downloads/AuthKey_2X9R4HXF34.p8"; onAccepted: root.submit() }
    }

    AppText { id: localError; Layout.fillWidth: true; visible: text !== ""; tone: "error"; size: "small"; wrapMode: Text.Wrap }

    // reassurance, kept short
    RowLayout {
      Layout.fillWidth: true
      Layout.topMargin: Style.spacing.xs
      visible: root.mode === "password"
      spacing: Style.spacing.md
      Icon { glyph: Model.Glyph.shieldCheck; color: Theme.faint; size: Style.font.bodySmall; Layout.alignment: Qt.AlignTop }
      AppText {
        Layout.fillWidth: true
        text: "Apple will send a 6-digit code to your other devices. Apps you install stay signed for 7 days on a free account, and iOS Studio can renew them."
        size: "small"; tone: "faint"; wrapMode: Text.Wrap; lineHeight: 1.15
      }
    }

    AppText {
      Layout.fillWidth: true
      text: root.mode === "password" ? "Have a paid team? Use an API key instead" : "Use my Apple ID and password instead"
      size: "small"; tone: "accent"
      MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor; onClicked: { root.mode = root.mode === "password" ? "key" : "password"; localError.text = "" } }
    }
  }

  // ================================================================ working
  ColumnLayout {
    Layout.fillWidth: true
    visible: root.view === "working"
    spacing: Style.spacing.lg
    Item { implicitHeight: Style.spacing.lg }
    Spinner { Layout.alignment: Qt.AlignHCenter; size: Style.font.displayLarge; running: root.view === "working"; color: Commons.Color.accent }
    AppText { Layout.fillWidth: true; horizontalAlignment: Text.AlignHCenter; text: (root.flow.step || "Signing in") + "…"; size: "subtitle" }
    AppText { Layout.fillWidth: true; horizontalAlignment: Text.AlignHCenter; text: "This usually takes 5 to 15 seconds."; size: "small"; tone: "faint" }
    Item { implicitHeight: Style.spacing.lg }
  }

  // ================================================================ 2FA code
  ColumnLayout {
    Layout.fillWidth: true
    visible: root.view === "code"
    spacing: Style.spacing.xl

    AppText {
      Layout.fillWidth: true
      text: (root.prompt && root.prompt.attempt > 1 ? "That code didn't work. " : "")
          + "Enter the 6-digit code Apple just sent to your iPhone, iPad or Mac."
      wrapMode: Text.Wrap
    }

    // six boxes over one hidden field: typing, paste and backspace all just work
    Item {
      Layout.fillWidth: true
      implicitHeight: boxes.implicitHeight
      TextField {
        id: codeField
        anchors.fill: boxes
        opacity: 0
        inputMethodHints: Qt.ImhDigitsOnly
        onTextChanged: {
          var d = text.replace(/\D/g, "").substring(0, 6)
          if (d !== text) { text = d; return }
          if (d.length === 6 && root.view === "code") Qt.callLater(root.submitCode)
        }
        onAccepted: root.submitCode()
      }
      Row {
        id: boxes
        anchors.horizontalCenter: parent.horizontalCenter
        spacing: Style.spacing.md
        Repeater {
          model: 6
          delegate: Rectangle {
            required property int index
            readonly property string ch: codeField.text.charAt(index)
            readonly property bool active: codeField.activeFocus && (index === codeField.text.length || (index === 5 && codeField.text.length === 6))
            width: Style.font.displayLarge * 1.55
            height: Style.font.displayLarge * 2
            radius: Style.cornerRadius
            color: ch !== "" ? Theme.surfaceRaised : Theme.surface
            border.width: active ? Style.normalBorderWidth * 2 : Style.normalBorderWidth
            border.color: active ? Commons.Color.accent : (ch !== "" ? Util.alpha(Commons.Color.foreground, 0.3) : Theme.hairline)
            Behavior on border.color { ColorAnimation { duration: 120 } }
            AppText { anchors.centerIn: parent; text: parent.ch; size: "displayLarge"; strong: true }
            Rectangle {
              visible: parent.active && parent.ch === ""
              anchors.centerIn: parent
              width: Style.normalBorderWidth * 2; height: parent.height * 0.42
              color: Commons.Color.accent
              SequentialAnimation on opacity { running: parent.visible; loops: Animation.Infinite; NumberAnimation { to: 0; duration: 500 } NumberAnimation { to: 1; duration: 500 } }
            }
          }
        }
      }
      MouseArea { anchors.fill: boxes; cursorShape: Qt.IBeamCursor; onClicked: codeField.forceActiveFocus() }
    }

    // Apple couldn't push a code: say exactly where to get one
    BorderSurface {
      Layout.fillWidth: true
      visible: root.flow.manualCode === true || root.showCodeHelp
      radius: Style.cornerRadius
      color: Theme.levelFill(root.flow.manualCode ? "warn" : "info")
      borderSpec: Border.flat(Util.alpha(Theme.levelColor(root.flow.manualCode ? "warn" : "info"), 0.35), Style.normalBorderWidth)
      implicitHeight: helpCol.implicitHeight + Style.spacing.lg * 2
      ColumnLayout {
        id: helpCol
        anchors.fill: parent
        anchors.margins: Style.spacing.lg
        spacing: Style.spacing.sm
        AppText { Layout.fillWidth: true; text: root.flow.manualCode ? "Apple couldn't send the code automatically" : "Get a code yourself"; strong: true; wrapMode: Text.Wrap }
        AppText {
          Layout.fillWidth: true
          text: "On a trusted iPhone or iPad open Settings › [your name] › Sign-In & Security › Two-Factor Authentication › Get Verification Code. On a Mac: System Settings › [your name] › Sign-In & Security."
          size: "small"; tone: "dim"; wrapMode: Text.Wrap; lineHeight: 1.15
        }
      }
    }

    RowLayout {
      Layout.fillWidth: true
      AppText {
        visible: !root.showCodeHelp && root.flow.manualCode !== true
        text: "Didn't get a code?"
        size: "small"; tone: "accent"
        MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor; onClicked: root.showCodeHelp = true }
      }
      Item { Layout.fillWidth: true }
      AppText { text: codeField.text.length + " of 6"; size: "small"; tone: "faint" }
    }
  }

  // ================================================================ team picker
  ColumnLayout {
    Layout.fillWidth: true
    visible: root.view === "team"
    spacing: Style.spacing.sm
    AppText { Layout.fillWidth: true; text: "This Apple ID belongs to more than one developer team. Apps you build will be signed with the one you pick."; tone: "dim"; wrapMode: Text.Wrap }
    Repeater {
      model: root.prompt && root.prompt.options ? root.prompt.options : []
      delegate: ListRow {
        required property var modelData
        required property int index
        Layout.fillWidth: true
        glyph: root.teamPick === index ? Model.Glyph.checkCircle : Model.Glyph.circle
        glyphColor: root.teamPick === index ? Commons.Color.accent : Theme.faint
        title: modelData.label
        subtitle: modelData.teamId ? "Team ID " + modelData.teamId : ""
        hasCursor: root.teamPick === index
        current: root.teamPick === index
        onClicked: root.teamPick = index
        onDoubleClicked: { root.teamPick = index; root.pickTeam() }
      }
    }
  }

  function pickTeam() {
    var opts = root.prompt && root.prompt.options ? root.prompt.options : []
    if (opts.length > 0) svc.answer(opts[Math.min(root.teamPick, opts.length - 1)].value)
  }

  // ================================================================ done
  ColumnLayout {
    Layout.fillWidth: true
    visible: root.view === "done"
    spacing: Style.spacing.lg
    Item { implicitHeight: Style.spacing.md }
    Rectangle {
      id: tick
      Layout.alignment: Qt.AlignHCenter
      implicitWidth: Style.font.displayLarge * 2.4; implicitHeight: implicitWidth
      radius: width / 2
      color: Theme.levelFill("ok")
      border.width: Style.normalBorderWidth * 2
      border.color: Theme.success
      scale: root.view === "done" ? 1 : 0.4
      opacity: root.view === "done" ? 1 : 0
      Behavior on scale { NumberAnimation { duration: 420; easing.type: Easing.OutBack; easing.overshoot: 2.2 } }
      Behavior on opacity { NumberAnimation { duration: 200 } }
      Icon { anchors.centerIn: parent; glyph: Model.Glyph.check; size: Style.font.displayLarge * 1.2; color: Theme.success }
    }
    AppText {
      Layout.fillWidth: true; horizontalAlignment: Text.AlignHCenter
      text: root.flow.result && root.flow.result.appleId ? root.flow.result.appleId : "Signed in"
      size: "subtitle"; strong: true
    }
    AppText {
      Layout.fillWidth: true; horizontalAlignment: Text.AlignHCenter
      text: "iOS Studio can now sign apps for your iPhone."
      tone: "dim"; wrapMode: Text.Wrap
    }
    Item { implicitHeight: Style.spacing.md }
  }

  // ================================================================ keys for the team picker
  Keys.onPressed: function(event) {
    if (root.view !== "team") return
    var a = Model.keyAction(event)
    var n = root.prompt && root.prompt.options ? root.prompt.options.length : 0
    if (a === "down") { root.teamPick = Math.min(n - 1, root.teamPick + 1); event.accepted = true }
    else if (a === "up") { root.teamPick = Math.max(0, root.teamPick - 1); event.accepted = true }
    else if (a === "activate") { root.pickTeam(); event.accepted = true }
  }

  footer: [
    ActionButton {
      visible: root.view !== "done"
      text: "Cancel"; kind: "ghost"
      onClicked: root.close()
    },
    ActionButton {
      visible: root.view === "form"
      text: root.phase === "failed" && root.flow.error && (root.flow.error.code === "code-wrong" || root.flow.error.code === "code-missing") && pwField.text !== ""
            ? "Send a new code" : "Sign in"
      glyph: Model.Glyph.arrowRight
      kind: "primary"
      onClicked: root.submit()
    },
    ActionButton {
      visible: root.view === "code"
      text: "Verify"; kind: "primary"; glyph: Model.Glyph.check
      enabled: codeField.text.length === 6
      onClicked: root.submitCode()
    },
    ActionButton {
      visible: root.view === "team"
      text: "Use this team"; kind: "primary"; glyph: Model.Glyph.arrowRight
      onClicked: root.pickTeam()
    },
    ActionButton {
      visible: root.view === "done"
      text: "Done"; kind: "primary"
      onClicked: root.close()
    }
  ]
}
