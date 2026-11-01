"""ios_status: one-call readiness check."""
from __future__ import annotations

import asyncio

from mcp.server.mcpserver import Context, MCPServer

from .. import helper
from ..models import DeviceBrief, HistoryEntry, Problem, StatusReport, iso
from ..state import get_state, last_device_state
from . import READ, tool

async def _safe(coro):
    try:
        return await coro
    except Exception as exc:  # noqa: BLE001  one failing source must not sink the whole status
        return exc


async def device_brief(state) -> DeviceBrief | None:
    """Live device from the device client if it's wired up, else the last-known state file."""
    if state.device_status:
        try:
            d = await state.device_status()
            if d and d.get("udid"):
                return DeviceBrief(udid=d["udid"], name=d.get("name"), model=d.get("model"),
                                   ios_version=d.get("iosVersion"), connection=d.get("connection"),
                                   transport=d.get("transport"), developer_mode=d.get("developerMode"),
                                   source="device-client", updated=iso(d.get("updatedAt")))
        except Exception:  # noqa: BLE001
            pass
    if helper.demo_mode():
        return DeviceBrief(udid="00008150-000000000000001C", name="Alex's iPhone", model="iPhone 17 Pro",
                           ios_version="27.2", connection="connected", transport="wifi", developer_mode=True,
                           source="last-known")
    d = last_device_state()
    if not d:
        return None
    return DeviceBrief(udid=d["udid"], name=d.get("name"), model=d.get("model"), ios_version=d.get("iosVersion"),
                       connection=d.get("connection"), transport=d.get("transport"),
                       developer_mode=d.get("developerMode"), source="last-known", updated=iso(d.get("updatedAt")))


def register(mcp: MCPServer) -> None:

    @tool(mcp, annotations=READ("iOS Studio status"))
    async def ios_status(ctx: Context) -> StatusReport:
        """START HERE. One-call readiness check for iPhone development on this computer.

        Reports whether the toolchain (Swift, xtool, iOS SDK) is installed, whether an Apple ID is signed in
        (free or paid team), the active iPhone and its connection, the known projects, the last build,
        running jobs and apps whose 7-day signing is about to expire. Every problem comes with the tool to
        call (or the human-only step) that fixes it, in `problems` and `next_steps`.
        Fast (about 1 s) and read-only.
        """
        state = get_state(ctx)
        setup, account, projects, history = await asyncio.gather(
            _safe(helper.call("setup", "check")), _safe(helper.call("account", "status")),
            _safe(state.projects()), _safe(helper.call("build", "history", {"limit": 5})))
        device = await device_brief(state)
        problems: list[Problem] = []

        tc_ready, tc_title = False, "unknown"
        if isinstance(setup, dict):
            summ = setup.get("summary") or {}
            tc_title = str(summ.get("title") or summ.get("readiness") or "")
            steps = {s["id"]: s for s in setup.get("steps") or []}
            missing = [steps[i]["title"] for i in summ.get("essentialMissing") or [] if i in steps
                       and steps[i].get("group") in ("System", "Toolchain")]
            tc_ready = not missing and not summ.get("broken")
            if missing:
                auto = summ.get("autoInstallable") or []
                problems.append(Problem(area="toolchain", message="Missing: " + ", ".join(missing),
                                        fix="ios_toolchain_install(all=true)" if auto else
                                        "ios_toolchain_check, then ios_open_app(page='setup') for the manual steps"))
            for b in summ.get("broken") or []:
                problems.append(Problem(area="toolchain", message=f"Broken: {steps.get(b, {}).get('title', b)}",
                                        fix="ios_toolchain_check for details, then ios_toolchain_install(step=...)"))
        else:
            problems.append(Problem(area="toolchain", message=f"Setup check failed: {setup}",
                                    fix="ios_toolchain_check"))

        signed_in = isinstance(account, dict) and bool(account.get("signedIn"))
        if not signed_in:
            problems.append(Problem(area="account", message="No Apple ID is signed in (needed to sign apps).",
                                    fix="Human step: ask the user to sign in; ios_open_app(page='account')."))

        if device is None:
            problems.append(Problem(area="device", message="No iPhone has been connected yet.",
                                    fix="Human step: plug in the iPhone, unlock it, tap Trust; "
                                        "ios_open_app(page='device')."))
        elif device.source == "last-known" and device.connection not in ("connected", None):
            problems.append(Problem(area="device", message=f"{device.name or 'The iPhone'} was last seen "
                                    f"{device.connection}.", fix="Ask the user to unlock it on the same Wi-Fi or "
                                    "plug it in; ios_open_app(page='device')."))

        proj_items = projects.get("projects", []) if isinstance(projects, dict) else []
        expiring = []
        for p in proj_items:
            s = p.get("signing") or {}
            if s.get("daysLeft") is not None and s["daysLeft"] < 2:
                expiring.append(f"{p.get('name')} ({s['daysLeft']:.1f} days left)")
        if expiring:
            problems.append(Problem(area="signing", message="Signing expires soon: " + ", ".join(expiring),
                                    fix="ios_project_renew(project_path=...) for each, with the iPhone reachable"))

        last = None
        if isinstance(history, dict) and history.get("builds"):
            b = max(history["builds"], key=lambda e: e.get("time") or 0)
            last = HistoryEntry.from_entry(b)
            if last.result == "failed":
                problems.append(Problem(area="build", message=f"Last build of {last.project} failed "
                                        f"({last.errors} errors).",
                                        fix=f"ios_build_diagnostics(project_path='{last.path}')"))

        running = [f"{j.id} {j.title} ({j.state})" for j in state.jobs.jobs.values() if not j.finished]
        ready = tc_ready and signed_in and device is not None
        nxt = [p.fix for p in problems]
        if not proj_items:
            nxt.append("No projects yet: ios_project_templates, then ios_project_create.")
        elif ready:
            nxt.append("Ready. Build with ios_build_start(project_path, action='compile') or build+install+launch "
                       "with ios_run(project_path).")
        return StatusReport(
            ready=ready, toolchain=tc_title, toolchain_ready=tc_ready, signed_in=signed_in,
            apple_id=account.get("appleId") if isinstance(account, dict) else None,
            team_id=account.get("teamId") if isinstance(account, dict) else None,
            device=device, projects=len(proj_items), project_names=[str(p.get("name")) for p in proj_items[:20]],
            last_build=last, running_jobs=running, expiring=expiring, problems=problems, next_steps=nxt)
