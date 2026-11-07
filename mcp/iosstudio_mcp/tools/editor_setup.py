"""Editor setup: ios_editor_setup sets up VS Code, Zed or Neovim for Swift completion and errors, once per
machine (the editor's Swift support; Neovim's config), and optionally writes one project's missing files
(.bsp/xtool.json, and .vscode/ when VS Code is the preferred editor). Projects otherwise get those files by
themselves after a successful build and on create/convert; ios_project_info's `editor` field shows a
project's state. See helper/editor_setup.py.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from .. import helper
from ..backends.projects import resolve_project
from ..core import budget as B
from ..core.params import MaxBytes
from ..core.registry import tool
from ..models import Out
from ..state import get_state

EDITORS = ("vscode", "zed", "neovim")
DETAIL_CUT = 400
PREVIEW_CUT = 1500
NOTE_CUT = 300

EditorId = Literal["auto", "vscode", "zed", "neovim"]


class EditorAction(Out):
    kind: str = Field(description="write (new file) | merge (keys added to an existing file; nothing in it "
                                  "changes) | skip (nothing to do, or a file it can't change safely) | backup")
    path: str
    scope: str = Field(description="project (inside the project) or user (your home folder: editor config, "
                                   "iOS Studio's settings)")
    detail: str
    keys: list[str] = Field(default_factory=list, description="Keys or list items it adds")
    kept: list[dict[str, Any]] = Field(default_factory=list, description="Keys already set to another value: "
                                                                         "the user's value is kept")
    preview: str | None = Field(None, description="The new file's text (write only)")
    done: bool = False

    @classmethod
    def from_helper(cls, a: dict) -> "EditorAction":
        return cls(kind=str(a.get("kind") or "skip"), path=helper.tilde(str(a.get("path") or "")) or "",
                   scope=str(a.get("scope") or "project"), detail=B.clip(str(a.get("detail") or ""), DETAIL_CUT),
                   keys=[str(k)[:120] for k in (a.get("keys") or [])][:20],
                   kept=[{"key": str(k.get("key"))[:120], "yours": _short(k.get("yours")), "ours": _short(k.get("ours"))}
                         for k in (a.get("kept") or []) if isinstance(k, dict)][:20],
                   preview=B.clip(a["preview"], PREVIEW_CUT) if a.get("preview") else None,
                   done=bool(a.get("done")))


def _short(v: Any) -> Any:
    s = repr(v)
    return v if len(s) <= 200 else s[:200] + "…"


class EditorResult(Out):
    editor: str
    name: str
    installed: bool | None = None
    state: str | None = Field(None, description="ready | needs-setup (confirm=true does the rest) | partial (a "
                                                "step only the user can do remains: see notes) | not-installed")
    actions: list[EditorAction] = Field(default_factory=list)
    applied: bool = False
    next: str | None = None
    notes: list[str] = Field(default_factory=list, description="Steps only the user can do, e.g. install the "
                                                               "editor's Swift extension")


class ProjectFix(Out):
    project: str
    path: str
    ready: bool = Field(description="The project has what an editor needs (.bsp/xtool.json, and .vscode/ when "
                                    "VS Code is the preferred editor)")
    hint: str | None = None
    actions: list[EditorAction] = Field(default_factory=list)
    applied: bool = False


class EditorSetupResult(B.Budgeted, Out):
    dry_run: bool
    message: str
    preferred: str | None = Field(None, description="The editor remembered in iOS Studio's settings")
    sourcekit_lsp: str | None = None
    editors: list[EditorResult] = Field(default_factory=list)
    project: ProjectFix | None = None
    next_steps: list[str] = Field(default_factory=list)


def register(mcp: MCPServer) -> None:

    @tool(mcp, "Set up editor", kind="write", idempotent=True)
    async def ios_editor_setup(
        ctx: Context,
        editor: Annotated[EditorId, Field(description="vscode, zed, neovim, or auto: every installed one")] = "auto",
        project_path: Annotated[str | None, Field(description="Also write this project's missing files now "
                                                              "(.bsp/xtool.json; .vscode/ for VS Code) instead of "
                                                              "waiting for its next build")] = None,
        confirm: Annotated[bool, Field(description="false = dry run listing every change. true only after the "
                                                   "user agreed")] = False,
        max_bytes: MaxBytes = None,
    ) -> EditorSetupResult:
        """Set up an editor for Swift completion and errors (SourceKit-LSP through xtool's build server), once
        per machine. A dry run unless confirm=true: it lists each file it would write or merge.

        - neovim: changes the user's Neovim config (LazyVim's lang.swift extra in lazyvim.json, backed up
          first, or a new lua/plugins/ios-studio-swift.lua). Non-LazyVim configs are left alone (notes say why).
        - zed, vscode: their Swift extension is a step the user does (notes); nothing in the project is needed
          for Zed. sourcekit-lsp's path goes into the editor's user settings only when it isn't on PATH.
        - The editor is remembered in iOS Studio's settings. With VS Code, projects get .vscode/settings.json
          and extensions.json (merged, never overwritten) on their next build.
        Projects get .bsp/xtool.json by themselves after a successful build and on create/convert; pass
        project_path to write a project's missing files now. ios_project_info's `editor` shows a project's
        state. Existing files are never overwritten; values the user set differently are kept and listed."""
        state = get_state(ctx)
        machine = await helper.call("editor", "status")
        installed = {e.get("id"): e for e in (machine.get("editors") or []) if isinstance(e, dict)}
        if editor == "auto":
            wanted = [e for e in EDITORS if (installed.get(e) or {}).get("installed")]
            if not wanted:
                raise ToolError("None of VS Code, Zed or Neovim is installed. Install one, or pass editor= "
                                "explicitly (VS Code's project files can be written without it).")
        else:
            wanted = [editor]
        # One editor is remembered: the one asked for, or with auto the remembered one (else the first set up)
        pref = editor if editor != "auto" else (machine.get("preferred") if machine.get("preferred") in wanted
                                                else wanted[0])
        results: list[EditorResult] = []
        for ed in wanted:
            flags: dict = {"editor": ed, "confirm": True if confirm else None, "prefer": None if ed == pref else "no"}
            d = await helper.call("editor", "setup", flags)
            results.append(EditorResult(
                editor=ed, name=str(d.get("name") or ed), installed=(installed.get(ed) or {}).get("installed"),
                state=d.get("state"), actions=[EditorAction.from_helper(a) for a in (d.get("actions") or [])
                                               if isinstance(a, dict)][:20],
                applied=bool(d.get("applied")), next=B.clip(str(d.get("next") or ""), NOTE_CUT) or None,
                notes=[B.clip(str(n), NOTE_CUT) for n in (d.get("notes") or [])][:6]))
        fix = None
        if project_path:
            p = await resolve_project(state, project_path)
            d = await helper.call("editor", "status", {"project": p["path"], "fix": True,
                                                       "confirm": True if confirm else None})
            f = d.get("fix") or {}
            fix = ProjectFix(project=str(p.get("name") or p["path"]), path=str(p["path"]), ready=bool(d.get("ready")),
                             hint=d.get("hint"), applied=bool(f.get("applied")),
                             actions=[EditorAction.from_helper(a) for a in (f.get("actions") or [])
                                      if isinstance(a, dict)][:10])
        changes = [a for r in results for a in r.actions if a.kind in ("write", "merge")]
        changes += [a for a in (fix.actions if fix else []) if a.kind in ("write", "merge")]
        nxt: list[str] = []
        if not confirm and changes:
            msg = (f"Dry run: {len(changes)} file change(s) listed in actions; nothing was changed. Ask the user "
                   "to confirm, then call again with confirm=true.")
            if any(a.scope == "user" and not a.path.endswith("ios-studio/settings.json") for a in changes):
                nxt.append("Some changes are in the user's own editor config (scope='user'), not in a project: "
                           "say so when asking.")
            nxt.append("Ask the user whether to proceed; only then repeat the call with confirm=true.")
        elif confirm:
            msg = "Done: " + (", ".join(f"{r.name} {r.state}" for r in results) or "project files written") + "."
        else:
            msg = "Nothing to change: " + ", ".join(f"{r.name} {r.state}" for r in results) + "."
        for r in results:
            for n in r.notes:
                if n not in nxt:
                    nxt.append(f"{r.name}: {n} (a step for the user)")
            if confirm and r.next and not r.notes:
                nxt.append(r.next)
        if fix and fix.hint:
            nxt.append(f"{fix.project}: {fix.hint}" + (" (ios_build_start(action='compile') does it)"
                                                       if "Build once" in fix.hint else ""))
        if not confirm:
            pref = machine.get("preferred")
        return EditorSetupResult(dry_run=not confirm, message=msg, preferred=pref,
                                 sourcekit_lsp=helper.tilde(machine.get("sourcekit_lsp")), editors=results,
                                 project=fix, next_steps=nxt[:8]).with_budget(max_bytes)
