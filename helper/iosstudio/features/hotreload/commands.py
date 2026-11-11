"""`hotreload <action>` handlers: flags in, JSON out; the work is in the other modules of this package.

    hotreload enable  --project P [--dry-run] [--max-bytes N]
    hotreload disable --project P [--dry-run] [--max-bytes N]

Each handler is one function in ACTIONS; the session commands (run, status, stop) are added next to them.
"""
from __future__ import annotations

from iosstudio.core import output
from iosstudio.core.output import HelperError
from iosstudio.features.packages.commands import fail, max_bytes_flag

# ================================================================ project setup

def cmd_enable(flags: dict) -> int:
    from iosstudio.features.hotreload import project_setup
    from iosstudio.features.packages import project
    try:
        root = project.project_root(flags)
        return output.ok(project_setup.enable(root, bool(flags.get("dry_run")), max_bytes_flag(flags)))
    except HelperError as exc:
        return fail(exc)


def cmd_disable(flags: dict) -> int:
    from iosstudio.features.hotreload import project_setup
    from iosstudio.features.packages import project
    try:
        root = project.project_root(flags)
        return output.ok(project_setup.disable(root, bool(flags.get("dry_run")), max_bytes_flag(flags)))
    except HelperError as exc:
        return fail(exc)


ACTIONS = {
    "enable": cmd_enable,
    "disable": cmd_disable,
}

GROUPS = {"hotreload": ACTIONS}
