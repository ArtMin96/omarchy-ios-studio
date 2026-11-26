"""Pinned versions, checksums and the paths Setup reads and writes (one place)."""
from __future__ import annotations

import os
from pathlib import Path

from iosstudio.core.appdirs import CACHE_DIR, HOME, LOCAL_BIN, REPO, STATE_DIR

# omarchy-apple-dev v0.4.8 (2026-11-23). Its install-toolchain.sh pins xtool 7cc56c3 (xtool main after 1.24.0,
# which already carries the Apple sign-in fix xtool-org/xtool#406 and the release-build fix #432, plus the
# fork's packaging fixes) and darwin-tools on AssetKit d797d6e (Icon Composer gradients, 1x single-scale bitmaps).
APPLE_DEV_REPO = "https://github.com/joshuaswarren/omarchy-apple-dev"
APPLE_DEV_COMMIT = "9f8352fc353d9d4f4112a454a81eb5769812387a"
# Where a fresh machine gets it: the same path as Omarchy PR #14255 (Install > Development > iOS).
APPLE_DEV_HOME = HOME / ".local/share/omarchy-apple-dev"

# Fallbacks when no checkout is present yet (the values at APPLE_DEV_COMMIT).
DEFAULT_XTOOL_REPO = "https://github.com/joshuaswarren/xtool"
DEFAULT_XTOOL_SHA = "7cc56c391a67652e86d164eddbbd62065a26599f"
DEFAULT_SDK_STAMP = "epoch=4,darwinTools=1.1.0,oam=1.4.1"

# An xtool binary that still contains the old GSA user agent lacks xtool-org/xtool#406: `xtool auth` gets
# HTTP 503 (#404). Every pin since omarchy-apple-dev v0.4.8 contains the fix; this check verifies the build.
XTOOL_OLD_UA = b"akd/1.0 CFNetwork/808.1.4"

# swift-bin from the AUR, pinned to a commit (6.4.2-2), built as the user.
SWIFT_AUR_COMMIT = "f78c5ec6f5be1052fa6ffd3bf78ee44b3fb607a0"
AUR_SNAPSHOT = "https://aur.archlinux.org/cgit/aur.git/snapshot/aur-{ref}.tar.gz"
AUR_SNAPSHOT_HEAD = "https://aur.archlinux.org/cgit/aur.git/snapshot/{pkg}.tar.gz"

PYMD3_VERSION = "11.49.0"

RCODESIGN_VERSION = "0.29.0"
RCODESIGN_SHA = {
    "aarch64": "4af92c87ddf52f5f2d1258a3b4e56c7dcb8f1b2468df744976c5f139e031961f",
    "x86_64": "dbe85cedd8ee4217b64e9a0e4c2aef92ab8bcaaa41f20bde99781ff02e600002",
}
IPSW_VERSION = "3.1.743"   # the same pin and checksums as install-toolchain.sh at APPLE_DEV_COMMIT
IPSW_SHA = {
    "aarch64": ("arm64", "c7976f40d648ede62bc2d8bb80b5e57ab6365d5ba531a917a3515466a753c21e"),
    "x86_64": ("x86_64", "56e9ba754781dfeaa3b84f733ad25e63ef8e2683214a321e06b31a0d85f95e4c"),
}

PACKAGES = ["usbmuxd", "zip", "base-devel", "git", "libimobiledevice", "openssl", "poppler", "libheif",
            "python", "curl", "ffmpeg"]
# GStreamer pieces UxPlay's mirror needs at run time: the H.264/H.265 parsers (bad) and hardware decode (va)
UXPLAY_RUNTIME = ["gst-plugins-bad", "gst-plugin-va"]

# Swift major.minor -> (Xcode we recommend, Xcode major that works, approx .xip bytes)
XCODE_FOR_SWIFT = {"6.4": ("27.1", "27", 2_027_369_056), "6.3": ("26", "26", 3_000_000_000)}
XIP_MIN_BYTES = 1_000_000_000

SDK_CACHE = HOME / ".cache/xtool"
APPLE_DEV_CACHE = HOME / ".cache/omarchy-apple-dev"
APPLE_DEV_LIB = HOME / ".local/lib/omarchy-apple-dev"
LEGACY_VENV = HOME / "pymobile3-venv"   # omarchy-apple-dev's venv path (its scripts expect it)
AUR_DIR = CACHE_DIR / "aur"
SETUP_STATE = STATE_DIR / "setup.json"
OMARCHY_HOOK = HOME / ".config/omarchy/hooks/pre-refresh-pacman.d/ios-studio-hold-swift-bin"
DESKTOP_FILE = HOME / ".local/share/applications/ios-studio.desktop"
LAUNCHER = LOCAL_BIN / "ios-studio"
ROOT_SCRIPT = REPO / "helper/root/root-tasks.sh"
XCSPEC_PY = REPO / "helper/root/xcspec.py"
HOOK_SRC = REPO / "helper/root/hold-swift-bin-hook.sh"
DOWNLOADS = Path(os.environ.get("XDG_DOWNLOAD_DIR", HOME / "Downloads"))
XTOOL_CONFIG = HOME / ".config/xtool"
DOWNLOAD_PAGE = "https://developer.apple.com/download/all/?q=Xcode%20{v}"
DIRECT_XIP = "https://download.developer.apple.com/Developer_Tools/Xcode_{v}/Xcode_{v}.xip"
