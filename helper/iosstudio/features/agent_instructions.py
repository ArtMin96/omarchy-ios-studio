"""Instructions for coding agents in a project: AGENTS.md (the text) and CLAUDE.md (imports it), from
templates/agent-instructions/. Written into new and converted projects when missing; never overwritten."""
from __future__ import annotations

import os
from pathlib import Path

from iosstudio.core import appdirs

TEMPLATE_DIR = appdirs.REPO / "templates" / "agent-instructions"
FILES = ("AGENTS.md", "CLAUDE.md")
SETTING = "agentInstructions"
ON = {"on", "true", "1", "yes"}
OFF = {"off", "false", "0", "no"}


def wanted(flags: dict) -> bool:
    """--agent-files on|off wins; else the agentInstructions setting (on when unset)."""
    raw = flags.get("agent_files")
    if raw is True:                       # a bare --agent-files
        return True
    if isinstance(raw, str) and raw.strip().lower() in ON | OFF:
        return raw.strip().lower() in ON
    setting = appdirs.settings().get(SETTING)
    return setting is not False and str(setting).lower() not in OFF


def write(project: Path, name: str, bundle_id: str) -> list[str]:
    """Write each missing file into `project` with {{NAME}}/{{BUNDLE_ID}} filled in. Anything already at the
    path (a file, a symlink, even a broken one, a folder) is left alone. Returns the names written; an
    OSError skips that file and never fails the caller."""
    written = []
    for fname in FILES:
        dest = project / fname
        try:
            if os.path.lexists(dest):
                continue
            text = (TEMPLATE_DIR / fname).read_text(encoding="utf-8")
            text = text.replace("{{NAME}}", name).replace("{{BUNDLE_ID}}", bundle_id)
            with open(dest, "x", encoding="utf-8") as f:   # "x": fails rather than replace a file made meanwhile
                f.write(text)
            written.append(fname)
        except OSError:
            continue
    return written
