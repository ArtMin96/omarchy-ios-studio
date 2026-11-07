#!/usr/bin/env bash
# iOS Studio: the root half of Setup. helper/iosstudio/features/setup/ runs this through `pkexec` exactly once per
# install step (or once for "Install everything"), so the user sees one password prompt. Every task
# is opt-in by flag and idempotent. It never touches anything outside the paths named below.
#
#   --packages "a b c"      pacman -S --needed --noconfirm <packages>
#   --deps "a b"            pacman -S --needed --noconfirm --asdeps <packages> (AUR make-deps)
#   --aur-build DIR         build the PKGBUILD in DIR as the calling user (runuser, never root),
#                           then pacman -U the result. DIR must be under ~/.cache/ios-studio/aur/
#   --hold-swift            add swift-bin to IgnorePkg in /etc/pacman.conf (timestamped backup)
#   --unhold-swift          remove swift-bin from IgnorePkg
#   --xcspec PY             apply the 3 SwiftBuild xcspec defaults with PY (xcspec.py) to swift's root
#   --usbmuxd-dropin        Restart=on-failure drop-in for usbmuxd.service + daemon-reload
#   --remove-usbmuxd-dropin
#   --udev-trigger          re-run udev for plugged-in Apple devices (starts usbmuxd)
#   --avahi                 enable --now avahi-daemon (UxPlay discovery)
#   --ufw-uxplay            open UxPlay's fixed ports (-p) for the local network when ufw is active
#   --remove-swift          pacman -Rns swift-bin
#   --dry-run               print what would run, change nothing
#
# Output: plain lines; "@@task <name>" marks the start of each task (the helper turns those into
# progress). Exit 0 ok, 2 bad usage, 3 pacman is locked, 1 a task failed.
set -euo pipefail
export LANG=C.UTF-8 LC_ALL=C.UTF-8
PATH=/usr/bin:/usr/sbin:/bin
export PATH

die() { echo "error: $*" >&2; exit "${2:-1}"; }
mark() { echo "@@task $1"; }

dry=0
packages="" deps="" xcspec_py="" hold=0 unhold=0 dropin=0 rmdropin=0 udev=0 avahi=0 ufw=0 rmswift=0
aur_dirs=()
while (($#)); do
  case "$1" in
    --packages) packages="${2:-}"; shift ;;
    --deps) deps="${2:-}"; shift ;;
    --aur-build) aur_dirs+=("${2:-}"); shift ;;
    --hold-swift) hold=1 ;;
    --unhold-swift) unhold=1 ;;
    --xcspec) xcspec_py="${2:-}"; shift ;;
    --usbmuxd-dropin) dropin=1 ;;
    --remove-usbmuxd-dropin) rmdropin=1 ;;
    --udev-trigger) udev=1 ;;
    --avahi) avahi=1 ;;
    --ufw-uxplay) ufw=1 ;;
    --remove-swift) rmswift=1 ;;
    --dry-run) dry=1 ;;
    *) die "unknown argument '$1'" 2 ;;
  esac
  shift
done

run() {
  echo "+ $*"
  if [[ "$dry" == 1 ]]; then return 0; fi
  "$@"
}

if [[ "$dry" != 1 ]]; then
  [[ "$(id -u)" == 0 ]] || die "run this through pkexec" 2
fi
uid="${PKEXEC_UID:-${SUDO_UID:-}}"
[[ -n "$uid" && "$uid" != 0 ]] || [[ "$dry" == 1 ]] || die "cannot tell which user started this (PKEXEC_UID)" 2
user="$(id -nu "${uid:-$(id -u)}")"
home="$(getent passwd "$user" | cut -d: -f6)"

valid_pkgs() { [[ "$1" =~ ^[a-z0-9@._+\ -]*$ ]]; }
valid_pkgs "$packages" || die "bad package list" 2
valid_pkgs "$deps" || die "bad dependency list" 2

pacman_unlocked() {
  if [[ -e /var/lib/pacman/db.lck ]]; then
    echo "Another program is installing packages right now (/var/lib/pacman/db.lck)." >&2
    exit 3
  fi
}

held() {
  local list
  list="$(pacman-conf IgnorePkg 2>/dev/null || true)"
  [[ $'\n'"$list"$'\n' == *$'\n'swift-bin$'\n'* ]]
}

CONF=/etc/pacman.conf

if [[ -n "$packages" ]]; then
  mark packages
  pacman_unlocked
  # shellcheck disable=SC2086  # one word per package
  run pacman -S --needed --noconfirm $packages
fi

if [[ -n "$deps" ]]; then
  mark deps
  pacman_unlocked
  # shellcheck disable=SC2086
  run pacman -S --needed --noconfirm --asdeps $deps
fi

for dir in "${aur_dirs[@]}"; do
  mark "aur $(basename "$dir")"
  real="$(readlink -f -- "$dir" || echo "$dir")"
  if [[ "$dry" != 1 ]]; then
    [[ "$real" == "$home/.cache/ios-studio/aur/"* && -f "$real/PKGBUILD" ]] || die "not an iOS Studio AUR build dir: $dir" 2
    [[ "$(stat -c %u "$real")" == "$uid" ]] || die "$real is not owned by $user" 2
    pacman_unlocked
  fi
  # makepkg refuses root; build as the user with a clean environment. Uncompressed packages:
  # swift-bin is 3.5 GB unpacked and zstd would add minutes for a file deleted right after.
  as_user=(runuser -u "$user" -- env -i HOME="$home" USER="$user" LOGNAME="$user" PATH=/usr/bin:/bin
    LANG=C.UTF-8 PKGEXT=.pkg.tar)
  (if ! cd "$real" 2>/dev/null; then [[ "$dry" == 1 ]] || exit 1; fi; run "${as_user[@]}" makepkg -f --nodeps --noconfirm --skippgpcheck)
  if [[ "$dry" == 1 ]]; then
    echo "+ pacman -U --noconfirm <packages built in $real>"
  else
    mapfile -t built < <(cd "$real" && "${as_user[@]}" makepkg --packagelist)
    files=()
    for f in "${built[@]}"; do [[ -f "$f" ]] && files+=("$f"); done
    ((${#files[@]})) || die "makepkg built no package in $real"
    run pacman -U --noconfirm --needed "${files[@]}"
  fi
done

if [[ "$hold" == 1 ]]; then
  mark hold
  if held; then
    echo "swift-bin is already held (IgnorePkg)"
  else
    backup="$CONF.bak-ios-studio-$(date +%Y%m%d-%H%M%S)"
    run cp -a "$CONF" "$backup"
    if grep -qE '^[[:space:]]*IgnorePkg[[:space:]]*=' "$CONF"; then
      run sed -i -E '0,/^[[:space:]]*IgnorePkg[[:space:]]*=/{s/^([[:space:]]*IgnorePkg[[:space:]]*=.*)$/\1 swift-bin/}' "$CONF"
    elif grep -qE '^HoldPkg' "$CONF"; then
      run sed -i '/^HoldPkg/a IgnorePkg   = swift-bin' "$CONF"
    else
      run sed -i '/^\[options\]/a IgnorePkg   = swift-bin' "$CONF"
    fi
    [[ "$dry" == 1 ]] || held || die "could not add swift-bin to IgnorePkg (backup: $backup)"
    echo "swift-bin added to IgnorePkg (backup: $backup)"
  fi
fi

if [[ "$unhold" == 1 ]]; then
  mark unhold
  if held; then
    run cp -a "$CONF" "$CONF.bak-ios-studio-$(date +%Y%m%d-%H%M%S)"
    # Drop the word from IgnorePkg lines; an emptied "IgnorePkg =" line is harmless.
    run sed -i -E '/^[[:space:]]*IgnorePkg[[:space:]]*=/{s/[[:space:]]+swift-bin([[:space:]]|$)/\1/g}' "$CONF"
  else
    echo "swift-bin is not held"
  fi
fi

if [[ -n "$xcspec_py" ]]; then
  mark xcspec
  [[ -f "$xcspec_py" ]] || die "missing $xcspec_py" 2
  [[ "$dry" == 1 ]] || pacman -Q swift-bin >/dev/null 2>&1 || die "swift-bin is not installed"
  swift_real="$(readlink -f /usr/bin/swift 2>/dev/null || true)"
  [[ -n "$swift_real" ]] || die "swift is not installed"
  root="${swift_real%/usr/bin/*}"
  run /usr/bin/python3 -I "$xcspec_py" apply "$root"
  [[ "$dry" == 1 ]] || /usr/bin/python3 -I "$xcspec_py" check "$root"
fi

DROPIN=/etc/systemd/system/usbmuxd.service.d/ios-studio-restart.conf
if [[ "$dropin" == 1 ]]; then
  mark usbmuxd
  if [[ "$dry" == 1 ]]; then
    echo "+ write $DROPIN"
  else
    mkdir -p "$(dirname "$DROPIN")"
    printf '[Unit]\nStartLimitIntervalSec=0\n\n[Service]\nRestart=on-failure\nRestartSec=1\n' >"$DROPIN"
    echo "wrote $DROPIN"
  fi
  run systemctl daemon-reload
fi

if [[ "$rmdropin" == 1 ]]; then
  mark usbmuxd
  if [[ -e "$DROPIN" ]]; then
    run rm -f "$DROPIN"
    run systemctl daemon-reload
  fi
fi

if [[ "$udev" == 1 ]]; then
  mark udev
  run udevadm trigger --subsystem-match=usb --attr-match=idVendor=05ac --action=add || true
fi

if [[ "$avahi" == 1 ]]; then
  mark avahi
  run systemctl enable --now avahi-daemon.service
fi

if [[ "$ufw" == 1 ]]; then
  mark firewall
  if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q '^Status: active'; then
    for net in 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16; do
      run ufw allow from "$net" to any port 7000,7001,7100 proto tcp comment 'iOS Studio UxPlay'
      run ufw allow from "$net" to any port 6000,6001,7011 proto udp comment 'iOS Studio UxPlay'
      run ufw allow from "$net" to any port 5353 proto udp comment 'iOS Studio mDNS'
    done
  else
    echo "ufw is not active: nothing to open"
  fi
fi

if [[ "$rmswift" == 1 ]]; then
  mark remove-swift
  pacman_unlocked
  if pacman -Q swift-bin >/dev/null 2>&1; then
    run pacman -Rns --noconfirm swift-bin
  else
    echo "swift-bin is not installed"
  fi
fi

echo "@@task done"
