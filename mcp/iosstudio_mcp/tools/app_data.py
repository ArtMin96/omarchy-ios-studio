"""An app's saved data on the iPhone: UserDefaults, SwiftData / Core Data stores, and resets.

Daemon commands appdata.defaults / defaults.set / defaults.delete / stores / rows / reset
(docs/deviced-protocol.md, "App data"; docs/features/app-data.md). Your own development builds only. Writes
(defaults set/delete, resets) are dry runs until confirm=true and never happen while the app runs:
stop_app=true stops it first. Anything that can outlast a call is a job (core/jobs.py): defaults changes
('app-defaults'), resets and their dry runs ('app-reset': a reinstall over Wi-Fi can take a minute, listing a big
container a while), and store reads ('app-store': a big SwiftData / Core Data store is copied off a real iPhone
first). The tool waits up to wait_seconds, so small reads answer at once; else state='running' + job_id, call
again with job_id.
"""
from __future__ import annotations

import json
import re
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from .. import helper
from ..backends.device import dcall, package_for, resolve_app
from ..core import budget as B
from ..core.errors import rehint
from ..core.job_status import still_running, wait_with_progress
from ..core.jobs import Job
from ..core.params import BundleId, MaxBytes, OptJobId, Udid, WaitSeconds
from ..core.registry import fit_page, tool
from ..models import Out
from ..state import AppState, get_state

ITEMS_SHOWN = 20            # reset dry run: items listed
COLUMNS_SHOWN = 40          # store summary: columns per table
KEYS_LISTED = 60            # defaults: names of keys left out
READ_TIMEOUT = 600          # store reads and reset dry runs (jobs): copying a big store off a real iPhone

NEXT = {
    "app-running": "The app is running, and it would write its own copy of this data back. Ask the user, then "
                   "repeat with stop_app=true (it stops the app first), or stop it with ios_app_kill.",
    "not-developer": "Only your own development builds share their data (ios_app_list(scope='own') lists them).",
    "no-build": "No signed build of this app was found. Pass project_path, or build one with "
                "ios_build_start(project_path, action='ipa') first.",
    "truncated-value": "That value was shortened for display. Read it whole with ios_app_defaults(key=...), then "
                       "send the full value.",
    "bad-value": "Check value and type: type is string, int, float, bool, date (ISO 8601), data (base64) or json.",
    "bad-type": "type is one of string, int, float, bool, date, data, json.",
    "no-store": "Call ios_app_store(bundle_id) to list the stores the app has now.",
    "no-table": "Call ios_app_store(bundle_id) to list the tables (entity names work too).",
}


async def call(state: AppState, cmd: str, args: dict, udid: str | None, timeout: float = 60.0) -> Any:
    """dcall with this feature's next steps on its own error codes."""
    return await dcall(state, cmd, args, udid=udid, timeout=timeout, hints=NEXT)


# ------------------------------------------------------------------ models

class AppDefaults(B.Budgeted, Out):
    action: str = Field(description="read, set, delete or refresh")
    bundle_id: str
    state: Literal["dry_run", "running", "succeeded", "failed", "cancelled"] | None = Field(
        None, description="set/delete/refresh: running means call again with job_id")
    job_id: str | None = Field(None, description="state=running: call ios_app_defaults_set(job_id=...) again")
    done: bool | None = Field(None, description="set/delete: False for a dry run or while running")
    message: str | None = None
    path: str | None = Field(None, description="The preferences file in the app's container")
    exists: bool | None = None
    running: bool | None = None
    keys: int | None = Field(None, description="read: how many keys the file has")
    shown: int | None = Field(None, description="read: how many of them are in `values`")
    values: dict[str, Any] = Field(default_factory=dict, description="read: key -> JSON value. Plist types JSON "
                                   "lacks are tagged: {'$type': 'data', 'base64', 'size'}, {'$type': 'date', "
                                   "'iso'}, {'$type': 'uid', 'value'}")
    types: dict[str, str] = Field(default_factory=dict, description="key -> string, int, float, bool, date, data, "
                                  "array, dict or uid")
    left_out: list[str] = Field(default_factory=list, description="read: keys not shown (read them with key=...)")
    size: int | None = Field(None, description="read: file size in bytes")
    key: str | None = None
    existed: bool | None = None
    previous: Any = None
    previous_type: str | None = None
    value: Any = None
    type: str | None = None
    stopped_app: bool = False
    method: str | None = Field(None, description="reinstall: the last signed build was installed over the app so "
                               "iOS dropped its cached copy of the settings (the app sees the change); file: only "
                               "the file changed and iOS may hand the app its cached copy")
    verified: bool | None = Field(None, description="The file read back holds the change")
    artifact: str | None = Field(None, description="The build installed over the app (method=reinstall)")
    launch_args: list[str] = Field(default_factory=list, description="method=file: launch arguments that apply the "
                                   "value for one launch (ios_app_launch(args=...))")
    warning: str | None = None
    reverted: list[dict[str, Any]] = Field(default_factory=list, description="read: keys whose earlier change iOS "
                                           "put back ({key, expected, actual})")
    backup: str | None = Field(None, description="Copy of the preferences file before the change, on this computer")
    elapsed_seconds: float | None = None
    error: str | None = None
    next_steps: list[str] = Field(default_factory=list)


class TableInfo(Out):
    name: str
    entity: str | None = Field(None, description="SwiftData / Core Data entity name")
    rows: int | None = None
    meta: bool = Field(False, description="Core Data's own bookkeeping table (Z_*, history)")
    columns: list[str] = Field(default_factory=list, description="'NAME TYPE'")


class StoreInfo(Out):
    name: str
    path: str = Field(description="Pass as store= to read a table")
    size: int
    kind: str = Field(description="swiftdata, coredata or sqlite")
    tables: list[TableInfo] = Field(default_factory=list)
    error: str | None = None


class StoreResult(B.Budgeted, Out):
    bundle_id: str
    state: Literal["running", "succeeded"] = "succeeded"
    job_id: str | None = Field(None, description="state=running: call ios_app_store(job_id=...) again")
    running: bool | None = None
    stores: list[StoreInfo] = Field(default_factory=list, description="Without table: every store")
    store: str | None = Field(None, description="With table: the store read")
    table: str | None = None
    columns: list[str] = Field(default_factory=list, description="'NAME TYPE'; TIMESTAMP cells of Core Data stores "
                                                                 "come as {'$type': 'date', 'iso', 'raw'}")
    rows: list[list[Any]] = Field(default_factory=list, description="Cells are bounded: long text {'$type': 'text', "
                                  "'preview', 'length'}, blobs {'$type': 'blob', 'size', 'hex'}")
    offset: int = 0
    count: int = 0
    total: int = 0
    has_more: bool = False
    next_offset: int | None = None
    next_steps: list[str] = Field(default_factory=list)


class ResetResult(B.Budgeted, Out):
    state: Literal["dry_run", "running", "succeeded", "failed", "cancelled"]
    mode: str
    bundle_id: str
    job_id: str | None = Field(None, description="state=running: call ios_app_reset(job_id=...) again")
    running: bool | None = Field(None, description="Whether the app is running now")
    items: list[str] = Field(default_factory=list, description="Dry run: what would be removed (biggest first)")
    count: int = 0
    files: int = 0
    bytes: int = 0
    project: str | None = None
    artifact: str | None = Field(None, description="reinstall: the signed build that would be installed")
    removed: int | None = None
    failed: list[str] = Field(default_factory=list)
    reinstalled: bool | None = None
    refreshed: bool | None = Field(None, description="wipe: the last build was installed over the app so iOS "
                                   "dropped its cached settings too")
    warning: str | None = None
    stopped_app: bool = False
    elapsed_seconds: float | None = None
    error: str | None = None
    message: str | None = None
    next_steps: list[str] = Field(default_factory=list)


# ------------------------------------------------------------------ helpers

def _size(b: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1000 or unit == "GB":
            return f"{b} {unit}" if unit == "B" else f"{b:.1f} {unit}"
        b /= 1000
    return f"{b} B"  # pragma: no cover


def _jsize(v: Any) -> int:
    return B.nbytes(json.dumps(v, ensure_ascii=False, separators=(",", ":"))) + 4


def _col(c: dict) -> str:
    return f"{c.get('name')} {c.get('type') or ''}".strip()


def _preview(v: Any) -> Any:
    return B.clip_data(v, 10) if isinstance(v, (dict, list, str)) else v


def _job_error(job: Job) -> ToolError:
    """A failed job's error with this feature's next steps."""
    if job.exc is not None:
        return rehint(job.exc, NEXT)
    return ToolError((job.error or {}).get("message") or f"The job {job.state}.")


def _dry_run(bid: str, name: str | None, mode: str, r: dict) -> ResetResult:
    """What a reset would remove (the dry run's reply)."""
    items = [f"{x.get('path')}{'/' if x.get('kind') == 'dir' else ''} ({_size(int(x.get('size') or 0))}"
             + (f", {x.get('files')} files" if x.get("kind") == "dir" else "") + ")"
             for x in (r.get("items") or [])]
    n, b = int(r.get("count") or 0), int(r.get("bytes") or 0)
    if mode == "wipe":
        msg = f"This would delete {n} item(s), {r.get('files') or 0} file(s), {_size(b)} from {name}."
    else:
        msg = (f"This would uninstall {name} (its {n} data item(s), {_size(b)}, go with it) and "
               f"install {helper.tilde(r.get('artifact'))}.")
    nxt = ["Ask the user whether to proceed; only then repeat the call with confirm=true."]
    if r.get("running"):
        nxt.append("The app is running: confirm needs stop_app=true (it stops the app first).")
    out = ResetResult(state="dry_run", mode=mode, bundle_id=bid, running=bool(r.get("running")),
                      items=items[:ITEMS_SHOWN], count=n, files=int(r.get("files") or 0), bytes=b,
                      project=r.get("project"), artifact=helper.tilde(r.get("artifact")),
                      message=msg + " Nothing was changed.", next_steps=nxt)
    if n > ITEMS_SHOWN:
        out.truncated, out.omitted = True, n - ITEMS_SHOWN
        out.note = f"Showing the {ITEMS_SHOWN} biggest of {n} items."
    return out


def _reset_status(job: Job) -> ResetResult:
    bundle_id = job.data["bundle_id"]
    dry = bool(job.data.get("dry_run"))
    if dry and job.state == "succeeded":
        return _dry_run(bundle_id, job.data.get("name"), job.action, job.result or {})
    common = dict(mode=job.action, bundle_id=bundle_id, job_id=job.id, elapsed_seconds=job.elapsed)
    if not job.finished:
        what = ("Listing what the reset would remove" if dry else
                "Reinstalling the app" if job.action == "reinstall" else "Deleting the app's data")
        return ResetResult(state="running", **common, message=f"{what}…", next_steps=[still_running(job)])
    if job.state != "succeeded":
        return ResetResult(state="cancelled" if job.state == "cancelled" else "failed", **common,
                           error=B.clip_error(str(_job_error(job))),
                           next_steps=["Fix the problem in the error, then repeat the reset (dry run first)."])
    r = job.result or {}
    failed = [str(x) for x in (r.get("failed") or [])]
    msg = (f"Reinstalled {helper.tilde(r.get('artifact'))}: the app starts like a fresh install."
           if r.get("reinstalled") else f"Removed {r.get('removed', 0)} item(s), {_size(int(r.get('bytes') or 0))}.")
    if failed:
        msg += f" {len(failed)} couldn't be removed (the iPhone doesn't allow it)."
    if r.get("warning"):
        msg += " iOS may still give the app its old settings: no build to reinstall over it was found."
    return ResetResult(state="succeeded", **common, removed=r.get("removed"), bytes=int(r.get("bytes") or 0),
                       failed=failed[:ITEMS_SHOWN], reinstalled=bool(r.get("reinstalled")),
                       refreshed=r.get("refreshed"), warning=r.get("warning"),
                       stopped_app=bool(r.get("stopped")), artifact=helper.tilde(r.get("artifact")),
                       project=r.get("project"), message=msg,
                       next_steps=[f"Launch it to see the first-launch state: ios_app_run_and_capture(bundle_id="
                                   f"'{r.get('bundleId') or bundle_id}')."])


def _defaults_status(job: Job) -> AppDefaults:
    action, key, bundle_id = job.action, job.data.get("key"), job.data["bundle_id"]
    common = dict(action=action, bundle_id=bundle_id, key=key, job_id=job.id, elapsed_seconds=job.elapsed)
    if not job.finished:
        return AppDefaults(state="running", done=False, **common,
                           message="Waiting for iOS to save the app's settings, then changing the file and "
                                   "installing the app's last build over it (about 20 s)…",
                           next_steps=[still_running(job)])
    if job.state != "succeeded":
        return AppDefaults(state="cancelled" if job.state == "cancelled" else "failed", done=False, **common,
                           error=B.clip_error(str(_job_error(job))),
                           next_steps=["Fix the problem in the error, then repeat (dry run first)."])
    r = job.result or {}
    refreshed = bool(r.get("refreshed"))
    if action == "refresh":
        msg = (f"Installed {helper.tilde(r.get('artifact'))} over the app: it reads its settings file on the next "
               "launch." if refreshed else "Couldn't refresh: " + str((r.get("refreshError") or {}).get("message")))
    elif action == "delete" and not r.get("existed"):
        msg = f"{key} wasn't set; nothing changed."
    else:
        msg = f"{'Set' if action == 'set' else 'Deleted'} {key}" + (" (stopped the app first)" if r.get("stopped") else "")
        if r.get("verified") is False:
            msg += ", but the file read back doesn't hold the change: iOS wrote its own copy over it."
        elif refreshed:
            msg += ". The app sees it on its next launch."
        else:
            msg += " in the file only."
    nxt = []
    if not refreshed and r.get("launchArgs"):
        nxt.append(f"To be sure the app sees it, launch it with these arguments: ios_app_launch(bundle_id="
                   f"'{bundle_id}', args={json.dumps(r['launchArgs'])}) (one launch only).")
    if not refreshed and (r.get("refreshError") or {}).get("code") == "no-build":
        nxt.append("Build the app once (ios_build_start(project_path, action='ipa')) so changes can be applied fully.")
    nxt.append(f"Launch the app to see it: ios_app_run_and_capture(bundle_id='{bundle_id}').")
    return AppDefaults(state="succeeded", done=True, **common, existed=r.get("existed"),
                       previous=_preview(r.get("previous")), previous_type=r.get("previousType"),
                       value=_preview(r.get("value")) if action == "set" else None,
                       type=r.get("type") if action == "set" else None, stopped_app=bool(r.get("stopped")),
                       method=r.get("method") or ("reinstall" if refreshed else None), verified=r.get("verified"),
                       artifact=helper.tilde(r.get("artifact")), launch_args=list(r.get("launchArgs") or []),
                       warning=r.get("warning") or (r.get("refreshError") or {}).get("message"),
                       backup=helper.tilde(r.get("backup")), message=msg, next_steps=nxt)


def _start_job(state: AppState, bid: str, mode: str, kind: str, cmd: str, args: dict, udid: str | None,
               meta: dict | None = None) -> Job:
    """A reset (kind 'app-reset') or a defaults change ('app-defaults') as a job of this server."""
    async def run(job: Job) -> dict:
        return await call(state, cmd, args, udid, timeout=900) or {}

    tool_name = "ios_app_reset" if kind == "app-reset" else "ios_app_defaults_set"
    title = f"{'Reset' if kind == 'app-reset' else 'Change UserDefaults of'} {bid} ({mode})"
    return state.jobs.run(run, kind, action=mode, title=title, tool=tool_name,
                          data={"bundle_id": bid, **(meta or {})})


async def _store_read(state: AppState, bid: str, store: str | None, table: str | None, offset: int, limit: int,
                      udid: str | None) -> dict:
    """The daemon reads behind one ios_app_store call (in its job): {'summary': appdata.stores} without table,
    else {'rows': appdata.rows} of the table, found by name or entity in the stores when no store path is given."""
    if not table or not (store and store.startswith("/")):
        s = await call(state, "appdata.stores", {"bundleId": bid}, udid, timeout=READ_TIMEOUT) or {}
        if not table:
            return {"summary": s}
        want = table.lower()
        cands = [x for x in s.get("stores") or [] if not store or x.get("name") == store or x.get("path") == store]
        hit = [(x, t) for x in cands for t in x.get("tables") or []
               if want in (str(t.get("name")).lower(), str(t.get("entity") or "").lower())]
        if not hit:
            names = sorted({str(t.get("entity") or t.get("name")) for x in cands for t in x.get("tables") or []})
            raise ToolError(f"No table '{table}' in {store or 'any store of ' + bid}. Tables: "
                            f"{', '.join(names)[:300] or 'none'}. Next: call ios_app_store(bundle_id) for the summary.")
        if len({x.get("path") for x, _ in hit}) > 1:
            raise ToolError(f"'{table}' is in several stores: {', '.join(x.get('path') for x, _ in hit)}. "
                            "Next: pass store=<path>.")
        store, table = hit[0][0].get("path"), hit[0][1].get("name")
    r = await call(state, "appdata.rows", {"bundleId": bid, "store": store, "table": table, "offset": offset,
                                           "limit": limit}, udid, timeout=READ_TIMEOUT) or {}
    return {"rows": r, "store": store, "table": table}


def _store_result(bid: str, data: dict, offset: int, max_bytes: int | None) -> StoreResult:
    if "summary" in data:
        s = data["summary"]
        stores = s.get("stores") or []
        infos = []
        for x in stores:
            tables = [TableInfo(name=t.get("name"), entity=t.get("entity"), rows=t.get("rows"),
                                meta=bool(t.get("meta")),
                                columns=[_col(c) for c in (t.get("columns") or [])][:COLUMNS_SHOWN])
                      for t in x.get("tables") or []]
            e = x.get("error")
            infos.append(StoreInfo(name=x.get("name"), path=x.get("path"), size=int(x.get("size") or 0),
                                   kind=x.get("kind") or "sqlite", tables=tables,
                                   error=(e.get("message") if isinstance(e, dict) else e) or None))
        out = StoreResult(bundle_id=bid, running=bool(s.get("running")), stores=infos)
        out.with_budget(max_bytes)
        if B.result_size(B.dump(out)) > B.ceiling(out):        # columns go first, then metadata tables
            for st in out.stores:
                for t in st.tables:
                    t.columns = []
            out.truncated = True
            out.note = "Columns left out to fit; ios_app_store(table=...) shows a table's columns."
        if B.result_size(B.dump(out)) > B.ceiling(out):
            for st in out.stores:
                st.tables = [t for t in st.tables if not t.meta]
            out.note = "Columns and Core Data's metadata tables left out to fit; raise max_bytes to see them."
        if not stores:
            out.next_steps = ["The app has no SwiftData, Core Data or SQLite store under Library/Application "
                              "Support or Documents (yet)."]
        else:
            out.next_steps = ["Read rows with ios_app_store(bundle_id, store=<path>, table=<name or entity>)."]
        return out
    r = data.get("rows") or {}
    rows = r.get("rows") or []
    total = int(r.get("total") or 0)
    end = offset + len(rows)
    page = StoreResult(bundle_id=bid, store=data.get("store"), table=r.get("table") or data.get("table"),
                       columns=[_col(c) for c in r.get("columns") or []], rows=rows, offset=offset, count=len(rows),
                       total=total, has_more=end < total, next_offset=end if end < total else None)
    if end < total:
        page.next_steps = [f"Next page: ios_app_store(bundle_id, store, table, offset={end})."]
    page.with_budget(max_bytes)
    return fit_page(page, "rows", offset, lambda k, e: f"Showing {k} of {total} rows; call again with offset={e}.")


# ------------------------------------------------------------------ tools

def register(mcp: MCPServer) -> None:

    @tool(mcp, "Read an app's UserDefaults", kind="read_remote")
    async def ios_app_defaults(
        ctx: Context,
        bundle_id: BundleId,
        key: Annotated[str | None, Field(description="Only this key, whole")] = None,
        max_bytes: MaxBytes = None,
        udid: Udid = None,
    ) -> AppDefaults:
        """An app's UserDefaults as JSON with each key's type (your own development builds). What doesn't fit
        goes to full_output_path. Change a key with ios_app_defaults_set."""
        state = get_state(ctx)
        app = await resolve_app(state, bundle_id, udid)
        bid = app["bundleId"]
        d = await call(state, "appdata.defaults", {"bundleId": bid, "full": True}, udid) or {}
        values, types = d.get("values") or {}, d.get("types") or {}
        if key is not None:
            if key not in values:
                raise ToolError(f"No key '{key}' in {bid}'s defaults ({len(values)} keys). Next: read without "
                                "key to see them.")
            values, types = {key: values[key]}, {key: types.get(key, "")}
        room = B.room(max_bytes) - 600
        shown: dict = {}
        used = 0
        left: list[str] = []
        for k in values:
            b = _jsize(values[k]) + len(k) * 2 + 20
            if used + b > room:
                left.append(k)
                continue
            shown[k] = values[k]
            used += b
        out = AppDefaults(action="read", bundle_id=bid, path=str(d.get("path") or ""), exists=bool(d.get("exists")),
                          running=bool(d.get("running")), keys=int(d.get("keys") or 0), shown=len(shown),
                          values=shown, types={k: types.get(k, "") for k in shown},
                          left_out=left[:KEYS_LISTED], size=int(d.get("size") or 0),
                          reverted=[B.clip_data(x, 5) for x in (d.get("reverted") or [])][:20])
        links = []
        if left:
            name = re.sub(r"[^A-Za-z0-9._-]", "_", bid) + (f"-{key}" if key else "") + "-defaults.json"
            path, _, link = B.spill(json.dumps({"bundleId": bid, "values": values, "types": types}, indent=1,
                                               ensure_ascii=False), "appdata", name, "application/json")
            links.append(link)
            out.full_output_path = path
            out.truncated = True
            out.omitted = len(left)
            out.note = (f"{len(left)} key(s) didn't fit; the full JSON is in full_output_path. Read one with "
                        "key=..., or raise max_bytes.")
        if out.reverted:
            out.warning = (f"{len(out.reverted)} earlier change(s) were undone by iOS's cached copy of the "
                           "settings (see reverted). Set them again with refresh=true (the default).")
        if not d.get("exists"):
            out.next_steps = ["The app hasn't saved any defaults yet (no preferences file). ios_app_defaults_set("
                              "action='set') creates it."]
        else:
            out.next_steps = ["Change a key with ios_app_defaults_set(action='set') (dry run first); the app reads it "
                              "on its next launch."]
        return out.with_budget(max_bytes, links)

    @tool(mcp, "Change an app's UserDefaults", kind="destructive", idempotent=True, open_world=True)
    async def ios_app_defaults_set(
        ctx: Context,
        bundle_id: Annotated[str | None, Field(description="Not needed with job_id")] = None,
        action: Annotated[Literal["set", "delete", "refresh"],
                          Field(description="refresh: reinstall the last build after refresh=false changes")] = "set",
        key: Annotated[str | None, Field(description="set/delete: the key")] = None,
        value: Annotated[Any, Field(description="set: the value as JSON (or tagged {'$type': 'date'|'data', ...})")] = None,
        type: Annotated[Literal["string", "int", "float", "bool", "date", "data", "json"] | None,
                        Field(description="set: store as this type (date: ISO 8601; data: base64). Default: from "
                                          "the JSON")] = None,
        confirm: Annotated[bool, Field(description="false = dry run (old -> new); true only after the user "
                                                   "agreed")] = False,
        stop_app: Annotated[bool, Field(description="With confirm: stop the app first if it's running")] = False,
        refresh: Annotated[bool, Field(description="Reinstall the last build afterwards so the app sees the change "
                                                   "(false for several changes, then action='refresh')")] = True,
        job_id: OptJobId = None,
        wait_seconds: WaitSeconds = 20,
        udid: Udid = None,
    ) -> AppDefaults:
        """Set or delete one UserDefaults key of your own development build. A dry run (old -> new) unless
        confirm=true; never while the app runs (stop_app=true stops it). The app's last signed build is then
        installed over it (data kept) so iOS drops its cached settings: a ~20 s job, state='running' + job_id,
        call again with job_id."""
        state = get_state(ctx)
        if job_id:
            job = state.jobs.get(job_id, "app-defaults")
            await wait_with_progress(state, job, wait_seconds, ctx)
            return _defaults_status(job)
        if not bundle_id:
            raise ToolError("Pass bundle_id (or job_id to wait for a change started earlier).")
        app = await resolve_app(state, bundle_id, udid)
        bid = app["bundleId"]
        if action == "refresh":
            a = {"bundleId": bid, **({"stopApp": True} if stop_app else {})}
            job = _start_job(state, bid, "refresh", "app-defaults", "appdata.defaults.refresh", a, udid)
            await wait_with_progress(state, job, wait_seconds, ctx)
            return _defaults_status(job)
        if not key:
            raise ToolError(f"Pass key for action='{action}'.")
        if action == "set" and value is None:
            raise ToolError("Pass value for action='set' (JSON). To remove a key use action='delete'.")
        cmd = "appdata.defaults.set" if action == "set" else "appdata.defaults.delete"
        args: dict = {"bundleId": bid, "key": key}
        if action == "set":
            args["value"] = value
            if type:
                args["type"] = type
        if not confirm:
            r = await call(state, cmd, {**args, "dryRun": True}, udid) or {}
            run = bool(r.get("running"))
            if action == "set":
                msg = (f"Would set {key} from {json.dumps(_preview(r.get('previous')))[:200]} ({r.get('previousType')}) "
                       if r.get("existed") else f"Would add {key} ") + \
                      f"to {json.dumps(_preview(r.get('value')))[:200]} ({r.get('type')})."
            else:
                msg = f"Would delete {key}." if r.get("existed") else f"{key} isn't set; nothing to delete."
            nxt = ["Ask the user whether to proceed; only then repeat the call with confirm=true."]
            if run:
                nxt.append("The app is running: the change needs stop_app=true (it stops the app first).")
            if refresh:
                msg += (" With confirm=true the app's last signed build is also installed over it (its data stays), "
                        "so iOS drops its cached settings; about 20 s.")
            return AppDefaults(state="dry_run", done=False, action=action, bundle_id=bid, key=key, existed=r.get("existed"),
                                  previous=_preview(r.get("previous")), previous_type=r.get("previousType"),
                                  value=_preview(r.get("value")) if action == "set" else None,
                                  type=r.get("type") if action == "set" else None, running=run,
                                  message=msg + " Nothing was changed.", next_steps=nxt)
        if stop_app:
            args["stopApp"] = True
        if not refresh:
            args["refresh"] = False
        job = _start_job(state, bid, action, "app-defaults", cmd, args, udid, {"key": key})
        await wait_with_progress(state, job, wait_seconds, ctx)
        if job.finished and job.state == "failed":
            state.jobs.jobs.pop(job.id, None)     # failed within the call: the error is the answer
            raise _job_error(job)
        return _defaults_status(job)

    @tool(mcp, "Read an app's SwiftData / Core Data stores", kind="read_remote")
    async def ios_app_store(
        ctx: Context,
        bundle_id: Annotated[str | None, Field(description="Not needed with job_id")] = None,
        store: Annotated[str | None, Field(description="Store path or name from the summary (default: the one "
                                                       "with `table`)")] = None,
        table: Annotated[str | None, Field(description="Table or entity ('ZITEM' or 'Item'); omit for the summary")] = None,
        offset: Annotated[int, Field(ge=0, description="First row")] = 0,
        limit: Annotated[int, Field(ge=1, le=200, description="Rows per page")] = 20,
        max_bytes: MaxBytes = None,
        job_id: OptJobId = None,
        wait_seconds: WaitSeconds = 20,
        udid: Udid = None,
    ) -> StoreResult:
        """An app's SwiftData / Core Data / SQLite stores, read-only (your own builds). Without table: each
        store's tables, entities, row counts and columns; with table: a page of rows. A big store can outlast a
        call: state='running' + job_id, call again with job_id."""
        state = get_state(ctx)
        if job_id:
            job = state.jobs.get(job_id, "app-store")
        else:
            if not bundle_id:
                raise ToolError("Pass bundle_id (or job_id to wait for a read started earlier).")
            bid = (await resolve_app(state, bundle_id, udid))["bundleId"]

            async def read(job: Job) -> dict:
                job.report(None, "copying the app's stores from the iPhone")
                return await _store_read(state, bid, store, table, offset, limit, udid)
            job = state.jobs.run(read, "app-store", title=f"Read the stores of {bid}", tool="ios_app_store",
                                 data={"bundle_id": bid, "offset": offset, "max_bytes": max_bytes})
        await wait_with_progress(state, job, wait_seconds, ctx)
        if not job.finished:
            return StoreResult(state="running", bundle_id=job.data["bundle_id"], job_id=job.id,
                               next_steps=[still_running(job)])
        if job.state != "succeeded":
            raise _job_error(job)
        return _store_result(job.data["bundle_id"], job.result or {}, job.data["offset"], job.data["max_bytes"])

    @tool(mcp, "Reset an app's data", kind="destructive", open_world=True)
    async def ios_app_reset(
        ctx: Context,
        bundle_id: Annotated[str | None, Field(description="Not needed with job_id")] = None,
        mode: Annotated[Literal["wipe", "reinstall"], Field(description="wipe: empty Documents, Library, tmp; "
                                                                        "reinstall: uninstall + install last build")] = "wipe",
        confirm: Annotated[bool, Field(description="false = dry run; true only after the user agreed")] = False,
        stop_app: Annotated[bool, Field(description="With confirm: stop the app first if it's running")] = False,
        project_path: Annotated[str | None, Field(description="reinstall: use this project's last signed build")] = None,
        job_id: OptJobId = None,
        wait_seconds: WaitSeconds = 20,
        udid: Udid = None,
    ) -> ResetResult:
        """Reset one of your own apps to first-launch state: wipe empties Documents, Library (settings,
        SwiftData / Core Data stores) and tmp; reinstall reinstalls the last signed build. Cannot be undone:
        confirm=false (default) lists what would go; ask the user, then confirm=true. A job: state='running' +
        job_id, call again with job_id."""
        state = get_state(ctx)
        if job_id:
            job = state.jobs.get(job_id, "app-reset")
            await wait_with_progress(state, job, wait_seconds, ctx)
            return _reset_status(job)
        if not bundle_id:
            raise ToolError("Pass bundle_id (or job_id to wait for a reset started earlier).")
        app = await resolve_app(state, bundle_id, udid)
        bid = app["bundleId"]
        args: dict = {"bundleId": bid, "mode": mode}
        if mode == "reinstall" and project_path:
            art, proj = await package_for(state, None, project_path)
            args.update(artifact=art, project=(proj or {}).get("name"))
        if not confirm:     # the dry run lists the container: a big one on a real iPhone takes a while
            async def dry(job: Job) -> dict:
                return await call(state, "appdata.reset", args, udid, timeout=READ_TIMEOUT) or {}
            job = state.jobs.run(dry, "app-reset", action=mode, title=f"List what a reset of {bid} removes",
                                 tool="ios_app_reset", data={"bundle_id": bid, "dry_run": True, "name": app.get("name")})
            await wait_with_progress(state, job, wait_seconds, ctx)
            if job.finished and job.state == "failed":
                state.jobs.jobs.pop(job.id, None)     # failed within the call: the error is the answer
                raise _job_error(job)
            return _reset_status(job)
        if stop_app:
            args["stopApp"] = True
        job = _start_job(state, bid, mode, "app-reset", "appdata.reset", {**args, "confirm": True}, udid)
        await wait_with_progress(state, job, wait_seconds, ctx)
        if job.finished and job.state == "failed":
            state.jobs.jobs.pop(job.id, None)     # failed within the call: the error is the answer
            if job.exc is not None and not isinstance(job.exc, ToolError):
                raise job.exc
            raise _job_error(job)
        return _reset_status(job)
