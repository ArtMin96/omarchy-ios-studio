import QtQuick
import QtQuick.Effects
import qs.Commons
import qs.Commons as Commons
import "../Model.js" as Model

// A drawn iPhone (bezel, Dynamic Island) that shows a screen image when it has one.
// It's a depiction of the device, so it keeps its physical rounded corners even on
// square-cornered themes.
//   DevicePhoneFrame { width: …; source: app.device.screenSource; live: app.device.mirroring
//                      onFrameShown: function(url) { app.device.ackFrame(url) }; mirrorService: app.device }
// With mirrorService set, the frame counts itself as a viewer while it's visible: DeviceService
// pauses the live mirror when nobody can see it (the page is hidden) and resumes it when shown.
// Mirror frames are double-buffered: the next one decodes in a hidden Image and the two swap
// once it's Ready, so a frame is never shown half-loaded. A frame that arrives while one is
// still decoding replaces the queued one (latest wins). frameShown(url) fires for each frame shown.
Item {
  id: root

  property string source: ""                // file:// URL of a screenshot / mirror frame
  property bool live: false                 // shows the LIVE marker
  property string liveLabel: "LIVE"         // its text (e.g. "REC 0:12" while recording)
  property bool dimmed: false               // device not connected
  property string glyph: Model.Glyph.phone  // placeholder glyph when there's no image
  property string placeholder: ""           // placeholder caption
  property int rotationSteps: 0             // 0..3, quarter turns clockwise
  property real screenAspect: 1206 / 2622   // iPhone 17 Pro
  property bool clickable: false
  property var mirrorService: null
  signal clicked()
  signal frameShown(string url)

  readonly property bool landscape: rotationSteps % 2 === 1
  readonly property real _bodyW: landscape ? Math.min(height, width * screenAspect) : Math.min(width, height * screenAspect)
  readonly property real _bodyH: _bodyW / screenAspect
  readonly property real _bezel: Math.max(3, _bodyW * 0.035)
  readonly property real _radius: _bodyW * 0.16
  // double buffer: the front Image is on screen, the back one loads the next frame
  property bool _aFront: true
  property string _pending: ""
  readonly property var _front: _aFront ? imgA : imgB
  readonly property var _back: _aFront ? imgB : imgA
  readonly property bool hasImage: root.source !== "" && _front.status === Image.Ready && String(_front.source) !== ""

  function _load(url) {
    if (url === "") { imgA.source = ""; imgB.source = ""; root._pending = ""; return }
    if (String(root._front.source) === url && root._front.status === Image.Ready) return
    if (root._back.status === Image.Loading) { root._pending = url; return }
    root._back.source = url
  }
  function _settled(img) {
    if (img !== root._back) return
    if (img.status === Image.Ready) {
      root._aFront = (img === imgA)
      root.frameShown(String(img.source))
    }
    if (root._pending !== "") { var next = root._pending; root._pending = ""; root._load(next) }
  }
  onSourceChanged: _load(root.source)
  Component.onCompleted: { _load(root.source); _countViewer() }

  property bool _counted: false
  function _countViewer() {
    var want = root.visible && !!root.mirrorService
    if (want === root._counted) return
    root._counted = want
    root.mirrorService.mirrorViewers += want ? 1 : -1
  }
  onVisibleChanged: _countViewer()
  Component.onDestruction: if (root._counted && root.mirrorService) root.mirrorService.mirrorViewers -= 1

  implicitWidth: Style.space(180)
  implicitHeight: implicitWidth / screenAspect

  Item {
    id: body
    width: root._bodyW
    height: root._bodyH
    anchors.centerIn: parent
    rotation: root.rotationSteps * 90
    opacity: root.dimmed ? 0.55 : 1
    Behavior on rotation { NumberAnimation { duration: 260; easing.type: Easing.OutCubic } }
    Behavior on opacity { NumberAnimation { duration: 200 } }

    // soft contact shadow
    Rectangle {
      anchors.fill: parent
      anchors.topMargin: root._bodyH * 0.02
      anchors.leftMargin: root._bodyW * 0.02
      anchors.rightMargin: -root._bodyW * 0.02
      anchors.bottomMargin: -root._bodyH * 0.015
      radius: root._radius
      color: Util.alpha(Commons.Color.background, 0.0)
      layer.enabled: true
      layer.effect: MultiEffect { shadowEnabled: true; shadowColor: Util.alpha("#000000", Theme.isDark ? 0.55 : 0.25); shadowBlur: 0.8; shadowVerticalOffset: root._bodyH * 0.012 }
    }

    // frame (titanium-ish rim from the theme foreground)
    Rectangle {
      id: frame
      anchors.fill: parent
      radius: root._radius
      color: Theme.isDark ? Qt.darker(Commons.Color.background, 1.35) : Qt.darker(Commons.Color.background, 1.08)
      border.width: Math.max(1, root._bezel * 0.35)
      border.color: Util.alpha(Commons.Color.foreground, hover.hovered && root.clickable ? 0.45 : 0.28)
      Behavior on border.color { ColorAnimation { duration: 140 } }
    }

    // side buttons
    Rectangle { x: -width * 0.6; y: root._bodyH * 0.22; width: Math.max(2, root._bezel * 0.45); height: root._bodyH * 0.06; radius: width / 2; color: Util.alpha(Commons.Color.foreground, 0.25) }
    Rectangle { x: -width * 0.6; y: root._bodyH * 0.30; width: Math.max(2, root._bezel * 0.45); height: root._bodyH * 0.09; radius: width / 2; color: Util.alpha(Commons.Color.foreground, 0.25) }
    Rectangle { x: parent.width - width * 0.4; y: root._bodyH * 0.27; width: Math.max(2, root._bezel * 0.45); height: root._bodyH * 0.11; radius: width / 2; color: Util.alpha(Commons.Color.foreground, 0.25) }

    // screen
    Item {
      id: screen
      anchors.fill: parent
      anchors.margins: root._bezel

      Rectangle {
        id: glass
        anchors.fill: parent
        radius: root._radius - root._bezel
        color: Theme.isDark ? Qt.darker(Commons.Color.background, 1.6) : Qt.darker(Commons.Color.background, 1.18)
      }

      Rectangle {
        id: mask
        anchors.fill: parent
        radius: glass.radius
        visible: false
        layer.enabled: true
      }

      // Both buffers sit in one container; the rounded-corner mask is applied once, as its layer
      // effect (a hidden container used as a MultiEffect source doesn't refresh its children).
      Item {
        id: shots
        anchors.fill: parent
        visible: root.hasImage
        opacity: root.dimmed ? 0.6 : 1
        layer.enabled: true
        layer.effect: MultiEffect { maskEnabled: true; maskSource: mask }
        Image {
          id: imgA
          anchors.fill: parent
          asynchronous: true
          cache: false
          smooth: true
          fillMode: Image.PreserveAspectCrop
          sourceSize.height: Math.round(parent.height * 2)
          visible: root._aFront
          onStatusChanged: if (status === Image.Ready || status === Image.Error) root._settled(imgA)
        }
        Image {
          id: imgB
          anchors.fill: parent
          asynchronous: true
          cache: false
          smooth: true
          fillMode: Image.PreserveAspectCrop
          sourceSize.height: Math.round(parent.height * 2)
          visible: !root._aFront
          onStatusChanged: if (status === Image.Ready || status === Image.Error) root._settled(imgB)
        }
      }

      // placeholder
      Column {
        anchors.centerIn: parent
        width: parent.width * 0.8
        spacing: Style.spacing.md
        visible: !root.hasImage
        rotation: -body.rotation
        Icon {
          anchors.horizontalCenter: parent.horizontalCenter
          glyph: root.glyph
          size: Math.max(Style.font.iconLarge, root._bodyW * 0.18)
          color: Theme.faint
        }
        AppText {
          width: parent.width
          visible: root.placeholder !== ""
          text: root.placeholder
          size: "small"; tone: "faint"
          horizontalAlignment: Text.AlignHCenter
          wrapMode: Text.WordWrap
        }
      }

      // Dynamic Island
      Rectangle {
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.top: parent.top
        anchors.topMargin: parent.height * 0.014
        width: parent.width * 0.31
        height: Math.max(4, parent.height * 0.040)
        radius: height / 2
        color: "#000000"
      }

      // LIVE marker
      Rectangle {
        visible: root.live
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        anchors.margins: parent.width * 0.06
        width: liveRow.implicitWidth + Style.spacing.lg
        height: liveRow.implicitHeight + Style.spacing.xs
        radius: height / 2
        color: Util.alpha("#000000", 0.55)
        rotation: -body.rotation
        Row {
          id: liveRow
          anchors.centerIn: parent
          spacing: Style.spacing.sm
          Rectangle {
            width: Style.spacing.md; height: width; radius: width / 2
            anchors.verticalCenter: parent.verticalCenter
            color: Commons.Color.urgent
            SequentialAnimation on opacity { running: root.live; loops: Animation.Infinite
              NumberAnimation { to: 0.35; duration: 700; easing.type: Easing.InOutSine }
              NumberAnimation { to: 1; duration: 700; easing.type: Easing.InOutSine } }
          }
          Text { text: root.liveLabel; color: "#ffffff"; font.family: Style.font.family; font.pixelSize: Style.font.caption; font.bold: true }
        }
      }
    }
  }

  HoverHandler { id: hover; enabled: root.clickable; cursorShape: Qt.PointingHandCursor }
  TapHandler { enabled: root.clickable; onTapped: root.clicked() }
}
