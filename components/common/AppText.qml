import QtQuick
import qs.Commons
import qs.Commons as Commons

// Themed text. Pick a size token and a tone instead of hand-setting font/colour.
//   AppText { text: "Signed in"; size: "title"; tone: "primary" }
//   size: caption | small | body | subtitle | title | heading | display | displayLarge
//   tone: primary | dim | faint | accent | ok | warn | error | info | inherit (keep `color`)
Text {
  id: root

  property string size: "body"
  property string tone: "primary"
  property bool strong: false
  property bool mono: false   // Omarchy's font is already monospace; kept for intent/clarity

  textFormat: Text.PlainText
  font.family: Style.font.family
  font.pixelSize: {
    switch (root.size) {
    case "caption": return Style.font.caption
    case "small": return Style.font.bodySmall
    case "subtitle": return Style.font.subtitle
    case "title": return Style.font.title
    case "heading": return Style.font.heading
    case "display": return Style.font.display
    case "displayLarge": return Style.font.displayLarge
    default: return Style.font.body
    }
  }
  font.bold: root.strong
  color: {
    switch (root.tone) {
    case "inherit": return root.color
    case "dim": return Theme.dim
    case "faint": return Theme.faint
    case "accent": return Commons.Color.accent
    case "ok": return Theme.success
    case "warn": return Theme.warn
    case "error": return Commons.Color.urgent
    case "info": return Commons.Color.accent
    default: return Commons.Color.foreground
    }
  }
  Behavior on color { ColorAnimation { duration: 160 } }
}
