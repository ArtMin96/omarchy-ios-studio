"""Setup slice: groups `setup` and `toolchain` (see commands.py for the command list).

pins       versions, checksums and paths          runtime    dry-run, events, user/root commands, file ops
facts      read-only probes, gathered in parallel steps      each step's state, the summary, `setup check`
xip        Xcode .xip checks and the Downloads watch       downloads  HTTPS + SHA-256, AUR recipes
sources    omarchy-apple-dev and the xtool build          installers one function per step, `setup install`
toolchain  `toolchain info|disk|clean|remove-all`         commands   GROUPS and the thin handlers
channel    the update channel's pins (effective_pins) and an update's backups (backup -> restore/discard)

GROUPS and toolchain_root are loaded on first use (PEP 562), so a module that needs only channel or facts (the
`updates` group, the debugger) doesn't pay for commands.py, steps.py and xip.py.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .commands import GROUPS
    from .facts import toolchain_root

__all__ = ["GROUPS", "toolchain_root"]


def __getattr__(name: str):
    if name == "GROUPS":
        from .commands import GROUPS
        return GROUPS
    if name == "toolchain_root":   # debugger.py finds lldb the same way
        from .facts import toolchain_root
        return toolchain_root
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
