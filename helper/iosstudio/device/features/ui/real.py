"""ui.tree, ui.press, ui.audit: the frontmost app's accessibility elements, pressing one, and Apple's
accessibility audit.

All three use pymobiledevice3's AccessibilityAudit, a DTX service of its own
(com.apple.accessibility.axAuditDaemon.remoteserver.shim.remote over RSD), not the shared DVT
connection. Each command opens one connection; commands for one device run one at a time.

On iOS 27 the inspector only answers for the process set with `deviceSetAuditTargetPid:`, and only
while that process is the app in front: point it at a background app and the focus never moves and
the audit finds nothing. So every command first targets the app (bundleId, or the newest running
apps in turn) and checks that the first focus event comes from that pid; an element's pid is the
first 4 bytes (little-endian) of its platform element value. That check is the `wrong-app` guard.

The tree is a focus walk (VoiceOver order, first to last). Ids ('e1', 'e2', ...) map to the elements
of the last walk on that device and go stale once the screen changes. Shaping and matching are in
helper/iosstudio/features/ui_inspect.py.
"""
from __future__ import annotations

import asyncio
import contextlib
import time

from pymobiledevice3.services.accessibilityaudit import AccessibilityAudit, Direction, deserialize_object

from iosstudio.device.features.apps.index import INDEX, bundle_dir, device_fetcher
from iosstudio.device.features.apps.lookup import exe_path, find_app
from iosstudio.device.real import core
from iosstudio.device.real.core import classify, dlog, err
from iosstudio.features import ui_inspect as U

KEY = "ui"
FOCUS_EVENT = "hostInspectorCurrentElementChanged:"
FRONT_WAIT_S = 1.0         # the first focus event after targeting the app in front
STEP_WAIT_S = 1.5          # one focus move normally answers in well under a second
STEP_RETRIES = 2           # re-send "next" this many times before giving up on a silent focus
PRESS_SETTLE_S = 0.8
PRESS_BUDGET_S = 19.0      # the whole ui.press (walk, press, walk) stays under ~20 s
AUDIT_TIMEOUT_S = 18.0
AUTO_CANDIDATES = 5        # without bundleId: newest running apps tried as "the app in front"
# attributes read for each focused element (the daemon answers only for the focused one). iOS 27.3
# answers none of Frame/AXFrame/Bounds/Rect/ActivationPoint/..., so frames come only from audit issues.
ATTRS = ("Label", "Value", "Hint", "Identifier", "Traits")
PRESS_ACTION = {
    "ObjectType": "AXAuditElementAttribute_v1",
    "Value": {"ObjectType": "passthrough", "Value": {
        "AttributeNameValue_v1": {"ObjectType": "passthrough", "Value": "AXAction-2010"},
        "DisplayAsTree_v1": {"ObjectType": "passthrough", "Value": 0},
        "HumanReadableNameValue_v1": {"ObjectType": "passthrough", "Value": "Activate"},
        "IsInternal_v1": {"ObjectType": "passthrough", "Value": 0},
        "PerformsActionValue_v1": {"ObjectType": "passthrough", "Value": 1},
        "SettableValue_v1": {"ObjectType": "passthrough", "Value": 0},
        "ValueTypeValue_v1": {"ObjectType": "passthrough", "Value": 1},
    }},
}


def _lock(s) -> asyncio.Lock:
    return s.data.setdefault("ui.lock", asyncio.Lock())


def _cache(s) -> dict:
    return s.data.setdefault(KEY, {})


def _clamp_max(a) -> int:
    try:
        n = int(a.get("max") or U.DEFAULT_MAX)
    except (TypeError, ValueError):
        raise err("bad-args", "max must be a whole number.")
    return max(1, min(n, U.HARD_MAX))


def _clamp_timeout(a) -> float:
    try:
        t = float(a.get("timeout") or U.DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        raise err("bad-args", "timeout must be a number of seconds.")
    return max(2.0, min(t, U.MAX_TIMEOUT))


AX_UNAVAILABLE = err("ax-unavailable", "Couldn't reach the iPhone's accessibility service. Make sure the iPhone "
                     "is unlocked and Developer Mode is on.")


@contextlib.asynccontextmanager
async def ax_session(s):
    async with s.open_service(AccessibilityAudit, fail=AX_UNAVAILABLE) as ax:
        try:
            yield ax
        finally:
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(ax.set_app_monitoring_enabled(False), 2)


def _drain(q: asyncio.Queue) -> None:
    while not q.empty():
        q.get_nowait()


def _focus_payload(ax, args):
    payload = ax._extract_event_payload(args)
    if payload is None:
        return None
    data = deserialize_object(payload)
    if isinstance(data, list):
        data = data[0] if data else None
    return data


async def _next_focus(ax, wait: float):
    """The next focus-changed event as plain JSON, or None after `wait` seconds."""
    loop = asyncio.get_running_loop()
    end = loop.time() + wait
    while True:
        left = end - loop.time()
        if left <= 0:
            return None
        try:
            name, args = await asyncio.wait_for(ax._event_queue.get(), left)
        except (asyncio.TimeoutError, TimeoutError):
            return None
        if name != FOCUS_EVENT:
            continue
        data = _focus_payload(ax, args)
        if data is not None:
            return U.to_plain(data)


async def target(ax, pid: int, wait: float = FRONT_WAIT_S) -> dict | None:
    """Point the inspector at `pid` and move to the first element. Returns that first focus event
    when it belongs to `pid` (the app is in front), else None."""
    _drain(ax._event_queue)
    await ax._invoke("deviceSetAuditTargetPid:", int(pid), expects_reply=False)
    await ax.set_app_monitoring_enabled(True)
    await ax.set_monitored_event_type()
    await ax.move_focus(Direction.First)
    raw = await _next_focus(ax, wait)
    if raw is None:   # the focus already sits on the first element (no change, no event): step off and back
        await ax.move_focus(Direction.Next)
        if await _next_focus(ax, wait) is not None:
            await ax.move_focus(Direction.Previous)
            raw = await _next_focus(ax, wait)
    if raw is None or U.element_pid(U.element_bytes(raw)) != int(pid):
        return None
    return raw


async def _running_apps(d, s, rsd) -> list[tuple[int, str]]:
    """(pid, bundle id) of running app processes, newest pid first. Bundle ids come from the cached
    app list; a running user app it doesn't know (installed elsewhere since) refreshes it once."""
    async with d.pmd3.AppServiceService(rsd) as svc:
        procs = await asyncio.wait_for(svc.list_processes(), 10)
    running = [(int(p["processIdentifier"]), bundle_dir(path)) for p in procs
               if ".app/" in (path := exe_path(p)) and "processIdentifier" in p]
    fetch = device_fetcher(d.pmd3, rsd)
    for sync in (core.ONCE, True):
        apps, _, _ = await INDEX.list(s.udid, "Any", fetch, sync=sync)
        by_dir = {bundle_dir(e["path"]): e["bundleId"] for e in apps if e.get("path")}
        out = [(pid, by_dir[app_dir]) for pid, app_dir in running if app_dir in by_dir]
        unknown = any(app_dir not in by_dir and "/Bundle/Application/" in app_dir for _, app_dir in running)
        if sync or not unknown:
            break
    return sorted(out, reverse=True)


async def front_app(d, s, rsd, ax, bundle_id: str | None) -> dict:
    """Target the app in front: {"bundleId", "pid", "first": first focus event}. Raises wrong-app
    (bundleId given but that app isn't in front) or no-app-in-front."""
    if bundle_id:
        from iosstudio.device.features.apps.real import app_pids  # noqa: PLC0415  (loaded by the same registry)
        try:
            app = await find_app(s.udid, bundle_id, device_fetcher(d.pmd3, rsd))
        except Exception as exc:  # noqa: BLE001
            raise classify(exc)
        if app is None:
            raise err("wrong-app", f"{bundle_id} isn't installed on this iPhone.")
        real = app["bundleId"]
        pids = await app_pids(d, s, rsd, real)
        if not pids:
            raise err("wrong-app", f"{real} isn't running. Launch it first and keep it in front.")
        for pid in sorted(pids, reverse=True):
            first = await target(ax, pid)
            if first is not None:
                return {"bundleId": real, "pid": pid, "first": first}
        raise err("wrong-app", f"{real} isn't the app in front. Bring it to the front (and unlock the iPhone), "
                  "then try again.", None, f"pids {pids}")
    tried = []
    for pid, bid in (await _running_apps(d, s, rsd))[:AUTO_CANDIDATES]:
        tried.append(bid)
        first = await target(ax, pid, wait=1.0)
        if first is not None:
            return {"bundleId": bid, "pid": pid, "first": first}
    raise err("no-app-in-front", "Couldn't find the app in front. Unlock the iPhone and open the app, or pass "
              "its bundleId.", None, f"tried {tried}")


async def _read_attrs(ax, b: bytes, names: list[str], deadline: float) -> dict:
    out = {}
    loop = asyncio.get_running_loop()
    for name in names:
        left = deadline - loop.time()
        if left <= 0:
            break
        try:
            v = await asyncio.wait_for(ax.element_attribute(b, name), min(2.0, left))
        except (asyncio.TimeoutError, TimeoutError):
            continue
        except Exception as exc:  # noqa: BLE001  one unreadable attribute isn't fatal
            dlog(f"ui: attribute {name}: {exc!r}")
            continue
        out[name] = U.to_plain(v)
    return out


async def walk(ax, app: dict, limit: int, timeout: float, raw_out: list | None = None) -> dict:
    """Focus walk of the targeted app, starting from app["first"]. Elements of other processes
    (a keyboard, an alert) are left out and counted."""
    loop = asyncio.get_running_loop()
    start = loop.time()
    deadline = start + timeout
    pid = app["pid"]
    w = U.Walk(limit)
    attrs_for: list[dict] = []
    foreign = 0
    raw = app.get("first")
    if raw is None:
        raw = await target(ax, pid, wait=min(FRONT_WAIT_S, max(0.5, deadline - loop.time())))
    stop = "no-focus"
    silent = 0
    while raw is not None or silent <= STEP_RETRIES:
        if raw is None:
            silent += 1
            if silent > STEP_RETRIES:
                break
            await ax.move_focus(Direction.Next)
        else:
            silent = 0
            b = U.element_bytes(raw)
            key = U.element_key(raw)
            if b is not None and U.element_pid(b) != pid:
                foreign += 1
                if foreign > 20:
                    stop = "foreign"
                    break
            elif b is not None and key not in w._seen and not w.full:
                attrs = await _read_attrs(ax, b, ATTRS, deadline)
                w.add(raw)
                attrs_for.append(attrs)
                if raw_out is not None:
                    raw_out.append({"focus": raw, "attrs": attrs})
                if w.full:
                    stop = "max"
                    break
            elif key in w._seen:
                stop = "loop"
                break
            await ax.move_focus(Direction.Next)
        left = deadline - loop.time()
        if left <= 0:
            stop = "timeout"
            break
        raw = await _next_focus(ax, min(STEP_WAIT_S, left))
    return {"raws": w.items, "attrs": attrs_for, "stop": stop, "foreign": foreign,
            "elapsedMs": int((loop.time() - start) * 1000)}


def _build(s, app: dict, result: dict) -> dict:
    elements, ids = [], {}
    for i, (raw, attrs) in enumerate(zip(result["raws"], result["attrs"]), start=1):
        eid = f"e{i}"
        elements.append(U.normalize_element(raw, attrs, eid))
        ids[eid] = U.element_bytes(raw)
    _cache(s).clear()
    _cache(s).update({"ids": ids, "elements": elements, "ts": time.monotonic(),
                      "bundleId": app["bundleId"], "pid": app["pid"]})
    out = {
        "elements": elements,
        "count": len(elements),
        "truncated": result["stop"] == "max",
        "complete": result["stop"] == "loop",
        "method": "focus-walk",
        "elapsedMs": result["elapsedMs"],
        "screen": None,
        "stoppedBy": result["stop"],
        "bundleId": app["bundleId"],
        "pid": app["pid"],
    }
    if result.get("foreign"):
        out["otherProcessElements"] = result["foreign"]
    return out


async def _tree(s, ax, app, a, timeout=None, raw_out=None) -> dict:
    res = await walk(ax, app, _clamp_max(a), timeout or _clamp_timeout(a), raw_out)
    return _build(s, app, res)


def _resolve(a, elements: list[dict]) -> dict:
    try:
        return U.pick_target(elements, a.get("id"), a.get("label"), a.get("match") or "exact")
    except U.TargetError as e:
        raise err(e.code, e.message, None, e.detail)


async def press_bytes(ax, b: bytes) -> None:
    """Activate (AXAction-2010) one element. pymobiledevice3 11.49's perform_press puts the element
    bytes outside PlatformElementValue_v1 and iOS 27.3 silently ignores it; this sends the same
    element archive as element_attribute does (checked on the phone: the button fires)."""
    await ax._invoke("deviceElement:performAction:withValue:", AccessibilityAudit._serialize_element(b),
                     PRESS_ACTION, 0, expects_reply=False)


def register(reg):
    @reg.command("ui.tree")
    async def c_ui_tree(d, a):
        s, rsd = await d.rsd(a)
        async with _lock(s):
            async with ax_session(s) as ax:
                app = await front_app(d, s, rsd, ax, a.get("bundleId"))
                raw_out = [] if a.get("raw") else None
                out = await _tree(s, ax, app, a, raw_out=raw_out)
                if raw_out is not None:
                    out["raw"] = raw_out
        return out

    @reg.command("ui.press")
    async def c_ui_press(d, a):
        if not a.get("id") and not a.get("label"):
            raise err("bad-args", "Which element? Pass an element id from the last screen read, or a label.")
        s, rsd = await d.rsd(a)
        t0 = time.monotonic()
        async with _lock(s):
            async with ax_session(s) as ax:
                cache = _cache(s)
                if a.get("id") and not cache.get("ids"):
                    raise err("stale-element", "Read the screen first: element ids come from the last read.")
                app = await front_app(d, s, rsd, ax, a.get("bundleId") or (cache.get("bundleId") if a.get("id") else None))
                if a.get("id"):
                    if cache.get("pid") != app["pid"]:
                        raise err("stale-element", "The app in front changed since the screen was last read. Read it again.")
                    before = cache["elements"]
                else:   # a label press walks first, so the label matches what's on screen now
                    before = (await _tree(s, ax, app, a, timeout=min(_clamp_timeout(a), 8.0)))["elements"]
                    app["first"] = None
                target_el = _resolve(a, before)
                b = _cache(s)["ids"].get(target_el["id"])
                if b is None or U.element_pid(b) != app["pid"]:
                    raise err("stale-element", "That element isn't in the app in front. Read the screen again.")
                try:
                    await asyncio.wait_for(press_bytes(ax, b), 5)
                except Exception as exc:  # noqa: BLE001
                    raise err("press-failed", "The iPhone didn't accept the press.", None, repr(exc))
                await asyncio.sleep(PRESS_SETTLE_S)
                left = PRESS_BUDGET_S - (time.monotonic() - t0)
                app["first"] = None
                after = await _tree(s, ax, app, a, timeout=max(2.0, min(_clamp_timeout(a), left)))
                changes = U.diff_trees(before, after["elements"])
        return {"pressed": target_el, "changes": changes,
                "after": {"count": after["count"], "complete": after["complete"]},
                "elapsedMs": int((time.monotonic() - t0) * 1000),
                "bundleId": app["bundleId"], "pid": app["pid"]}

    @reg.command("ui.audit")
    async def c_ui_audit(d, a):
        s, rsd = await d.rsd(a)
        types = a.get("types")
        if types is not None and not isinstance(types, list):
            raise err("bad-args", "types must be a list of audit types (e.g. ['contrast', 'hit-region']).")
        t0 = time.monotonic()
        async with _lock(s):
            async with ax_session(s) as ax:
                app = await front_app(d, s, rsd, ax, a.get("bundleId"))
                supported_raw = list(await asyncio.wait_for(ax.supported_audits_types(), 8) or [])
                run, slugs, missing = U.audit_plan(types, supported_raw)
                issues_raw, timed_out = [], False
                if run:
                    try:
                        issues_raw = await asyncio.wait_for(ax.run_audit(run), AUDIT_TIMEOUT_S)
                    except (asyncio.TimeoutError, TimeoutError):
                        timed_out = True
        cache = _cache(s)
        elements = cache.get("elements") if cache.get("pid") == app["pid"] else []
        ids = cache.get("ids") if elements else {}
        plain = [U.to_plain(i) for i in (issues_raw or [])]
        out = {
            "issues": [U.normalize_issue(p, elements or [], ids or {}) for p in plain],
            "supported": slugs,
            "unsupported": missing,
            "elapsedMs": int((time.monotonic() - t0) * 1000),
            "bundleId": app["bundleId"],
            "pid": app["pid"],
        }
        if timed_out:
            out["timedOut"] = True
        if a.get("raw"):
            out["raw"] = {"supported": U.to_plain(supported_raw), "issues": plain}
        return out

    @reg.on_tunnel_drop
    async def forget(d, s):
        s.data.pop(KEY, None)   # element references belong to the old connection's screen
