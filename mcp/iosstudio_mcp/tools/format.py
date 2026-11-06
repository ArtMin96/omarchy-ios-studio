"""Format and lint: ios_format shows (dry run) or applies swift-format's changes to a project's Swift files as a
bounded unified diff; ios_format_lint lists swift-format's (and SwiftLint's, when it runs) style findings.
The work happens in helper/format.py (`projects format` / `projects format-lint`)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, ClassVar

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from .. import budget as B
from .. import helper
from ..models import Out, iso
from ..state import get_state, resolve_project
from . import READ, ann, fit_page, tool
from .build import MaxBytes
from .projects import ProjectPath

FILES_CAP = 50              # files_changed entries in a result
ERRORS_CAP = 20
HELPER_MAX_DIAGS = 500      # asked of the helper; max_items applies on this side
MESSAGE_CUT = 300
TIMEOUT = 28                # the helper stops itself at 20 s

Files = Annotated[list[str] | None, Field(max_length=200, description="Only these files (relative to the project or "
                                                                       "absolute, inside it); default: every Swift "
                                                                       "file in Package.swift, Sources/, Tests/, "
                                                                       "Plugins/")]


class FileError(Out):
    file: str
    message: str


class FormatResult(B.Budgeted, Out):
    BULK: ClassVar[tuple[str, ...]] = ("diff",)
    project: str
    path: str
    applied: bool = Field(description="True when the files were written (apply=true)")
    files_checked: int = 0
    files_changed: list[str] = Field(default_factory=list, description="Files (relative) that change / changed")
    files_changed_total: int = 0
    diff: str = Field("", description="Unified diff, a/ b/ paths relative to the project; cut to the budget")
    diff_bytes: int = Field(0, description="Size of the whole diff")
    config_path: str | None = Field(None, description=".swift-format used ('.swift-format' in the project, an "
                                                      "absolute path above it), or null for swift-format's defaults")
    tool: str = "swift-format"
    tool_version: str | None = None
    errors: list[FileError] = Field(default_factory=list, description="Files swift-format couldn't format "
                                                                      "(syntax errors, outside the project, …)")
    errors_total: int = 0
    timed_out: bool = False
    elapsed_ms: int | None = None
    next_steps: list[str] = Field(default_factory=list)


class StyleDiagnostic(Out):
    severity: str = Field(description="warning or error (as the linter reports it; style only, never a build error)")
    category: str = "style"
    file: str | None = Field(None, description="Relative to the project")
    line: int | None = None
    col: int | None = None
    rule: str | None = Field(None, description="swift-format rule (Indentation, Spacing, …, 'syntax' when the file "
                                               "doesn't parse) or SwiftLint rule id")
    source: str = Field(description="swift-format or swiftlint")
    message: str
    hint: str | None = None


class LintResult(B.Budgeted, Out):
    project: str
    path: str
    clean: bool = Field(description="True when nothing was found")
    total: int
    count: int
    counts: dict[str, int] = Field(default_factory=dict, description="Findings per severity (whole project)")
    by_source: dict[str, int] = Field(default_factory=dict)
    by_rule: dict[str, int] = Field(default_factory=dict, description="Most frequent rules (top 20)")
    diagnostics: list[StyleDiagnostic] = Field(default_factory=list)
    files_checked: int = 0
    config_path: str | None = None
    tools: list[str] = Field(default_factory=list, description="Linters that ran")
    swiftlint: bool = Field(False, description="SwiftLint is installed and ran")
    cached: bool = Field(False, description="The stored result of the last whole-project lint (cached=true)")
    linted_at: str | None = None
    timed_out: bool = False
    elapsed_ms: int | None = None
    next_steps: list[str] = Field(default_factory=list)


def _rel(f: str | None, root: str) -> str | None:
    if not f:
        return None
    try:
        return str(Path(f).relative_to(root))
    except ValueError:
        return helper.tilde(f)


def _files_flag(files: list[str] | None) -> str | None:
    if not files:
        return None
    clean = [str(f) for f in files if str(f).strip()]
    for f in clean:
        if "\x00" in f:
            raise ValueError("file names can't contain NUL")
    return json.dumps(clean) if clean else None


# The helper's hints speak in app terms (buttons, pages); agents get the tool to call instead.
HINT_FIXABLE = "ios_format(apply=true) fixes this (dry run first)."
HINT_PARSE = ("swift-format can't read this file until its syntax is fixed: ios_build_start(action='compile'), then "
              "ios_build_diagnostics shows the compiler's error.")


def _agent_hint(d: dict) -> str | None:
    hint = d.get("hint")
    if not hint:
        return None
    if d.get("rule") == "syntax":
        return HINT_PARSE
    if str(hint).startswith("Format project"):
        return HINT_FIXABLE
    return B.clip(str(hint), MESSAGE_CUT)


def _lint_note(k: int, total: int) -> str:
    return (f"Showing {k} of {total} findings (see by_rule for the rest); call again with a larger max_items, "
            "files=[...] for fewer files, or a larger max_bytes. Most whitespace findings go away with "
            "ios_format(apply=true).")


def register(mcp: MCPServer) -> None:

    @tool(mcp, annotations=ann("Format Swift code", read_only=False, destructive=False, idempotent=True))
    async def ios_format(
        ctx: Context,
        project_path: ProjectPath,
        files: Files = None,
        apply: Annotated[bool, Field(description="False (default): dry run, nothing is written. True: write the "
                                                 "formatted files (only those that change)")] = False,
        max_bytes: MaxBytes = None,
    ) -> FormatResult:
        """Format a project's Swift code with swift-format (comes with the Swift toolchain), honoring the
        project's .swift-format. Call it first without apply: it returns which files would change and a
        unified diff (cut to the budget; the whole diff is then saved to full_output_path). Show or summarize
        the diff to the user, then call again with apply=true to write the files. Apply writes atomically,
        only files whose content changes, never files outside the project; .build/ and hidden folders are
        skipped. Files that don't parse are listed in `errors` and left alone. Needs no iPhone or Apple ID."""
        state = get_state(ctx)
        p = await resolve_project(state, project_path)
        root = str(p.get("path") or "")
        try:
            flag_files = _files_flag(files)
        except ValueError as exc:
            raise ToolError(str(exc)) from None
        diff_room = int(B.room(max_bytes) * 0.9)
        data = await helper.call("projects", "format", {"project": root, "files": flag_files, "apply": apply,
                                                        "max_bytes": diff_room}, timeout=TIMEOUT)
        changed = [str(x) for x in (data.get("files_changed") or [])]
        total_changed = int(data.get("files_changed_total") or len(changed))
        errs = [FileError(file=str(e.get("file") or ""), message=B.clip(str(e.get("message") or ""), MESSAGE_CUT))
                for e in (data.get("errors") or [])[:ERRORS_CAP] if isinstance(e, dict)]
        errors_total = int(data.get("errors_total") or len(errs))
        notes = [str(data["note"])] if data.get("note") else []
        if total_changed > FILES_CAP:
            notes.append(f"files_changed lists {FILES_CAP} of {total_changed} files.")
        if errors_total > len(errs):
            notes.append(f"errors lists {len(errs)} of {errors_total}.")
        nxt: list[str] = []
        if apply:
            if total_changed:
                nxt.append("Files were written. Build with ios_build_start(action='compile') to confirm it still "
                           "compiles; ios_format_lint shows what formatting can't fix.")
        elif total_changed:
            nxt.append(f"Show the user the diff (or summarize it), then call ios_format(apply=true"
                       f"{', files=[...]' if files else ''}) to write {total_changed} file"
                       f"{'s' if total_changed != 1 else ''}.")
        else:
            nxt.append("Nothing to change. ios_format_lint lists style findings formatting doesn't fix.")
        if errs:
            nxt.append("Files in errors weren't formatted; most don't parse. ios_build_diagnostics after a compile "
                       "shows the syntax errors.")
        model = FormatResult(
            project=str(p.get("name") or Path(root).name), path=root, applied=bool(apply),
            files_checked=int(data.get("files_checked") or 0), files_changed=changed[:FILES_CAP],
            files_changed_total=total_changed, diff=str(data.get("diff") or ""),
            diff_bytes=int(data.get("diff_bytes") or 0), config_path=data.get("config_path"),
            tool_version=data.get("tool_version"), errors=errs, errors_total=errors_total,
            timed_out=bool(data.get("timed_out")), elapsed_ms=data.get("elapsed_ms"), next_steps=nxt,
            truncated=bool(data.get("truncated")) or total_changed > FILES_CAP,
            full_output_path=data.get("full_output_path"), note=" ".join(notes) or None)
        return model.with_budget(max_bytes)

    @tool(mcp, annotations=READ("Lint Swift code"))
    async def ios_format_lint(
        ctx: Context,
        project_path: ProjectPath,
        files: Files = None,
        max_items: Annotated[int, Field(ge=1, le=200, description="Max findings to return")] = 50,
        cached: Annotated[bool, Field(description="Return the stored result of the last whole-project lint "
                                                  "instead of running the linters")] = False,
        max_bytes: MaxBytes = None,
    ) -> LintResult:
        """Lint a project's Swift code: swift-format's lint (honoring .swift-format) and, when it's installed,
        SwiftLint (honoring .swiftlint.yml). Each finding has severity, file:line:col (relative to the project),
        rule, source and message; whitespace findings (Indentation, Spacing, …) have a hint because
        ios_format(apply=true) fixes them. Style findings never fail a build. Fast (seconds), read-only, needs
        no iPhone or Apple ID. The app shows the same findings in the project's Problems list."""
        state = get_state(ctx)
        p = await resolve_project(state, project_path)
        root = str(p.get("path") or "")
        try:
            flag_files = None if cached else _files_flag(files)
        except ValueError as exc:
            raise ToolError(str(exc)) from None
        data = await helper.call("projects", "format-lint", {"project": root, "files": flag_files,
                                                             "max": HELPER_MAX_DIAGS, "last": cached},
                                 timeout=TIMEOUT)
        raw = [d for d in (data.get("diagnostics") or []) if isinstance(d, dict)]
        total = int(data.get("total") or len(raw))
        diags = [StyleDiagnostic(
            severity=str(d.get("severity") or "warning"), file=_rel(d.get("file"), root),
            line=d.get("line") if isinstance(d.get("line"), int) else None,
            col=d.get("col") if isinstance(d.get("col"), int) else None, rule=d.get("rule"),
            source=str(d.get("source") or "swift-format"),
            message=B.clip(" ".join(str(d.get("message") or "").split()), MESSAGE_CUT),
            hint=_agent_hint(d)) for d in raw[:max_items]]
        notes = []
        if data.get("note") and "Showing" not in str(data.get("note")):
            notes.append(str(data["note"]))
        cut = total - len(diags)
        if cut > 0:
            notes.append(_lint_note(len(diags), total))
        by_rule = {str(k): int(v) for k, v in (data.get("by_rule") or {}).items()}
        nxt: list[str] = []
        if cached and not data.get("cached"):
            nxt.append("No stored result: call ios_format_lint without cached to lint now.")
        elif not total:
            nxt.append("No style findings.")
        else:
            fixable = sum(1 for d in raw if d.get("hint") and d.get("source") == "swift-format"
                          and d.get("rule") != "syntax")
            if fixable or any(r in by_rule for r in ("Indentation", "Spacing")):
                nxt.append("Whitespace findings (Indentation, Spacing, …) are fixed by ios_format: dry run first, "
                           "then apply=true after the user agrees.")
            nxt.append("Fix the others in the files named, then call ios_format_lint again.")
        at = data.get("at")
        model = LintResult(
            project=str(p.get("name") or Path(root).name), path=root, clean=not total, total=total,
            count=len(diags), counts={k: int(v) for k, v in (data.get("counts") or {}).items() if v},
            by_source={str(k): int(v) for k, v in (data.get("by_source") or {}).items()}, by_rule=by_rule,
            diagnostics=diags, files_checked=int(data.get("files_checked") or 0),
            config_path=data.get("config_path"), tools=[str(t) for t in data.get("tools") or []][:5],
            swiftlint=bool(data.get("swiftlint")), cached=bool(data.get("cached")),
            linted_at=iso(at), timed_out=bool(data.get("timed_out")), elapsed_ms=data.get("elapsed_ms"),
            next_steps=nxt, truncated=cut > 0, omitted=max(cut, 0), note=" ".join(notes) or None,
        ).with_budget(max_bytes)
        return fit_page(model, "diagnostics", 0, lambda k, end: _lint_note(k, total))

