"""Project check: ios_project_check lists config mistakes in a project before a build (misspelled xtool.yml
keys, missing NS*UsageDescription keys, a bad icon, entitlements a free account can't use, bundle id and
version fields). Read-only and fast; see helper/iosstudio/features/project_check.py for the checks themselves."""
from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from mcp.server.mcpserver import Context, MCPServer
from pydantic import Field

from .. import helper
from ..backends.projects import resolve_project
from ..core import budget as B
from ..core.params import MaxBytes, ProjectPath
from ..core.registry import fit_page, tool
from ..models import Out
from ..state import get_state

CHECKS = ("xtool-keys", "usage-descriptions", "icon", "entitlements", "bundle-id", "version")
HELPER_MAX = 500        # issues asked of the helper; max_issues and the check filter apply on this side
MESSAGE_CUT = 400
HINT_CUT = 300

CheckId = Literal["xtool-keys", "usage-descriptions", "icon", "entitlements", "bundle-id", "version"]


class ProjectIssue(Out):
    severity: str = Field(description="warning or note (the check never reports errors and never blocks a build)")
    category: str = "config"
    check: str = Field(description="Which check found it: " + ", ".join(CHECKS))
    message: str
    file: str | None = Field(None, description="File relative to the project, e.g. 'xtool.yml'")
    line: int | None = None
    hint: str | None = Field(None, description="How to fix it")

    @classmethod
    def from_helper(cls, d: dict, root: str | None) -> "ProjectIssue":
        f = d.get("file")
        if f and root:
            try:
                f = str(Path(f).relative_to(root))
            except ValueError:
                f = helper.tilde(str(f))
        line = d.get("line")
        return cls(severity=str(d.get("severity") or "warning"), category=str(d.get("category") or "config"),
                   check=str(d.get("check") or ""), message=B.clip(" ".join(str(d.get("message") or "").split()),
                                                                   MESSAGE_CUT),
                   file=f or None, line=line if isinstance(line, int) else None,
                   hint=B.clip(" ".join(str(d["hint"]).split()), HINT_CUT) if d.get("hint") else None)


class ProjectCheckResult(B.Budgeted, Out):
    project: str
    path: str
    clean: bool = Field(description="True when no check found anything")
    total: int = Field(description="Issues found (after the check filter)")
    count: int = Field(description="Issues in this result")
    counts: dict[str, int] = Field(default_factory=dict, description="Issues per severity (whole project)")
    by_check: dict[str, int] = Field(default_factory=dict, description="Issues per check (whole project)")
    issues: list[ProjectIssue] = Field(default_factory=list)
    checks_run: list[str] = Field(default_factory=list)
    keys_source: str | None = Field(None, description="Where the valid xtool.yml keys came from: 'xtool-cache' "
                                                      "(the cached xtool source) or 'builtin' (a built-in list)")
    elapsed_ms: int | None = None
    next_steps: list[str] = Field(default_factory=list)


def _more_note(k: int, total: int, check: str | None, by_check: dict[str, int]) -> str:
    tips = [f"call again with max_issues={min(max(total, k + 1), 200)}"]
    if not check and len(by_check) > 1:
        tips.append("pass check=<id> for one check (" + ", ".join(f"{c}: {n}" for c, n in by_check.items()) + ")")
    tips.append("or raise max_bytes")
    return f"Showing {k} of {total} issues; " + ", ".join(tips) + "."


def register(mcp: MCPServer) -> None:

    @tool(mcp, "Check project config", kind="read")
    async def ios_project_check(
        ctx: Context,
        project_path: ProjectPath,
        check: Annotated[CheckId | None, Field(description="Only this check: xtool-keys (unknown or misspelled "
                                                           "xtool.yml keys), usage-descriptions (missing "
                                                           "NS*UsageDescription for camera, location, photos, …), "
                                                           "icon (PNG, 1024×1024, no alpha), entitlements (ones a "
                                                           "free account can't use), bundle-id, version")] = None,
        max_issues: Annotated[int, Field(ge=1, le=200, description="Max issues to return")] = 50,
        max_bytes: MaxBytes = None,
    ) -> ProjectCheckResult:
        """Check a project for config mistakes before building: unknown or misspelled xtool.yml keys (with the
        closest valid key), missing NS*UsageDescription keys for APIs the sources use (the app would crash
        the first time it uses the camera, location, photos, …), an icon that isn't a 1024×1024 PNG without
        alpha, entitlements a free Apple ID can't get, and malformed bundle id or version fields. Each issue
        has severity (warning or note), check, message, file:line when known and a fix hint.

        Fast (under a second), needs no iPhone or Apple ID, and never changes files. Results are warnings
        only; a build is never blocked by them. The same issues show up automatically as category='config'
        in ios_build_diagnostics for every build, so call this before a build to fix them early, or after
        editing xtool.yml or Info.plist."""
        state = get_state(ctx)
        p = await resolve_project(state, project_path)
        root = str(p.get("path") or "")
        name = str(p.get("name") or root)
        data = await helper.call("projects", "check", {"path": root, "max": HELPER_MAX})
        raw = [d for d in (data.get("issues") or []) if isinstance(d, dict)]
        by_check: dict[str, int] = {}
        for d in raw:
            c = str(d.get("check") or "other")
            by_check[c] = by_check.get(c, 0) + 1
        counts = {k: int(v) for k, v in (data.get("counts") or {}).items() if isinstance(v, int) and v}
        whole = int(data.get("total") or len(raw))
        if check:
            raw = [d for d in raw if d.get("check") == check]
        total = len(raw) if (check or not data.get("truncated")) else max(whole, len(raw))
        issues = [ProjectIssue.from_helper(d, root) for d in raw[:max_issues]]
        nxt: list[str] = []
        if not whole:
            nxt.append("No config problems found. Build with ios_build_start(action='compile') or ios_run.")
        elif check and not raw:
            nxt.append(f"The {check} check found nothing; other checks did (see by_check).")
        else:
            nxt.append("Fix the files named in each issue (hint says how), then call ios_project_check again. "
                       "These are warnings: a build still runs, but a missing usage description crashes the app "
                       "when it first uses that feature.")
            if by_check.get("xtool-keys"):
                nxt.append("xtool ignores unknown xtool.yml keys without a word, so a misspelled key silently has "
                           "no effect.")
        cut = total - len(issues)
        model = ProjectCheckResult(
            project=name, path=str(data.get("path") or root), clean=not whole, total=total, count=len(issues),
            counts=counts, by_check=by_check, issues=issues, checks_run=list(data.get("checks") or [])[:20],
            keys_source=data.get("keysSource"), elapsed_ms=data.get("elapsedMs"), next_steps=nxt,
            truncated=cut > 0, omitted=max(cut, 0),
            note=_more_note(len(issues), total, check, by_check) if cut > 0 else None).with_budget(max_bytes)
        return fit_page(model, "issues", 0, lambda k, end: _more_note(k, total, check, by_check))
