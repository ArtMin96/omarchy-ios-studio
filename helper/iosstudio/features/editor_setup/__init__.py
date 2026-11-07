"""Editor setup: Swift completion and errors in VS Code, Zed or Neovim for xtool projects.

SourceKit-LSP (the Swift language server) understands an xtool project through xtool's build server: the
file `.bsp/xtool.json` in the project tells it to start `xtool dev build-server`, which compiles for the
iPhone (arm64-apple-ios) instead of for Linux. Two parts:

1. Machine-wide, once per editor (`editor setup`): the editor's Swift support. Neovim: LazyVim's
   `lang.swift` extra in lazyvim.json when the installed LazyVim has one (backed up first), else a new plugin
   file `lua/plugins/ios-studio-swift.lua`; other Neovim configs get a snippet (docs/features/editor-setup.md).
   Zed and VS Code: their Swift extension (a step you do; status says whether it's there), plus
   sourcekit-lsp's path in the editor's user settings only when it isn't on PATH. Also remembers the editor
   in iOS Studio's settings.json (`editorSetup`).
2. Per project, automatic (auto_project, called after a successful build and on create / add / convert):
   writes `.bsp/xtool.json` when it's missing (the same file xtool writes on its first build) and, when the
   preferred editor is VS Code and iOS Studio created the project from a template, merges
   `.vscode/settings.json` and `.vscode/extensions.json` (added or converted projects get them only through
   `--fix`). Zed and Neovim need nothing in the project.

  editor status
      -> {swift, xtool, sourcekit_lsp, preferred, editors: [{id, name, installed, binary, state, extension,
          configured, files, note, notes}]}
  editor setup --editor vscode|zed|neovim [--confirm] [--prefer no]
      -> {editor, name, dryRun, actions: [{kind, path, scope, detail, keys?, kept?, preview?, done?}], applied,
          state, next, notes}
  editor status --project P [--fix [--editor E] [--confirm]]
      -> {project, bsp: {present, stale, reason, path, kind, built, note}, ready, hint, preferred, created,
          auto: {at, wrote: [{kind, path}]} | null, pending: [actions], fix?: {dryRun, actions, applied, next}}

state: ready, needs-setup (setup can do the rest), partial (a step only you can do remains, see notes:
install the Swift extension, a file it couldn't read, a non-LazyVim Neovim config), not-installed.

Action kinds: write (a new file), merge (missing keys added to an existing file; every value already
there is kept, and differing ones are listed in `kept`), skip (nothing to do, or a file it can't change
safely), backup (a copy of a user-level file before it's changed). scope: "project" or "user" (a file in
your home folder: editor config, iOS Studio's settings).

Files are never overwritten. JSON with comments and trailing commas (JSONC, what VS Code and Zed use) is
edited as text: new keys and list items are inserted, everything else stays byte for byte.

Demo mode (IOSSTUDIO_DEMO): made-up status; setup and --fix list their plan and write nothing.

Modules: env (where to look, the toolchain, the remembered editor), actions (the plan and applying it),
editors (machine-wide, per editor), project (.bsp and per-project files), commands (CLI), demo. The JSONC
text editing and file writing are shared with the agents feature: features/config_files.py.
"""
from __future__ import annotations

from .actions import apply_actions, json_action
from .commands import GROUPS
from .demo import demo, demo_brief
from .editors import (
    LAZY_EXTRA,
    NVIM_PLUGIN_DETAIL,
    NVIM_PLUGIN_REL,
    VSCODE_EXT,
    VSCODE_EXTENSIONS,
    VSCODE_PROJECT_EXTENSIONS,
    VSCODE_PROJECT_SETTINGS,
    VSCODE_SETTINGS,
    editor_info,
    machine_setup,
    machine_status,
    neovim_actions,
    vscode_machine_actions,
    vscode_project_actions,
    zed_machine_actions,
)
from .env import EDITORS, Env, ios_sdk, preferred
from .project import (
    BSP_REL,
    BSP_TEXT,
    LEGACY_REL,
    LEGACY_TEXT,
    auto_project,
    brief,
    bsp_action,
    bsp_status,
    project_fix,
    project_status,
)

__all__ = [
    "GROUPS", "demo", "demo_brief", "auto_project", "brief", "Env", "EDITORS", "ios_sdk", "preferred",
    "machine_status", "machine_setup", "editor_info", "neovim_actions", "vscode_machine_actions",
    "vscode_project_actions", "zed_machine_actions", "project_status", "project_fix", "bsp_status", "bsp_action",
    "json_action", "apply_actions",
    "BSP_REL", "BSP_TEXT", "LEGACY_REL", "LEGACY_TEXT", "LAZY_EXTRA", "NVIM_PLUGIN_DETAIL", "NVIM_PLUGIN_REL",
    "VSCODE_EXT", "VSCODE_EXTENSIONS", "VSCODE_PROJECT_EXTENSIONS", "VSCODE_PROJECT_SETTINGS", "VSCODE_SETTINGS",
]
