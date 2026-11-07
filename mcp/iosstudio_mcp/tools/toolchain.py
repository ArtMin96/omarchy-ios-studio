"""Toolchain tools: what's installed (Swift, xtool, iOS SDK, pymobiledevice3…), installing it, and
reclaiming disk space."""
from __future__ import annotations

from typing import Annotated

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from .. import helper
from ..core.job_status import start_and_wait
from ..core.params import WaitSeconds
from ..core.registry import dry_run, tool
from ..models import ActionResult, DiskItem, JobStatus, SetupStep, ToolchainCheck, ToolchainInfo, ToolInfo, iso
from ..state import get_state

MB = 1024 * 1024


def _human(step: dict) -> str | None:
    act = step.get("action") or {}
    if act.get("kind") == "page" or step.get("interactive"):
        return f"{act.get('label') or 'Open'} in the app: ios_open_app(page='{act.get('page') or 'setup'}')"
    return None


def register(mcp: MCPServer) -> None:

    @tool(mcp, "Toolchain check", kind="read")
    async def ios_toolchain_check(
        include_done: Annotated[bool, Field(description="Also list steps that are already done")] = False,
    ) -> ToolchainCheck:
        """Per-step setup status of everything needed to build iPhone apps on Linux: system packages, Swift,
        xtool, the iOS SDK (built from Xcode's .xip), pymobiledevice3, Apple ID, iPhone pairing and
        Developer Mode. Each step says whether ios_toolchain_install can do it (can_auto_install) or
        whether it's a human step in the app."""
        data = await helper.call("setup", "check", timeout=40)
        summ = data.get("summary") or {}
        steps = []
        for s in data.get("steps") or []:
            if not include_done and s.get("state") == "done":
                continue
            steps.append(SetupStep(id=s["id"], title=s.get("title", s["id"]), group=s.get("group"),
                                   state=s.get("state", "unknown"), detail=s.get("detail") or None,
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
        refresh_disk: Annotated[bool, Field(description="Re-measure disk usage (can take 10-30 s); default uses "
                                                        "the last measurement")] = False,
    ) -> ToolchainInfo:
        """Installed versions (xtool, Swift, iOS SDKs, pymobiledevice3, rcodesign, ipsw…) and disk usage per
        item, with which items ios_toolchain_clean may remove (cleanable + safe)."""
        info = await helper.call("toolchain", "info", timeout=40)
        try:
            disk = await helper.call("toolchain", "disk", {} if refresh_disk else {"cached": True},
                                     timeout=60 if refresh_disk else 20)
        except helper.HelperFailure:
            disk = {}
        tools = [ToolInfo(id=i.get("id", ""), name=i.get("name", ""), version=i.get("version"), status=i.get("status"),
                          detail=helper.tilde(i.get("detail"))) for i in info.get("items") or []]
        items = [DiskItem(id=d["id"], name=d.get("name", d["id"]), size_mb=round((d.get("size") or 0) / MB, 1),
                          cleanable=bool(d.get("cleanable")), safe=bool(d.get("safe")), note=d.get("note"))
                 for d in disk.get("items") or []]
        nxt = []
        reclaim = (disk.get("reclaimable") or 0) / MB
        if reclaim > 100:
            nxt.append(f"About {reclaim:.0f} MB can be reclaimed safely with ios_toolchain_clean(item=...).")
        return ToolchainInfo(tools=tools, sdks=info.get("sdks") or {}, disk=items,
                             disk_total_mb=round((disk.get("total") or 0) / MB, 1) if disk else None,
                             reclaimable_mb=round(reclaim, 1) if disk else None,
                             free_mb=round((disk.get("free") or 0) / MB, 1) if disk else None,
                             disk_cached_at=iso(disk.get("cachedAt")), next_steps=nxt)

    @tool(mcp, "Install toolchain", kind="write", idempotent=True, open_world=True)
    async def ios_toolchain_install(
        ctx: Context,
        step: Annotated[str | None, Field(description="One step id from ios_toolchain_check, e.g. 'xtool'")] = None,
        all: Annotated[bool, Field(description="Install every missing step that can be automated")] = False,
        include_optional: Annotated[bool, Field(description="With all: also optional extras")] = False,
        force: Annotated[bool, Field(description="Reinstall even if the step looks done")] = False,
        wait_seconds: WaitSeconds = 5,
    ) -> JobStatus:
        """Install missing toolchain pieces as a background job (job_id 't-N'; poll ios_job_status).

        Big steps (Swift, xtool, the SDK) download up to ~2 GB and take 15-45 min. The job first asks
        for confirmation (state needs_input): show the user the prompt and answer with
        ios_job_respond. System packages need root, so a polkit password dialog appears on the user's
        screen; tell them to expect it. Steps that need the human (Xcode download, sign-in, pairing)
        are not done here: see ios_toolchain_check.
        """
        if bool(step) == bool(all):
            raise ToolError("Pass either step='<id>' (from ios_toolchain_check) or all=true.")
        state = get_state(ctx)
        busy = [j for j in state.jobs.jobs.values() if j.kind == "toolchain-install" and not j.finished]
        if busy:
            raise ToolError(f"Toolchain install {busy[0].id} is already running; poll ios_job_status.")
        flags = {"all": all, "step": step, "include_optional": include_optional and all, "force": force}
        return await start_and_wait(state, ctx, kind="toolchain-install", group="setup", action="install",
                                    flags=flags, title=f"Install {'toolchain' if all else step}", prefix="t",
                                    wait_seconds=wait_seconds)

    @tool(mcp, "Clean toolchain files", kind="destructive", idempotent=True)
    async def ios_toolchain_clean(
        ctx: Context,
        item: Annotated[str, Field(description="Disk item id from ios_toolchain_info with cleanable=true, e.g. "
                                               "'project-builds', 'aur', 'xip', 'build-trees', 'xtool-old'")],
        confirm: Annotated[bool, Field(description="false = dry run. true only after the user agreed")] = False,
        wait_seconds: WaitSeconds = 20,
    ) -> ActionResult:
        """Delete one cleanable toolchain item (download caches, old xtool builds, project .build folders…)
        to free disk space. Never touches what's in use. Call with confirm=false first to see exactly what
        would be deleted; repeat with confirm=true once the user agrees."""
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
