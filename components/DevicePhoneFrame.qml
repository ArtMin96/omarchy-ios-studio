import QtQuick
import QtQuick.Effects
import qs.Commons
import qs.Commons as Commons
import "../Model.js" as Model

// A drawn iPhone (bezel, Dynamic Island) that shows a screen image when it has one.
// It's a depiction of the device, so it keeps its physical rounded corners even on
// square-cornered themes.
//   DevicePhoneFrame { width: …; source: app.device.screenSource; live: app.device.mirroring }
Item {
  id: root

  property string source: ""                // file:// URL of a screenshot / mirror frame
  property bool live: false                 // shows the LIVE marker
  property bool dimmed: false               // device not connected
  property string glyph: Model.Glyph.phone  // placeholder glyph when there's no image
  property string placeholder: ""           // placeholder caption
  property int rotationSteps: 0             // 0..3, quarter turns clockwise
  property real screenAspect: 1206 / 2622   // iPhone 17 Pro
  property bool clickable: false
  signal clicked()

  readonly property bool landscape: rotationSteps % 2 === 1
  readonly property real _bodyW: landscape ? Math.min(height, width * screenAspect) : Math.min(width, height * screenAspect)
  readonly property real _bodyH: _bodyW / screenAspect
  readonly property real _bezel: Math.max(3, _bodyW * 0.035)
  readonly property real _radius: _bodyW * 0.16
  // Mirror frames replace each other every ~0.5 s; keep showing the previous one while
  // the next decodes (retainWhileLoading), so "has an image" survives the Loading state.
  property bool _loadedOnce: false
  readonly property bool hasImage: root.source !== "" && (shot.status === Image.Ready || (shot.status === Image.Loading && _loadedOnce))
  onSourceChanged: if (source === "") _loadedOnce = false

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

      Image {
        id: shot
        anchors.fill: parent
        source: root.source
        asynchronous: true
        cache: false
        smooth: true
        mipmap: true
        fillMode: Image.PreserveAspectCrop
        sourceSize.height: Math.round(height * 2)
        visible: false
        retainWhileLoading: true
        onStatusChanged: if (status === Image.Ready) root._loadedOnce = true
      }

      Rectangle {
        id: mask
        anchors.fill: parent
        radius: glass.radius
        visible: false
        layer.enabled: true
      }

      MultiEffect {
        anchors.fill: parent
        source: shot
        maskEnabled: true
        maskSource: mask
        visible: root.hasImage
        opacity: root.dimmed ? 0.6 : 1
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
          Text { text: "LIVE"; color: "#ffffff"; font.family: Style.font.family; font.pixelSize: Style.font.caption; font.bold: true }
        }
      }
    }
  }

  HoverHandler { id: hover; enabled: root.clickable; cursorShape: Qt.PointingHandCursor }
  TapHandler { enabled: root.clickable; onTapped: root.clicked() }
}
