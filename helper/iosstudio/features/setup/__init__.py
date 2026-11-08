"""Setup slice: groups `setup` and `toolchain` (see commands.py for the command list).

pins       versions, checksums and paths          runtime    dry-run, events, user/root commands, file ops
facts      read-only probes, gathered in parallel steps      each step's state, the summary, `setup check`
xip        Xcode .xip checks and the Downloads watch       downloads  HTTPS + SHA-256, AUR recipes
sources    omarchy-apple-dev and the xtool build          installers one function per step, `setup install`
toolchain  `toolchain info|disk|clean|remove-all`         commands   GROUPS and the thin handlers
"""
from iosstudio.features.setup.commands import GROUPS
from iosstudio.features.setup.facts import toolchain_root  # debugger.py finds lldb the same way

__all__ = ["GROUPS", "toolchain_root"]
