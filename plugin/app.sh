#!/bin/bash
# The iOS Studio bar button's only helper (Omarchy plugin). Fixed commands: nothing the plugin passes reaches a shell.
#   app.sh status         prints "installed" or "missing"
#   app.sh open [setup]   opens iOS Studio, or brings its window to the front (on the Setup page with "setup")
#   app.sh install        runs install.sh from this plugin folder (it copies the app to ~/.local/share/omarchy-ios-studio)
set -u

here="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
launcher="$HOME/.local/bin/ios-studio"   # install.sh links it; --uninstall removes it

case "${1:-}" in
  status)
    if [[ -x $launcher ]]; then echo installed; else echo missing; fi
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
    exec timeout 300 "$here/../install.sh" 2>&1
    ;;
  *)
    echo "usage: app.sh status|open [setup]|install" >&2
    exit 2
    ;;
esac
