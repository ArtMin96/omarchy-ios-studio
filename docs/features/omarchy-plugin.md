# Omarchy plugin (bar button)

iOS Studio's repository is also an Omarchy shell plugin, `io.github.artmin96.ios-studio`. The plugin is one bar
button with the phone glyph and the tooltip "iOS Studio". It has no status, menu or settings.

## What a click does

1. `plugin/app.sh status` checks for the launcher, `~/.local/bin/ios-studio` (`install.sh` links it and
   `--uninstall` removes it; a link to a deleted folder counts as missing). Nothing runs before a click, so the
   button costs nothing while it sits in the bar.
2. Installed: `plugin/app.sh open` runs that launcher, which opens iOS Studio or brings its window to the front
   (`omarchy-launch-or-focus`), as the app launcher does.
3. Not installed: a panel under the button asks "Install it now?" and lists every change the install makes (see
   below). Cancel is a click or Esc away; Enter or Install goes ahead. Install runs `plugin/app.sh install`, which
   runs this folder's `install.sh` with a 5-minute limit. When it ends well, the panel closes and iOS Studio opens on its Setup page, which installs the iOS tools.
   When it fails, the panel shows install.sh's last line and Try again.
4. Outdated (`app.sh status` prints `outdated`): the panel says "Update available", lists what the update does and
   asks first. Update runs `plugin/app.sh update`, which runs this folder's `install.sh --update` (also 5 minutes at
   most), then opens iOS Studio.

## Updates

`omarchy plugin update` fast-forwards the plugin folder only. On the next click, `app.sh status` reports `outdated`
when all of these hold: `~/.local/bin/ios-studio` points into `~/.local/share/omarchy-ios-studio`, both folders are
git checkouts, their commits differ, and the plugin folder's history contains the app folder's commit (the app is
simply behind). That's two `git rev-parse` calls and one `git merge-base --is-ancestor`, run only on a click. An app
folder with commits of its own, one without git, or an app installed somewhere else is never offered an update.

`install.sh --update` (only from a plugin folder; anywhere else it exits 2):

1. requires the app folder to be an iOS Studio git checkout (the manifest check below), else stops with a message;
2. stops without changing anything when tracked files there have local changes;
3. `git fetch`es the plugin folder's `HEAD` from the local plugin folder (no network, no tags, `origin` untouched);
4. stops when the app folder has commits the plugin doesn't, then `git merge --ff-only`s, which also stops when an
   untracked file is in the way;
5. runs the updated app folder's `install.sh`, which repairs the launcher entry, icon, command, theme hook and
   Hyprland block as an install does.

Settings, state, caches and projects live outside the app folder and aren't touched. A running window keeps the old
code until it is reopened.

## Where iOS Studio lives

`omarchy plugin add https://github.com/ArtMin96/omarchy-ios-studio --enable` (or the marketplace) clones the whole
repository into `~/.config/omarchy/plugins/io.github.artmin96.ios-studio/`.
iOS Studio doesn't run from there: the shell refuses a plugin folder that holds symlinks (the app links
`Commons/` and `Ui/` into its folder) and reloads plugins whenever a file there changes. So `install.sh`, run from
a plugin folder, first makes a local `git clone` of the plugin's commit in `~/.local/share/omarchy-ios-studio`
(the README's install folder, with `origin` set to the plugin's remote; a plain copy when the plugin folder has no
`.git`), then runs that copy's `install.sh`. Nothing is downloaded. An existing `~/.local/share/omarchy-ios-studio`
is kept and repaired, but only when it is an iOS Studio checkout: its `manifest.json` has the id
`io.github.artmin96.ios-studio` and it has an executable `install.sh`. Any other folder there stops the install with
"… isn't an iOS Studio folder. Move it aside, then install again.", and its `install.sh` never runs. `install.sh --uninstall`
from the plugin folder runs the app folder's uninstall (or its own, when the app folder isn't iOS Studio).

## What the install changes

The panel's text (`askDetail` in `plugin/i18n/en.json`) names all of it; README › What the install changes has the
same list with full paths. Everything is in the home folder, without a password:

- `~/.local/share/omarchy-ios-studio`: the app folder, copied from the plugin folder
- `~/.local/share/applications/ios-studio.desktop` and the icon `~/.local/share/icons/hicolor/scalable/apps/ios-studio.svg`
- `~/.local/bin/ios-studio`, a link to the app's launcher
- `~/.config/omarchy/hooks/theme-set.d/ios-studio-theme`, the Omarchy theme hook
- a marked block (`-- ios-studio: opacity rule start` … `end`) in `~/.config/hypr/hyprland.lua` that keeps the
  window opaque, followed by `hyprctl reload`; the file is backed up to `~/.local/state/ios-studio/backups/` first
  and restored if `hyprctl configerrors` reports anything. Skipped when Settings › Opaque window is off or `hyprctl`
  isn't installed.
- `~/.config/ios-studio`, `~/.local/state/ios-studio`, `~/.cache/ios-studio`
- `Commons` and `Ui` links inside the app folder

No packages, services or timers: automatic re-signing's user timer and the launcher shortcut only come from Settings.
The theme hook is written in a private `mktemp -d` folder that is removed however `install.sh` ends.

## Updates and removal

The button and the app are separate copies. `omarchy plugin update io.github.artmin96.ios-studio` updates the button
only; the next click offers to update the app folder (Updates above, README › Updating).
`omarchy plugin remove io.github.artmin96.ios-studio` removes only the button; Settings › Uninstall iOS Studio (or
`ios-studio --uninstall`) removes the app's launcher, hook and Hyprland blocks (README › Uninstall covers both).

## Files

| File | What |
|---|---|
| `manifest.json` | the plugin manifest (schema 1, kind `bar-widget`, default section `right`). Its `version` follows the app's, checked by `.github/scripts/check-version.sh` |
| `plugin/BarWidget.qml` | the button and the install panel (`qs.Ui` `BarIconButton`, `KeyboardPanel`, `PanelHero`, `Button`); every text is plain text |
| `plugin/Model.js` | turns `app.sh status` into open, install or update, keeps the last 20 lines of install.sh's output and picks the reason shown on failure |
| `plugin/app.sh` | `status` (`installed`, `outdated` or `missing`), `open [setup]`, `install`, `update`: fixed commands, nothing from the widget reaches a shell |
| `preview.png` | the marketplace listing's image (1920×1080; the marketplace makes the card and detail sizes from it) |
| `plugin/i18n/en.json` | the panel's strings (`Commons.Translations`); other languages fall back to English |

Tests: `tests/cli/test_omarchy_plugin.py` (manifest, `app.sh`, `install.sh` from a plugin folder under a private
HOME, `omarchy-plugin-validate` when it's installed), `tests/cli/test_install_sh_safety.py` (a foreign app folder is
never run, the theme hook's private staging folder, the Hyprland block with a fake `hyprctl`, the prompt naming every
change), `tests/cli/test_plugin_update.py` (updates with throwaway git repos: offered and applied, local changes,
diverged and plain copies, an untracked file in the way) and `tests/qml/tst_plugin_install.qml` (`Model.js`).

## Limits

- The panel's install only covers what `install.sh` does (seconds: a copy and a few files in the home folder).
  The iOS toolchain still comes from Setup.
- The marketplace's static security scan reports the review capabilities iOS Studio needs (an installer,
  `pkexec` and `pacman` in Setup, a systemd user timer for re-signing, `pip` for its own venvs, the README's clone-and-install line) and no findings.
