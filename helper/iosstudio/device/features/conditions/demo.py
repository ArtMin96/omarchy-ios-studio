"""Demo: condition.list, condition.set, condition.clear, condition.status, plus demo.transport.

Same rules as the real daemon (helper/iosstudio/device/features/conditions.py): one condition at a time, held
as a resource with a timeout, released on clear, timeout, client exit and shutdown, and the Wi-Fi
safety rules. `demo.transport {transport: "usb"|"wifi"}` plugs the made-up iPhone's cable in or out,
so both the USB and the Wi-Fi states of the Network tab can be tried.
"""
from __future__ import annotations

import time

from iosstudio.core.output import HelperError
from iosstudio.device import network as net
from iosstudio.device.demo.core import DEVICE, INFO, UDID, event

KEY = "condition"


def _p(pid: str, name: str, desc: str) -> dict:
    return {"identifier": pid, "name": name, "description": desc}


def _net(pid: str, name: str, title: str, down: str, up: str, lat: int, loss: str) -> dict:
    return _p(pid, name, f"Name: {title}\nDownlink Bandwidth: {down} Mbps\nDownlink Latency: {lat} ms\n"
                         f"Downlink Packet Loss Ratio: {loss}\nUplink Bandwidth: {up} Mbps\nUplink Latency: {lat} ms\n"
                         f"Uplink Packet Loss Ratio: {loss}")


# The made-up phone's list, shaped like availableConditionInducers.
GROUPS = [
    {"identifier": "SlowNetworkCondition", "name": "Network Link", "isActive": False, "activeProfile": "",
     "isDestructive": False, "isInternal": False, "profiles": [
         _net("SlowNetwork100PctLoss", "100% packet loss", "100% Loss Scenario", "0", "0", 0, "100%"),
         _net("SlowNetworkVeryBadNetwork", "Very poor network", "Very Bad Network Scenario", "1", "1", 500, "10%"),
         _net("SlowNetworkEdgeBad", "Edge Network - poor", "Bad EDGE Scenario", "0.24", "0.2", 400, "1%"),
         _net("SlowNetworkEdge", "Edge Network - best", "Nominal EDGE Scenario", "0.24", "0.2", 400, "0%"),
         _net("SlowNetwork2GUrban", "2G Network - better", "2G Urban Scenario", "0.035", "0.032", 650, "0%"),
         _net("SlowNetwork3GAverage", "3G Network - average", "Average 3G Scenario", "0.78", "0.33", 100, "0.025%"),
         _net("SlowNetwork3G", "3G Network - best", "Nominal 3G Scenario", "0.78", "0.33", 100, "0%"),
         _net("SlowNetworkLTE", "LTE Network", "Nominal LTE Scenario", "50", "10", 50, "0%"),
         _net("SlowNetworkWiFi", "WiFi Network", "Nominal Wi-Fi Scenario", "40", "33", 1, "0%"),
         _net("SlowNetworkDSL", "DSL Network", "Digital Subscriber Line Scenario", "2", "0.256", 5, "0.05%"),
         _p("SlowNetworkHighLatencyDNS", "High Latency DNS", "Name: High Latency DNS Scenario\nDNS Delay: 3000 ms"),
     ]},
    {"identifier": "ThermalCondition", "name": "Thermal State", "isActive": False, "activeProfile": "",
     "isDestructive": False, "isInternal": False, "profiles": [
         _p("ThermalFair", "Fair", "The system behaves as though under slightly elevated thermal state"),
         _p("ThermalSerious", "Serious", "The system behaves as though under a high thermal state. The app should "
                                         "reduce its use of the CPU, GPU and I/O."),
         _p("ThermalCritical", "Critical", "The system behaves as though there is significant thermal impact. The app "
                                           "should reduce CPU, GPU and I/O to the minimum for user interaction."),
     ]},
    {"identifier": "GPUPerformanceState", "name": "GPU Performance State", "isActive": False, "activeProfile": "",
     "isDestructive": False, "isInternal": False, "profiles": [
         _p("GPUPerformanceStateMin", "Minimum", "GPU workloads run at the minimum performance state."),
         _p("GPUPerformanceStateMid", "Medium", "GPU workloads try to run at a medium performance state."),
         _p("GPUPerformanceStateMax", "Maximum", "GPU workloads try to run at the maximum performance state."),
     ]},
]


def transport() -> str:
    return DEVICE.get("transport") or "wifi"


class State:
    def __init__(self):
        self.cur: dict | None = None
        self.replacing = False

    def view(self) -> dict:
        if not self.cur:
            return {"active": False}
        return {**self.cur, "remainingS": max(0, round(self.cur["expiresAt"] - time.time()))}

    def phone(self, gid: str = "", pid: str = "") -> None:
        for g in GROUPS:
            on = g["identifier"] == gid
            g["isActive"], g["activeProfile"] = on, (pid if on else "")

    def close(self, _h, why: str) -> None:
        ended, self.cur = self.cur, None
        self.phone()
        INFO["condition"] = None
        if ended and not self.replacing:
            event("condition", {**ended, "active": False, "remainingS": 0, "reason": net.reason_for(why)})
            event("deviceInfo", {"udid": UDID, "condition": None})


def register(reg):
    st = State()

    @reg.command("condition.list")
    def c_list(ctx, a):
        active = [{"group": g["identifier"], "profile": g["activeProfile"]} for g in GROUPS if g["isActive"]]
        return {"groups": net.view_groups(GROUPS, transport()), "transport": transport(), "active": st.view(),
                "phoneActive": active, "defaultTimeoutS": net.DEFAULT_TIMEOUT_S, "maxTimeoutS": net.MAX_TIMEOUT_S}

    @reg.command("condition.set")
    async def c_set(ctx, a):
        profile = str(a.get("profile") or "").strip()
        if not profile:
            raise HelperError("bad-args", "Pick a condition first.")
        hit = net.find_profile(GROUPS, profile)
        if hit is None:
            raise HelperError("not-found", f"Your iPhone doesn't offer the condition {profile!r}.")
        group, prof = hit
        gid = group["identifier"]
        allowed, note = net.profile_allowed(gid, profile, transport())
        if not allowed:
            raise HelperError("unsafe-over-wifi", note)
        if note and not a.get("acceptWifiRisk"):
            raise HelperError("wifi-risk", note + " Confirm to apply it anyway.")
        timeout = net.clamp_timeout(a.get("timeoutS", net.DEFAULT_TIMEOUT_S))
        if ctx.resources.held(KEY):
            st.replacing = True
            try:
                await ctx.resources.close(KEY, "released")
            finally:
                st.replacing = False
        client = ctx.client()
        await ctx.resources.acquire(KEY, client, lambda: st, st.close, scope="tunnel", timeout=timeout)
        now = time.time()
        st.phone(gid, profile)
        st.cur = {"active": True, "group": gid, "groupName": group["name"], "kind": net.group_kind(gid),
                  "profile": profile, "name": prof["name"],
                  "summary": net.short_summary(net.parse_details(prof["description"]), prof["description"]),
                  "setAt": now, "expiresAt": now + timeout, "timeoutS": timeout,
                  "setBy": getattr(client, "name", None) or "background", "transport": transport()}
        INFO["condition"] = st.cur
        event("condition", {**st.view(), "reason": "set"})
        event("deviceInfo", {"udid": UDID, "condition": st.cur})
        return st.view()

    @reg.command("condition.clear")
    async def c_clear(ctx, a):
        if await ctx.resources.close(KEY, "released"):
            return {"active": False, "cleared": True}
        return {"active": False, "cleared": False}

    @reg.command("condition.status")
    def c_status(ctx, a):
        out = st.view()
        if a.get("verify"):
            out["phoneActive"] = [{"group": g["identifier"], "profile": g["activeProfile"]} for g in GROUPS if g["isActive"]]
        return out

    @reg.command("demo.transport")
    async def c_transport(ctx, a):
        """Demo only: plug the cable in (usb) or out (wifi). Like a real reconnect, it ends what the tunnel held."""
        want = "usb" if str(a.get("transport") or "").lower() == "usb" else "wifi"
        if want != transport():
            await ctx.resources.release_all("tunnel", ("tunnel",))
            DEVICE.update(transport=want, usb=want == "usb")
            INFO["transport"] = want
            INFO["address"] = "usb" if want == "usb" else "fe80::1%wlan0"
            msg = "Connected over USB" if want == "usb" else "Connected over Wi-Fi"
            event("devices", [DEVICE])
            event("connection", {"udid": UDID, "state": "connected", "transport": want, "message": msg, "detail": "",
                                 "retryIn": 0, "since": time.time()})
            event("deviceInfo", {"udid": UDID, "transport": want})
        return {"transport": want}

    @reg.welcome
    def snapshot(ctx):
        return [("condition", {**st.view(), "reason": "set"})] if st.cur else []
