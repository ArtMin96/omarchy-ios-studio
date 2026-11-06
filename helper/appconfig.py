"""App integration (group "app"): launcher keybinding, window rule, about info.

Edits only marker-delimited blocks in the user's Hyprland Lua files, backs the file up first
(~/.local/state/ios-studio/backups/), validates with `hyprctl reload` + `hyprctl configerrors`,
and restores the backup if Hyprland reports errors.
"""
from __future__ import annotations

import re
import shutil
import time
from pathlib import Path

import common
from common import HelperError, ok

HYPR = common.HOME / ".config/hypr"
BINDINGS = HYPR / "bindings.lua"
HYPRLAND = HYPR / "hyprland.lua"
APP_ID = "ios-studio"            # Hyprland class / Wayland app id (shell.qml //@ pragma AppId)
CLASS_MATCH = f"^{APP_ID}$"      # window rules match the class as a regex
# Earlier builds used a per-user reverse-DNS id ("dev.<user>.iosstudio"); `app migrate` rewrites old blocks.
LEGACY_CLASS_RE = re.compile(r'"\^?dev\.[A-Za-z0-9_-]+\.iosstudio\$?"')
KEY = "SUPER + SHIFT + I"
KEY_LABEL = "SUPER SHIFT + I"   # how `omarchy menu keybindings --print` writes it
LAUNCHER = str(common.LOCAL_BIN / "ios-studio")
BACKUPS = common.STATE_DIR / "backups"

BIND_START, BIND_END = "-- ios-studio: keybinding start", "-- ios-studio: keybinding end"
WIN_START, WIN_END = "-- ios-studio: window rule start", "-- ios-studio: window rule end"
OPAQUE_START, OPAQUE_END = "-- ios-studio: opacity rule start", "-- ios-studio: opacity rule end"
# Omarchy tags every window "default-opacity" and makes it slightly see-through
# ($OMARCHY_PATH/default/hypr/windows.lua). Opt out the same way Omarchy's own
# app rules do (apps/hermes.lua, apps/pip.lua): drop the tag and pin opacity to 1.
OPAQUE_RULE = f'o.window("{CLASS_MATCH}", {{ tag = "-default-opacity", opacity = "1 1" }})'


def _block_re(start: str, end: str) -> re.Pattern:
    return re.compile(r"\n?" + re.escape(start) + r".*?" + re.escape(end) + r"\n?", re.S)


def _read(path: Path) -> str:
    try:
        return path.read_text()
    except FileNotFoundError:
        return ""


def _has_block(path: Path, start: str) -> bool:
    return start in _read(path)


def _backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    BACKUPS.mkdir(parents=True, exist_ok=True)
    dest = BACKUPS / f"{path.name}.{int(time.time())}"
    shutil.copy2(path, dest)
    # keep the 10 newest per file
    olds = sorted(BACKUPS.glob(f"{path.name}.*"))[:-10]
    for o in olds:
        o.unlink(missing_ok=True)
    return dest


def _validate_or_restore(path: Path, backup: Path | None) -> None:
    if not common.which("hyprctl"):
        return
    common.run_cmd(["hyprctl", "reload"], timeout=15)
    res = common.run_cmd(["hyprctl", "configerrors"], timeout=15)
    errors = [l for l in (res.stdout or "").splitlines() if l.strip()]
    if errors:
        if backup:
            shutil.copy2(backup, path)
        else:
            path.unlink(missing_ok=True)
        common.run_cmd(["hyprctl", "reload"], timeout=15)
        raise HelperError("hypr-config", "Hyprland didn't accept the change, so it was undone.",
                          detail="\n".join(errors))


def _write_block(path: Path, start: str, end: str, body: str | None) -> None:
    """body=None removes the block; otherwise inserts/replaces it at the end of the file."""
    if path.is_symlink():
        raise HelperError("unsafe-file", f"{path.name} is a symlink; iOS Studio won't edit it.")
    text = _read(path)
    new = _block_re(start, end).sub("\n", text).rstrip("\n") + "\n"
    if body is not None:
        new += f"\n{start}\n{body}\n{end}\n"
    if new == text:
        return
    backup = _backup(path)
    common.atomic_write(path, new)
    _validate_or_restore(path, backup)


def _key_conflict() -> str:
    """What else is bound to SUPER+SHIFT+I (excluding us), or ""."""
    if not common.which("omarchy"):
        return ""
    try:
        res = common.run_cmd(["omarchy", "menu", "keybindings", "--print"], timeout=15)
    except HelperError:
        return ""
    for line in (res.stdout or "").splitlines():
        m = re.match(r"\s*(.+?)\s+→\s+(.*)$", line)
        if not m:
            continue
        keys = re.sub(r"\s+", " ", m.group(1).strip())
        if keys.upper() == KEY_LABEL and "iOS Studio" not in m.group(2):
            return m.group(2).strip()
    return ""


def status(flags: dict) -> int:
    enabled = _has_block(BINDINGS, BIND_START)
    win = "float" if _has_block(HYPRLAND, WIN_START) else "tile"
    return ok({
        "keybinding": {"enabled": enabled, "key": KEY, "label": "Super + Shift + I",
                       "conflict": "" if enabled else _key_conflict()},
        "windowRule": {"mode": win},
        "opaque": _has_block(HYPRLAND, OPAQUE_START),
        "paths": {"bindings": str(BINDINGS), "hyprland": str(HYPRLAND)},
    })


def keybinding(flags: dict) -> int:
    enable = flags.get("enable") is True or str(flags.get("enabled", "")).lower() in ("1", "true", "on", "yes")
    if enable:
        conflict = _key_conflict()
        if conflict:
            raise HelperError("key-taken", f"Super + Shift + I is already used for “{conflict}”.")
        body = f'o.bind("{KEY}", "iOS Studio", "{LAUNCHER}")'
        _write_block(BINDINGS, BIND_START, BIND_END, body)
    else:
        _write_block(BINDINGS, BIND_START, BIND_END, None)
    return ok({"enabled": enable, "key": KEY})


def window_rule(flags: dict) -> int:
    mode = str(flags.get("mode", "")).lower()
    if mode not in ("float", "tile"):
        raise HelperError("usage", "Window mode must be float or tile.")
    if mode == "float":
        w = int(flags.get("width") or 1180)
        h = int(flags.get("height") or 780)
        body = f'o.window("{CLASS_MATCH}", {{ float = true, center = true, size = {{ {w}, {h} }} }})'
        _write_block(HYPRLAND, WIN_START, WIN_END, body)
    else:
        _write_block(HYPRLAND, WIN_START, WIN_END, None)
    return ok({"mode": mode})


def opacity(flags: dict) -> int:
    """`app opacity --opaque on|off`: solid window (default) or Omarchy's see-through default."""
    val = str(flags.get("opaque", "")).lower()
    if val not in ("1", "true", "on", "yes", "0", "false", "off", "no"):
        raise HelperError("usage", "Pass --opaque on or --opaque off.")
    on = val in ("1", "true", "on", "yes")
    _write_block(HYPRLAND, OPAQUE_START, OPAQUE_END, OPAQUE_RULE if on else None)
    return ok({"opaque": on})


def migrate(flags: dict) -> int:
    """`app migrate`: point blocks written for the old app id at the current one, keeping each choice."""
    changed = []
    for path in (BINDINGS, HYPRLAND):
        text = _read(path)
        if not text or path.is_symlink():
            continue
        new = text
        for start, end in ((BIND_START, BIND_END), (WIN_START, WIN_END), (OPAQUE_START, OPAQUE_END)):
            new = _block_re(start, end).sub(lambda m: LEGACY_CLASS_RE.sub(f'"{CLASS_MATCH}"', m.group(0)), new)
        if new != text:
            backup = _backup(path)
            common.atomic_write(path, new)
            _validate_or_restore(path, backup)
            changed.append(str(path))
    return ok({"migrated": changed})


def about(flags: dict) -> int:
    def ver(argv):
        try:
            r = common.run_cmd(argv, timeout=5)
            return (r.stdout or r.stderr or "").strip().splitlines()[0] if r.returncode == 0 else ""
        except (HelperError, IndexError):
            return ""
    git = ""
    try:
        r = common.run_cmd(["git", "-C", str(common.REPO), "log", "-1", "--format=%h %cs"], timeout=5)
        git = r.stdout.strip() if r.returncode == 0 else ""
    except HelperError:
        pass
    return ok({
        "repo": str(common.REPO),
        "commit": git,
        "quickshell": ver(["qs", "--version"]),
        "omarchy": ver(["omarchy", "version"]),
        "configDir": str(common.CONFIG_DIR),
        "stateDir": str(common.STATE_DIR),
        "cacheDir": str(common.CACHE_DIR),
    })


GROUPS = {
    "app": {
        "status": status,
        "keybinding": keybinding,
        "window-rule": window_rule,
        "opacity": opacity,
        "migrate": migrate,
        "about": about,
    }
}
