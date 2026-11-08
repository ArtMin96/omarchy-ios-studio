"""AI agents (group "agents"): connect iOS Studio's MCP server to AI coding tools.

  agents list                         which tools are installed and whether iosstudio is registered
  agents connect --client X [--demo]  register bin/iosstudio-mcp with that tool  (--dry-run: show the plan)
  agents disconnect --client X        remove it again                            (--dry-run: show the plan)
  agents test [--client X] [--demo]   start the server over stdio, initialize + tools/list, count the tools
  agents prepare [--repair]           (streaming) create or update the server's Python environment now

Tools with a reliable CLI of their own are driven through it (Claude Code, Gemini CLI). For the rest the
config file is edited: parsed first (JSON with comments, TOML, crushrc), changed in one place, checked by
parsing the result again and comparing it with the intended data, backed up to
~/.local/state/ios-studio/backups/, written atomically, and restored from the backup if anything fails.
The server is always called "iosstudio" (Gemini CLI doesn't allow underscores in server names).

Modules: clients (the tools and reading their registrations), writers (connect / disconnect), rpc (test),
server (the MCP server's environment, prepare), commands (CLI), demo. File edits go through
features/config_files.py, shared with editor setup.
"""
from __future__ import annotations

from .commands import GROUPS
from .demo import demo

__all__ = ["GROUPS", "demo"]
