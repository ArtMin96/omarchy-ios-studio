"""Network tools: simulated network/thermal/GPU conditions and packet capture on the iPhone, through the
shared device daemon (condition.* and pcap.* in docs/deviced-protocol.md).

A condition belongs to this server's daemon client: it switches off at its timeout, on
ios_network_condition_clear, or when this MCP server exits. A capture is a daemon-side job: ios_pcap_capture
starts it and waits up to 25 s; a longer one returns capture_id, and the same tool called with capture_id waits
for it and returns the summary (no payloads) and the pcapng path.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Annotated, Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from .. import budget as B
from ..models import ActionResult, Out, iso
from ..state import get_state
from . import MAX_WAIT, READ_REMOTE, ann, dry_run, tool
from .device import Udid, dcall

USB_NEXT = ("Ask the user to connect the iPhone with a USB cable (ios_device_list shows the transport), then "
            "call again.")


def _hint(exc: ToolError) -> ToolError:
    text = str(exc)
    if "[needs-usb]" in text:
        return ToolError(text.split(" Next:")[0] + " Next: " + USB_NEXT)
    if "[unsafe-over-wifi]" in text:
        return ToolError(text + " Next: pick a milder profile from ios_network_conditions, or ask the user to plug "
                         "the iPhone in by USB.")
    if "[busy]" in text:
        return ToolError(text + " Next: another capture (maybe in the iOS Studio window) is running; wait or ask "
                         "the user to stop it.")
    return exc


async def _call(state, cmd: str, args: dict | None = None, **kw) -> Any:
    try:
        return await dcall(state, cmd, args, **kw)
    except ToolError as exc:
        raise _hint(exc) from None


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
    capture_id: str = Field(description="Pass back with capture_id= to wait for a running capture")
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


def _capture(st: dict, state_: str | None = None, note: list[str] | None = None) -> PcapCapture:
    sm = st.get("summary") or {}
    running = bool(st.get("running"))
    err = st.get("error")
    state = state_ or ("running" if running else "failed" if err else "done")
    out = PcapCapture(
        capture_id=_capture_id(st), state=state, file=st.get("file"), file_bytes=st.get("fileBytes"),
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


async def _wait_capture(state, ctx: Context | None, cid: str, seconds: float) -> dict:
    deadline = time.monotonic() + max(0.0, min(seconds, MAX_WAIT))
    st: dict = {}
    last = -1.0
    while True:
        st = await _call(state, "pcap.status", {}, timeout=20) or {}
        if _capture_id(st) != cid:
            raise ToolError(f"Capture '{cid}' isn't the current one (latest: '{_capture_id(st)}'). Captures live only "
                            "until the next one starts; start a new one with ios_pcap_capture.")
        if not st.get("running") or time.monotonic() >= deadline:
            return st
        if ctx is not None and st.get("maxSeconds"):
            pct = min(99.0, round(100 * float(st.get("seconds") or 0) / float(st["maxSeconds"]), 1))
            if pct > last:
                last = pct
                try:
                    await ctx.report_progress(pct, 100, message=f"{st.get('packets', 0)} packets")
                except Exception:  # noqa: BLE001  progress is best effort
                    pass
        await asyncio.sleep(1)


# ------------------------------------------------------------------ tools

def register(mcp: MCPServer) -> None:
    @tool(mcp, annotations=READ_REMOTE("List network conditions"))
    async def ios_network_conditions(ctx: Context, udid: Udid = None) -> ConditionList:
        """Conditions the iPhone can simulate (Developer Mode needed): slow networks (Edge, 3G, LTE, DSL, 100% loss,
        high-latency DNS...), thermal states and GPU performance states, and the one active now if any. Over Wi-Fi
        the network profiles also slow this computer's link to the phone; the worst two are refused (allowed=false).
        Apply one with ios_network_condition_set(profile, minutes)."""
        state = get_state(ctx)
        r = await _call(state, "condition.list", {}, udid=udid, timeout=60) or {}
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

    @tool(mcp, annotations=ann("Simulate network condition", read_only=False, destructive=False, idempotent=True,
                               open_world=True))
    async def ios_network_condition_set(
        ctx: Context,
        profile: Annotated[str, Field(description="Profile id from ios_network_conditions, e.g. 'SlowNetwork3G', "
                                                  "'SlowNetworkLTE', 'ThermalSerious'")],
        minutes: Annotated[int, Field(ge=1, le=30, description="Switches off by itself after this long")] = 5,
        confirm: Annotated[bool, Field(description="false = dry run that explains the effect. true only after the "
                                                   "user agreed")] = False,
        udid: Udid = None,
    ) -> ActionResult:
        """Make the whole iPhone behave as if on a slow network (or hot, or with a throttled GPU) to test how an
        app copes. Call with confirm=false first: it changes nothing and explains the effect (including the
        Wi-Fi caveat). Ask the user, then call with confirm=true. It replaces any active condition, and switches
        off after `minutes`, on ios_network_condition_clear, or when this MCP server stops."""
        state = get_state(ctx)
        r = await _call(state, "condition.list", {}, udid=udid, timeout=60) or {}
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
                        udid=udid, timeout=60) or {}
        return ActionResult(done=True, message=f"The iPhone now simulates {what} until {iso(v.get('expiresAt'))} "
                            "(or ios_network_condition_clear, or this MCP server stopping).",
                            data={"profile": profile, "expires_at": iso(v.get("expiresAt")), "transport": v.get("transport")},
                            next_steps=["Exercise the app now (ios_app_run_and_capture, ios_screenshot), then call "
                                        "ios_network_condition_clear."])

    @tool(mcp, annotations=ann("Clear network condition", read_only=False, destructive=False, idempotent=True,
                               open_world=True))
    async def ios_network_condition_clear(ctx: Context, udid: Udid = None) -> ActionResult:
        """Switch off the simulated condition (network, thermal or GPU); the iPhone behaves normally again."""
        state = get_state(ctx)
        r = await _call(state, "condition.clear", {}, udid=udid, timeout=60) or {}
        return ActionResult(done=True, message="Condition switched off; the iPhone is back to normal." if r.get("cleared")
                            else "No condition was active.")

    @tool(mcp, annotations=ann("Capture network traffic", read_only=True, destructive=False, idempotent=False,
                               open_world=True))
    async def ios_pcap_capture(
        ctx: Context,
        bundle_id: Annotated[str | None, Field(description="Only this app's traffic (bundle id, plain or XTL- form); "
                                                           "omit for the whole phone")] = None,
        seconds: Annotated[int, Field(ge=3, le=120, description="How long to capture")] = 10,
        capture_id: Annotated[str | None, Field(description="Wait for this running capture instead of starting one")] = None,
        stop: Annotated[bool, Field(description="With capture_id: stop it now and return the summary")] = False,
        wait_seconds: Annotated[int, Field(ge=0, le=MAX_WAIT, description="How long this call waits for the result")] = MAX_WAIT,
        udid: Udid = None,
    ) -> PcapCapture:
        """Record the iPhone's network traffic (needs a USB cable) and summarize it: packets and bytes per process,
        top remote hosts:ports, protocols and services (HTTPS, QUIC, DNS...). Only headers are read, never payloads.
        The full capture is saved as pcapng (file) for Wireshark. Captures of more than ~20 s come back as
        state='running' with capture_id: call again with capture_id (and wait_seconds) for the result. Reproduce the
        app's network activity while it records (ios_app_launch)."""
        state = get_state(ctx)
        if capture_id:
            cid = Path(capture_id).stem
            if stop:
                st = await _call(state, "pcap.status", {}, timeout=20) or {}
                if _capture_id(st) == cid and st.get("running"):
                    await _call(state, "pcap.stop", {}, timeout=30)
            st = await _wait_capture(state, ctx, cid, 0 if stop else wait_seconds)
        else:
            args: dict = {"maxSeconds": seconds}
            if bundle_id:
                args["bundleId"] = bundle_id
            st = await _call(state, "pcap.start", args, udid=udid, timeout=60,
                             not_found="Check the bundle id with ios_app_list(scope='own').") or {}
            st = await _wait_capture(state, ctx, _capture_id(st), wait_seconds)
        cid = _capture_id(st)
        if st.get("running"):
            left = max(0, round(float(st.get("maxSeconds") or 0) - float(st.get("seconds") or 0)))
            return _capture(st, "running", [f"Still recording (about {left} s left). Call ios_pcap_capture("
                                            f"capture_id='{cid}', wait_seconds=25) for the summary, or add stop=true "
                                            "to end it now."])
        nxt = []
        if st.get("error"):
            nxt.append("The capture failed; check the iPhone is still plugged in by USB and unlocked, then retry.")
        elif not st.get("packets"):
            nxt.append("No packets matched. Make the app use the network while recording (ios_app_launch), or omit "
                       "bundle_id to see the whole phone.")
        else:
            nxt.append(f"Full capture: {st.get('file')} (pcapng, comments name the process). The user can open it "
                       "in Wireshark from the iPhone page's Network tab.")
        return _capture(st, None, nxt)
