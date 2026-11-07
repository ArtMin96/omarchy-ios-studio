"""Builds: the helper's build history, and what happens after a Wi-Fi build (install, then launch).

    await history(project_path, limit)          newest builds first (helper `build history`)
    make_installer(state, launch=True)          finisher for run/install/renew jobs (jobs.py)
"""
from __future__ import annotations

import asyncio

from mcp.server.mcpserver.exceptions import ToolError

from .. import helper
from ..jobs import Job
from ..state import AppState

DEMO_INSTALL_SECONDS = 1.5


async def history(project_path: str | None, limit: int) -> list[dict]:
    """A project's builds (or every project's), newest first, from the helper's build history."""
    flags = {"limit": max(limit, 50)}
    if project_path:
        flags["path"] = project_path
    data = await helper.call("build", "history", flags)
    builds = [b for b in data.get("builds") or [] if not project_path or b.get("path") == project_path]
    builds.sort(key=lambda b: b.get("time") or 0, reverse=True)
    return builds[:limit]


def make_installer(state: AppState, launch: bool):
    """Finisher for run/install/renew jobs: the helper builds and signs; installing over Wi-Fi (and
    launching) is done by the app's device daemon, which the device client reaches."""

    async def launch_installed(job: Job, r: dict, udid: str | None, bundle: str | None) -> None:
        """Open the installed app with the run's launch args/env (its preset's). Installed fine + launch
        failed stays green with the error in the step and next_steps, as in the app."""
        if not (launch and bundle and udid and state.launch_app) or r.get("launched"):
            return
        job.set_step("launch", "Launching", "running", owner="mcp")
        job.current_step = "launch"
        job.touch()
        how = r.get("launch") if isinstance(r.get("launch"), dict) else {}
        extra = {k: how[k] for k in ("args", "env") if how.get(k)}   # the run preset's, if any
        try:
            res = await state.launch_app(udid, bundle, **extra) or {}
        except ToolError as exc:
            job.set_step("launch", "Launching", "failed", error=str(exc))
            job.next_steps.append(f"Installed, but it didn't open: {exc}")
        else:
            job.set_step("launch", "Launching", "done")
            r.update(launched=True, pid=res.get("pid"))
        job.touch()

    async def finish(job: Job) -> None:
        r = job.result or {}
        how = r.get("launch") if isinstance(r.get("launch"), dict) else {}
        udid, ipa = r.get("udid") or how.get("udid"), r.get("ipa")
        bundle = how.get("bundleId") or r.get("bundleId")
        if r.get("installed"):
            # Installed by the helper (USB: `xtool dev run` installs but never launches): launch it here.
            job.check_cancel()
            await launch_installed(job, r, udid, bundle)
            return
        if not r.get("needsInstall"):
            return
        if helper.demo_mode() and state.install_app is None:
            job.check_cancel()
            job.set_step("install", "Installing", "running", owner="mcp")
            job.touch()
            await asyncio.sleep(DEMO_INSTALL_SECONDS)
            job.set_step("install", "Installing", "done")
            r.update(installed=True, needsInstall=False)
            job.check_cancel()
            if launch:
                job.set_step("launch", "Launching", "done")
                r.update(launched=True, pid=4242)
            job.next_steps.append("Demo mode: the install and launch were simulated.")
            return
        if state.install_app is None or not ipa or not udid:
            job.next_steps.append(
                f"Built and signed {ipa or 'the app'}, but installing over Wi-Fi needs the device tools, which "
                "aren't available in this server. Plug the iPhone in and rerun with via='usb', or ask the user "
                "to press Run in iOS Studio (ios_open_app(page='projects')).")
            return
        job.check_cancel()
        job.set_step("install", "Installing", "running", owner="mcp")
        job.current_step = "install"
        job.touch()
        await state.install_app(udid, ipa, job)
        job.set_step("install", "Installing", "done")
        r.update(installed=True, needsInstall=False)
        try:
            await helper.call("projects", "record-install",
                              {"path": job.project_path, "udid": udid, "via": "wifi", "ipa": ipa})
        except ToolError:
            pass
        job.check_cancel()
        await launch_installed(job, r, udid, bundle)
        job.touch()

    return finish
