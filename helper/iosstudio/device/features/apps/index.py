"""AppIndex: the per-device list of installed apps, cached so the Phone page opens instantly.

Used by apps.list (apps/real.py and apps/demo.py) and by
any feature that needs "the installed app with this bundle id" (`INDEX.lookup`). Stdlib only.

* Per UDID and kind ("User" or "Any"), in memory plus a disk copy at <cache>/apps-<udid>.json, so
  the first open after a daemon restart is instant too. The memory copy follows the file's mtime,
  so a `--once` daemon that installs an app (and invalidates) is seen by the serving daemon.
* Older than TTL (60 s), forced, or soft-invalidated (reconnect, device switch): served as
  `stale` and refreshed in a background task; `on_change` runs only when the list changed.
* Hard-invalidated (app.install / app.uninstall): dropped, the next list() fetches now.
* Sizes are cached separately per UDID with SIZES_TTL (10 min).
* fetch_icons(): missing icons over 2-3 connections, reported in batched `appIcons` events.

Fetchers are `async fetch(kind, bundle_ids | None) -> {bundleId: raw installation_proxy info}`;
device_fetcher() makes one for a real phone with ReturnAttributes limited to ATTRS.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

TTL = 60.0
SIZES_TTL = 600.0
ICON_CONCURRENCY = 3
ICON_BATCH_S = 0.25
ICON_BATCH_N = 10

# The installation_proxy attributes apps.list actually reads (everything else is left on the phone).
ATTRS = ["CFBundleIdentifier", "CFBundleDisplayName", "CFBundleName", "CFBundleShortVersionString",
         "CFBundleVersion", "CFBundleExecutable", "Path", "ApplicationType", "IsRemovable", "SBAppTags",
         "SignerIdentity", "ProfileValidated", "UIFileSharingEnabled", "Entitlements"]
SIZE_ATTRS = ["CFBundleIdentifier", "StaticDiskUsage", "DynamicDiskUsage"]

Fetch = Callable[[str, "list[str] | None"], Awaitable[dict]]
OnChange = Callable[[str, str, list], Awaitable[None]]


def bundle_matches(key: str, bid: str) -> bool:
    """Whether installed bundle id `key` is `bid`; free teams install com.x.app as XTL-<TEAM>.com.x.app."""
    return bool(bid) and (key == bid or key.endswith("." + bid))


def entry(bid: str, info: dict) -> dict:
    """The cached, JSON-clean part of an App (running/pid/icon/size are added per reply)."""
    signer = info.get("SignerIdentity", "") or ""
    return {
        "bundleId": bid,
        "name": info.get("CFBundleDisplayName") or info.get("CFBundleName") or bid,
        "version": info.get("CFBundleShortVersionString", ""),
        "build": info.get("CFBundleVersion", ""),
        "developer": signer.startswith(("Apple Development", "iPhone Developer"))
        or bool(info.get("ProfileValidated") and "Development" in signer),
        "signer": signer,
        "removable": info.get("ApplicationType") == "User" or bool(info.get("IsRemovable", False)),
        "hidden": "hidden" in (info.get("SBAppTags") or []),
        "system": info.get("ApplicationType") == "System",
        "executable": info.get("CFBundleExecutable", ""),
        "path": info.get("Path", ""),
        # Files tab: App Store apps that share Documents, and your builds (whole container)
        "fileSharing": bool(info.get("UIFileSharingEnabled")),
        "debuggable": bool((info.get("Entitlements") or {}).get("get-task-allow")),
    }


def entries(raw: dict) -> list[dict]:
    out = [entry(bid, info) for bid, info in raw.items()]
    out.sort(key=lambda x: ((x["name"] or "").lower(), x["bundleId"]))
    return out


def bundle_dir(path: str) -> str:
    """'/private/var/…/X.app/X' or 'file:///var/…/X.app/' -> '/var/…/X.app' (the key both sides share)."""
    path = path.removeprefix("file://")
    if ".app/" in path:
        path = path.split(".app/")[0] + ".app"
    return path.rstrip("/").removeprefix("/private")


def compose(apps: list[dict], running: dict[str, int], sizes: dict | None,
            icon_for: Callable[[str], Path]) -> list[dict]:
    """Cached entries + running/pid (`running` = {bundle_dir: pid}), size and icon -> App objects."""
    out = []
    for e in apps:
        bid = e["bundleId"]
        pid = running.get(bundle_dir(e["path"])) if e["path"] else None
        icon = icon_for(bid)
        out.append({**e, "size": (sizes or {}).get(bid), "icon": str(icon) if icon.exists() else None,
                    "running": pid is not None, "pid": pid})
    return out


def device_fetcher(pmd3, rsd) -> Fetch:
    """A Fetch for a real phone: one installation_proxy Lookup with only ATTRS."""
    async def fetch(kind: str, bundle_ids: list[str] | None = None) -> dict:
        opts: dict[str, Any] = {"ApplicationType": kind, "ReturnAttributes": ATTRS}
        if bundle_ids is not None:
            opts["BundleIDs"] = bundle_ids
        async with pmd3.InstallationProxyService(lockdown=rsd) as ip:
            return await ip.lookup(opts) or {}
    return fetch


def size_fetcher(pmd3, rsd) -> Callable[[str], Awaitable[dict]]:
    """Sizes only (one Lookup with three attributes, not get_apps' full list plus sizes)."""
    async def fetch(kind: str) -> dict:
        async with pmd3.InstallationProxyService(lockdown=rsd) as ip:
            raw = await ip.lookup({"ApplicationType": kind, "ReturnAttributes": SIZE_ATTRS}) or {}
        return {bid: int(i.get("StaticDiskUsage") or 0) + int(i.get("DynamicDiskUsage") or 0)
                for bid, i in raw.items() if i.get("StaticDiskUsage") is not None}
    return fetch


@dataclass
class _Snap:
    apps: list[dict]
    fetched_at: float


@dataclass
class _Dev:
    lists: dict[str, _Snap] = field(default_factory=dict)   # kind -> snapshot
    mtime: int | None = None                                  # disk file mtime this copy matches
    extra: dict[str, dict] = field(default_factory=dict)     # lookup() misses fetched one by one
    sizes: dict[str, tuple[dict, float]] = field(default_factory=dict)   # kind -> (sizes, monotonic)
    dirty: set[str] = field(default_factory=set)              # soft-invalidated kinds: refresh on next list()


def _safe(s: str) -> str:
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in s)[:200]


class AppIndex:
    def __init__(self, cache_dir: Path, log: Callable[[str], None] = lambda _t: None,
                 clock: Callable[[], float] = time.time, ttl: float = TTL, sizes_ttl: float = SIZES_TTL):
        self.cache_dir = Path(cache_dir)
        self.log = log
        self.clock = clock
        self.ttl = ttl
        self.sizes_ttl = sizes_ttl
        self._devs: dict[str, _Dev] = {}
        self._tasks: dict[tuple[str, str], asyncio.Task] = {}
        self._gen: dict[str, int] = {}   # bumped by a hard invalidate: refreshes started before it are dropped

    # ------------------------------------------------------------ disk
    def path(self, udid: str) -> Path:
        return self.cache_dir / f"apps-{_safe(udid)}.json"

    def _mtime(self, udid: str) -> int | None:
        try:
            return self.path(udid).stat().st_mtime_ns
        except OSError:
            return None

    def _dev(self, udid: str) -> _Dev:
        """The in-memory copy, reloaded when another process changed (or removed) the disk file."""
        dev = self._devs.get(udid)
        m = self._mtime(udid)
        if dev is not None and dev.mtime == m:
            return dev
        fresh = _Dev(mtime=m)
        if dev is not None:      # keep what isn't on disk
            fresh.sizes, fresh.dirty = dev.sizes, dev.dirty
        if m is not None:
            try:
                data = json.loads(self.path(udid).read_text(encoding="utf-8"))
                for kind, snap in (data.get("lists") or {}).items():
                    fresh.lists[kind] = _Snap(list(snap["apps"]), float(snap["fetchedAt"]))
            except (OSError, ValueError, KeyError, TypeError) as exc:
                self.log(f"apps cache {self.path(udid)}: {exc!r}")
                fresh.lists.clear()
        self._devs[udid] = fresh
        return fresh

    def _save(self, udid: str, dev: _Dev) -> None:
        p = self.path(udid)
        data = {"version": 1, "udid": udid,
                "lists": {k: {"fetchedAt": s.fetched_at, "apps": s.apps} for k, s in dev.lists.items()}}
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, p)
            dev.mtime = self._mtime(udid)
        except OSError as exc:
            self.log(f"apps cache: can't write {p}: {exc!r}")

    # ------------------------------------------------------------ list
    async def list(self, udid: str, kind: str, fetch: Fetch, *, force: bool = False, sync: bool = False,
                   on_change: OnChange | None = None) -> tuple[list[dict], bool, float]:
        """-> (entries, stale, fetchedAt). No cache (or sync=True, e.g. --once mode): fetched now."""
        dev = self._dev(udid)
        snap = dev.lists.get(kind)
        if snap is None or sync:
            snap = await asyncio.shield(self._refresh_task(udid, kind, fetch, None))
            return snap.apps, False, snap.fetched_at
        stale = force or kind in dev.dirty or self.clock() - snap.fetched_at > self.ttl
        if stale:
            self._refresh_task(udid, kind, fetch, on_change)
        return snap.apps, stale, snap.fetched_at

    def _refresh_task(self, udid: str, kind: str, fetch: Fetch, on_change: OnChange | None) -> asyncio.Task:
        key = (udid, f"list:{kind}")
        t = self._tasks.get(key)
        if t is None or t.done():
            t = asyncio.create_task(self._refresh(udid, kind, fetch, on_change, self._gen.get(udid, 0)))
            self._tasks[key] = t
            t.add_done_callback(lambda done: self._tasks.pop(key, None) if self._tasks.get(key) is done else None)
            t.add_done_callback(self._retrieve)
        return t

    def _retrieve(self, t: asyncio.Task) -> None:
        if not t.cancelled() and t.exception() is not None:
            self.log(f"apps refresh: {t.exception()!r}")

    async def _refresh(self, udid: str, kind: str, fetch: Fetch, on_change: OnChange | None, gen: int) -> _Snap:
        raw = await fetch(kind, None)
        apps = entries(raw)
        snap = _Snap(apps, self.clock())
        if self._gen.get(udid, 0) != gen:     # an app was installed/removed meanwhile: don't cache this
            return snap
        dev = self._dev(udid)
        old = dev.lists.get(kind)
        dev.lists[kind] = snap
        dev.extra.clear()
        dev.dirty.discard(kind)
        self._save(udid, dev)
        if on_change is not None and old is not None and old.apps != apps:
            await on_change(udid, kind, apps)
        return snap

    # ------------------------------------------------------------ lookup
    def cached(self, udid: str, bundle_id: str, exact: bool = False) -> dict | None:
        """The cached entry for an installed bundle id (exact, then bundle_matches), no fetching."""
        dev = self._dev(udid)
        pools = [s.apps for s in dev.lists.values()] + [list(dev.extra.values())]
        for pool in pools:
            for e in pool:
                if e["bundleId"] == bundle_id:
                    return e
        if exact:
            return None
        for pool in pools:
            for e in pool:
                if bundle_matches(e["bundleId"], bundle_id):
                    return e
        return None

    async def lookup(self, udid: str, bundle_id: str, fetch: Fetch | None = None) -> dict | None:
        """Entry for `bundle_id`: cached exact match, else one cheap Lookup of just that bundle (if
        `fetch`), else a cached bundle_matches() match (XTL-<TEAM>.<id>)."""
        hit = self.cached(udid, bundle_id, exact=True)
        if hit is not None:
            return hit
        if fetch is not None:
            info = (await fetch("Any", [bundle_id])).get(bundle_id)
            if info:
                e = entry(bundle_id, info)
                self._dev(udid).extra[bundle_id] = e
                return e
        return self.cached(udid, bundle_id)

    # ------------------------------------------------------------ sizes
    def sizes(self, udid: str, kind: str) -> dict | None:
        """Cached sizes younger than SIZES_TTL ("Any" also covers "User"), else None."""
        dev = self._devs.get(udid)
        if dev is None:
            return None
        for k in (kind, "Any"):
            hit = dev.sizes.get(k)
            if hit and self.clock() - hit[1] <= self.sizes_ttl:
                return hit[0]
        return None

    def put_sizes(self, udid: str, kind: str, sizes: dict) -> None:
        self._dev(udid).sizes[kind] = (dict(sizes), self.clock())

    def start(self, udid: str, name: str, coro) -> bool:
        """Run a background job (icons, sizes) for this device unless one by that name runs."""
        key = (udid, name)
        t = self._tasks.get(key)
        if t is not None and not t.done():
            coro.close()
            return False
        t = asyncio.create_task(coro)
        self._tasks[key] = t
        t.add_done_callback(lambda done: self._tasks.pop(key, None) if self._tasks.get(key) is done else None)
        t.add_done_callback(self._retrieve)
        return True

    # ------------------------------------------------------------ invalidation
    def invalidate(self, udid: str, hard: bool = True) -> None:
        """hard (an app was installed/removed): forget the list, the next list() fetches now.
        soft (reconnect, device switch): keep serving it, but as stale, refreshed in the background."""
        if not hard:
            dev = self._dev(udid)
            dev.dirty = set(dev.lists)
            return
        self._gen[udid] = self._gen.get(udid, 0) + 1
        for key in [k for k in self._tasks if k[0] == udid and k[1].startswith("list:")]:
            self._tasks.pop(key)      # still running, but its result is dropped; the next list() fetches anew
        self._devs.pop(udid, None)
        with contextlib.suppress(OSError):
            self.path(udid).unlink()

    async def idle(self) -> None:
        """Wait for the background jobs running now (tests, shutdown)."""
        await asyncio.gather(*list(self._tasks.values()), return_exceptions=True)

    def cancel(self, udid: str | None = None) -> None:
        for (u, _), t in list(self._tasks.items()):
            if udid is None or u == udid:
                t.cancel()


# ====================================================================== icons

class IconBatch:
    """Collects fetched icons and emits them as one `appIcons {udid, icons}` event at most every
    `interval` seconds (or as soon as `size` are waiting)."""

    def __init__(self, udid: str, emit: Callable[[str, dict], None], interval: float = ICON_BATCH_S,
                 size: int = ICON_BATCH_N):
        self.udid = udid
        self.emit = emit
        self.interval = interval
        self.size = size
        self.pending: dict[str, str] = {}
        self._last = 0.0
        self._timer: asyncio.TimerHandle | None = None

    def add(self, bundle_id: str, path: str) -> None:
        self.pending[bundle_id] = path
        wait = self._last + self.interval - time.monotonic()
        if len(self.pending) >= self.size or wait <= 0:
            self.flush()
        elif self._timer is None:
            self._timer = asyncio.get_running_loop().call_later(wait, self.flush)

    def flush(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if not self.pending:
            return
        icons, self.pending = self.pending, {}
        self._last = time.monotonic()
        self.emit("appIcons", {"udid": self.udid, "icons": icons})


async def fetch_icons(udid: str, bundle_ids: list[str], open_service, path_for: Callable[[str], Path],
                      write: Callable[[Path, bytes], None], emit: Callable[[str, dict], None], *,
                      fatal: Callable[[BaseException], bool] = lambda _e: False,
                      concurrency: int = ICON_CONCURRENCY, log: Callable[[str], None] = lambda _t: None,
                      interval: float = ICON_BATCH_S, size: int = ICON_BATCH_N) -> int:
    """Fetch icons that aren't on disk yet with `concurrency` workers, each on its own connection
    (`open_service()` -> async context manager whose `fetch(bundle_id)` returns PNG bytes or None).
    A `fatal` error (connection gone) stops all workers. Returns how many icons were written."""
    todo: asyncio.Queue[str] = asyncio.Queue()
    for bid in bundle_ids:
        if not path_for(bid).exists():
            todo.put_nowait(bid)
    if todo.empty():
        return 0
    batch = IconBatch(udid, emit, interval, size)
    done = 0

    async def worker():
        nonlocal done
        async with open_service() as svc:
            while True:
                try:
                    bid = todo.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    data = await asyncio.wait_for(svc.fetch(bid), 10)
                except asyncio.CancelledError:
                    raise
                except BaseException as exc:  # noqa: BLE001
                    if fatal(exc):
                        raise
                    continue
                if not data:
                    continue
                path = path_for(bid)
                await asyncio.to_thread(write, path, data)
                done += 1
                batch.add(bid, str(path))

    workers = [asyncio.create_task(worker()) for _ in range(max(1, min(concurrency, todo.qsize())))]
    try:
        finished, _ = await asyncio.wait(workers, return_when=asyncio.FIRST_EXCEPTION)
        for w in finished:
            if not w.cancelled() and w.exception() is not None:
                log(f"icons: {w.exception()!r}")
    finally:
        for w in workers:
            w.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        batch.flush()
    return done


# ====================================================================== the daemon's index

def _default_index() -> AppIndex:
    from iosstudio.core import appdirs
    return AppIndex(appdirs.CACHE_DIR)


INDEX = _default_index()


def register(reg):
    """Hooks for the real daemon: reconnect / device switch mark the list stale, shutdown stops jobs."""
    @reg.on_tunnel_drop
    def _drop(d, s):
        INDEX.invalidate(s.udid, hard=False)

    @reg.on_session_end
    def _end(d, s):
        INDEX.cancel(s.udid)
        INDEX.invalidate(s.udid, hard=False)

    @reg.on_shutdown
    def _shutdown(d):
        INDEX.cancel()
