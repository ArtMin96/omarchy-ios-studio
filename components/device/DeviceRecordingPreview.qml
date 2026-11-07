import QtQuick
import QtMultimedia
import qs.Commons
import qs.Commons as Commons
import "../common"
import "../../lib/Model.js" as Model

// Inline player for a finished screen recording (muted, looping; click to pause).
// Needs Qt Multimedia (qt6-multimedia). DeviceScreenView loads it through a Loader, so without the
// module the preview is simply left out and Show in folder / Copy path still work.
//   Loader { source: "DeviceRecordingPreview.qml"; onLoaded: item.source = "file:///…/rec.mp4" }
Rectangle {
  id: root
  property string source: ""

  radius: Style.cornerRadius
  color: Theme.isDark ? Qt.darker(Commons.Color.background, 1.6) : Qt.darker(Commons.Color.background, 1.18)
  border.width: Style.normalBorderWidth
  border.color: Theme.hairline
  clip: true

  MediaPlayer {
    id: player
    source: root.source
    loops: MediaPlayer.Infinite
    videoOutput: out
    audioOutput: AudioOutput { muted: true }
    onSourceChanged: if (source != "") play()
  }
  VideoOutput {
    id: out
    anchors.fill: parent
    anchors.margins: Style.normalBorderWidth
    fillMode: VideoOutput.PreserveAspectFit
  }
  Rectangle {
    visible: player.playbackState !== MediaPlayer.PlayingState
    anchors.centerIn: parent
    width: Style.space(40); height: width; radius: width / 2
    color: Util.alpha("#000000", 0.55)
    Icon { anchors.centerIn: parent; glyph: Model.Glyph.play; color: "#ffffff" }
  }
  HoverHandler { cursorShape: Qt.PointingHandCursor }
  TapHandler { onTapped: player.playbackState === MediaPlayer.PlayingState ? player.pause() : player.play() }
  // nobody sees it while the tab (or page) is hidden: don't keep decoding the loop
  property bool _resume: false
  onVisibleChanged: {
    if (!visible) { root._resume = player.playbackState === MediaPlayer.PlayingState; player.pause() }
    else if (root._resume) { root._resume = false; player.play() }
  }
  Component.onDestruction: player.stop()
}
