"""App integration (group "app"): launcher keybinding, window rule, about info.

Edits only marker-delimited blocks in the user's existing Hyprland Lua files (it never creates them),
backs the file up first (~/.local/state/ios-studio/backups/), validates with `hyprctl reload` +
`hyprctl configerrors`, and restores the backup if the change brought new errors (errors Hyprland
already reported before the edit are left alone and don't undo it).
"""
from __future__ import annotations

import re
import shutil
import time
from pathlib import Path

from iosstudio.core import appdirs, fileio, subproc
from iosstudio.core.output import HelperError, fix, ok

HYPR = appdirs.HOME / ".config/hypr"
BINDINGS = HYPR / "bindings.lua"
HYPRLAND = HYPR / "hyprland.lua"
APP_ID = "ios-studio"            # Hyprland class / Wayland app id (shell.qml //@ pragma AppId)
CLASS_MATCH = f"^{APP_ID}$"      # window rules match the class as a regex
# Earlier builds used a per-user reverse-DNS id ("dev.<user>.iosstudio"); `app migrate` rewrites old blocks.
LEGACY_CLASS_RE = re.compile(r'"\^?dev\.[A-Za-z0-9_-]+\.iosstudio\$?"')
KEY = "SUPER + SHIFT + I"
KEY_LABEL = "SUPER SHIFT + I"   # how `omarchy menu keybindings --print` writes it
LAUNCHER = str(appdirs.LOCAL_BIN / "ios-studio")
BACKUPS = appdirs.STATE_DIR / "backups"

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


_LINE_NO = re.compile(r"\b(line|:)\s*\d+")


def _config_errors() -> list[str] | None:
    """What `hyprctl configerrors` reports now (one entry per non-blank line); None without hyprctl."""
    if not subproc.which("hyprctl"):
        return None
    res = subproc.run_cmd(["hyprctl", "configerrors"], timeout=15)
    return [l.strip() for l in (res.stdout or "").splitlines() if l.strip()]


def _new_errors(before: list[str], after: list[str]) -> list[str]:
    """Errors in `after` that weren't there `before`. Line numbers are ignored when comparing, so an
    old error that only moved (our block was added or removed above it) doesn't count as new."""
    old = {_LINE_NO.sub(r"\1N", e) for e in before}
    return [e for e in after if _LINE_NO.sub(r"\1N", e) not in old]


def _validate_or_restore(path: Path, backup: Path | None, before: list[str] | None) -> None:
    """Reload Hyprland and undo the edit if it brought errors that weren't reported before it.

    `before` is _config_errors() taken before the write (None: no hyprctl, nothing to check)."""
    if before is None or not subproc.which("hyprctl"):
        return
    subproc.run_cmd(["hyprctl", "reload"], timeout=15)
    errors = _new_errors(before, _config_errors() or [])
    if errors:
        if backup:
            shutil.copy2(backup, path)
        else:
            path.unlink(missing_ok=True)
        subproc.run_cmd(["hyprctl", "reload"], timeout=15)
        raise HelperError("hypr-config", "Hyprland didn't accept the change, so it was undone.",
                          detail="\n".join(errors))


def _write_block(path: Path, start: str, end: str, body: str | None) -> None:
    """body=None removes the block; otherwise inserts/replaces it at the end of the file."""
    if path.is_symlink():
        raise HelperError("unsafe-file", f"{path.name} is a symlink; iOS Studio won't edit it.")
    if not path.exists():
        if body is None:
            return    # nothing to remove: don't create the file or reload Hyprland
        # Never create it: a new file would hold only our block and replace the user's whole config.
        raise HelperError("hypr-config", f"Couldn't find your Hyprland config file {path.name} in "
                          f"{path.parent}, so iOS Studio didn't change anything. Restore the file "
                          "(Omarchy's Refresh Hyprland config in the Omarchy menu does this), then try again.",
                          fix("Try again", "settings"))
    text = _read(path)
    new = _block_re(start, end).sub("\n", text).rstrip("\n") + "\n"
    if body is not None:
        new += f"\n{start}\n{body}\n{end}\n"
    if new == text:
        return
    before = _config_errors()
    backup = _backup(path)
    fileio.atomic_write(path, new)
    _validate_or_restore(path, backup, before)


def _key_conflict() -> str:
    """What else is bound to SUPER+SHIFT+I (excluding us), or ""."""
    if not subproc.which("omarchy"):
        return ""
    try:
        res = subproc.run_cmd(["omarchy", "menu", "keybindings", "--print"], timeout=15)
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
            before = _config_errors()
            backup = _backup(path)
            fileio.atomic_write(path, new)
            _validate_or_restore(path, backup, before)
            changed.append(str(path))
    return ok({"migrated": changed})


def about(flags: dict) -> int:
    def ver(argv):
        try:
            r = subproc.run_cmd(argv, timeout=5)
            return (r.stdout or r.stderr or "").strip().splitlines()[0] if r.returncode == 0 else ""
        except (HelperError, IndexError):
            return ""
    git = ""
    try:
        r = subproc.run_cmd(["git", "-C", str(appdirs.REPO), "log", "-1", "--format=%h %cs"], timeout=5)
        git = r.stdout.strip() if r.returncode == 0 else ""
    except HelperError:
        pass
    return ok({
        "repo": str(appdirs.REPO),
        "commit": git,
        "quickshell": ver(["qs", "--version"]),
        "omarchy": ver(["omarchy", "version"]),
        "configDir": str(appdirs.CONFIG_DIR),
        "stateDir": str(appdirs.STATE_DIR),
        "cacheDir": str(appdirs.CACHE_DIR),
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
