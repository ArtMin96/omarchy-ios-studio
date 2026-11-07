"""Pinned versions, checksums and the paths Setup reads and writes (one place)."""
from __future__ import annotations

import os
from pathlib import Path

from iosstudio.core.appdirs import CACHE_DIR, HOME, LOCAL_BIN, REPO, STATE_DIR

# omarchy-apple-dev: the commit proven on the author's machine (2026-10-31): xtool fork pin f300489.
APPLE_DEV_REPO = "https://github.com/joshuaswarren/omarchy-apple-dev"
APPLE_DEV_COMMIT = "79bda716bf207924efc25bce923bda261dc67837"
# Where a fresh machine gets it: the same path as Omarchy PR #14255 (Install > Development > iOS).
APPLE_DEV_HOME = HOME / ".local/share/omarchy-apple-dev"

# Fallbacks when no checkout is present yet (the values at APPLE_DEV_COMMIT).
DEFAULT_XTOOL_REPO = "https://github.com/joshuaswarren/xtool"
DEFAULT_XTOOL_SHA = "f300489f3a3fddbd9fb598ba6cf69eb24a494527"
DEFAULT_SDK_STAMP = "epoch=4,darwinTools=1.1.0,oam=1.4.1"

# xtool-org/xtool PR #406 (GSA user agent). Without it `xtool auth` gets HTTP 503 (#404). A binary that
# still contains the old user agent string lacks the fix.
XTOOL_FIX_REPO = "https://github.com/xtool-org/xtool"
XTOOL_FIX_SHA = "70e1568fdfcb20b560774057cd783f02361293b9"
XTOOL_OLD_UA = b"akd/1.0 CFNetwork/808.1.4"
XTOOL_KNOWN_BASE = "f300489f3a3fddbd9fb598ba6cf69eb24a494527"
XTOOL_KNOWN_TREE = "bb93e00eaa35bca365d8625749b912648d665fae"

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
IPSW_VERSION = "3.1.731"
IPSW_SHA = {
    "aarch64": ("arm64", "a8d373966d79d24537aa17ec8b9a61bbc73964e6200383f7708c53400ad9f42a"),
    "x86_64": ("x86_64", "98e5b127641be3511242a5ba9c5872ef34010e5171318b616e5aa80a607749ff"),
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
