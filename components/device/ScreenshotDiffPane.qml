import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../common"
import "../../core"
import "../../lib/Model.js" as Model

// One image of the screenshot comparison (ScreenshotCompareView): a caption over a framed cell
// that shows the image whole (aspect kept, centred), with changed `regions` outlined and the
// ignored status bar (`maskedTop`) shaded. Regions and maskedTop are in the image's pixels;
// `imageSize` ({w, h}) is that pixel size; with it the image is decoded at about the cell's size,
// without it at full size (then the decoded size is the pixel size). The cell's
// height comes from the caller, so the three panes stay the same size. Without a `path` the
// cell shows `placeholder` instead. A click opens the image in the default viewer.
//   ScreenshotDiffPane { title: "Diff"; path: r.diff_path; regions: r.regions; cellHeight: 400 }
ColumnLayout {
  id: root
  property string title: ""
  property string detail: ""               // caption after the title (e.g. "1206 × 2622")
  property string path: ""                 // local file path
  property int revision: 0                 // bump to reload a file that was rewritten in place
  property var imageSize: null             // {w, h} in pixels
  property var regions: []                 // [{x, y, w, h}] in pixels
  property real maskedTop: 0               // pixels at the top that the compare ignored
  property string placeholder: ""
  property string placeholderGlyph: Model.Glyph.image
  property real cellHeight: Style.space(360)

  spacing: Style.spacing.md

  RowLayout {
    Layout.fillWidth: true
    spacing: Style.spacing.md
    AppText { text: root.title; size: "small"; strong: true }
    AppText { Layout.fillWidth: true; text: root.detail; size: "caption"; tone: "faint"; elide: Text.ElideRight }
  }

  Rectangle {
    id: cell
    Layout.fillWidth: true
    Layout.preferredHeight: root.cellHeight
    radius: Style.cornerRadius
    color: Theme.surface
    border.width: 1
    border.color: hover.hovered && img.status === Image.Ready ? Util.alpha(Commons.Color.foreground, 0.35) : Theme.hairline
    clip: true

    Image {
      id: img
      anchors.fill: parent
      anchors.margins: Style.spacing.md
      // waits for the layout, so a bounded decode never starts at full size
      source: root.path !== "" && width > 0 && height > 0 ? Util.fileUrl(root.path) + "?v=" + root.revision : ""
      fillMode: Image.PreserveAspectFit
      // Decoded at about the cell's size, not the screenshot's (a 1206 × 2622 PNG is 12 MB as a bitmap).
      // Regions and maskedTop are in the file's pixels, so this needs imageSize; without it the
      // decoded size is the file's own.
      sourceSize.width: root.imageSize ? Math.ceil(width * Screen.devicePixelRatio) : 0
      sourceSize.height: root.imageSize ? Math.ceil(height * Screen.devicePixelRatio) : 0
      asynchronous: true
      cache: false
      smooth: true
      mipmap: true

      // the image as drawn: its offset in the Image and its scale from pixels
      readonly property real natW: root.imageSize && root.imageSize.w > 0 ? root.imageSize.w : implicitWidth
      readonly property real k: natW > 0 ? paintedWidth / natW : 0
      readonly property real ox: (width - paintedWidth) / 2
      readonly property real oy: (height - paintedHeight) / 2

      // status bar the compare ignored
      Rectangle {
        visible: img.status === Image.Ready && root.maskedTop > 0 && img.k > 0
        x: img.ox; y: img.oy
        width: img.paintedWidth
        height: Math.min(img.paintedHeight, root.maskedTop * img.k)
        color: Util.alpha(Commons.Color.background, 0.6)
        Rectangle { anchors.left: parent.left; anchors.right: parent.right; anchors.bottom: parent.bottom; height: 1; color: Theme.hairline }
      }

      Repeater {
        model: img.status === Image.Ready && img.k > 0 ? (root.regions || []) : []
        delegate: Rectangle {
          required property var modelData
          // The border is drawn inside the box, so the box grows outward by the stroke plus a small
          // gap and never touches the changed pixels (like the helper's padded boxes in the diff
          // image); at least a few pixels on screen, so a one-pixel change still shows.
          readonly property real stroke: Math.max(1, Style.space(1.5))
          readonly property real pad: stroke + Style.space(2)
          readonly property real minSide: Style.space(6)
          readonly property real w0: modelData.w * img.k + pad * 2
          readonly property real h0: modelData.h * img.k + pad * 2
          x: img.ox + modelData.x * img.k - pad - (width - w0) / 2
          y: img.oy + modelData.y * img.k - pad - (height - h0) / 2
          width: Math.max(minSide, w0)
          height: Math.max(minSide, h0)
          color: "transparent"
          radius: 2
          border.width: stroke
          border.color: Theme.levelColor("warn")
        }
      }
    }

    // loading / missing / placeholder
    ColumnLayout {
      anchors.centerIn: parent
      width: parent.width - Style.spacing.xl * 2
      visible: img.status !== Image.Ready
      spacing: Style.spacing.md
      Spinner { Layout.alignment: Qt.AlignHCenter; visible: img.status === Image.Loading; running: visible; size: Style.font.iconLarge }
      Icon {
        Layout.alignment: Qt.AlignHCenter
        visible: img.status !== Image.Loading
        glyph: img.status === Image.Error ? Model.Glyph.warn : root.placeholderGlyph
        color: Theme.faint
        size: Style.font.iconLarge
      }
      AppText {
        Layout.fillWidth: true
        visible: img.status !== Image.Loading
        text: img.status === Image.Error ? "The image couldn't be read" : root.placeholder
        size: "small"; tone: "faint"
        horizontalAlignment: Text.AlignHCenter
        wrapMode: Text.Wrap
      }
    }

    HoverHandler { id: hover; cursorShape: img.status === Image.Ready ? Qt.PointingHandCursor : Qt.ArrowCursor }
    TapHandler { enabled: img.status === Image.Ready; onTapped: Util.execArgv(["xdg-open", root.path]) }
    PanelToolTip { visible: hover.hovered && img.status === Image.Ready && !Host.covers(cell, Host.top); text: "Open in the image viewer" }
  }
}
