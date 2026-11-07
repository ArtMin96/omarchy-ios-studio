"""Project tools: list, inspect, create, register, convert, configure and remove iOS Studio projects.

An iOS Studio project is a SwiftPM package with an xtool.yml next to Package.swift (that's what xtool
builds into an iPhone app). Sources live in Sources/<Product>/.
"""
from __future__ import annotations

import re
from typing import Annotated

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from .. import helper
from ..backends.projects import existing_dir, resolve_project
from ..core.job_status import still_running, wait_with_progress
from ..core.jobs import Job
from ..core.params import OptJobId, ProjectPath, WaitSeconds
from ..core.registry import dry_run, fit_page, page_of, tool
from ..models import (
    ActionResult,
    Convertible,
    ProjectBrief,
    ProjectChange,
    ProjectDetail,
    ProjectList,
    Template,
    TemplateList,
)
from ..state import AppState, get_state

NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,48}$")
BUNDLE_RE = re.compile(r"^[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")


def _is_demo_stub(data) -> bool:
    return isinstance(data, dict) and data.get("demo") is True


def _detail_next(p: dict) -> list[str]:
    if p.get("generated") and not (p.get("buildSettings") or {}).get("allowGenerated"):
        out = ["This project generates its own xtool.yml, so iOS Studio won't build, run or renew it until its "
               "build settings allow it: ios_project_build_settings(project_path, prebuild=<the script that "
               "regenerates xtool.yml>, env=..., allow_generated=true) — a dry run first, then confirm=true after "
               "the user agrees. Otherwise use the project's own build script. The iPhone tools "
               "(ios_app_run_and_capture, ios_logs_start, ios_crash_list, ios_metrics_start, …) still work on the "
               "installed app by bundle id."]
        if p.get("suggestedBuildSettings"):
            out.insert(0, "Its own scripts suggest build settings (suggested_build_settings): "
                          "ios_project_build_settings(project_path, apply_suggested=true) shows them as a dry run.")
        return out
    out = [f"Edit the Swift sources in {p.get('path')}/Sources/{p.get('product') or p.get('name')}/.",
           "Build with ios_build_start(project_path, action='compile'); run on the iPhone with ios_run(project_path)."]
    lb = p.get("lastBuild") or {}
    if lb.get("result") == "failed":
        out.insert(0, f"The last build failed: ios_build_diagnostics(project_path='{p.get('path')}').")
    return out


DEMO_NEXT = ["Demo mode keeps its three made-up projects. Continue with one of them, e.g. "
             "ios_build_start(project_path='HabitTracker', action='compile') (it has 2 compiler errors) or "
             "ios_run(project_path='WeatherGlance')."]


async def _creation(state: AppState, ctx: Context, job: Job, wait_seconds: float) -> ProjectChange:
    """Wait for a project-create job; its ProjectChange once done (raising the helper's error if it failed)."""
    name = job.data.get("name")
    await wait_with_progress(state, job, wait_seconds, ctx)
    if not job.finished:
        return ProjectChange(done=False, job_id=job.id, message=f"Still creating {name} ({job.elapsed:g} s so far).",
                             next_steps=[still_running(job)])
    job.data["delivered"] = True
    if job.state != "succeeded":
        raise ToolError((job.error or {}).get("message") or f"Creating {name} was {job.state}.")
    data = job.result or {}
    state.invalidate_projects()
    if _is_demo_stub(data):
        return ProjectChange(done=False, message=f"Demo mode: {name} was not really created.",
                             next_steps=DEMO_NEXT)
    return ProjectChange(done=True, message=f"Created {data.get('name')} at {data.get('path')}.",
                         project=ProjectDetail.from_helper(data), next_steps=_detail_next(data))


def register(mcp: MCPServer) -> None:

    @tool(mcp, "List projects", kind="read")
    async def ios_project_list(
        ctx: Context,
        query: Annotated[str | None, Field(description="Case-insensitive filter on name, path or bundle id")] = None,
        offset: Annotated[int, Field(ge=0, description="Skip this many")] = 0,
        limit: Annotated[int, Field(ge=1, le=100, description="Max projects")] = 20,
    ) -> ProjectList:
        """The user's iPhone app projects, most recently built first: path (pass as project_path), bundle id,
        minimum iOS, branch, last build and signing expiry. Also SwiftUI packages that could become apps
        (`convertible`, see ios_project_convert)."""
        state = get_state(ctx)
        state.invalidate_projects()
        data = await state.projects()
        items = data.get("projects") or []
        if query:
            q = query.lower()
            items = [p for p in items if q in str(p.get("name", "")).lower() or q in str(p.get("path", "")).lower()
                     or q in str(p.get("bundleId") or "").lower()]
        page, nxt_off = page_of(items, offset, limit, ProjectBrief.from_helper)
        nxt = []
        if not items:
            nxt.append("No projects found. Create one with ios_project_create (see ios_project_templates), or "
                       "register an existing folder with ios_project_add.")
        note = f"Showing {len(page)} of {len(items)}; call again with offset={nxt_off}, or narrow with query." \
            if nxt_off is not None else None
        return fit_page(ProjectList(
            total=len(items), count=len(page), offset=offset, has_more=nxt_off is not None, next_offset=nxt_off,
            projects=page, note=note, truncated=nxt_off is not None, omitted=len(items) - len(page),
            convertible=[Convertible(name=c.get("name", ""), path=c.get("path", ""), reason=c.get("reason"))
                         for c in (data.get("convertible") or [])[:20]],
            missing=(data.get("missing") or [])[:20], roots=(data.get("roots") or [])[:20], default_dir=data.get("defaultDir"),
            next_steps=nxt), "projects", offset,
            lambda k, end: f"Showing {k} of {len(items)}; call again with offset={end}, or narrow with query.")

    @tool(mcp, "Project details", kind="read")
    async def ios_project_info(project_path: ProjectPath, ctx: Context) -> ProjectDetail:
        """One project: sources folder, bundle id, minimum iOS, last build, signing expiry, auto-renew, last
        .ipa, build settings and the ones its scripts suggest (ios_project_build_settings), run presets, and
        `editor`: whether editors get completion (see ios_editor_setup)."""
        state = get_state(ctx)
        p = await resolve_project(state, project_path)
        p = await helper.call("projects", "info", {"path": p["path"]})
        return ProjectDetail.from_helper(p, _detail_next(p))

    @tool(mcp, "App templates", kind="read")
    async def ios_project_templates() -> TemplateList:
        """Templates ios_project_create accepts, the default bundle-id prefix and folder for new apps."""
        data = await helper.call("projects", "templates")
        return TemplateList(
            templates=[Template(id=t.get("id", ""), title=t.get("title"), description=t.get("description"),
                                default=bool(t.get("default")), files=t.get("files") or [])
                       for t in data.get("templates") or []],
            bundle_prefix=data.get("bundlePrefix") or "com.example", default_dir=data.get("defaultDir") or "")

    @tool(mcp, "Create app", kind="write")
    async def ios_project_create(
        ctx: Context,
        name: Annotated[str | None, Field(description="Letters, digits, _, e.g. 'WeatherGlance' (not with "
                                                      "job_id)")] = None,
        template: Annotated[str, Field(description="Id from ios_project_templates")] = "swiftui-app",
        directory: Annotated[str | None, Field(description="Parent folder (default: ~/Developer)")] = None,
        bundle_id: Annotated[str | None, Field(description="Default: <prefix>.<name>")] = None,
        git: Annotated[bool, Field(description="git init with a first commit")] = True,
        wait_seconds: WaitSeconds = 20,
        job_id: OptJobId = None,
    ) -> ProjectChange:
        """Create a SwiftUI iPhone app from a template (<directory>/<name>/ with Package.swift, xtool.yml,
        Sources/<name>/) and register it; fails if the folder exists. Next: edit sources, ios_build_start. If
        still running after wait_seconds: done=false + job_id, call again with job_id."""
        state = get_state(ctx)
        if job_id:
            return await _creation(state, ctx, state.jobs.get(job_id, "project-create"), wait_seconds)
        if not name or not NAME_RE.match(name):
            raise ToolError("App names start with a letter and use only letters, digits and _ (no spaces).")
        if bundle_id and not BUNDLE_RE.match(bundle_id):
            raise ToolError("bundle_id must look like com.yourname.AppName (letters, digits, dots and dashes).")
        try:
            ids = [str(t.get("id")) for t in (await helper.call("projects", "templates")).get("templates") or []
                   if t.get("id")]
        except Exception:  # noqa: BLE001 - the helper still validates it
            ids = []
        if ids and template not in ids:
            raise ToolError(f"Unknown template '{template}'. Valid ids: {', '.join(ids)} (see ios_project_templates).")
        flags = {"name": name, "template": template, "bundle_id": bundle_id, "git": None if git else "0"}
        if directory:
            d = helper.expand(directory)
            if not helper.inside_home(d):
                raise ToolError("directory must be inside your home folder (or a folder listed in IOSSTUDIO_MCP_ALLOW_PATHS).")
            flags["dir"] = str(d)
        key = f"{name}|{flags.get('dir') or ''}"
        job = next((j for j in reversed(list(state.jobs.jobs.values()))
                    if j.kind == "project-create" and j.data.get("key") == key and not j.data.get("delivered")), None)
        if job is None:
            async def create(j: Job) -> dict:
                return await helper.call("projects", "new", flags, timeout=90)
            job = state.jobs.run(create, "project-create", title=f"Create {name}", tool="ios_project_create",
                                 data={"key": key, "name": name})
        return await _creation(state, ctx, job, wait_seconds)

    @tool(mcp, "Add existing project", kind="write", idempotent=True)
    async def ios_project_add(
        ctx: Context,
        project_path: Annotated[str, Field(description="Folder with Package.swift and xtool.yml")],
    ) -> ProjectChange:
        """Register an existing xtool app folder (Package.swift + xtool.yml) so it shows in the project list
        (also un-hides a removed one). A package without xtool.yml needs ios_project_convert."""
        p = existing_dir(project_path, "project folder")
        data = await helper.call("projects", "add", {"path": str(p)})
        get_state(ctx).invalidate_projects()
        if _is_demo_stub(data):
            return ProjectChange(done=False, message="Demo mode: nothing was registered.",
                                 next_steps=DEMO_NEXT)
        return ProjectChange(done=True, message=f"Added {data.get('name')}.", project=ProjectDetail.from_helper(data),
                             next_steps=_detail_next(data))

    @tool(mcp, "Convert package to app", kind="write", idempotent=True)
    async def ios_project_convert(
        ctx: Context,
        project_path: Annotated[str, Field(description="Package with one library product and a SwiftUI @main App")],
        bundle_id: Annotated[str | None, Field(description="Default: <prefix>.<library>")] = None,
    ) -> ProjectChange:
        """Turn a SwiftUI Swift package into an app: writes only an xtool.yml (bundle id + product) and
        registers it. Candidates: ios_project_list `convertible`."""
        p = existing_dir(project_path, "package folder")
        if not (p / "Package.swift").is_file() and not helper.demo_mode():
            raise ToolError("That folder has no Package.swift. Use ios_project_create for a new app.")
        if bundle_id and not BUNDLE_RE.match(bundle_id):
            raise ToolError("bundle_id must look like com.yourname.AppName.")
        data = await helper.call("projects", "convert", {"path": str(p), "bundle_id": bundle_id})
        get_state(ctx).invalidate_projects()
        if _is_demo_stub(data):
            return ProjectChange(done=False, message="Demo mode: nothing was converted.",
                                 next_steps=DEMO_NEXT)
        return ProjectChange(done=True, message=f"{data.get('name')} is now an app ({data.get('bundleId')}).",
                             project=ProjectDetail.from_helper(data), next_steps=_detail_next(data))

    @tool(mcp, "Project settings", kind="write", idempotent=True)
    async def ios_project_set(
        ctx: Context,
        project_path: ProjectPath,
        auto_renew: Annotated[bool | None, Field(description="Include in the daily re-signing "
                                                             "(ios_project_autorenew)")] = None,
        udid: Annotated[str | None, Field(description="Default iPhone udid for this project")] = None,
    ) -> ProjectChange:
        """Set a project's auto-renew flag and default iPhone (only the given fields change). Bundle id and
        product live in its xtool.yml."""
        if auto_renew is None and udid is None:
            raise ToolError("Nothing to change: pass auto_renew and/or udid.")
        state = get_state(ctx)
        p = await resolve_project(state, project_path)
        flags = {"path": p["path"], "udid": udid}
        if auto_renew is not None:
            flags["auto_renew"] = "1" if auto_renew else "0"
        data = await helper.call("projects", "set", flags)
        state.invalidate_projects()
        if _is_demo_stub(data):
            return ProjectChange(done=False, message="Demo mode: settings were not saved.")
        msg = "Saved."
        nxt = []
        if auto_renew:
            nxt.append("Make sure the daily renewal timer is on: ios_project_autorenew(action='status').")
        return ProjectChange(done=True, message=msg, project=ProjectDetail.from_helper(data), next_steps=nxt)

    @tool(mcp, "Remove project from list", kind="destructive", idempotent=True)
    async def ios_project_remove(
        ctx: Context,
        project_path: ProjectPath,
        confirm: Annotated[bool, Field(description="false = dry run; true only after the user agreed")] = False,
    ) -> ActionResult:
        """Hide a project from iOS Studio's list and forget its settings; its files are NOT deleted. Dry run
        first; repeat with confirm=true only after the user agrees."""
        state = get_state(ctx)
        p = await resolve_project(state, project_path)
        would = [f"Hide {p.get('name')} ({p.get('path')}) from the project list",
                 "Forget its auto-renew flag, default iPhone and build settings",
                 "Delete its saved screenshot baselines and diffs (iOS Studio's data, not the project's)",
                 "Keep every file in the project folder"]
        if not confirm:
            return dry_run(f"This would remove {p.get('name')} from iOS Studio.", would)
        data = await helper.call("projects", "remove", {"path": p["path"]})
        state.invalidate_projects()
        return ActionResult(done=True, message=f"Removed {p.get('name')} from the list; files kept.", data=data,
                            next_steps=["ios_project_add(project_path) brings it back."])
