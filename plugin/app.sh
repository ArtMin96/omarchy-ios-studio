#!/bin/bash
# The iOS Studio bar button's only helper (Omarchy plugin). Fixed commands: nothing the plugin passes reaches a shell.
#   app.sh status         prints "installed", "outdated" (the app folder is behind this plugin folder) or "missing"
#   app.sh open [setup]   opens iOS Studio, or brings its window to the front (on the Setup page with "setup")
#   app.sh install        runs install.sh from this plugin folder (it copies the app to ~/.local/share/omarchy-ios-studio)
#   app.sh update         runs install.sh --update: fast-forwards that copy to this plugin folder's commit, then repairs
# Runs only when the button is clicked: nothing here is called at bar load.
set -euo pipefail

here="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
plugin="$(cd "$here/.." && pwd)"
launcher="$HOME/.local/bin/ios-studio"   # install.sh links it; --uninstall removes it
app="$HOME/.local/share/omarchy-ios-studio"

# True when the launcher runs the app folder and that folder's commit is an older commit of this plugin folder
# (after `omarchy plugin update`). Two quick `git rev-parse`es and one ancestry check, local only.
# A copy with commits of its own, a plain copy without git, or an app installed elsewhere is never "outdated".
outdated() {
  local target mine theirs
  target="$(readlink -f "$launcher" 2>/dev/null)" || return 1
  [[ $target == "$(readlink -f "$app")"/* ]] || return 1
  [[ -d $plugin/.git && -d $app/.git ]] && command -v git >/dev/null 2>&1 || return 1
  theirs="$(timeout 5 git -C "$plugin" rev-parse --verify -q HEAD)" || return 1
  mine="$(timeout 5 git -C "$app" rev-parse --verify -q HEAD)" || return 1
  [[ $mine != "$theirs" ]] || return 1
  timeout 5 git -C "$plugin" merge-base --is-ancestor "$mine" "$theirs" 2>/dev/null
}

case "${1:-}" in
  status)
    if [[ ! -x $launcher ]]; then echo missing
    elif outdated; then echo outdated
    else echo installed
    fi
    ;;
  open)
    [[ -x $launcher ]] || exit 1
    case "${2:-}" in
      "") exec "$launcher" ;;
      setup) exec "$launcher" setup ;;
      *) echo "app.sh: unknown page '${2}'" >&2; exit 2 ;;
    esac
    ;;
  install)
    # A copy and a few links in the home folder: seconds, so a stuck run is cut off after five minutes.
    exec timeout 300 "$plugin/install.sh" 2>&1
    ;;
  update)
    exec timeout 300 "$plugin/install.sh" --update 2>&1
    ;;
  *)
    echo "usage: app.sh status|open [setup]|install|update" >&2
    exit 2
    ;;
esac
