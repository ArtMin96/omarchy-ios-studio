#!/usr/bin/env bash
# iOS Studio: Omarchy pre-refresh-pacman hook (installed as
# ~/.config/omarchy/hooks/pre-refresh-pacman.d/ios-studio-hold-swift-bin by Setup step "swift-hold").
# `omarchy refresh pacman` overwrites /etc/pacman.conf with Omarchy's template, then runs this hook
# (as the user, inside its own sudo session) before `pacman -Syyuu`. It puts swift-bin back in
# IgnorePkg so the update cannot move Swift off the version that matches the iOS SDK.
set -euo pipefail

CONF=/etc/pacman.conf
list="$(pacman-conf IgnorePkg 2>/dev/null || true)"
if [[ $'\n'"$list"$'\n' == *$'\n'swift-bin$'\n'* ]]; then
  exit 0
fi
pacman -Q swift-bin >/dev/null 2>&1 || exit 0

if grep -qE '^[[:space:]]*IgnorePkg[[:space:]]*=' "$CONF"; then
  sudo sed -i -E '0,/^[[:space:]]*IgnorePkg[[:space:]]*=/{s/^([[:space:]]*IgnorePkg[[:space:]]*=.*)$/\1 swift-bin/}' "$CONF"
elif grep -qE '^HoldPkg' "$CONF"; then
  sudo sed -i '/^HoldPkg/a IgnorePkg   = swift-bin' "$CONF"
else
  sudo sed -i '/^\[options\]/a IgnorePkg   = swift-bin' "$CONF"
fi
echo "iOS Studio: kept swift-bin on hold (IgnorePkg)"
