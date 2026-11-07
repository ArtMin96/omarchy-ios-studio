"""Apple ID and signing tools. Sign-in itself is deliberately not here: passwords and 2FA codes are
typed by the human in the app (ios_open_app(page='account')), never passed through an agent."""
from __future__ import annotations

from typing import Annotated, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from .. import helper
from ..backends.builds import make_installer
from ..backends.projects import resolve_project
from ..core import budget as B
from ..core.job_status import start_and_wait
from ..core.params import ProjectPath, Udid, WaitSeconds
from ..core.registry import dry_run, tool
from ..models import (
    AccountStatus,
    ActionResult,
    Certificate,
    JobStatus,
    Profile,
    ProjectSigning,
    RenewalDue,
    SigningBrief,
    SigningStatus,
    iso,
)
from ..state import get_state

SIGN_IN = ("Signing in needs the human: ask them to sign in with their Apple ID in iOS Studio "
           "(ios_open_app(page='account')). Never ask for their password or 2FA code yourself.")


def _profiles(details: dict) -> list[Profile]:
    items = ((details.get("sections") or {}).get("profiles") or {}).get("items") or []
    out = [Profile(bundle_id=p.get("displayBundleId") or p.get("bundleId"), registered_bundle_id=p.get("bundleId"),
                   expires=iso(p.get("expires")), days_left=p.get("daysLeft"),
                   devices=int(p.get("devicesCount") or len(p.get("devices") or [])))
           for p in items if p.get("active", True)]
    out.sort(key=lambda p: p.days_left if p.days_left is not None else 1e9)
    return out


def register(mcp: MCPServer) -> None:

    @tool(mcp, "Apple ID status", kind="read_remote")
    async def ios_account_status(
        refresh: Annotated[bool, Field(description="Fetch fresh lists from Apple (5-15 s)")] = False,
    ) -> AccountStatus:
        """Apple ID sign-in state for signing: team id and type (free/paid), free-team limits, the signing
        key, certificates and profiles with expiry. Free teams register bundle ids as XTL-<TEAM>.<id>. Sign-in
        is human-only (see next_steps)."""
        st = await helper.call("account", "status")
        out = AccountStatus(signed_in=bool(st.get("signedIn")), apple_id=st.get("appleId"), team_id=st.get("teamId"),
                            token_days_left=st.get("tokenDaysLeft"),
                            has_local_signing_key=bool(st.get("hasLocalSigningKey")))
        if not out.signed_in:
            out.next_steps = [SIGN_IN]
            return out
        try:
            details = await helper.call("account", "details", {} if refresh else {"cached": True},
                                        timeout=40 if refresh else 15)
        except helper.HelperFailure as exc:
            out.errors.append(str(exc))
            details = {}
        summ = details.get("summary") or {}
        team = summ.get("team") or {}
        out.team_name = team.get("name")
        out.team_type = summ.get("teamType")
        out.limits = summ.get("limits")
        out.counts = summ.get("counts")
        h = summ.get("health") or {}
        out.health = f"{h.get('title')}: {h.get('detail')}" if h.get("title") else None
        certs = ((details.get("sections") or {}).get("certificates") or {}).get("items") or []
        out.certificates, _ = B.cap_list(
            [Certificate(name=c.get("displayName") or c.get("name"), kind=c.get("kindLabel") or c.get("kind"),
                         expires=iso(c.get("expires")), days_left=c.get("daysLeft")) for c in certs])
        out.certificates_total = len(certs)
        profiles = _profiles(details)
        out.profiles, _ = B.cap_list(profiles)
        out.profiles_total = len(profiles)
        out.details_fetched = iso(details.get("fetchedAt"))
        out.details_stale = bool(details.get("stale") or details.get("empty"))
        out.errors += [B.clip(f"{e.get('section')}: {e.get('message')}", B.ERROR_CUT) for e in details.get("errors") or []]
        out.errors = out.errors[:B.LIST_CAP]
        if out.profiles_total > len(out.profiles) or out.certificates_total > len(out.certificates):
            out.next_steps.append(f"Lists show at most {B.LIST_CAP} (soonest expiry first): {out.profiles_total} "
                                  f"profiles, {out.certificates_total} certificates in total. The full lists are in "
                                  "iOS Studio: ios_open_app(page='account').")
        if details.get("empty"):
            out.next_steps.append("No cached account details yet: call ios_account_status(refresh=true).")
        elif out.details_stale and not refresh:
            out.next_steps.append("Cached details may be outdated; refresh=true fetches them from Apple.")
        return out

    @tool(mcp, "Signing status", kind="read")
    async def ios_signing_status(ctx: Context) -> SigningStatus:
        """When each app's signing expires (free Apple IDs: 7 days; ios_project_renew re-signs): per-project
        days_left, cached profiles, the daily auto-renew timer and what it would renew."""
        state = get_state(ctx)
        data = await state.projects()
        projects = [ProjectSigning(name=str(p.get("name")), path=str(p.get("path")), bundle_id=p.get("bundleId"),
                                   signing=SigningBrief.from_helper(p.get("signing")),
                                   auto_renew=bool(p.get("autoRenew")))
                    for p in data.get("projects") or []]
        projects.sort(key=lambda p: (p.signing is None, p.signing.days_left if p.signing and p.signing.days_left
                                     is not None else 1e9))
        try:
            details = await helper.call("account", "details", {"cached": True})
        except helper.HelperFailure:
            details = {}
        try:
            timer = await helper.call("projects", "autorenew")
        except helper.HelperFailure:
            timer = {}
        summ = details.get("summary") or {}
        nxt = []
        for p in projects:
            if p.signing and p.signing.level in ("warn", "error"):
                nxt.append(f"{p.name}: {'expired' if p.signing.level == 'error' else 'expires soon'}; "
                           f"ios_project_renew(project_path='{p.path}').")
        if not timer.get("enabled") and summ.get("free"):
            nxt.append("Automatic daily renewal is off: ios_project_autorenew(action='enable') turns it on "
                       "(ask the user first).")
        profiles = _profiles(details)
        due = [RenewalDue(path=r.get("path"), days_left=r.get("daysLeft"), due=r.get("due"))
               for r in timer.get("projects") or []]
        due.sort(key=lambda r: (not r.due, r.days_left if r.days_left is not None else 1e9))
        if max(len(projects), len(profiles), len(due)) > B.LIST_CAP:
            nxt.append(f"Lists show at most {B.LIST_CAP}, soonest expiry first; the *_total fields give the full "
                       "counts. ios_project_list(query=...) finds one project.")
        return SigningStatus(
            team_type=summ.get("teamType"), limits=summ.get("limits"), projects=projects[:B.LIST_CAP],
            projects_total=len(projects), profiles=profiles[:B.LIST_CAP], profiles_total=len(profiles),
            autorenew_timer={k: B.clip(timer.get(k), B.ERROR_CUT) if isinstance(timer.get(k), str) else timer.get(k)
                             for k in ("installed", "enabled", "next", "last", "lastResult") if k in timer},
            renewal_due=due[:B.LIST_CAP], renewal_due_total=len(due), next_steps=nxt[:B.LIST_CAP])

    @tool(mcp, "Renew app signing", kind="write", open_world=True)
    async def ios_project_renew(
        ctx: Context,
        project_path: ProjectPath,
        udid: Udid = None,
        wait_seconds: WaitSeconds = 0,
    ) -> JobStatus:
        """Re-sign and reinstall an app so it opens for another 7 days (free Apple IDs); its data stays. A job
        ('r-N', poll ios_job_status); needs the Apple ID and the iPhone."""
        state = get_state(ctx)
        p = await resolve_project(state, project_path)
        busy = state.jobs.running_for(p["path"])
        if busy:
            raise ToolError(f"{p.get('name')} already has a running job {busy.id}; poll ios_job_status.")
        udid = udid or (p.get("signing") or {}).get("udid") or await state.active_udid()
        if not udid:
            raise ToolError("No iPhone is known. Ask the user to connect it (ios_open_app(page='device')) or pass udid.")
        return await start_and_wait(state, ctx, kind="renew", group="projects", action="renew",
                                    flags={"path": p["path"], "udid": udid}, title=f"Renew {p.get('name')}",
                                    prefix="r", wait_seconds=wait_seconds, project_path=p["path"],
                                    project=p.get("name"), finisher=make_installer(state, launch=False))

    @tool(mcp, "Automatic renewal", kind="destructive", idempotent=True)
    async def ios_project_autorenew(
        ctx: Context,
        action: Annotated[Literal["status", "enable", "disable"],
                          Field(description="enable/disable the daily systemd user timer")] = "status",
        project_path: Annotated[str | None, Field(description="Also include/exclude this project")] = None,
        confirm: Annotated[bool, Field(description="false = dry run; true only after the user agreed")] = False,
    ) -> ActionResult:
        """Read or change automatic re-signing: a daily systemd user timer renews auto-renew projects with
        under 2 days left. enable/disable install/remove it: dry runs until confirm=true after the user
        agrees."""
        state = get_state(ctx)
        if action == "status":
            data = await helper.call("projects", "autorenew")
            return ActionResult(done=True, message="Automatic renewal is " +
                                ("on." if data.get("enabled") else "off."), data=data)
        changes = []
        p = await resolve_project(state, project_path) if project_path else None
        if not confirm:
            if p:
                changes.append(f"set autoRenew={action == 'enable'} for {p['path']}")
            data = await helper.call("projects", "autorenew", {action: True, "dry_run": True})
            return dry_run(f"This would {action} automatic renewal" + (f" and {'include' if action == 'enable' else 'exclude'} "
                           f"{p.get('name')}" if p else "") + ".", changes + list(data.get("actions") or []))
        if p:
            await helper.call("projects", "set", {"path": p["path"], "auto_renew": "1" if action == "enable" else "0"})
            state.invalidate_projects()
        data = await helper.call("projects", "autorenew", {action: True, "dry_run": False})
        return ActionResult(done=True, message=f"Automatic renewal {'enabled' if action == 'enable' else 'disabled'}.",
                            data=data)

    @tool(mcp, "Sign out of Apple ID", kind="destructive", idempotent=True, open_world=True)
    async def ios_account_logout(
        confirm: Annotated[bool, Field(description="false = dry run; true only after the user agreed")] = False,
        reset_2fa: Annotated[bool, Field(description="Also forget the trusted 2FA session")] = False,
    ) -> ActionResult:
        """Sign out of the Apple ID: nothing can be signed or renewed until the human signs in again; installed
        apps work until their profiles expire. Dry run first; confirm=true only if the user asked."""
        st = await helper.call("account", "status")
        if not st.get("signedIn"):
            return ActionResult(done=False, message="Not signed in; nothing to do.")
        would = [f"Sign out {st.get('appleId')} (team {st.get('teamId')}) from xtool",
                 "Clear the cached account details"] + (["Forget the trusted 2FA session"] if reset_2fa else [])
        if not confirm:
            return dry_run("This would sign out of the Apple ID.", would)
        data = await helper.call("account", "logout", {"reset_2fa": reset_2fa}, timeout=40)
        return ActionResult(done=True, message="Signed out.", data=data if isinstance(data, dict) else None,
                            next_steps=[SIGN_IN])
