# Omarchy plugin (bar button)

iOS Studio's repository is also an Omarchy shell plugin, `io.github.artmin96.ios-studio`. The plugin is one bar
button with the phone glyph and the tooltip "iOS Studio". It has no status, menu or settings.

## What a click does

1. `plugin/app.sh status` checks for the launcher, `~/.local/bin/ios-studio` (`install.sh` links it and
   `--uninstall` removes it; a link to a deleted folder counts as missing). Nothing runs before a click, so the
   button costs nothing while it sits in the bar.
2. Installed: `plugin/app.sh open` runs that launcher, which opens iOS Studio or brings its window to the front
   (`omarchy-launch-or-focus`), as the app launcher does.
3. Not installed: a panel under the button asks "Install it now?" (Cancel is a click or Esc away; Enter or Install
   goes ahead). Install runs `plugin/app.sh install`, which runs this folder's `install.sh` with a 5-minute
   limit. When it ends well, the panel closes and iOS Studio opens on its Setup page, which installs the iOS tools.
   When it fails, the panel shows install.sh's last line and Try again.

## Where iOS Studio lives

`omarchy plugin add` clones the whole repository into `~/.config/omarchy/plugins/io.github.artmin96.ios-studio/`.
iOS Studio doesn't run from there: the shell refuses a plugin folder that holds symlinks (the app links
`Commons/` and `Ui/` into its folder) and reloads plugins whenever a file there changes. So `install.sh`, run from
a plugin folder, first makes a local `git clone` of the plugin's commit in `~/.local/share/omarchy-ios-studio`
(the README's install folder, with `origin` set to the plugin's remote; a plain copy when the plugin folder has no
`.git`), then runs that copy's `install.sh`. Nothing is downloaded. An existing `~/.local/share/omarchy-ios-studio`
is kept and repaired; a folder there that isn't iOS Studio stops the install with a message. `install.sh --uninstall`
from the plugin folder runs the app folder's uninstall.

So the button and the app are separate copies: `omarchy plugin update` updates the button, and
`omarchy plugin remove io.github.artmin96.ios-studio` removes only the button (README › Uninstall covers both).

## Files

| File | What |
|---|---|
| `manifest.json` | the plugin manifest (schema 1, kind `bar-widget`, default section `right`). Its `version` follows the app's, checked by `.github/scripts/check-version.sh` |
| `plugin/BarWidget.qml` | the button and the install panel (`qs.Ui` `BarIconButton`, `KeyboardPanel`, `PanelHero`, `Button`); every text is plain text |
| `plugin/Model.js` | keeps the last 20 lines of install.sh's output and picks the reason shown on failure |
| `plugin/app.sh` | `status`, `open [setup]`, `install`: fixed commands, nothing from the widget reaches a shell |
| `plugin/i18n/en.json` | the panel's strings (`Commons.Translations`); other languages fall back to English |

Tests: `tests/cli/test_omarchy_plugin.py` (manifest, `app.sh`, `install.sh` from a plugin folder under a private
HOME, `omarchy-plugin-validate` when it's installed) and `tests/qml/tst_plugin_install.qml` (`Model.js`).

## Limits

- The panel's install only covers what `install.sh` does (seconds: a copy and a few files in the home folder).
  The iOS toolchain still comes from Setup.
- The marketplace's static security scan reports the review capabilities iOS Studio needs (an installer,
  `pkexec` and `pacman` in Setup, a systemd user timer for re-signing, `pip` for its own venvs) and no findings.
