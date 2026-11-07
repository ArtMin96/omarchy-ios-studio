"""The `editor` command group: status (machine-wide or one project, with --fix) and setup."""
from __future__ import annotations

import os
from pathlib import Path

from iosstudio.core import output
from iosstudio.core.output import HelperError

from .editors import machine_setup, machine_status
from .env import EDITOR_USAGE, EDITORS
from .project import project_fix, project_status


def project_flag(flags: dict) -> Path:
    """--project (or --path): an existing Swift package folder."""
    raw = flags.get("project") or flags.get("path")
    if not raw or raw is True:
        raise HelperError("usage", "Missing --project.")
    p = Path(os.path.expanduser(str(raw))).resolve()
    if not p.is_dir():
        raise HelperError("not-found", "That project folder doesn't exist anymore.")
    if not (p / "Package.swift").is_file():
        raise HelperError("not-a-project", "That folder isn't a Swift package (it has no Package.swift).")
    return p


def prefer_flag(flags: dict) -> bool:
    """--prefer no leaves the remembered editor as it is."""
    return str(flags.get("prefer", "yes")).lower() not in ("no", "0", "false")


def cmd_status(flags: dict) -> int:
    """editor status                         -> machine_status
    editor status --project P               -> project_status
    editor status --project P --fix [--confirm] [--editor E] -> project_status + fix"""
    if not flags.get("project") and not flags.get("path"):
        return output.ok(machine_status())
    p = project_flag(flags)
    fix = project_fix(p, bool(flags.get("confirm")), editor=flags.get("editor") or None) if flags.get("fix") else None
    out = project_status(p)
    if fix is not None:
        out["fix"] = fix
    return output.ok(out)


def cmd_setup(flags: dict) -> int:
    editor = str(flags.get("editor") or "")
    if editor not in EDITORS:
        raise HelperError("usage", EDITOR_USAGE)
    return output.ok(machine_setup(editor, bool(flags.get("confirm")), prefer=prefer_flag(flags)))


GROUPS = {"editor": {"status": cmd_status, "setup": cmd_setup}}
