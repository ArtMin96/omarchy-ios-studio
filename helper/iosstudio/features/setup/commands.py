"""Groups `setup` and `toolchain`: parse flags, call the logic modules, emit output.

setup check                      one JSON object: every Setup step with its state (read-only, fast)
setup install --step ID          JSON lines: install or repair one step
setup install --all              JSON lines: every missing auto-installable step, in order
      [--dry-run] [--force] [--yes] [--include-optional]
setup xip --watch                JSON lines: watch ~/Downloads for the Xcode .xip and validate it
setup xip --use PATH             JSON lines: validate a .xip the user picked
setup xip --open                 one JSON object: open Apple's download page for the needed Xcode
toolchain info                   one JSON object: versions of every piece
toolchain disk [--cached]        one JSON object: disk usage per piece, which are safe to clean
toolchain clean --item ID        JSON lines: delete one allow-listed item  [--dry-run]
toolchain remove-all             JSON lines: remove the iOS toolchain  [--remove-auth] [--keep-swift] [--dry-run] [--yes]

Root work goes through ONE `pkexec helper/root/root-tasks.sh ...` per step (one password prompt).
yay/makepkg never run as root; AUR packages are built as the user (runuser inside the root script).
--dry-run prints every command that would run as log events and changes nothing.
Installers and the toolchain group are imported only when their command runs (`setup check` stays lean).
"""
from __future__ import annotations

import os
from pathlib import Path

from iosstudio.core import appdirs, fileio
from iosstudio.core.output import HelperError, ok

from .facts import gather, needed_xcode
from .runtime import streaming
from .steps import build_steps, check


def _xcode_needed() -> dict:
    """build_steps' context: which Xcode the installed Swift needs (xcode, xcode_major, swift_ver)."""
    _steps, c = build_steps(gather())
    return c


# ------------------------------------------------------------------ setup

def cmd_check(flags: dict) -> int:
    fixture = os.environ.get("IOS_STUDIO_SETUP_FIXTURE")  # UI development only: a saved `setup check` data object
    if fixture:
        return ok(fileio.load_json(Path(fixture), {}))
    return ok(check())


@streaming
def cmd_install(flags: dict) -> int:
    from . import installers
    return installers.install(flags)


def cmd_xip(flags: dict) -> int:
    from . import xip
    if flags.get("open"):
        return xip.open_download_page(_xcode_needed()["xcode"], flags)
    if flags.get("use"):
        return _xip_use(flags)
    if flags.get("watch"):
        return _xip_watch(flags)
    raise HelperError("usage", "usage: setup xip --watch | --use PATH | --open")


@streaming
def _xip_use(flags: dict) -> int:
    from . import xip
    p = Path(os.path.expanduser(str(flags["use"])))
    if not p.is_file():
        raise HelperError("not-found", "That file doesn't exist.")
    c = _xcode_needed()
    return xip.use(p, c["xcode_major"], c["xcode"])


@streaming
def _xip_watch(flags: dict) -> int:
    from . import xip
    c = _xcode_needed()
    expected = needed_xcode(c.get("swift_ver"))[2]
    return xip.watch(c["xcode_major"], c["xcode"], expected, float(flags.get("interval") or 1.5))


# ------------------------------------------------------------------ toolchain

def cmd_info(flags: dict) -> int:
    from . import toolchain
    return ok(toolchain.info())


def cmd_disk(flags: dict) -> int:
    if flags.get("cached"):
        c = appdirs.cache_get("toolchain-disk")
        if c:
            return ok(dict(c["data"], cachedAt=c["at"], stale=True))
    from . import toolchain
    return ok(toolchain.disk_usage())


@streaming
def cmd_clean(flags: dict) -> int:
    from . import toolchain
    return toolchain.clean(flags)


@streaming
def cmd_remove_all(flags: dict) -> int:
    from . import toolchain
    return toolchain.remove_all(flags)


GROUPS = {
    "setup": {"check": cmd_check, "install": cmd_install, "xip": cmd_xip},
    "toolchain": {"info": cmd_info, "disk": cmd_disk, "clean": cmd_clean, "remove-all": cmd_remove_all},
}
