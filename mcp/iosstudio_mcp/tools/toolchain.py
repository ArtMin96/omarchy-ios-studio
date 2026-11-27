"""Toolchain tools: what's installed (Swift, xtool, iOS SDK, pymobiledevice3…), installing it, reclaiming
disk space, and updates (helper `updates status|check|apply`, features/updates/): the "tested" channel is this
iOS Studio release's pins, "latest" the newest verifiable upstream releases (untested)."""
from __future__ import annotations

import time
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from .. import helper
from ..core import budget as B
from ..core.job_status import (
    follow_job,
    job_next_steps,
    job_result,
    job_status,
    start_and_wait,
    still_running,
    wait_with_progress,
)
from ..core.jobs import Job
from ..core.params import OptJobId, WaitSeconds
from ..core.registry import MAX_WAIT, dry_run, tool
from ..models import (
    ActionResult,
    DiskItem,
    ErrorInfo,
    JobStatus,
    SetupStep,
    ToolchainCheck,
    ToolchainInfo,
    ToolchainUpdateResult,
    ToolchainUpdates,
    ToolInfo,
    UpdateComponent,
    UpdateInterrupted,
    UpdateNotice,
    iso,
)
from ..state import get_state

MB = 1024 * 1024
INFO_TIMEOUT = 60     # `toolchain info` runs every tool's --version (Swift's alone can take seconds)
DISK_TIMEOUT = 120    # `toolchain disk` walks the SDK, toolchain and caches
STATUS_TIMEOUT = 20   # `updates status` reads the cache and a few files (no subprocess)
CHECK_KIND = "toolchain-updates-check"    # `updates check --force` (≤ 40 s)
APPLY_KIND = "toolchain-update"           # `updates apply`
CAP = 8               # components, notices and dry-run lines in one result
TEXT_CUT = 300
DRY_RUN_TIMEOUT = 20  # `updates apply --dry-run`: the lock, a --version probe of changed binaries, the daemon
KILL_GRACE = 120.0    # a cancelled update gets this long to put the previous version back before SIGKILL

# The helper's error codes for updates, as what it means for the agent: (plain message, next step). The helper's
# own message is kept when it has one; the message here is the fallback.
ERRORS: dict[str, tuple[str, str]] = {
    "busy": ("Another toolchain update is running.",
             "Wait for it to finish (the app or ios_job_status shows it), then try again."),
    "iphone-busy": ("The iPhone connection is in use (debugging, screen mirroring or hot reload).",
                    "Ask the user to stop that session first (ios_open_app(page='device')), then try again: "
                    "updating pymobiledevice3 restarts the connection."),
    "blocked": ("This update can't be installed here yet.",
                "ios_toolchain_updates says why (blocked_reason); a notice may name the fix."),
    "verify-failed": ("The new version failed its post-update check; the previous version is back.",
                      "Nothing to undo. ios_toolchain_check confirms the toolchain; stay on the current version or "
                      "try again after the next release."),
    "cancelled": ("Cancelled; the previous version is back.",
                  "ios_toolchain_updates shows what is installed now."),
    "rate-limited": ("GitHub asked iOS Studio to wait before checking again.",
                     "Nothing to do now: the cached result stays valid and checks resume after the time given."),
    "partial": ("Some sources couldn't be checked; the others are current.",
                "ios_toolchain_updates(refresh=true) tries again later (the app retries in a few hours)."),
    "network": ("Couldn't reach GitHub or PyPI to check for updates.",
                "Check the internet connection, then ios_toolchain_updates(refresh=true)."),
}

Channel = Literal["tested", "latest"]


def _human(step: dict) -> str | None:
    act = step.get("action") or {}
    if act.get("kind") == "page" or step.get("interactive"):
        return f"{act.get('label') or 'Open'} in the app: ios_open_app(page='{act.get('page') or 'setup'}')"
    return None


async def _info(job: Job, refresh_disk: bool) -> tuple[dict, dict]:
    """The job behind ios_toolchain_info: versions, then disk usage (cached unless refresh_disk)."""
    job.report(None, "asking each tool for its version")
    info = await helper.call("toolchain", "info", timeout=INFO_TIMEOUT) or {}
    job.report(0.5, "measuring disk usage" if refresh_disk else "reading the last disk measurement")
    try:
        disk = await helper.call("toolchain", "disk", {} if refresh_disk else {"cached": True},
                                 timeout=DISK_TIMEOUT if refresh_disk else 20) or {}
    except helper.HelperFailure:
        disk = {}
    return info, disk


# ------------------------------------------------------------------ updates

def _when(v: Any) -> str | None:
    """A time the helper gives as unix seconds or as an ISO string."""
    if isinstance(v, bool) or v in (None, ""):
        return None
    return iso(v) if isinstance(v, (int, float)) else B.clip(str(v), 40)


def _text(v: Any, n: int = 80) -> str | None:
    return B.clip(str(v), n) if v not in (None, "") else None


def _fix_text(fix: Any) -> str | None:
    """The helper's fix link ({label, page, step?}) as the call that opens that page for the human."""
    if isinstance(fix, dict) and fix.get("page"):
        label = _text(fix.get("label"), 60)
        return (f"{label}: " if label else "") + f"ios_open_app(page='{B.clip(str(fix['page']), 30)}')"
    return _text(fix, TEXT_CUT) if isinstance(fix, str) else None


def _component(c: dict) -> UpdateComponent:
    chk = c.get("checksum")
    return UpdateComponent(
        id=str(c.get("id") or "?"), name=str(_text(c.get("name")) or c.get("id") or "?"),
        installed=_text(c.get("installed")), tested=_text(c.get("tested")), latest=_text(c.get("latest")),
        target=_text(c.get("target")), state=str(c.get("state") or "unknown"), untested=bool(c.get("untested")),
        applicable=c.get("applicable") is not False, blocked_reason=_text(c.get("blockedReason"), TEXT_CUT),
        checksum=_text(chk.get("source") if isinstance(chk, dict) else chk, 40),
        restarts=[str(x) for x in c.get("restarts") or [] if isinstance(x, str)][:4],
        notes_url=_text(c.get("notesUrl"), 300), latest_at=_when(c.get("latestAt")))


def _notice(n: dict) -> UpdateNotice:
    return UpdateNotice(id=str(n.get("id") or "?"), level=str(n.get("level") or "info"),
                        message=B.clip(" ".join(str(n.get("message") or "").split()), TEXT_CUT),
                        fix=_fix_text(n.get("fix")))


def _dicts(v: Any) -> list[dict]:
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []


def _error(err: dict | None) -> tuple[ErrorInfo, list[str]]:
    """A helper error as {code, plain message} plus next steps: the fix link and what the code means."""
    err = err if isinstance(err, dict) else {}
    code = str(err.get("code") or "failed")
    plain, nxt = ERRORS.get(code, ("The update failed.", ""))
    steps = [x for x in (_fix_text(err.get("fix")), nxt or helper.next_step_for(err)) if x]
    return ErrorInfo(code=code, message=B.clip_error(str(err.get("message") or plain))), steps


def _name(status: dict, cid: str | None) -> str:
    c = next((c for c in _dicts(status.get("components")) if c.get("id") == cid), None)
    return str((c or {}).get("name") or cid or "a toolchain part")


def _interrupted(status: dict) -> dict | None:
    """`interrupted` of `updates status`: {component, since} of an update whose process died mid-way."""
    i = status.get("interrupted") if isinstance(status, dict) else None
    return i if isinstance(i, dict) and i.get("component") else None


def _status(data: dict | None, job_id: str | None = None, checked: bool = False) -> ToolchainUpdates:
    """`updates status` (or the result of `updates check`) as a bounded result with next steps."""
    data = data if isinstance(data, dict) else {}
    comps, notes = _dicts(data.get("components")), _dicts(data.get("notices"))
    channel = str(data.get("channel") or "tested")
    count = int(data.get("count") or 0)
    rate = data.get("rate") if isinstance(data.get("rate"), dict) else {}
    reset = rate.get("resetAt")
    limited = _when(reset) if isinstance(reset, (int, float)) and reset > time.time() else None
    err = data.get("error") if isinstance(data.get("error"), dict) else None
    out = ToolchainUpdates(
        job_id=job_id, channel=channel, checked_at=_when(data.get("checkedAt")),
        next_check_at=_when(data.get("nextCheckAt")), due=bool(data.get("due")), count=count,
        components=[_component(c) for c in comps[:CAP]], notices=[_notice(n) for n in notes[:CAP]],
        error=ErrorInfo(code=err.get("code"), message=B.clip_error(str(err.get("message") or "The check failed.")))
        if err else None, rate_limited_until=limited)
    intr = _interrupted(data)
    if intr:
        out.interrupted = UpdateInterrupted(component=str(intr["component"]), name=_name(data, intr["component"]),
                                            since=_when(intr.get("since")))
    if len(comps) > CAP or len(notes) > CAP:
        out.truncated = True
        out.omitted = max(0, len(comps) - CAP) + max(0, len(notes) - CAP)
        out.note = (f"Showing {min(len(comps), CAP)} of {len(comps)} components and {min(len(notes), CAP)} of "
                    f"{len(notes)} notices; the Toolchain page of the app lists all.")
    nxt = []
    if out.interrupted:
        nxt.append(f"An update of {out.interrupted.name} was interrupted (the app or computer stopped mid-way); its "
                   "previous version is set aside. ios_toolchain_update() with no component shows how it is put "
                   "back; ask the user before confirm=true. Any other update puts it back first.")
    updates = [c for c in out.components if c.state == "update"]
    if count:
        ids = ", ".join(f"{c.id} {c.installed} -> {c.target}" for c in updates) or f"{count}"
        nxt.append(f"{count} update(s) on the {channel} channel ({ids}). ios_toolchain_update(component='<id>') or "
                   "(all=true) shows what it would do; ask the user before confirm=true.")
    newer = [c.id for c in out.components if channel == "tested" and c.latest and c.latest != c.tested
             and c.applicable]
    if newer:
        nxt.append(f"Untested newer releases exist ({', '.join(newer)}): ios_toolchain_update(..., channel='latest') "
                   "installs them, only if the user wants untested versions.")
    for c in out.components:
        if c.state == "blocked" and c.blocked_reason:
            nxt.append(f"{c.name} can't be updated: {c.blocked_reason}")
    if limited:
        nxt.append(f"GitHub's rate limit was reached; checks resume after {limited}.")
    elif out.error:
        nxt += _error(err)[1] or ["The last check failed; ios_toolchain_updates(refresh=true) tries again."]
    elif out.due and not checked:
        nxt.append("The last check is over a day old (or never ran): ios_toolchain_updates(refresh=true) checks "
                   "now (up to 40 s).")
    if not count and not nxt:
        nxt.append(f"Everything is up to date on the {channel} channel.")
    out.next_steps = nxt
    return out


def _would(status: dict, w: dict) -> str:
    """One dry-run line from the helper's {id, installed, target}, with the status's name, tested pin, checksum
    source and restarts: "ipsw: 3.1.743 -> 3.1.752 (untested), verified by checksums.txt"."""
    c = next((_component(x) for x in _dicts(status.get("components")) if x.get("id") == w.get("id")), None)
    target = _text(w.get("target"), 60) or "?"
    line = f"{c.name if c else w.get('id')}: {_text(w.get('installed'), 60) or 'not installed'} -> {target}"
    if c and c.tested and target != c.tested:
        line += " (untested)"
    if c and c.checksum:
        line += f", verified by {c.checksum}"
    if c and "iphone-connection" in c.restarts:
        line += ("; restarts the iPhone connection and asks first (refused while a debug, mirror or hot-reload "
                 "session runs)")
    return line


def _blocked(status: dict, component: str | None, channel: str) -> list[str]:
    """Selected components the helper marks as not installable (blocked_reason says why)."""
    out = []
    for c in (_component(x) for x in _dicts(status.get("components"))):
        if (component is None or c.id == component) and c.blocked_reason and (
                c.state == "blocked" or (channel == "latest" and not c.applicable)):
            out.append(B.clip(f"{c.name} {c.latest or ''}: {c.blocked_reason}", TEXT_CUT))
    return out


async def _dry_run(status: dict, component: str | None, all_: bool, channel: str | None) -> ToolchainUpdateResult:
    """`updates apply --dry-run` (the helper's own plan: the lock, the targets, the iPhone check) as a result."""
    ch = channel or str(status.get("channel") or "tested")
    switch = bool(channel) and channel != (status.get("channel") or "tested")
    intr = _interrupted(status)
    first = [f"First put back the previous {_name(status, intr['component'])} (an earlier update of it was "
             "interrupted)."] if intr else []
    if not component and not all_:                    # only put back what an interrupted update set aside
        res = ToolchainUpdateResult(state="dry_run", dry_run=True, channel=ch, would=first,
                                    message=f"An update of {_name(status, intr['component'])} was interrupted; this "
                                    "puts its previous version back and removes what it left behind.")
    else:
        flags = {"component": component, "all": all_, "channel": channel, "dry_run": True}
        try:
            data, _ = await helper.call_stream_collect("updates", "apply", flags, timeout=DRY_RUN_TIMEOUT)
        except helper.HelperFailure as exc:
            if exc.code not in ERRORS:
                raise
            info, steps = _error(exc.error)
            return ToolchainUpdateResult(state="dry_run", dry_run=True, channel=ch, error=info, next_steps=steps,
                                         message=f"Nothing can be updated now: {info.message}",
                                         blocked=[info.message] if exc.code == "blocked" else [])
        items = _dicts((data or {}).get("would"))
        would = first + [_would(status, w) for w in items]
        if switch:
            would.append(f"Save the update channel '{ch}' once the update succeeds.")
        if items:
            msg = (f"This would update {len(items)} component(s) from the {ch} channel. Each download is verified, "
                   "the previous version is kept until the post-update check passes, and a failure or cancel puts "
                   "it back.")
        elif switch:
            msg = f"Nothing needs installing; this would only switch the update channel to '{ch}'."
        else:
            msg = f"Nothing to update on the {ch} channel."
        res = ToolchainUpdateResult(state="dry_run", dry_run=True, channel=ch, message=msg, would=would,
                                    blocked=_blocked(status, component, ch))
    if len(res.would) > CAP or len(res.blocked) > CAP:
        res.truncated = True
        res.omitted = max(0, len(res.would) - CAP) + max(0, len(res.blocked) - CAP)
        res.note = f"Showing {CAP} lines each; update components one at a time with component='<id>' to see all."
        res.would, res.blocked = res.would[:CAP], res.blocked[:CAP]
    if res.would:
        res.message += " Nothing was changed. Ask the user to confirm, then call again with confirm=true."
        res.next_steps = ["Ask the user whether to proceed; only then repeat the call with confirm=true. It runs as "
                          "a job (10 s to a few minutes) and may ask a question (needs_input)."]
    else:
        res.next_steps = ["ios_toolchain_updates shows every component; refresh=true checks for new releases."]
    return res


def _apply_view(job: Job) -> ToolchainUpdateResult:
    """The `updates apply` job as a result: running, a question for the human, or what was updated."""
    common: dict[str, Any] = dict(job_id=job.id, channel=job.data.get("channel"))
    step = (job.steps.get(job.current_step or "", {}) or {}).get("title") or job.current_step
    if not job.finished and job.state != "needs_input":
        return ToolchainUpdateResult(
            **common, state="running", message=f"{step or 'Starting'}…", step=step,
            progress=round(job.progress, 3) if job.progress is not None else None,
            progress_detail=B.clip(job.progress_detail, 200) if job.progress_detail else None,
            next_steps=[f"Call ios_toolchain_update(job_id='{job.id}', wait_seconds=20) again; "
                        f"ios_job_cancel(job_id='{job.id}') stops it and puts the component in progress back."])
    if job.state == "needs_input":
        p = job_status(job).pending_prompt
        return ToolchainUpdateResult(**common, state="needs_input", step=step, pending_prompt=p,
                                     message=(p.title or p.message) if p else "The update waits for an answer.",
                                     next_steps=job_next_steps(job))
    r = job.result if isinstance(job.result, dict) else {}
    applied = [str(x) for x in r.get("applied") or []][:CAP]
    rolled = [str(x) for x in r.get("rolledBack") or []][:CAP]
    after = r.get("status") if isinstance(r.get("status"), dict) else None
    extra: dict[str, Any] = {}
    if after:
        extra["count"] = int(after.get("count") or 0)
        extra["components"] = [_component(c) for c in _dicts(after.get("components"))
                               if c.get("state") != "current"][:CAP]
    back = f" Put back: {', '.join(rolled)}." if rolled else ""
    if job.state == "succeeded":
        if applied:
            msg = f"Updated {', '.join(applied)}; each passed its post-update check." + back
        elif rolled:
            msg = f"Put back the previous {', '.join(rolled)}; the interrupted update is undone."
        else:
            msg = "Nothing needed updating."
        nxt = ["ios_toolchain_check confirms the toolchain is ready."] if applied else []
        if extra.get("count"):
            nxt.append(f"{extra['count']} update(s) still available: ios_toolchain_updates lists them.")
        return ToolchainUpdateResult(**common, **extra, state="succeeded", message=msg, applied=applied,
                                     rolled_back=rolled, next_steps=nxt)
    info, nxt = _error(job.error or {"code": "failed", "message": "The update failed."})
    if job.state == "cancelled":
        text = "Cancelled; the component in progress was put back to its previous version." + back
    else:
        text = info.message + back
    return ToolchainUpdateResult(**common, **extra, state=job.state, message=text, applied=applied,
                                 rolled_back=rolled, next_steps=nxt, error=info)


def register(mcp: MCPServer) -> None:

    @tool(mcp, "Toolchain check", kind="read")
    async def ios_toolchain_check(
        include_done: Annotated[bool, Field(description="Also list steps that are already done")] = False,
    ) -> ToolchainCheck:
        """Setup status per step (system packages, Swift, xtool, iOS SDK from Xcode's .xip, pymobiledevice3,
        Apple ID, pairing, Developer Mode), each saying whether ios_toolchain_install can do it
        (can_auto_install) or it's a human step in the app."""
        data = await helper.call("setup", "check", timeout=MAX_WAIT)
        summ = data.get("summary") or {}
        steps = []
        for s in data.get("steps") or []:
            if not include_done and s.get("state") == "done":
                continue
            steps.append(SetupStep(id=s["id"], title=s.get("title", s["id"]), group=s.get("group"),
                                   state="update" if s.get("update") and s.get("state") == "broken"
                                   else s.get("state", "unknown"), detail=s.get("detail") or None,
                                   essential=bool(s.get("essential")), can_auto_install=bool(s.get("canAutoInstall")),
                                   needs_root=bool(s.get("needsRoot")), size=s.get("size") or None,
                                   time=s.get("time") or None,
                                   human_step=_human(s) if s.get("state") != "done" else None))
        auto = list(summ.get("autoInstallable") or [])
        nxt = []
        if auto:
            nxt.append(f"ios_toolchain_install(all=true) can install: {', '.join(auto)}.")
        if any(s.human_step for s in steps):
            nxt.append("Some steps need the human: see human_step on each.")
        if summ.get("ready"):
            nxt.append("The toolchain is ready.")
        return ToolchainCheck(ready=bool(summ.get("ready")), title=str(summ.get("title") or ""),
                              done=int(summ.get("done") or 0), total=int(summ.get("total") or 0),
                              essential_missing=list(summ.get("essentialMissing") or []), auto_installable=auto,
                              steps=steps, next_steps=nxt)

    @tool(mcp, "Toolchain versions and disk", kind="read")
    async def ios_toolchain_info(
        ctx: Context,
        refresh_disk: Annotated[bool, Field(description="Re-measure disk usage (10-30 s)")] = False,
        job_id: OptJobId = None,
        wait_seconds: WaitSeconds = MAX_WAIT,
    ) -> ToolchainInfo:
        """Installed versions (xtool, Swift, iOS SDKs, pymobiledevice3…) and disk usage per item, with what
        ios_toolchain_clean may remove. A job: state='running' + job_id, call again with job_id."""
        state = get_state(ctx)
        if not job_id:
            job_id = state.jobs.run(lambda job: _info(job, refresh_disk), "toolchain-info",
                                    title="Toolchain versions and disk", tool="ios_toolchain_info").id
        job = await follow_job(state, ctx, job_id, "toolchain-info", wait_seconds)
        if not job.finished:
            return ToolchainInfo(state="running", job_id=job.id, next_steps=[still_running(job)])
        info, disk = job_result(job)
        tools = [ToolInfo(id=i.get("id", ""), name=i.get("name", ""), version=i.get("version"), status=i.get("status"),
                          detail=helper.tilde(i.get("detail"))) for i in info.get("items") or []]
        items = [DiskItem(id=d["id"], name=d.get("name", d["id"]), size_mb=round((d.get("size") or 0) / MB, 1),
                          cleanable=bool(d.get("cleanable")), safe=bool(d.get("safe")), note=d.get("note"))
                 for d in disk.get("items") or []]
        nxt = []
        reclaim = (disk.get("reclaimable") or 0) / MB
        if reclaim > 100:
            nxt.append(f"About {reclaim:.0f} MB can be reclaimed safely with ios_toolchain_clean(item=...).")
        return ToolchainInfo(job_id=job.id, tools=tools, sdks=info.get("sdks") or {}, disk=items,
                             disk_total_mb=round((disk.get("total") or 0) / MB, 1) if disk else None,
                             reclaimable_mb=round(reclaim, 1) if disk else None,
                             free_mb=round((disk.get("free") or 0) / MB, 1) if disk else None,
                             disk_cached_at=iso(disk.get("cachedAt")), next_steps=nxt)

    @tool(mcp, "Install toolchain", kind="write", idempotent=True, open_world=True)
    async def ios_toolchain_install(
        ctx: Context,
        step: Annotated[str | None, Field(description="Step id from ios_toolchain_check, e.g. 'xtool'")] = None,
        all: Annotated[bool, Field(description="Every missing step that can be automated")] = False,
        include_optional: Annotated[bool, Field(description="With all: also optional extras")] = False,
        force: Annotated[bool, Field(description="Reinstall even if the step looks done")] = False,
        wait_seconds: WaitSeconds = 5,
    ) -> JobStatus:
        """Install missing toolchain pieces as a job ('t-N', poll ios_job_status). Big steps download up to
        ~2 GB, 15-45 min. It first asks (needs_input): show the user, answer with ios_job_respond. System
        packages show a polkit password dialog; warn the user. Human steps (Xcode download, sign-in, pairing)
        aren't done here."""
        if bool(step) == bool(all):
            raise ToolError("Pass either step='<id>' (from ios_toolchain_check) or all=true.")
        state = get_state(ctx)
        busy = [j for j in state.jobs.jobs.values() if j.kind in ("toolchain-install", APPLY_KIND) and not j.finished]
        if busy:
            raise ToolError(f"{busy[0].title} ({busy[0].id}) is already running; poll ios_job_status.")
        flags = {"all": all, "step": step, "include_optional": include_optional and all, "force": force}
        return await start_and_wait(state, ctx, kind="toolchain-install", group="setup", action="install",
                                    flags=flags, title=f"Install {'toolchain' if all else step}", prefix="t",
                                    wait_seconds=wait_seconds)

    @tool(mcp, "Clean toolchain files", kind="destructive", idempotent=True)
    async def ios_toolchain_clean(
        ctx: Context,
        item: Annotated[str, Field(description="Cleanable item id from ios_toolchain_info, e.g. 'xip'")],
        confirm: Annotated[bool, Field(description="false = dry run; true only after the user agreed")] = False,
        wait_seconds: WaitSeconds = 20,
    ) -> ActionResult:
        """Delete one cleanable toolchain item (download caches, old xtool builds, project .build folders…)
        to free disk; never what's in use. Dry run first; confirm=true once the user agrees."""
        disk = await helper.call("toolchain", "disk", {"cached": True}, timeout=20)
        items = {d["id"]: d for d in disk.get("items") or []}
        it = items.get(item)
        if not it:
            raise ToolError(f"Unknown item '{item}'. Items: {', '.join(items) or 'none'} (see ios_toolchain_info).")
        if not it.get("cleanable"):
            raise ToolError(f"'{it.get('name')}' can't be cleaned ({it.get('note')}).")
        would = [f"Delete {p.get('path')} ({(p.get('size') or 0) / MB:.0f} MB)" for p in it.get("paths") or []]
        if not confirm:
            note = "" if it.get("safe") else " This item is NOT marked safe: it may be needed again later."
            return dry_run(f"This would delete {it.get('name')} ({(it.get('size') or 0) / MB:.0f} MB).{note}", would,
                           {"note": it.get("note")})
        state = get_state(ctx)
        st = await start_and_wait(state, ctx, kind="toolchain-clean", group="toolchain", action="clean",
                                  flags={"item": item}, title=f"Clean {it.get('name')}", prefix="t",
                                  wait_seconds=wait_seconds)
        if st.state == "succeeded":
            freed = (state.jobs.get(st.job_id).result or {}).get("freed") or 0
            return ActionResult(done=True, message=f"Deleted {it.get('name')}, freed {freed / MB:.0f} MB.")
        if st.state in ("failed", "cancelled"):
            raise ToolError(f"Cleaning failed: {st.error.message if st.error else st.state}.")
        return ActionResult(done=True, message=f"Still deleting; poll ios_job_status('{st.job_id}').",
                            data={"job_id": st.job_id})

    @tool(mcp, "Toolchain updates", kind="read_remote")
    async def ios_toolchain_updates(
        ctx: Context,
        refresh: Annotated[bool, Field(description="Check now (a job, ≤ 40 s)")] = False,
        job_id: OptJobId = None,
        wait_seconds: WaitSeconds = 20,
    ) -> ToolchainUpdates:
        """Toolchain updates (apple-dev, xtool, pymobiledevice3, ipsw, rcodesign) on the channel: tested = this
        release's pins, latest = newest upstream (untested); Swift/SDK notices; an interrupted update. From the
        daily check; refresh=true checks now."""
        state = get_state(ctx)
        if not job_id and not refresh:
            return _status(await helper.call("updates", "status", timeout=STATUS_TIMEOUT))
        if not job_id:
            job = next(iter(state.jobs.running(CHECK_KIND)), None)
            if job is None:
                job = await state.jobs.start(CHECK_KIND, "updates", "check", {"force": True},
                                             title="Check for toolchain updates", prefix="t")
                job.tool = "ios_toolchain_updates"
            job_id = job.id
        job = await follow_job(state, ctx, job_id, CHECK_KIND, wait_seconds)
        if not job.finished:
            return ToolchainUpdates(state="running", job_id=job.id, next_steps=[still_running(job)])
        if job.state == "failed" and isinstance(job.result, dict) and "components" in job.result:
            # a failed check still reports the cached status; its error says what went wrong
            data = dict(job.result)
            data["error"] = data.get("error") or job.error
            return _status(data, job.id, checked=True)
        if job.state == "failed":
            info, nxt = _error(job.error)
            raise ToolError(f"{info.message} [{info.code}]" + (f" Next: {nxt[0]}" if nxt else ""))
        return _status(job_result(job), job.id, checked=True)

    @tool(mcp, "Update toolchain", kind="destructive", open_world=True)
    async def ios_toolchain_update(
        ctx: Context,
        component: Annotated[str | None, Field(description="Id from ios_toolchain_updates")] = None,
        all: Annotated[bool, Field(description="Every update")] = False,
        channel: Annotated[Channel | None, Field(description="Switch to it (latest: untested)")] = None,
        confirm: Annotated[bool, Field(description="Only after the user agreed")] = False,
        job_id: OptJobId = None,
        wait_seconds: WaitSeconds = 10,
    ) -> ToolchainUpdateResult:
        """Update to the channel's versions: verified, and the old version returns if its check fails or on
        ios_job_cancel. No component/all: put back an interrupted update. Dry run first; confirm=true runs a
        job."""
        state = get_state(ctx)
        if job_id:
            return _apply_view(await follow_job(state, ctx, job_id, APPLY_KIND, wait_seconds))
        if component and all:
            raise ToolError("Pass either component='<id>' (from ios_toolchain_updates) or all=true, not both.")
        status = await helper.call("updates", "status", timeout=STATUS_TIMEOUT) or {}
        intr = _interrupted(status)
        if not component and not all and not intr:
            raise ToolError("Pass either component='<id>' (from ios_toolchain_updates) or all=true.")
        known = [str(c.get("id")) for c in _dicts(status.get("components"))]
        if component and component not in known:
            raise ToolError(f"'{component}' isn't in the update list ({', '.join(known) or 'empty'}); see "
                            "ios_toolchain_updates.")
        if not confirm:
            return await _dry_run(status, component, all, channel)
        busy = [j for j in state.jobs.jobs.values() if j.kind in (APPLY_KIND, "toolchain-install") and not j.finished]
        if busy:
            raise ToolError(f"{busy[0].title} ({busy[0].id}) is still running; wait for it with "
                            f"ios_job_status(job_id='{busy[0].id}') first.")
        title = (f"Update {'toolchain' if all else component}" if component or all
                 else f"Put back {_name(status, intr['component'])}")
        job = await state.jobs.start(APPLY_KIND, "updates", "apply",
                                     {"component": component, "all": all, "channel": channel},
                                     title=title, prefix="t", kill_grace=KILL_GRACE)
        job.tool = "ios_toolchain_update"
        job.data["channel"] = channel or status.get("channel")
        await wait_with_progress(state, job, wait_seconds, ctx)
        return _apply_view(job)
