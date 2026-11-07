"""Swift package dependencies: ios_package_list, ios_package_add, ios_package_update, ios_package_resolve and
ios_package_remove. The work happens in helper/iosstudio/features/packages.py (`packages <action>`); add, update and resolve fetch
from the network and run as jobs (state='running' + job_id; call the same tool again with job_id; cancel with
ios_job_cancel(job_id), which puts Package.swift and Package.resolved back)."""
from __future__ import annotations

import re
from typing import Annotated, Any, ClassVar, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from .. import helper
from ..backends.projects import resolve_project
from ..core import budget as B
from ..core.job_status import wait_with_progress
from ..core.jobs import Job
from ..core.params import MaxBytes, OptJobId, ProjectPath, WaitSeconds
from ..core.registry import fit_page, tool
from ..models import ErrorInfo, Out
from ..state import get_state

PRODUCTS_CAP = 30
CHANGES_CAP = 50
DIAGS_CAP = 10
TARGET_DEPS_CAP = 30
MESSAGE_CUT = 300
LIST_TIMEOUT = 150         # the first dump-package of a project compiles its manifest
REMOVE_TIMEOUT = 150
DRY_RUN_TIMEOUT = 90
CHECK_BUILD = "ios_build_start(project_path, action='compile') checks that the app still builds."

OptPath = Annotated[str | None, Field(description="Project folder or name (not with job_id)")]


# ------------------------------------------------------------------ models

class Requirement(Out):
    rule: str = Field(description="from (up to next major), upToNextMinor, exact, range, branch, revision, local")
    text: str = Field(description="e.g. 'from 1.2.0'")
    version: str | None = None
    lower: str | None = None
    upper: str | None = None
    branch: str | None = None
    revision: str | None = None


class Resolved(Out):
    version: str | None = None
    branch: str | None = None
    revision: str | None = Field(None, description="Short commit")


class PackageInfo(Out):
    identity: str
    name: str
    kind: str = Field(description="remote, local or registry")
    url: str | None = None
    path: str | None = None
    requirement: Requirement
    resolved: Resolved | None = Field(None, description="From Package.resolved; null: not resolved yet (or local)")
    used_by: list[str] = Field(default_factory=list, description="Targets that link a product of it")
    products_used: list[str] = Field(default_factory=list)

    @classmethod
    def of(cls, d: dict) -> "PackageInfo":
        req = d.get("requirement") if isinstance(d.get("requirement"), dict) else {"rule": "unknown", "text": "?"}
        res = d.get("resolved") if isinstance(d.get("resolved"), dict) else None
        return cls(identity=str(d.get("identity") or ""), name=str(d.get("name") or d.get("identity") or ""),
                   kind=str(d.get("kind") or "remote"), url=d.get("url"), path=helper.tilde(d.get("path")),
                   requirement=Requirement(**{k: (str(v) if v is not None else None) for k, v in req.items()
                                              if k in Requirement.model_fields}),
                   resolved=Resolved(**{k: str(v) for k, v in res.items() if k in Resolved.model_fields})
                   if res else None,
                   used_by=[str(x) for x in d.get("used_by") or []][:20],
                   products_used=[str(x) for x in d.get("products_used") or []][:20])


class TargetInfo(Out):
    name: str
    type: str
    app: bool = Field(False, description="The app's own target (the default for ios_package_add)")


class PackageList(B.Budgeted, Out):
    project: str
    path: str
    packages: list[PackageInfo] = Field(default_factory=list)
    count: int = 0
    total: int = 0
    targets: list[TargetInfo] = Field(default_factory=list)
    app_target: str | None = None
    resolved_file: bool = Field(False, description="Package.resolved exists")
    unresolved: list[str] = Field(default_factory=list, description="Declared but not in Package.resolved: "
                                                                    "ios_package_resolve fetches them")
    elapsed_ms: int | None = None
    next_steps: list[str] = Field(default_factory=list)


class Change(Out):
    identity: str
    from_version: str | None = Field(None, description="Before (null: new pin)")
    to_version: str | None = Field(None, description="After (null: pin removed)")


class TargetDep(Out):
    target: str | None = None
    product: str


class PackageDiagnostic(Out):
    severity: str = "error"
    category: str = "package"
    message: str
    hint: str | None = None


class PackageResult(B.Budgeted, Out):
    BULK: ClassVar[tuple[str, ...]] = ("diff",)
    action: str = Field(description="add, add-product, update, resolve or remove")
    state: Literal["running", "succeeded", "failed", "cancelled", "dry_run"]
    job_id: str | None = None
    project: str | None = None
    path: str | None = None
    message: str
    step: str | None = Field(None, description="Running: the current step")
    progress: float | None = None
    progress_detail: str | None = None
    steps: list[dict[str, Any]] = Field(default_factory=list, description="[{id, title, state, detail?}]")
    package: PackageInfo | None = None
    target: str | None = None
    needs_product: bool = Field(False, description="Several products: ask the user, then call ios_package_add "
                                                   "again with product=")
    products: list[str] = Field(default_factory=list, description="The package's library products")
    products_added: list[str] = Field(default_factory=list)
    changes: list[Change] = Field(default_factory=list)
    would_remove: list[TargetDep] = Field(default_factory=list, description="Remove: target dependencies dropped "
                                                                            "with the package")
    removed: bool = False
    valid: bool | None = Field(None, description="Remove: Package.swift checked out after the edit")
    problems: list[str] = Field(default_factory=list)
    pins_removed: list[str] = Field(default_factory=list)
    diff: str = Field("", description="Unified diff of Package.swift")
    diff_bytes: int = 0
    diagnostics: list[PackageDiagnostic] = Field(default_factory=list)
    error: ErrorInfo | None = None
    elapsed_ms: int | None = None
    next_steps: list[str] = Field(default_factory=list)


# ------------------------------------------------------------------ helpers

def identity_of(location: str) -> str:
    """SwiftPM's identity of a URL or path (helper/iosstudio/features/package_manifest.py identity_of)."""
    s = str(location or "").strip().rstrip("/")
    if s.endswith(".git"):
        s = s[:-4]
    return (re.split(r"[/:]", s)[-1] if s else s).lower()


# The helper's hints speak in app terms; agents get these instead.
AGENT_HINTS = {
    "fetch-failed": "Check the URL (a git repository that clones without a password), then call ios_package_add "
                    "again.",
    "network": "Check the internet connection, then retry.",
    "resolve-failed": "No version matches: call again with another version or rule (rule='branch', version='main').",
    "manifest-invalid": "Package.swift has errors: fix the lines listed, then retry.",
    "package-failed": "Read the message; fix Package.swift or the request, then retry.",
}


def _diags(err: dict) -> list[PackageDiagnostic]:
    code = str(err.get("code") or "")
    out = []
    for d in (err.get("diagnostics") or [])[:DIAGS_CAP]:
        if not isinstance(d, dict):
            continue
        out.append(PackageDiagnostic(severity=str(d.get("severity") or "error"),
                                     category=str(d.get("category") or "package"),
                                     message=B.clip(" ".join(str(d.get("message") or "").split()), MESSAGE_CUT),
                                     hint=AGENT_HINTS.get(code) or (B.clip(str(d["hint"]), MESSAGE_CUT)
                                                                    if d.get("hint") else None)))
    return out


def _changes(raw: Any) -> list[Change]:
    return [Change(identity=str(c.get("identity") or ""), from_version=c.get("from"), to_version=c.get("to"))
            for c in (raw or [])[:CHANGES_CAP] if isinstance(c, dict)]


def _err_result(action: str, p: dict, err: dict, **extra: Any) -> PackageResult:
    nxt = helper.next_step_for(err)
    fields: dict[str, Any] = dict(
        project=p.get("name"), path=p.get("path"), message=B.clip_error(str(err.get("message") or "Failed.")),
        error=ErrorInfo(code=err.get("code"), message=B.clip_error(str(err.get("message") or ""))),
        diagnostics=_diags(err), problems=[B.clip(str(x), MESSAGE_CUT) for x in (err.get("problems") or [])[:10]],
        next_steps=[nxt] if nxt else [])
    fields.update({k: v for k, v in extra.items() if v is not None})
    return PackageResult(action=action, state="cancelled" if err.get("code") == "cancelled" else "failed",
                         **fields)


def _diff_fields(data: dict) -> dict:
    out = {"diff": str(data.get("diff") or ""), "diff_bytes": int(data.get("diff_bytes") or 0)}
    if data.get("truncated"):
        out.update(truncated=True, full_output_path=data.get("full_output_path"),
                   note="The diff was cut; the whole diff is in full_output_path.")
    return out


def _flags(p: dict, **kw: Any) -> dict:
    return {"project": p["path"], **{k: v for k, v in kw.items() if v is not None}}


def _busy(state, path: str) -> None:
    job = state.jobs.running_for(path)
    if job:
        raise ToolError(f"{job.title} ({job.id}) is still running for this project. Wait for it with "
                        f"ios_job_status(job_id='{job.id}') or stop it with ios_job_cancel, then retry.")


def _job_view(job: Job, action: str, tool_name: str, p: dict) -> PackageResult:
    steps = [{k: v for k, v in s.items() if k in ("id", "title", "state", "detail") and v not in (None, "")}
             for s in job.steps.values()]
    common = dict(job_id=job.id, project=job.project or p.get("name"), path=job.project_path,
                  steps=steps, elapsed_ms=int(job.elapsed * 1000))
    if not job.finished:
        title = (job.steps.get(job.current_step or "", {}) or {}).get("title") or "Starting"
        return PackageResult(
            **common, action=action, state="running", message=f"{title}…", step=title,
            progress=round(job.progress, 3) if job.progress is not None else None,
            progress_detail=B.clip(job.progress_detail, 200) if job.progress_detail else None,
            next_steps=[f"Call {tool_name}(job_id='{job.id}', wait_seconds=20) again; "
                        f"ios_job_cancel(job_id='{job.id}') stops it and puts Package.swift back."])
    if job.state != "succeeded":
        err = dict(job.error or {"code": "failed", "message": "Failed."})
        r = _err_result(action, p, err, **common)
        if err.get("code") == "cancelled":
            r.next_steps = ["Cancelled: Package.swift and Package.resolved are as they were."]
        return r
    return _success(action, p, job.result or {}, **common)


def _success(action: str, p: dict, data: dict, **common: Any) -> PackageResult:
    common.setdefault("project", p.get("name"))
    common.setdefault("path", p.get("path"))
    pkg = PackageInfo.of(data["package"]) if isinstance(data.get("package"), dict) else None
    ident = data.get("identity") or (pkg.identity if pkg else None)
    if action in ("add", "add-product"):
        products = [str(x) for x in data.get("products") or []]
        added = [str(x) for x in data.get("products_added") or []]
        needs = bool(data.get("needs_product"))
        if needs:
            msg = (f"Added {ident} to Package.swift and fetched it. It has {len(products)} products, so none was "
                   f"linked to {data.get('target')} yet.")
            src = (pkg.url or pkg.path) if pkg else "<same url>"
            nxt = [f"Show the user the products and ask which one(s) the app needs, then call ios_package_add("
                   f"project_path, url='{src}', product='<name>') (it only links the product now)."]
        else:
            linked = ", ".join(added) or ", ".join(data.get("already_linked") or [])
            msg = (f"{ident} {'is' if action == 'add-product' else 'was added and'} linked to {data.get('target')}"
                   f" ({linked})." if linked else f"{ident} was added.")
            nxt = [CHECK_BUILD]
        cut = len(products) > PRODUCTS_CAP
        r = PackageResult(**common, action=action, state="succeeded", message=msg, package=pkg,
                          target=data.get("target"), needs_product=needs, products=products[:PRODUCTS_CAP],
                          products_added=added, next_steps=nxt, **_diff_fields(data))
        if cut:
            r.truncated = True
            r.note = (r.note + " " if r.note else "") + f"products lists {PRODUCTS_CAP} of {len(products)}."
        return r
    if action in ("update", "resolve"):
        changes = _changes(data.get("changes"))
        n = int(data.get("changed") or len(changes))
        msg = str(data.get("summary") or (f"{n} package(s) changed." if n else "Everything is up to date."))
        r = PackageResult(**common, action=action, state="succeeded", message=msg, changes=changes,
                          next_steps=[CHECK_BUILD] if n else ["Nothing changed."])
        if n > len(changes):
            r.truncated = True
            r.note = f"changes lists {len(changes)} of {n}."
        return r
    raise ToolError(f"Unknown action {action}.")


# ------------------------------------------------------------------ tools

def register(mcp: MCPServer) -> None:

    @tool(mcp, "List Swift packages", kind="read")
    async def ios_package_list(
        ctx: Context,
        project_path: ProjectPath,
        max_items: Annotated[int, Field(ge=1, le=200)] = 50,
        max_bytes: MaxBytes = None,
    ) -> PackageList:
        """A project's Swift package dependencies: identity, URL, version rule, resolved version and the targets
        using each product; also the project's targets and `unresolved` (declared, not fetched) packages.
        Read-only; the first call takes a few seconds."""
        state = get_state(ctx)
        p = await resolve_project(state, project_path)
        data = await helper.call("packages", "list", _flags(p), timeout=LIST_TIMEOUT)
        pkgs = [PackageInfo.of(d) for d in data.get("packages") or [] if isinstance(d, dict)]
        total = int(data.get("total") or len(pkgs))
        nxt = []
        if data.get("unresolved"):
            nxt.append(f"{len(data['unresolved'])} package(s) aren't fetched yet: ios_package_resolve(project_path).")
        if not total:
            nxt.append("No packages yet. Add one with ios_package_add(project_path, url=...).")
        notes = [str(data["note"])] if data.get("note") else []
        shown = pkgs[:max_items]
        if total > len(shown):
            notes.append(f"Showing {len(shown)} of {total}; raise max_items for more.")
        model = PackageList(
            project=str(p.get("name") or ""), path=str(p.get("path") or ""), packages=shown, count=len(shown),
            total=total, targets=[TargetInfo(name=str(t.get("name")), type=str(t.get("type") or "regular"),
                                             app=bool(t.get("app"))) for t in (data.get("targets") or [])[:50]],
            app_target=data.get("app_target"), resolved_file=bool(data.get("resolved_file")),
            unresolved=[str(x) for x in data.get("unresolved") or []][:50], elapsed_ms=data.get("elapsed_ms"),
            next_steps=nxt, truncated=total > len(shown), omitted=max(0, total - len(shown)),
            note=" ".join(notes) or None).with_budget(max_bytes)
        return fit_page(model, "packages", 0, lambda k, end: f"Showing {k} of {total} packages; raise max_bytes "
                                                             "for more.")

    @tool(mcp, "Add a Swift package", kind="write", open_world=True)
    async def ios_package_add(
        ctx: Context,
        project_path: OptPath = None,
        url: Annotated[str | None, Field(description="Git URL or local folder of the package")] = None,
        rule: Annotated[Literal["from", "upToNextMinor", "exact", "branch", "revision"],
                        Field(description="from: up to the next major (usual)")] = "from",
        version: Annotated[str | None, Field(description="1.2.0 (default: newest release), branch or commit")] = None,
        target: Annotated[str | None, Field(description="Target to link (default: the app's)")] = None,
        product: Annotated[str | list[str] | None, Field(description="Product(s) to link if there are several")]
        = None,
        dry_run: Annotated[bool, Field(description="Only show the Package.swift change")] = False,
        job_id: OptJobId = None,
        wait_seconds: WaitSeconds = 20,
        max_bytes: MaxBytes = None,
    ) -> PackageResult:
        """Add a Swift package by URL and link its product to the app's target. Several products and no `product`:
        needs_product=true with the list; ask the user, then call again with product='Name'. A job: state
        'running' means call again with job_id. A failure puts Package.swift back. Then check with a compile."""
        state = get_state(ctx)
        if job_id:
            job = state.jobs.get(job_id)
            await wait_with_progress(state, job, wait_seconds, ctx)
            return _job_view(job, "add", "ios_package_add", {"name": job.project, "path": job.project_path}
                             ).with_budget(max_bytes)
        if not project_path or not url:
            raise ToolError("Pass project_path and url (or job_id to keep waiting for an add).")
        p = await resolve_project(state, project_path)
        _busy(state, p["path"])
        products = [product] if isinstance(product, str) else list(product or [])
        products = [x.strip() for x in products if x and x.strip()]
        if products and not dry_run:
            existing = await _declared(p, url)
            if existing:
                return await _add_product(p, existing, products, target, max_bytes)
        flags = _flags(p, url=url.strip(), rule=rule, version=(version or "").strip() or None, target=target)
        if products:
            import json
            flags["products"] = json.dumps(products)
        if dry_run:
            try:
                data, _ = await helper.call_stream_collect("packages", "add", {**flags, "dry_run": True},
                                                           timeout=DRY_RUN_TIMEOUT)
            except helper.HelperFailure as exc:
                return _err_result("add", p, exc.error).with_budget(max_bytes)
            return PackageResult(action="add", state="dry_run", project=p.get("name"), path=p.get("path"),
                                 message=f"Adding would write this to Package.swift ({data.get('requirement')}), "
                                         f"then fetch {data.get('identity')} and link it to {data.get('target')}.",
                                 target=data.get("target"), next_steps=["Call again without dry_run to add it."],
                                 **_diff_fields(data)).with_budget(max_bytes)
        job = await state.jobs.start("packages", "packages", "add", flags, title=f"Add {identity_of(url)}",
                                     prefix="pkg", project_path=p["path"], project=p.get("name"))
        await wait_with_progress(state, job, wait_seconds, ctx)
        return _job_view(job, "add", "ios_package_add", p).with_budget(max_bytes)

    async def _declared(p: dict, url: str) -> str | None:
        """The identity of `url` when Package.swift already declares it."""
        ident = identity_of(url)
        try:
            data = await helper.call("packages", "list", _flags(p), timeout=LIST_TIMEOUT)
        except helper.HelperFailure:
            return None
        for d in data.get("packages") or []:
            if str(d.get("identity") or "").lower() == ident:
                return str(d["identity"])
        return None

    async def _add_product(p: dict, ident: str, products: list[str], target: str | None,
                           max_bytes: int | None) -> PackageResult:
        import json
        try:
            data = await helper.call("packages", "add-product",
                                     _flags(p, package=ident, products=json.dumps(products), target=target),
                                     timeout=REMOVE_TIMEOUT)
        except helper.HelperFailure as exc:
            return _err_result("add-product", p, exc.error).with_budget(max_bytes)
        return _success("add-product", p, data).with_budget(max_bytes)

    @tool(mcp, "Update Swift packages", kind="write", open_world=True)
    async def ios_package_update(
        ctx: Context,
        project_path: OptPath = None,
        package: Annotated[str | None, Field(description="Only this package (default: all)")] = None,
        job_id: OptJobId = None,
        wait_seconds: WaitSeconds = 20,
        max_bytes: MaxBytes = None,
    ) -> PackageResult:
        """Update packages to the newest versions their rules allow and report version `changes`. Package.swift
        isn't changed. A job: state 'running' means call again with job_id; ios_job_cancel undoes it."""
        return await _resolve_like(ctx, "update", project_path, package, job_id, wait_seconds, max_bytes)

    @tool(mcp, "Resolve Swift packages", kind="write", idempotent=True, open_world=True)
    async def ios_package_resolve(
        ctx: Context,
        project_path: OptPath = None,
        job_id: OptJobId = None,
        wait_seconds: WaitSeconds = 20,
        max_bytes: MaxBytes = None,
    ) -> PackageResult:
        """Fetch the packages at the versions in Package.resolved (e.g. `unresolved` ones, or after editing
        Package.swift by hand) and report changed pins. A job like ios_package_update."""
        return await _resolve_like(ctx, "resolve", project_path, None, job_id, wait_seconds, max_bytes)

    async def _resolve_like(ctx: Context, action: str, project_path: str | None, package: str | None,
                            job_id: str | None, wait_seconds: float, max_bytes: int | None) -> PackageResult:
        state = get_state(ctx)
        name = f"ios_package_{action}"
        if job_id:
            job = state.jobs.get(job_id)
            await wait_with_progress(state, job, wait_seconds, ctx)
            return _job_view(job, action, name, {"name": job.project, "path": job.project_path}
                             ).with_budget(max_bytes)
        if not project_path:
            raise ToolError(f"Pass project_path (or job_id to keep waiting for an {action}).")
        p = await resolve_project(state, project_path)
        _busy(state, p["path"])
        title = f"Update {package}" if package else ("Update packages" if action == "update" else "Resolve packages")
        job = await state.jobs.start("packages", "packages", action, _flags(p, package=package), title=title,
                                     prefix="pkg", project_path=p["path"], project=p.get("name"))
        await wait_with_progress(state, job, wait_seconds, ctx)
        return _job_view(job, action, name, p).with_budget(max_bytes)

    @tool(mcp, "Remove a Swift package", kind="destructive")
    async def ios_package_remove(
        ctx: Context,
        project_path: ProjectPath,
        package: Annotated[str, Field(description="Identity, name or URL from ios_package_list")],
        confirm: Annotated[bool, Field(description="false: dry run with the diff; true after the user agreed")] = False,
        max_bytes: MaxBytes = None,
    ) -> PackageResult:
        """Remove a package and its products' target dependencies from Package.swift. A dry run with the diff
        unless confirm=true (show the user the diff first). A manifest it can't edit safely fails with
        edit-invalid and nothing changes."""
        state = get_state(ctx)
        p = await resolve_project(state, project_path)
        _busy(state, p["path"])
        room = int(B.room(max_bytes) * 0.9)
        try:
            data = await helper.call("packages", "remove", _flags(p, package=package.strip(), confirm=confirm or None,
                                                                  max_bytes=room), timeout=REMOVE_TIMEOUT)
        except helper.HelperFailure as exc:
            err = exc.error
            r = _err_result("remove", p, err, valid=False, **_diff_fields(err))
            return r.with_budget(max_bytes)
        would = data.get("would_remove") or {}
        deps = [TargetDep(target=d.get("target"), product=str(d.get("product")))
                for d in (would.get("target_deps") or [])[:TARGET_DEPS_CAP] if isinstance(d, dict)]
        ident = data.get("identity")
        if data.get("removed"):
            msg = f"Removed {ident} from Package.swift" + (f" and {len(deps)} target dependenc"
                                                           f"{'y' if len(deps) == 1 else 'ies'}." if deps else ".")
            return PackageResult(action="remove", state="succeeded", project=p.get("name"), path=p.get("path"),
                                 message=msg, would_remove=deps, removed=True, valid=True,
                                 pins_removed=[str(x) for x in data.get("pins_removed") or []][:50],
                                 next_steps=[CHECK_BUILD + " Code that still imports the package fails there."],
                                 **_diff_fields(data)).with_budget(max_bytes)
        return PackageResult(action="remove", state="dry_run", project=p.get("name"), path=p.get("path"),
                             message=f"Removing {ident} changes Package.swift as in the diff"
                                     + (f" and drops {len(deps)} target dependenc{'y' if len(deps) == 1 else 'ies'}"
                                        if deps else "") + ". Nothing was changed yet.",
                             would_remove=deps, valid=bool(data.get("valid")),
                             next_steps=["Show the user the diff; only if they agree, call again with confirm=true."],
                             **_diff_fields(data)).with_budget(max_bytes)
