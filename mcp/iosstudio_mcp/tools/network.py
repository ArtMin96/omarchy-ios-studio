"""Network tools: simulated network/thermal/GPU conditions and packet capture on the iPhone, through the
shared device daemon (condition.* and pcap.* in docs/deviced-protocol.md).

A condition belongs to this server's daemon client: it switches off at its timeout, on
ios_network_condition_clear, or when this MCP server exits. A capture runs in the daemon and is followed by a
job of this server (core/jobs.py, kind 'pcap'): ios_pcap_capture starts it and waits up to 25 s; a longer one
returns job_id, and the same tool called with job_id waits for it and returns the summary (no payloads) and the
pcapng path. ios_job_cancel ends it early (the packets so far are kept).
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Annotated, Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from ..backends.device import dcall
from ..core import budget as B
from ..core.job_status import still_running, wait_with_progress
from ..core.jobs import Job
from ..core.params import OptJobId, Udid, WaitSeconds
from ..core.registry import MAX_WAIT, dry_run, tool
from ..models import ActionResult, Out, iso
from ..state import get_state

USB_NEXT = ("Ask the user to connect the iPhone with a USB cable (ios_device_list shows the transport), then "
            "call again.")


# This feature's next steps, in place of the generic ones (core/errors.py).
HINTS = {
    "needs-usb": USB_NEXT,
    "unsafe-over-wifi": "pick a milder profile from ios_network_conditions, or ask the user to plug the iPhone in "
                        "by USB.",
    "busy": "another capture (maybe in the iOS Studio window) is running; wait or ask the user to stop it.",
}


async def _call(state, cmd: str, args: dict | None = None, *, hints: dict[str, str] | None = None, **kw) -> Any:
    return await dcall(state, cmd, args, hints={**HINTS, **(hints or {})}, **kw)


# ------------------------------------------------------------------ models

class ConditionProfile(Out):
    id: str
    name: str
    effect: str = Field(description="Bandwidth/latency/loss, or what the state does")
    wifi: str | None = Field(None, description="Only while the iPhone is on Wi-Fi: 'warn' (also slows this "
                                               "computer's link to the phone) or 'blocked' (refused over Wi-Fi)")
    allowed: bool = Field(True, description="Can be applied over the current connection")


class ConditionGroup(Out):
    id: str
    name: str
    kind: str = Field(description="network | thermal | gpu | other")
    profiles: list[ConditionProfile]


class ActiveCondition(Out):
    profile: str
    name: str
    group: str
    expires_at: str | None = None
    remaining_seconds: int | None = None
    set_by: str | None = None


class ConditionList(Out):
    transport: str | None = Field(None, description="usb | wifi: Wi-Fi limits the network profiles")
    active: ActiveCondition | None = None
    groups: list[ConditionGroup]
    note: str


class CaptureProcess(Out):
    process: str
    packets: int
    bytes: int


class CaptureHost(Out):
    host: str
    port: int | None = None
    protocol: str
    service: str | None = None
    packets: int
    bytes: int


class PcapCapture(Out):
    job_id: str = Field(description="The capture's job: pass back as job_id to wait for it")
    state: str = Field(description="running | done | failed")
    file: str | None = Field(None, description="The pcapng file (open in Wireshark); packet comments name the process")
    file_bytes: int | None = None
    filter: dict[str, Any] = Field(default_factory=dict)
    seconds: float | None = None
    packets: int = 0
    bytes: int = 0
    sent_bytes: int | None = None
    received_bytes: int | None = None
    stop_reason: str | None = Field(None, description="time | size | stopped | client-gone | tunnel-lost | shutdown | error")
    by_process: list[CaptureProcess] = Field(default_factory=list)
    top_hosts: list[CaptureHost] = Field(default_factory=list, description="Remote IP:port by packets (headers only)")
    protocols: dict[str, int] = Field(default_factory=dict)
    services: dict[str, int] = Field(default_factory=dict, description="By well-known port, e.g. HTTPS, QUIC, DNS")
    excluded_tunnel_packets: int = 0
    error: str | None = None
    next_steps: list[str] = Field(default_factory=list)


# ------------------------------------------------------------------ helpers

def _active(v: dict | None) -> ActiveCondition | None:
    if not v or not v.get("active"):
        return None
    return ActiveCondition(profile=v.get("profile", ""), name=v.get("name", ""), group=v.get("groupName") or v.get("group", ""),
                           expires_at=iso(v.get("expiresAt")), remaining_seconds=v.get("remainingS"),
                           set_by=v.get("setBy"))


def _capture_id(st: dict) -> str:
    return Path(str(st.get("file") or "capture")).stem


def _capture(job_id: str, st: dict, state_: str | None = None, note: list[str] | None = None) -> PcapCapture:
    sm = st.get("summary") or {}
    running = bool(st.get("running"))
    err = st.get("error")
    state = state_ or ("running" if running else "failed" if err else "done")
    out = PcapCapture(
        job_id=job_id, state=state, file=st.get("file"), file_bytes=st.get("fileBytes"),
        filter={k: v for k, v in (st.get("filter") or {}).items() if v}, seconds=st.get("seconds"),
        packets=int(st.get("packets") or 0), bytes=int(st.get("bytes") or 0),
        sent_bytes=sm.get("sentBytes"), received_bytes=sm.get("receivedBytes"), stop_reason=st.get("stopReason"),
        by_process=[CaptureProcess(**p) for p in (sm.get("byProcess") or st.get("byProcess") or [])[:10]],
        top_hosts=[CaptureHost(**h) for h in (sm.get("topHosts") or [])[:10]],
        protocols=dict(list((sm.get("protocols") or {}).items())[:8]),
        services=dict(list((sm.get("services") or {}).items())[:10]),
        excluded_tunnel_packets=int(sm.get("excludedTunnelPackets") or st.get("excluded") or 0),
        error=B.clip_error(str(err.get("message") if isinstance(err, dict) else err)) if err else None,
        next_steps=note or [])
    return out


async def _follow(state, job: Job, started: dict) -> dict:
    """The job's body: poll pcap.status once a second until the capture ends; its last status is the result."""
    cid = _capture_id(started)
    job.data["status"] = started
    limit = time.monotonic() + float(started.get("maxSeconds") or 120) + 60
    while True:
        st = await _call(state, "pcap.status", {}, timeout=20) or {}
        if _capture_id(st) != cid:
            raise ToolError(f"Capture '{cid}' isn't the current one (latest: '{_capture_id(st)}'): a newer capture "
                            "replaced it. Start a new one with ios_pcap_capture.")
        job.data["status"] = st
        if not st.get("running"):
            return st
        if time.monotonic() > limit:     # overdue: ask the daemon to stop and save it
            await _call(state, "pcap.stop", {}, timeout=30)
        if st.get("maxSeconds"):
            job.report(min(0.99, float(st.get("seconds") or 0) / float(st["maxSeconds"])),
                       f"{st.get('packets', 0)} packets")
        await asyncio.sleep(1)


# ------------------------------------------------------------------ tools

def register(mcp: MCPServer) -> None:
    @tool(mcp, "List network conditions", kind="read_remote")
    async def ios_network_conditions(ctx: Context, udid: Udid = None) -> ConditionList:
        """Conditions the iPhone can simulate (Developer Mode needed): slow networks, thermal and GPU states,
        and the active one. Over Wi-Fi they also slow this computer's link, so the worst are refused
        (allowed=false). Apply one with ios_network_condition_set."""
        state = get_state(ctx)
        r = await _call(state, "condition.list", {}, udid=udid, timeout=MAX_WAIT) or {}
        groups = []
        wifi = r.get("transport") == "wifi"
        for g in r.get("groups") or []:
            profs = [ConditionProfile(id=p["id"], name=p["name"], effect=B.clip(p.get("summary") or "", 160),
                                      wifi=p.get("wifi") if wifi and p.get("wifi") != "ok" else None,
                                      allowed=bool(p.get("allowed", True)))
                     for p in g.get("profiles") or []]
            groups.append(ConditionGroup(id=g["id"], name=g["name"], kind=g.get("kind") or "other", profiles=profs))
        note = ("Only one condition is active at a time; setting another replaces it. It applies to the whole phone "
                "and switches off by itself after its time limit (default 5 minutes, max 30).")
        if wifi:
            note += " The iPhone is on Wi-Fi: network profiles also slow down iOS Studio's own connection to it."
        return ConditionList(transport=r.get("transport"), active=_active(r.get("active")), groups=groups, note=note)

    @tool(mcp, "Simulate network condition", kind="write", idempotent=True, open_world=True)
    async def ios_network_condition_set(
        ctx: Context,
        profile: Annotated[str, Field(description="Profile id from ios_network_conditions, e.g. 'SlowNetwork3G'")],
        minutes: Annotated[int, Field(ge=1, le=30, description="Switches off by itself after this long")] = 5,
        confirm: Annotated[bool, Field(description="false = dry run; true after the user agreed")] = False,
        udid: Udid = None,
    ) -> ActionResult:
        """Make the whole iPhone act as if on a slow network (or hot, or GPU-throttled) to test an app. A dry
        run that explains the effect unless confirm=true (ask the user first). Replaces any active condition;
        ends after `minutes`, on ios_network_condition_clear, or when this server stops."""
        state = get_state(ctx)
        r = await _call(state, "condition.list", {}, udid=udid, timeout=MAX_WAIT) or {}
        hit = None
        for g in r.get("groups") or []:
            for p in g.get("profiles") or []:
                if p.get("id") == profile:
                    hit = (g, p)
        if hit is None:
            ids = ", ".join(p["id"] for g in r.get("groups") or [] for p in g.get("profiles") or [])
            raise ToolError(f"No condition profile '{profile}'. Available: {B.clip(ids, 600)}.")
        g, p = hit
        if not p.get("allowed", True):
            raise ToolError(f"{p['name']} is refused over Wi-Fi: {p.get('note')} [unsafe-over-wifi] Next: pick a "
                            "milder profile, or ask the user to plug the iPhone in by USB.")
        what = f"{g['name']}: {p['name']} ({p.get('summary') or 'see ios_network_conditions'})"
        if not confirm:
            would = [f"Apply {what} to the whole iPhone, every app included",
                     f"Switch it off automatically after {minutes} minute(s)"]
            cur = _active(r.get("active"))
            if cur:
                would.append(f"Replace the active condition {cur.name}")
            if p.get("note"):
                would.append(p["note"])
            return dry_run(f"This would simulate {what} for {minutes} minute(s).", would,
                           {"profile": profile, "minutes": minutes, "transport": r.get("transport")})
        v = await _call(state, "condition.set", {"profile": profile, "timeoutS": minutes * 60, "acceptWifiRisk": True},
                        udid=udid, timeout=MAX_WAIT) or {}
        return ActionResult(done=True, message=f"The iPhone now simulates {what} until {iso(v.get('expiresAt'))} "
                            "(or ios_network_condition_clear, or this MCP server stopping).",
                            data={"profile": profile, "expires_at": iso(v.get("expiresAt")), "transport": v.get("transport")},
                            next_steps=["Exercise the app now (ios_app_run_and_capture, ios_screenshot), then call "
                                        "ios_network_condition_clear."])

    @tool(mcp, "Clear network condition", kind="write", idempotent=True, open_world=True)
    async def ios_network_condition_clear(ctx: Context, udid: Udid = None) -> ActionResult:
        """Switch off the simulated condition (network, thermal or GPU); the iPhone behaves normally again."""
        state = get_state(ctx)
        r = await _call(state, "condition.clear", {}, udid=udid, timeout=MAX_WAIT) or {}
        return ActionResult(done=True, message="Condition switched off; the iPhone is back to normal." if r.get("cleared")
                            else "No condition was active.")

    @tool(mcp, "Capture network traffic", kind="read_remote", idempotent=False)
    async def ios_pcap_capture(
        ctx: Context,
        bundle_id: Annotated[str | None, Field(description="Only this app's traffic (default: the whole phone)")] = None,
        seconds: Annotated[int, Field(ge=3, le=120, description="How long to capture")] = 10,
        job_id: OptJobId = None,
        wait_seconds: WaitSeconds = MAX_WAIT,
        udid: Udid = None,
    ) -> PcapCapture:
        """Record the iPhone's network traffic over USB and summarize headers only: bytes per process, top
        hosts:ports, protocols; the pcapng file is kept for Wireshark. Long captures: state='running' + job_id,
        call again with job_id (ios_job_cancel ends it early, keeping the capture)."""
        state = get_state(ctx)
        if job_id:
            job = state.jobs.get(job_id, "pcap")
        else:
            args: dict = {"maxSeconds": seconds}
            if bundle_id:
                args["bundleId"] = bundle_id
            started = await _call(state, "pcap.start", args, udid=udid, timeout=MAX_WAIT,
                                  hints={"not-found": "Check the bundle id with ios_app_list(scope='own')."}) or {}

            async def stop(j: Job) -> None:
                await _call(state, "pcap.stop", {}, timeout=30)

            job = state.jobs.run(lambda j: _follow(state, j, started), "pcap", title=f"Capture traffic ({seconds} s)",
                                 tool="ios_pcap_capture", data={"status": started}, on_cancel=stop)
        await wait_with_progress(state, job, wait_seconds, ctx)
        st = job.result if isinstance(job.result, dict) else job.data.get("status") or {}
        if not job.finished:
            left = max(0, round(float(st.get("maxSeconds") or 0) - float(st.get("seconds") or 0)))
            return _capture(job.id, st, "running", [f"Still recording (about {left} s left). " + still_running(job)])
        if not isinstance(job.result, dict):     # failed, or cancelled before the daemon saved anything
            st = {**st, "running": False, "error": (job.error or {}).get("message") or job.state}
        nxt = []
        if st.get("error"):
            nxt.append("The capture failed; check the iPhone is still plugged in by USB and unlocked, then retry.")
        elif not st.get("packets"):
            nxt.append("No packets matched. Make the app use the network while recording (ios_app_launch), or omit "
                       "bundle_id to see the whole phone.")
        else:
            nxt.append(f"Full capture: {st.get('file')} (pcapng, comments name the process). The user can open it "
                       "in Wireshark from the iPhone page's Network tab.")
        return _capture(job.id, st, None, nxt)
