"""condition.list, condition.set, condition.clear, condition.status (DVT condition inducer).

A condition (slow network, thermal state, GPU state) changes the whole phone. It is held as a
d.resources resource on a DVT connection of its own (s.dedicated_dvt()), scope "tunnel", always with
a timeout, so it switches off on condition.clear, the timeout, the setting client disconnecting,
a tunnel drop, a device switch and daemon shutdown. The phone also resets it by itself when that DVT
connection closes. After a tunnel drop the daemon re-lists the conditions once it's reconnected and
clears anything still active.

Over Wi-Fi a network condition also throttles this computer's own link to the phone: the profiles
that cut it (100% loss, very bad network) are refused, the others need `acceptWifiRisk`.
Protocol: docs/deviced-protocol.md#network-conditions.
"""
from __future__ import annotations

import asyncio
import contextlib
import time

from pymobiledevice3.services.dvt.instruments.condition_inducer import ConditionInducer

import device_network as net
from deviced_core import dlog, emit, err

KEY = "condition"
CLEANUP_WAIT_S = 300   # how long the post-reconnect cleanup waits for the tunnel to come back


class Held:
    """What the resource handle holds: the dedicated DVT connection and the inducer channel."""

    def __init__(self, stack: contextlib.AsyncExitStack, ci):
        self.stack = stack
        self.ci = ci


class Conditions:
    def __init__(self):
        self.state: dict | None = None     # the active condition (as the `condition` event reports it)
        self.replacing = False
        self.lock = asyncio.Lock()

    # -- phone access
    @staticmethod
    async def phone_list(d, s) -> list:
        """The phone's raw list. Uses the held channel while a condition is on, so no other inducer
        channel opens and closes next to it; otherwise a short-lived channel on the shared DVT."""
        held = d.resources.handle(KEY)
        if held is not None:
            return await asyncio.wait_for(held.ci.list(), 15)

        async def go():
            async with s.dvt_channel(ConditionInducer) as ci:
                return await asyncio.wait_for(ci.list(), 15)
        return await s.dvt_retry(go, err("failed", "Couldn't read the condition list from your iPhone."))

    @staticmethod
    def phone_active(raw: list) -> list[tuple[str, str]]:
        return [(str(g.get("identifier")), str(g.get("activeProfile") or "")) for g in raw or []
                if isinstance(g, dict) and g.get("isActive")]

    # -- events
    def view(self) -> dict:
        if not self.state:
            return {"active": False}
        out = dict(self.state)
        out["remainingS"] = max(0, round(out.get("expiresAt", 0) - time.time()))
        return out

    def announce(self, reason: str, udid: str | None, ended: dict | None = None) -> None:
        """`condition` event; after it ended, it still names the condition that was on."""
        data = {**ended, "active": False, "remainingS": 0, "reason": reason} if ended else {**self.view(), "reason": reason}
        emit("condition", data)
        if udid:
            emit("deviceInfo", {"udid": udid, "condition": self.state})

    # -- resource
    async def close(self, handle: Held, reason: str, s) -> None:
        """Resources close_fn: switch it off on the phone (fire and forget), check, close the connection."""
        verified = None
        try:
            # On a tunnel drop the phone can't be reached; it resets the condition by itself when the
            # connection is gone, and after_reconnect() double-checks.
            if reason not in ("tunnel", "error"):
                with contextlib.suppress(BaseException):
                    await asyncio.wait_for(handle.ci.clear(), 2)

                async def check():
                    for _ in range(4):
                        await asyncio.sleep(0.4)
                        if not self.phone_active(await handle.ci.list()):
                            return True
                    return False
                with contextlib.suppress(BaseException):
                    verified = await asyncio.wait_for(check(), 3)
        finally:
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(handle.stack.aclose(), 3)
            if verified is False:
                dlog("condition: the phone still reported a condition after clearing; closing its connection reset it")
            state, self.state = self.state, None
            if not self.replacing and state:
                self.announce(net.reason_for(reason), s.udid, ended=state)

    async def after_reconnect(self, d, s) -> None:
        """After a tunnel drop: once connected again, clear any condition the phone still reports."""
        try:
            await asyncio.wait_for(s.connected.wait(), CLEANUP_WAIT_S)
            if s.closing or d.resources.held(KEY):
                return
            raw = await self.phone_list(d, s)
            active = self.phone_active(raw)
            if not active or d.resources.held(KEY):
                return
            dlog(f"condition: still active after reconnecting ({active}); clearing it")

            async def go():
                async with s.dvt_channel(ConditionInducer) as ci:
                    await ci.clear()
                    await asyncio.sleep(0.5)
                    return self.phone_active(await ci.list())
            left = await s.dvt_retry(go)
            if left:
                dlog(f"condition: the phone still reports {left} after clearing")
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001
            dlog(f"condition: cleanup after reconnect failed: {exc!r}")
        finally:
            s.data.pop("conditionCleanup", None)


def register(reg):
    c = Conditions()

    @reg.command("condition.list")
    async def c_list(d, a):
        s, _ = await d.rsd(a)
        raw = await c.phone_list(d, s)
        s.tunnel_data["conditionRaw"] = raw
        return {"groups": net.view_groups(raw, s.transport), "transport": s.transport,
                "active": c.view(), "phoneActive": [{"group": g, "profile": p} for g, p in c.phone_active(raw)],
                "defaultTimeoutS": net.DEFAULT_TIMEOUT_S, "maxTimeoutS": net.MAX_TIMEOUT_S}

    @reg.command("condition.set")
    async def c_set(d, a):
        profile = str(a.get("profile") or "").strip()
        if not profile:
            raise err("bad-args", "Pick a condition first.")
        timeout = net.clamp_timeout(a.get("timeoutS", net.DEFAULT_TIMEOUT_S))
        s, _ = await d.rsd(a)
        async with c.lock:
            raw = s.tunnel_data.get("conditionRaw")
            hit = net.find_profile(raw, profile) if raw else None
            if hit is None:
                raw = await c.phone_list(d, s)
                s.tunnel_data["conditionRaw"] = raw
                hit = net.find_profile(raw, profile)
            if hit is None:
                raise err("not-found", f"Your iPhone doesn't offer the condition {profile!r}.")
            group, prof = hit
            gid = str(group.get("identifier"))
            allowed, note = net.profile_allowed(gid, profile, s.transport)
            if not allowed:
                raise err("unsafe-over-wifi", note)
            if note and not a.get("acceptWifiRisk"):
                raise err("wifi-risk", note + " Confirm to apply it anyway.")

            client = d.client()
            # Only one condition at a time: replacing one switches the old one off first.
            if d.resources.held(KEY):
                c.replacing = True
                try:
                    await d.resources.close(KEY, "released")
                finally:
                    c.replacing = False

            async def open_():
                stack = contextlib.AsyncExitStack()
                try:
                    dvt = await stack.enter_async_context(s.dedicated_dvt())
                    ci = await stack.enter_async_context(ConditionInducer(dvt))
                    await asyncio.wait_for(ci.service.enable_condition_with_identifier_profile_identifier_(gid, profile), 15)
                    now = await asyncio.wait_for(ci.list(), 10)
                    if (gid, profile) not in c.phone_active(now):
                        dlog(f"condition: {profile} enabled but the phone lists {c.phone_active(now)}")
                except BaseException:
                    with contextlib.suppress(BaseException):
                        await asyncio.wait_for(stack.aclose(), 4)
                    raise
                return Held(stack, ci)

            await d.resources.acquire(KEY, client, open_, lambda h, why: c.close(h, why, s),
                                      scope="tunnel", timeout=timeout)
            now = time.time()
            c.state = {"active": True, "group": gid, "groupName": str(group.get("name") or gid),
                       "kind": net.group_kind(gid), "profile": profile, "name": str(prof.get("name") or profile),
                       "summary": net.short_summary(net.parse_details(prof.get("description", "")),
                                                    prof.get("description", "")),
                       "setAt": now, "expiresAt": now + timeout, "timeoutS": timeout,
                       "setBy": getattr(client, "name", None) or "background", "transport": s.transport}
            s.data["conditionUsed"] = True   # a later tunnel drop checks the phone once reconnected
            c.announce("set", s.udid)
            return c.view()

    @reg.command("condition.clear")
    async def c_clear(d, a):
        s = d.need_session(a)
        if await d.resources.close(KEY, "released"):
            return {"active": False, "cleared": True}
        # Nothing held by us: still make sure the phone has nothing on (e.g. left over after a crash).
        if s.rsd is not None and a.get("force", True):
            raw = await c.phone_list(d, s)
            if c.phone_active(raw):
                async def go():
                    async with s.dvt_channel(ConditionInducer) as ci:
                        await ci.clear()
                await s.dvt_retry(go)
                return {"active": False, "cleared": True}
        return {"active": False, "cleared": False}

    @reg.command("condition.status")
    async def c_status(d, a):
        out = c.view()
        if a.get("verify"):
            s, _ = await d.rsd(a)
            raw = await c.phone_list(d, s)
            out["phoneActive"] = [{"group": g, "profile": p} for g, p in c.phone_active(raw)]
        return out

    @reg.on_tunnel_drop
    def dropped(d, s):
        # The resource is released already (scope "tunnel"); check the phone once we're back.
        if not s.closing and "conditionCleanup" not in s.data and s.data.get("conditionUsed"):
            s.data["conditionCleanup"] = asyncio.create_task(c.after_reconnect(d, s))

    @reg.on_shutdown
    async def shutdown(d):
        t = (d.session.data.pop("conditionCleanup", None) if d.session else None)
        if t:
            t.cancel()

    @reg.info
    def info(d, s):
        return {"condition": c.state}

    @reg.welcome
    def snapshot(d):
        return [("condition", {**c.view(), "reason": "set"})] if c.state else []
