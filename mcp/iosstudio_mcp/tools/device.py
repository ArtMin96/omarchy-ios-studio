"""iPhone and app tools, through the shared device daemon (helper/device_client.py).

The MCP server is one more client of the per-user device broker that the iOS Studio window also
uses, so both share one tunnel to the phone. On startup this module fills
the AppState device slots (install_app, launch_app, device_udid, device_status) used by ios_run,
renewals and ios_status.

The client, dcall and the install/launch helpers live in backends/device.py.
"""
from __future__ import annotations

import asyncio
import base64
import logging
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, ImageContent, TextContent
from pydantic import Field

from .. import helper
from ..backends.device import (
    AppInstallStatus,
    apps,
    attach,
    close,
    dcall,
    get_install,
    hub,
    install_status,
    launch,
    package_for,
    resolve_app,
    start_install,
    wait_install,
)
from ..backends.projects import merge_launch, resolve_preset
from ..core import budget as B
from ..core.params import BundleId, PresetName, Udid
from ..core.registry import MAX_WAIT, dry_run, fit_page, page_of, tool
from ..models import (
    ActionResult,
    AppBrief,
    AppLaunchResult,
    AppList,
    DeviceDetails,
    DeviceEntry,
    DeviceList,
    DevicePrepare,
    LocationResult,
    ProcessInfo,
    ProcessList,
    ScreenshotInfo,
)
from ..state import AppState, get_state

log = logging.getLogger("iosstudio.device")


# ------------------------------------------------------------------ formatting

def _entry(d: dict) -> DeviceEntry:
    return DeviceEntry(udid=str(d.get("udid")), name=d.get("name") or None, model=d.get("model") or None,
                       ios_version=d.get("ios") or None, state=d.get("state"), transport=d.get("transport"),
                       usb_paired=bool(d.get("usbPaired")), wifi_paired=bool(d.get("wifiPaired")),
                       active=bool(d.get("active")))


def _gb(v: Any) -> float | None:
    try:
        return round(float(v) / 1e9, 1)
    except (TypeError, ValueError):
        return None


def _app(a: dict) -> AppBrief:
    return AppBrief(name=str(a.get("name") or a.get("bundleId")), bundle_id=str(a.get("bundleId")),
                    version=a.get("version") or None, developer=bool(a.get("developer")),
                    running=bool(a.get("running")), pid=a.get("pid"))


# ------------------------------------------------------------------ tools

async def on_startup(state: AppState) -> None:
    attach(state)


async def on_shutdown(state: AppState) -> None:
    await close(state)


def register(mcp: MCPServer) -> None:

    # -------------------------------------------------------------- devices

    @tool(mcp, "List iPhones", kind="read_remote")
    async def ios_device_list(ctx: Context) -> DeviceList:
        """iPhones this computer knows (paired over USB and/or Wi-Fi), which one is active, and its
        connection state (connected / locked / unreachable / not-paired…).

        Use it when a device tool fails or before choosing a udid. Every other device tool defaults to
        the active iPhone; switch with ios_device_select. Pairing a new phone is a human step
        (ios_open_app(page='device')).
        """
        state = get_state(ctx)
        devs = await dcall(state, "devices.list", timeout=15) or []
        conn = hub(state).get("connection") or {}
        active = next((d for d in devs if d.get("active")), None)
        nxt = []
        if not devs:
            nxt.append("No iPhone is paired. Human step: ask the user to plug it in, unlock it and tap Trust "
                       "(ios_open_app(page='device')).")
        elif active is None:
            nxt.append("No iPhone is active: ios_device_select(udid).")
        elif (conn.get("state") or active.get("state")) not in ("connected",):
            nxt.append(f"The iPhone is {conn.get('state') or active.get('state')}: {conn.get('message') or ''} "
                       "Ask the user to wake/unlock it, then ios_device_reconnect.".strip())
        else:
            nxt.append("Connected. ios_app_list shows installed apps; ios_app_run_and_capture launches one and "
                       "reports logs and crashes.")
        return DeviceList(devices=[_entry(d) for d in devs], active_udid=active.get("udid") if active else None,
                          connection=conn.get("message") if conn else None, next_steps=nxt)

    @tool(mcp, "iPhone details", kind="read_remote")
    async def ios_device_info(
        ctx: Context,
        udid: Udid = None,
        fresh: Annotated[bool, Field(description="Re-read battery, storage and disk-image state from the phone "
                                                 "(slower)")] = False,
    ) -> DeviceDetails:
        """Details of the iPhone: model, iOS version, battery, free storage, Developer Mode, developer disk
        image (DDI), lock state and pairing. Use before installing (iOS version / space) or when launches
        fail (Developer Mode off or DDI not mounted: call ios_device_prepare)."""
        state = get_state(ctx)
        d = await dcall(state, "device.info", {"fresh": fresh}, udid=udid, timeout=45) or {}
        bat = d.get("battery") or {}
        sto = d.get("storage") or {}
        conn = hub(state).get("connection") or {}
        nxt = []
        if d.get("developerMode") is False or d.get("ddiMounted") is False:
            nxt.append("Not ready to launch apps: call ios_device_prepare.")
        if d.get("locked"):
            nxt.append("The iPhone is locked; ask the user to unlock it.")
        return DeviceDetails(udid=str(d.get("udid")), name=d.get("name"), model=d.get("model"),
                             product_type=d.get("productType"), ios_version=d.get("ios"), build=d.get("build"),
                             transport=d.get("transport"),
                             connection=conn.get("state") if conn.get("udid") == d.get("udid") else None,
                             battery_percent=bat.get("level"), charging=bat.get("charging"),
                             storage_free_gb=_gb(sto.get("free")), storage_total_gb=_gb(sto.get("total")),
                             developer_mode=d.get("developerMode"), ddi_mounted=d.get("ddiMounted"),
                             locked=d.get("locked"), wifi_paired=d.get("wifiPaired"), usb_paired=d.get("usbPaired"),
                             next_steps=nxt)

    @tool(mcp, "Select iPhone", kind="write", idempotent=True)
    async def ios_device_select(
        ctx: Context,
        udid: Annotated[str, Field(description="udid from ios_device_list")],
    ) -> DeviceList:
        """Make another paired iPhone the active one; device tools, ios_run and the iOS Studio window
        then use it. Shared with the window (last choice wins). Returns the updated device list."""
        state = get_state(ctx)
        await dcall(state, "device.select", {"udid": udid}, timeout=30,
                    hints={"not-found": "Call ios_device_list for valid udids."})
        return await ios_device_list(ctx)

    @tool(mcp, "Reconnect iPhone", kind="write", idempotent=True, open_world=True)
    async def ios_device_reconnect(ctx: Context, udid: Udid = None) -> DeviceList:
        """Drop and reopen the connection to the iPhone now (skips the retry back-off). Use after the user
        woke/unlocked the phone or joined the same Wi-Fi, or when calls time out. Returns the device list
        a few seconds later; poll ios_device_list if it still says connecting."""
        state = get_state(ctx)
        await dcall(state, "device.reconnect", udid=udid, timeout=20)
        for _ in range(16):
            await asyncio.sleep(0.5)
            if (hub(state).get("connection") or {}).get("state") in ("connected", "locked", "not-paired",
                                                                        "untrusted", "error"):
                break
        return await ios_device_list(ctx)

    @tool(mcp, "Prepare iPhone for development", kind="write", idempotent=True, open_world=True)
    async def ios_device_prepare(
        ctx: Context,
        udid: Udid = None,
        mount: Annotated[bool, Field(description="Mount the developer disk image if it isn't (downloads it from "
                                                 "Apple; needs internet). false = only report")] = True,
        reveal_developer_mode: Annotated[bool, Field(description="If Developer Mode is off, make its switch appear "
                                                                 "in the phone's Settings (USB only)")] = True,
    ) -> DevicePrepare:
        """Check and fix what launching apps, logs and location need: Developer Mode on, and the developer
        disk image (DDI) mounted. Mounts the DDI itself; Developer Mode can only be turned on by the human
        on the phone, so `human_steps` lists exactly what to tell them. Call it when a launch fails with
        developer-mode-off or ddi, or before the first run on a new iPhone."""
        state = get_state(ctx)
        actions, human, nxt = [], [], []
        dm = (await dcall(state, "devmode.status", udid=udid, timeout=30) or {}).get("enabled")
        if dm is False and reveal_developer_mode:
            try:
                await dcall(state, "devmode.reveal", udid=udid, timeout=30)
                actions.append("Made the Developer Mode switch visible in Settings.")
            except ToolError as exc:
                actions.append(f"Couldn't reveal the Developer Mode switch ({str(exc)[:120]}); it may need a USB cable.")
        if dm is False:
            human += ["On the iPhone: Settings > Privacy & Security > Developer Mode, turn it on.",
                      "The iPhone restarts; after it boots, unlock it and tap Turn On, then enter the passcode.",
                      "Then call ios_device_prepare again."]
        ddi = None
        if dm is not False:
            ddi = (await dcall(state, "ddi.status", udid=udid, timeout=30) or {}).get("mounted")
            if ddi is False and mount:
                await ctx.report_progress(10, 100, message="Mounting the developer disk image")
                ddi = bool((await dcall(state, "ddi.mount", udid=udid, timeout=300) or {}).get("mounted"))
                actions.append("Mounted the developer disk image." if ddi else "Tried to mount the disk image.")
            elif ddi is False:
                nxt.append("Call again with mount=true to mount the developer disk image (needs internet).")
        ready = dm is True and ddi is True
        if ready:
            nxt.append("Ready: ios_app_launch, ios_app_run_and_capture and ios_logs_start will work.")
        return DevicePrepare(ready=ready, developer_mode=dm, ddi_mounted=ddi, actions=actions, human_steps=human,
                             next_steps=nxt)

    # -------------------------------------------------------------- apps

    @tool(mcp, "List installed apps", kind="read_remote")
    async def ios_app_list(
        ctx: Context,
        scope: Annotated[Literal["own", "all"], Field(description="own: development-signed apps (yours, e.g. built "
                                                                  "here); all: every user app")] = "own",
        include_system: Annotated[bool, Field(description="With scope='all', include Apple's system apps")] = False,
        query: Annotated[str | None, Field(description="Case-insensitive filter on name or bundle id")] = None,
        offset: Annotated[int, Field(ge=0, description="Skip this many apps (pagination)")] = 0,
        limit: Annotated[int, Field(ge=1, le=200, description="Max apps to return")] = 50,
        udid: Udid = None,
    ) -> AppList:
        """Apps installed on the iPhone: name, bundle id AS INSTALLED, version, whether it's your
        development build, and whether it's running (pid). Free teams install com.example.app as
        XTL-<TEAM>.com.example.app; the app tools accept either form. Free Apple IDs allow at most 3 own
        apps per iPhone (`own_apps`)."""
        state = get_state(ctx)
        items = await apps(state, udid, system=include_system and scope == "all")
        own = sum(1 for a in items if a.get("developer"))
        if scope == "own":
            items = [a for a in items if a.get("developer")]
        if query:
            q = query.lower()
            items = [a for a in items if q in str(a.get("name", "")).lower() or q in str(a.get("bundleId", "")).lower()]
        page, nxt_off = page_of(items, offset, limit, _app)
        nxt = []
        note = f"Showing {len(page)} of {len(items)}; call again with offset={nxt_off}, or narrow with query." \
            if nxt_off is not None else None
        if scope == "own" and not items and not query:
            nxt.append("None of your apps are installed. ios_run(project_path) builds and installs one.")
        return fit_page(AppList(scope=scope, total=len(items), count=len(page), offset=offset,
                                has_more=nxt_off is not None, next_offset=nxt_off, apps=page, own_apps=own,
                                next_steps=nxt, note=note, truncated=nxt_off is not None,
                                omitted=len(items) - len(page)), "apps", offset,
                        lambda k, end: f"Showing {k} of {len(items)}; call again with offset={end}, or narrow with query.")

    @tool(mcp, "Install app", kind="write", open_world=True)
    async def ios_app_install(
        ctx: Context,
        path: Annotated[str | None, Field(description="A signed .ipa file or .app folder, e.g. "
                                                      "'~/Projects/MyApp/xtool/MyApp.ipa'")] = None,
        project_path: Annotated[str | None, Field(description="Instead of path: install this project's last signed "
                                                              "build")] = None,
        install_id: Annotated[str | None, Field(description="Wait for an install started earlier (from a result with "
                                                            "state='running') instead of starting one")] = None,
        wait_seconds: Annotated[float, Field(ge=0, le=MAX_WAIT, description="How long to wait for the install "
                                                                           "before returning state='running'")] = 20,
        udid: Udid = None,
    ) -> AppInstallStatus:
        """Install an already signed .ipa/.app on the iPhone over the current connection (Wi-Fi or USB);
        ~10 s for a typical app, with progress notifications. Replaces an installed copy and keeps its data.
        Runs in the background: if it takes longer than wait_seconds (≤25) the result has state='running'
        and an install_id; call again with install_id to keep waiting. To build AND install, use ios_run
        instead. Fails with app-limit when a free Apple ID already has 3 apps on the phone. Then
        ios_app_launch or ios_app_run_and_capture."""
        state = get_state(ctx)
        last = [-1.0]

        async def prog(pct: float, status: str) -> None:
            if pct > last[0]:
                last[0] = pct
                await ctx.report_progress(pct, 100, message=f"{status} {pct:.0f}%")

        if install_id:
            job = get_install(state, install_id)
        else:
            pkg, proj = await package_for(state, path, project_path)
            job = start_install(state, udid, pkg, proj)
        await wait_install(job, wait_seconds, prog)
        return install_status(job)

    @tool(mcp, "Launch app", kind="write", open_world=True)
    async def ios_app_launch(
        ctx: Context,
        bundle_id: BundleId,
        args: Annotated[list[str] | None, Field(description="Launch arguments, e.g. ['-UITestMode', 'YES']")] = None,
        env: Annotated[dict[str, str] | None, Field(description="Environment variables for the process")] = None,
        console: Annotated[bool, Field(description="Also capture what the app prints (stdout/stderr, Swift "
                                                   "print()) for ios_app_console_read. Restarts it if running")] = False,
        udid: Udid = None,
        preset: PresetName = None,
        project_path: Annotated[str | None, Field(description="With preset: the app's project (name or path). "
                                                              "Default: the listed project that builds bundle_id")] = None,
    ) -> AppLaunchResult:
        """Open an installed app on the iPhone and return its pid. A running copy is ended and started
        again, so the launch arguments and environment always apply (unsaved state in it is lost). The
        phone must be unlocked. console=true also captures its print() output (read it with
        ios_app_console_read). To also capture its logs and catch a crash, prefer ios_app_run_and_capture.
        preset='<name>' launches with a run preset of the app's project (ios_project_presets): its args come
        first, then `args`, and `env` overrides its environment."""
        state = get_state(ctx)
        how = await resolve_preset(state, preset, project_path=project_path, bundle_id=bundle_id) if preset else None
        args, env = merge_launch(how, args, env)
        res = await launch(state, udid, bundle_id, args, env, console=console)
        bid = res.get("bundleId") or bundle_id
        con = (res.get("console") or {}) if console else {}
        con_err = None
        if console and not con.get("sessionId"):
            e = con.get("error") or {}
            con_err = B.clip(e.get("message") if isinstance(e, dict) else str(e or "Not captured."), B.ERROR_CUT)
        steps = ["ios_logs_start(bundle_id=...) to follow its log; ios_crash_list(bundle_id=...) "
                 "if it closes by itself; ios_screenshot to see the screen."]
        if con.get("sessionId"):
            steps.insert(0, f"ios_app_console_read(session_id='{con['sessionId']}', wait_seconds=5) for what it prints.")
        return AppLaunchResult(bundle_id=bid, pid=res.get("pid"), console_session_id=con.get("sessionId"),
                               console_error=con_err, preset=how["name"] if how else None,
                               args=[B.clip(x, 300) for x in args or []][:64] if how else [],
                               env={k: B.clip(v, 300) for k, v in (env or {}).items()} if how else {},
                               next_steps=steps)

    @tool(mcp, "Stop app", kind="write", idempotent=True, open_world=True)
    async def ios_app_kill(ctx: Context, bundle_id: BundleId, udid: Udid = None) -> ActionResult:
        """Terminate a running app on the iPhone (like swiping it away; unsaved state in the app is lost).
        Does nothing if it isn't running."""
        state = get_state(ctx)
        app = await resolve_app(state, bundle_id, udid)
        res = await dcall(state, "app.kill", {"bundleId": app["bundleId"]}, udid=udid, timeout=30) or {}
        killed = res.get("killed") or []
        return ActionResult(done=True, message=f"Stopped {app.get('name')} (pid {', '.join(map(str, killed))})."
                            if killed else f"{app.get('name')} wasn't running.", data={"bundle_id": app["bundleId"]})

    @tool(mcp, "Uninstall app", kind="destructive", idempotent=True, open_world=True)
    async def ios_app_uninstall(
        ctx: Context,
        bundle_id: BundleId,
        confirm: Annotated[bool, Field(description="false = dry run. true only after the user agreed to delete the "
                                                   "app and its data")] = False,
        udid: Udid = None,
    ) -> ActionResult:
        """Delete an app AND ALL ITS DATA from the iPhone. Cannot be undone. Call with confirm=false first:
        it changes nothing and says exactly what would be removed. Ask the user, and only if they agree
        call again with confirm=true. Typical reason: a free Apple ID hit its 3-app limit."""
        state = get_state(ctx)
        app = await resolve_app(state, bundle_id, udid)
        what = f"{app.get('name')} ({app['bundleId']}, version {app.get('version') or '?'})"
        if not confirm:
            return dry_run(f"This would uninstall {what} from the iPhone.",
                           [f"Uninstall {what}", "Delete all of its data and documents on the iPhone"],
                           {"bundle_id": app["bundleId"]})
        await dcall(state, "app.uninstall", {"bundleId": app["bundleId"]}, udid=udid, timeout=60)
        return ActionResult(done=True, message=f"Uninstalled {what}.", data={"bundle_id": app["bundleId"]})

    @tool(mcp, "List processes", kind="read_remote")
    async def ios_process_list(
        ctx: Context,
        apps_only: Annotated[bool, Field(description="Only app processes (not system daemons)")] = True,
        query: Annotated[str | None, Field(description="Case-insensitive filter on name or bundle id")] = None,
        limit: Annotated[int, Field(ge=1, le=200, description="Max processes to return")] = 50,
        udid: Udid = None,
    ) -> ProcessList:
        """Processes running on the iPhone: pid, name and bundle id. Use it to check whether an app is still
        alive, or to find a pid for ios_logs_start(pid=...)."""
        state = get_state(ctx)
        items = await dcall(state, "processes.list", {"appsOnly": apps_only}, udid=udid, timeout=40) or []
        if query:
            q = query.lower()
            items = [p for p in items if q in str(p.get("name", "")).lower() or q in str(p.get("bundleId") or "").lower()]
        items.sort(key=lambda p: str(p.get("name", "")).lower())
        out, _ = page_of(items, 0, limit, lambda p: ProcessInfo(pid=int(p.get("pid") or 0), name=str(p.get("name")),
                                                                bundle_id=p.get("bundleId"), is_app=bool(p.get("isApp"))))
        cut = len(items) - len(out)
        return ProcessList(total=len(items), count=len(out), processes=out, truncated=cut > 0, omitted=cut,
                           note=f"Showing {len(out)} of {len(items)} (sorted by name); narrow with query"
                                + (" or apps_only=true." if not apps_only else ".") if cut else None)

    @tool(mcp, "Screenshot", kind="read_remote", idempotent=False)
    async def ios_screenshot(
        ctx: Context,
        max_dimension: Annotated[int, Field(ge=256, le=B.IMG_MAX_DIM, description="Downscale so the long edge is "
                                                                                 "at most this many pixels")] = B.IMG_MAX_DIM,
        format: Annotated[Literal["jpeg", "png"], Field(description="jpeg is ~10x smaller; png is sent only if it "
                                                                    "fits 350 KB, else jpeg")] = "jpeg",
        quality: Annotated[int, Field(ge=30, le=95, description="Starting JPEG quality; steps down to 65 and 50 "
                                                                "if the image is over 350 KB")] = 80,
        inline: Annotated[bool, Field(description="false: return only the saved file's path, no image")] = True,
        udid: Udid = None,
    ) -> Annotated[CallToolResult, ScreenshotInfo]:
        """Capture the iPhone's screen and return it as an image, plus the saved file path, so you can see
        what the app shows. The image is at most 1568 px on the long edge and 350 KB (JPEG, quality stepping
        down from 80 until it fits); width, height and bytes are reported. inline=false returns only the
        path. The phone must be unlocked with the screen on. If your client can't show images, open the path."""
        state = get_state(ctx)
        shot = await dcall(state, "screenshot", udid=udid, timeout=45) or {}
        src = Path(str(shot.get("path") or ""))
        if not src.is_file():
            raise ToolError("The iPhone returned no screenshot file. Retry with the phone unlocked.")
        steps = [quality] + [q for q in (65, 50) if q < quality]
        img = await asyncio.to_thread(B.fit_image, src, max_dimension, B.IMG_MAX_BYTES, format, steps)
        w, h = img.width or int(shot.get("width") or 0), img.height or int(shot.get("height") or 0)
        out = src.with_suffix(".jpg" if img.format == "jpeg" else ".png")
        if out != src:
            out.write_bytes(img.data)
        send = inline and img.fits
        notes = list(img.notes)
        if inline and not img.fits:
            notes.append(f"The image is {len(img.data) // 1000} KB, over the {B.IMG_MAX_BYTES // 1000} KB cap, so it "
                         "isn't inline; open the path or call again with a smaller max_dimension.")
        info = ScreenshotInfo(path=helper.tilde(str(out)), original=helper.tilde(str(src)), width=w, height=h,
                              original_width=img.original_width or None, original_height=img.original_height or None,
                              format=img.format, bytes=len(img.data), quality=img.quality, inline=send,
                              note=" ".join(notes) or None)
        text = (f"Screenshot {w}x{h} {img.format.upper()} ({len(img.data) // 1000} KB) saved to {info.path}; "
                f"full-resolution PNG: {info.original}." + (f" {info.note}" if info.note else ""))
        content: list = [TextContent(type="text", text=text)]
        if send:
            content.insert(0, ImageContent(type="image", data=base64.b64encode(img.data).decode(),
                                           mime_type=f"image/{img.format}"))
        return CallToolResult(content=content, structured_content=B.dump(info))

    # -------------------------------------------------------------- location

    LASTS = ("It lasts until ios_location_clear, a phone restart, or the iOS Studio device service stopping "
             "(about 2 minutes after both this server and the iOS Studio window have closed). It applies to the "
             "whole phone and every app.")

    @tool(mcp, "Simulate location", kind="write", idempotent=True, open_world=True)
    async def ios_location_set(
        ctx: Context,
        lat: Annotated[float, Field(ge=-90, le=90, description="Latitude, e.g. 38.7223")],
        lon: Annotated[float, Field(ge=-180, le=180, description="Longitude, e.g. -9.1393")],
        udid: Udid = None,
    ) -> LocationResult:
        """Make the iPhone report a simulated GPS position (for testing location features). Needs Developer
        Mode and the developer disk image (mounted automatically). Temporary: it lasts only while the
        iOS Studio device service runs, see the result message. Undo with ios_location_clear."""
        state = get_state(ctx)
        res = await dcall(state, "location.set", {"lat": lat, "lon": lon}, udid=udid, timeout=90) or {}
        return LocationResult(active=True, lat=res.get("lat", lat), lon=res.get("lon", lon),
                              message="The iPhone now reports this location. " + LASTS)

    @tool(mcp, "Clear simulated location", kind="write", idempotent=True, open_world=True)
    async def ios_location_clear(ctx: Context, udid: Udid = None) -> LocationResult:
        """Stop simulating a location; the iPhone goes back to its real position."""
        state = get_state(ctx)
        await dcall(state, "location.clear", udid=udid, timeout=60)
        return LocationResult(active=False, message="The iPhone uses its real location again.")
