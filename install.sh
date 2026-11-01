#!/bin/bash
# iOS Studio installer. Idempotent, non-interactive, no sudo.
#   ./install.sh              install / repair
#   ./install.sh --uninstall  remove everything this script created (keeps your settings and data)
set -euo pipefail

REPO="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
OMARCHY_PATH="${OMARCHY_PATH:-/usr/share/omarchy}"
APP_ID="ios-studio"
BIN_DIR="$HOME/.local/bin"
APPS_DIR="$HOME/.local/share/applications"
ICON_ROOT="$HOME/.local/share/icons/hicolor"
ICON_PATH="$ICON_ROOT/scalable/apps/$APP_ID.svg"
DESKTOP="$APPS_DIR/$APP_ID.desktop"
HOOK_DIR="$HOME/.config/omarchy/hooks/theme-set.d"
HOOK="$HOOK_DIR/ios-studio-theme"

say() { printf '  %s\n' "$*"; }

# Earlier builds used a reverse-DNS app id ("dev.<user>.iosstudio"). Remove what they installed.
remove_legacy() {
  local f
  shopt -s nullglob
  for f in "$APPS_DIR"/dev.*.iosstudio.desktop "$ICON_ROOT"/*/apps/dev.*.iosstudio.*; do
    rm -f "$f" && say "removed old $f"
  done
  shopt -u nullglob
}

refresh_icons() {
  if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -q -t -f "$ICON_ROOT" 2>/dev/null || true
  fi
  command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database -q "$APPS_DIR" 2>/dev/null || true
}

uninstall() {
  echo "Uninstalling iOS Studio launcher integration"
  [[ -L $BIN_DIR/ios-studio ]] && rm -f "$BIN_DIR/ios-studio" && say "removed $BIN_DIR/ios-studio"
  [[ -f $DESKTOP ]] && rm -f "$DESKTOP" && say "removed $DESKTOP"
  [[ -f $ICON_PATH ]] && rm -f "$ICON_PATH" && say "removed $ICON_PATH"
  [[ -f $HOOK ]] && rm -f "$HOOK" && say "removed $HOOK"
  remove_legacy
  # Hyprland blocks iOS Studio added (keybinding, window rule, opacity rule); each is a marked block.
  if [[ -x $REPO/bin/iosstudio-helper ]]; then
    "$REPO/bin/iosstudio-helper" app keybinding --enabled false >/dev/null 2>&1 || true
    "$REPO/bin/iosstudio-helper" app window-rule --mode tile >/dev/null 2>&1 || true
    "$REPO/bin/iosstudio-helper" app opacity --opaque off >/dev/null 2>&1 || true
    say "removed iOS Studio's Hyprland keybinding / window rules (if any)"
    # The daily re-sign timer (a systemd user unit) and AI agent registrations point into this folder.
    "$REPO/bin/iosstudio-helper" projects autorenew --disable >/dev/null 2>&1 || true
    say "turned off automatic re-signing (if it was on)"
    local clients
    clients=$("$REPO/bin/iosstudio-helper" agents list 2>/dev/null | /usr/bin/python3 -I -c '
import json, sys
try:
    for c in json.load(sys.stdin)["data"]["clients"]:
        if c.get("registered"):
            print(c["id"])
except Exception:
    pass' || true)
    for c in $clients; do
      "$REPO/bin/iosstudio-helper" agents disconnect --client "$c" >/dev/null 2>&1 && say "disconnected iOS Studio from $c"
    done
  fi
  for l in Commons Ui; do [[ -L $REPO/$l ]] && rm -f "$REPO/$l"; done
  refresh_icons
  say "kept ~/.config/ios-studio, ~/.local/state/ios-studio, ~/.local/share/ios-studio (delete them by hand to reset)"
}

if [[ ${1:-} == "--uninstall" ]]; then uninstall; exit 0; fi

echo "Installing iOS Studio"

[[ -d $OMARCHY_PATH/shell/Commons && -d $OMARCHY_PATH/shell/Ui ]] || { echo "Omarchy shell not found at $OMARCHY_PATH/shell" >&2; exit 1; }
command -v qs >/dev/null 2>&1 || { echo "Quickshell (qs) not found" >&2; exit 1; }

# 1. Omarchy UI kit
ln -sfn "$OMARCHY_PATH/shell/Commons" "$REPO/Commons"
ln -sfn "$OMARCHY_PATH/shell/Ui" "$REPO/Ui"
say "linked Commons/ and Ui/ from $OMARCHY_PATH/shell"

# 2. launcher
chmod +x "$REPO/bin/ios-studio" "$REPO/bin/iosstudio-helper"
[[ -f $REPO/bin/iosstudio-deviced ]] && chmod +x "$REPO/bin/iosstudio-deviced"
mkdir -p "$BIN_DIR"
ln -sfn "$REPO/bin/ios-studio" "$BIN_DIR/ios-studio"
say "linked $BIN_DIR/ios-studio"

# 3. icon (and anything an older build installed under its previous app id)
remove_legacy
mkdir -p "$(dirname "$ICON_PATH")"
install -m 644 "$REPO/assets/icon.svg" "$ICON_PATH"
say "installed icon $ICON_PATH"

# 4. desktop entry
mkdir -p "$APPS_DIR"
cat > "$DESKTOP.tmp" <<DESK
[Desktop Entry]
Type=Application
Name=iOS Studio
GenericName=iOS development
Comment=Build and run iPhone apps on Linux: toolchain, Apple ID, iPhone, projects and logs
Exec=$BIN_DIR/ios-studio
Icon=$APP_ID
Terminal=false
Categories=Development;IDE;
Keywords=ios;iphone;xtool;xcode;swift;apple;ipa;
StartupWMClass=$APP_ID
StartupNotify=true
Actions=projects;device;logs;

[Desktop Action projects]
Name=Projects
Exec=$BIN_DIR/ios-studio projects

[Desktop Action device]
Name=iPhone
Exec=$BIN_DIR/ios-studio device

[Desktop Action logs]
Name=Logs
Exec=$BIN_DIR/ios-studio logs
DESK
mv "$DESKTOP.tmp" "$DESKTOP"
say "wrote $DESKTOP"

# 5. live theme hook (the app also watches the theme itself; this is belt and braces)
hook_tmp="$(mktemp)"
cat > "$hook_tmp" <<HOOK
#!/bin/bash
# Installed by iOS Studio ($REPO/install.sh). Re-colours a running iOS Studio window.
qs ipc -p $(printf '%q' "$REPO") call iosstudio applyTheme >/dev/null 2>&1 || true
HOOK
mkdir -p "$HOOK_DIR"
if command -v omarchy-hook-install >/dev/null 2>&1; then
  cp "$hook_tmp" "${hook_tmp%/*}/ios-studio-theme"
  omarchy-hook-install theme-set "${hook_tmp%/*}/ios-studio-theme" >/dev/null
  rm -f "${hook_tmp%/*}/ios-studio-theme"
else
  install -m 755 "$hook_tmp" "$HOOK"
fi
rm -f "$hook_tmp"
say "installed theme-set hook $HOOK"

# 6. app dirs
mkdir -p "$HOME/.config/ios-studio" "$HOME/.local/state/ios-studio" "$HOME/.cache/ios-studio"

# 7. Hyprland blocks written for the old app id: keep each choice, point it at the new id
if command -v hyprctl >/dev/null 2>&1; then
  "$REPO/bin/iosstudio-helper" app migrate >/dev/null 2>&1 || say "couldn't update iOS Studio's older Hyprland rules; re-apply them in Settings"
fi

# 8. solid window (Omarchy makes windows slightly see-through by default, which hurts
#    readability). Skipped when you turned "Opaque window" off in Settings.
settings="$HOME/.config/ios-studio/settings.json"
opt_out=0
if [[ -f $settings ]] && command -v jq >/dev/null 2>&1; then
  [[ $(jq -r '.opaqueWindow // true' "$settings" 2>/dev/null) == "false" ]] && opt_out=1
fi
if (( opt_out )); then
  say "left the window see-through (Opaque window is off in Settings)"
elif command -v hyprctl >/dev/null 2>&1; then
  if "$REPO/bin/iosstudio-helper" app opacity --opaque on >/dev/null 2>&1; then
    say "made the iOS Studio window opaque (Settings → Opaque window)"
  else
    say "couldn't add the opaque window rule; you can turn it on in Settings"
  fi
fi

refresh_icons
echo "Done. Launch \"iOS Studio\" from the app launcher, or run: ios-studio"
