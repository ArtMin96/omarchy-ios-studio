"""App data: appdata.defaults / defaults.set / defaults.delete / defaults.refresh, appdata.stores / rows,
appdata.reset.

Your own development builds only (house_arrest VendContainer, like container.* in files.py). Every command
opens its own container connection: a cached one goes stale when the app is killed or installed and then
reads as "no file". UserDefaults is Library/Preferences/<bundle id>.plist. An edit stops the app (or refuses:
app-running), waits until the file stops changing (cfprefsd flushes the app's last writes after it exits),
changes one key and writes the file back through a temp file and a rename.

cfprefsd, the iPhone's settings service, keeps the app's settings in memory after the app exits and hands
that copy to the next launch, then writes it back over the file (checked on an iPhone: waiting 60 s or
rewriting the file in place doesn't help). Installing the app's signed build over itself makes iOS drop that
copy and keeps the container, so after a write the last build is installed again ("refresh", on by
default). Without a build the write stays in the file only, and the reply says the app may not see it and
gives launch arguments (-key value) that override it for one launch. See docs/features/app-data.md.
"""
from __future__ import annotations

import asyncio
import contextlib
import posixpath
import time

from pymobiledevice3.exceptions import AfcException, AfcFileNotFoundError, PyMobileDevice3Exception

from iosstudio.core.output import HelperError, fix
from iosstudio.device.features.apps import real as A
from iosstudio.device.features.files import core as df
from iosstudio.device.features.files import real as F
from iosstudio.device.real.core import dlog, err
from iosstudio.features import app_data as AD

BUILD_FIX = fix("Build the app", "projects", action="build")
NOT_DEV = ("Only your own apps (built and installed here) share their saved data. App Store apps keep "
           "theirs private.")
SETTLE_TRIES = 8           # stat the preferences file up to this many times, 1 s apart, until it stops changing
FLUSH_GRACE = 10.0         # seconds after a kill before the app's settings file is final
SETTLE_FIRST = 1.0         # seconds before the first comparison (a just-stopped app's settings arrive late)
WRITE_TRIES = 3            # writes of one change when the file read back shows cfprefsd overwrote it


def stop_fix(bid: str) -> dict:
    return {"label": "Stop the app", "page": "device", "tab": "apps", "app": bid, "section": "data",
            "action": "stop-app"}


def _ad(exc: AD.AppDataError):
    return err(exc.code, exc.message)


def _udid(a: dict) -> dict:
    return {"udid": a["udid"]} if a.get("udid") else {}


async def _app(d, s, a: dict) -> dict:
    bid = str(a.get("bundleId") or "")
    if not bid:
        raise err("bad-args", "Which app? (bundleId missing)")
    info = await F.app_info(d, s, bid)
    if not info["developer"]:
        raise err("not-developer", NOT_DEV)
    return info


async def _running(d, a: dict, bid: str, strict: bool = False) -> list[int]:
    """The app's pids. strict: a failed check raises (a write must never guess "not running")."""
    s, rsd = await d.rsd(_udid(a))
    try:
        return await asyncio.wait_for(A.app_pids(d, rsd, bid), 15)
    except Exception as exc:  # noqa: BLE001
        if strict:
            raise err("failed", "Couldn't check whether the app is running, so nothing was changed. Retry.",
                      None, repr(exc)) from None
        return []


def _since(s, bid: str, *keys: str) -> float | None:
    """Seconds since the newest of these events for the app (tunnel_data[key][bid] = monotonic time)."""
    ts = [s.tunnel_data.get(k, {}).get(bid) for k in keys]
    ts = [t for t in ts if t is not None]
    return time.monotonic() - max(ts) if ts else None


def _recent_stop(s, bid: str) -> bool:
    """Killed or reinstalled in the last 30 s: the container can briefly read as empty."""
    age = _since(s, bid, "app.killed", "appdata.stopped", "appdata.installed")
    return age is not None and age < 30


async def _wait_flush(s, bid: str) -> float:
    """cfprefsd saves a killed app's last settings 2 to 7 s after the kill (measured on an iPhone), not at
    the kill. Reading or writing before that would miss them, or have them land over our write. Waits until
    FLUSH_GRACE seconds have passed since the app was last stopped; returns the seconds waited."""
    age = _since(s, bid, "app.killed", "appdata.stopped")
    if age is None or age >= FLUSH_GRACE:
        return 0.0
    left = FLUSH_GRACE - age
    await asyncio.sleep(left)
    return round(left, 1)


async def _ensure_stopped(d, s, a: dict, bid: str, what: str) -> bool:
    """Raise app-running unless the app is stopped (or stopApp asks to stop it). True when we stopped it."""
    pids = await _running(d, a, bid, strict=True)
    if not pids:
        return False
    if not a.get("stopApp"):
        raise err("app-running", f"Stop the app before {what}: it keeps its own copy of this data in memory and "
                  "would write it back over the change.", stop_fix(bid))
    await d.registry.lookup("app.kill")(d, {"bundleId": bid, **_udid(a)})
    for _ in range(20):        # wait until it's really gone
        await asyncio.sleep(0.25)
        if not await _running(d, a, bid):
            break
    s.tunnel_data.setdefault("appdata.stopped", {})[bid] = time.monotonic()
    return True


async def _call(s, bid: str, fn, what: str, retry: bool = True):
    """Run fn(afc) on a fresh connection to the app's container. An AFC error is retried once on a new
    connection a second later (right after a kill or an install the container is briefly not there)."""
    for attempt in (0, 1):
        try:
            async with s.house_arrest(bid) as afc:
                return await fn(afc)
        except asyncio.CancelledError:
            raise
        except AD.AppDataError as exc:
            raise _ad(exc) from None
        except HelperError:
            raise
        except BaseException as exc:
            if retry and attempt == 0 and isinstance(exc, (AfcException, PyMobileDevice3Exception)):
                await asyncio.sleep(1.0)
                continue
            raise F.afc_error(exc, what)


async def _read(afc, path: str, limit: int) -> bytes:
    st = await afc.stat(path)
    size = int(st.get("st_size") or 0)
    if size > limit:
        raise err("too-big", f"{posixpath.basename(path)} is {size // (1 << 20)} MB, too big to read here.")
    h = await afc.fopen(path, "r")
    try:
        out = bytearray()
        while len(out) < size:
            chunk = await afc.fread(h, min(df.CHUNK, size - len(out)))
            if not chunk:
                break
            out += chunk
        return bytes(out)
    finally:
        with contextlib.suppress(BaseException):
            await asyncio.shield(afc.fclose(h))


async def _write(afc, path: str, data: bytes) -> None:
    """Write through <path>.iosstudio-new and a rename, so a dropped link never leaves half a plist."""
    if not await afc.exists(posixpath.dirname(path)):
        await afc.makedirs(posixpath.dirname(path))
    tmp = path + ".iosstudio-new"
    h = await afc.fopen(tmp, "w")
    try:
        for i in range(0, len(data), df.CHUNK):
            await afc.fwrite(h, data[i:i + df.CHUNK])
    finally:
        with contextlib.suppress(BaseException):
            await asyncio.shield(afc.fclose(h))
    try:
        await afc.rename(tmp, path)
    except AfcException:          # some iOS versions refuse to rename over a file: replace it instead
        with contextlib.suppress(AfcFileNotFoundError):
            await afc.rm(path, force=True)
        await afc.rename(tmp, path)


async def _defaults_path(afc, bid: str) -> str | None:
    try:
        names = await afc.listdir("/Library/Preferences")
    except AfcFileNotFoundError:
        return None
    return AD.pick_defaults(bid, names)


async def _settle(afc, path: str) -> None:
    """Wait until the preferences file stops changing: cfprefsd writes an app's last changes a moment
    after the app exits, and a write before that would be overwritten."""
    async def sample():
        try:
            st = await afc.stat(path)
        except AfcFileNotFoundError:
            return None
        return int(st.get("st_size") or 0), F._mtime(st)
    last = await sample()
    for i in range(SETTLE_TRIES):
        await asyncio.sleep(SETTLE_FIRST if i == 0 else 1.0)
        cur = await sample()
        if cur == last:
            return
        last = cur


async def _refresh(d, s, a: dict, bid: str) -> dict:
    """Install the app's last signed build over itself. iOS keeps the container and drops cfprefsd's cached
    copy of the app's settings, so the next launch reads the file. {refreshed, artifact?, refreshError?}"""
    build = {"artifact": a["artifact"]} if a.get("artifact") else await asyncio.to_thread(AD.find_build, bid)
    if not build:
        return {"refreshed": False, "refreshError": {"code": "no-build", "message": AD.NO_BUILD_REFRESH}}
    try:
        await d.registry.lookup("app.install")(d, {"path": build["artifact"], **_udid(a)})
    except HelperError as exc:
        return {"refreshed": False, "artifact": build["artifact"],
                "refreshError": {"code": exc.code, "message": exc.message}}
    finally:
        await F.forget_container(s, bid, False)
        s.tunnel_data.get("files.apps", {}).pop(bid, None)
        s.tunnel_data.setdefault("appdata.installed", {})[bid] = time.monotonic()
    dlog(f"appdata: reinstalled {build['artifact']} over {bid} to drop cached settings")
    return {"refreshed": True, "artifact": build["artifact"]}


async def _verify(s, bid: str, path: str, check) -> bool | None:
    """Read the file back (a few tries: right after an install the container comes back slowly)."""
    for _ in range(4):
        try:
            async def go(afc):
                return await _read(afc, path, AD.MAX_PLIST) if await afc.exists(path) else b""
            data = await _call(s, bid, go, "the app's settings")
            return bool(check(AD.load_plist(data)[0]))
        except (HelperError, AD.AppDataError):
            await asyncio.sleep(1.0)
    return None


async def _listing(afc, tops) -> tuple[list[tuple[str, int]], list[str]]:
    """(files with sizes, folders) under each existing top folder; unreadable entries are skipped."""
    files: list[tuple[str, int]] = []
    dirs: list[str] = []
    for top in tops:
        todo = [top]
        while todo and len(files) < 20000:
            cur = todo.pop()
            try:
                names = await afc.listdir(cur)
            except AfcException:
                continue
            paths = [posixpath.join(cur, n) for n in names]
            for p, st in zip(paths, await F.stat_many(afc, paths)):
                if st is None:
                    continue
                if st.get("st_ifmt") == "S_IFDIR":
                    dirs.append(p)
                    todo.append(p)
                elif st.get("st_ifmt") in ("S_IFREG", "S_IFLNK"):
                    files.append((p, int(st.get("st_size") or 0)))
    return files, dirs


def register(reg):
    locks: dict[str, asyncio.Lock] = {}

    def lock(bid: str) -> asyncio.Lock:
        return locks.setdefault(bid, asyncio.Lock())

    # ------------------------------------------------ UserDefaults
    @reg.command("appdata.defaults")
    async def c_defaults(d, a):
        s = d.need_session(a)
        info = await _app(d, s, a)
        bid = info["bundleId"]

        async def go(afc):
            path = await _defaults_path(afc, bid)
            if not path:
                return None, AD.defaults_candidates(bid)[0]
            return await _read(afc, path, AD.MAX_PLIST), path
        waited = await _wait_flush(s, bid)
        data, path = await _call(s, bid, go, "the app's settings")
        if data is None and _recent_stop(s, bid):       # just stopped: cfprefsd may not have written it yet
            await asyncio.sleep(1.5)
            data, path = await _call(s, bid, go, "the app's settings")
        running = bool(await _running(d, a, bid))
        if data is None:
            return {"bundleId": bid, "path": path, "exists": False, "running": running, "keys": 0, "values": {},
                    "types": {}, "truncated": False, "size": 0, **({"waited": waited} if waited else {})}
        try:
            out = AD.read_defaults(data, full=bool(a.get("full")))
            reverted = AD.check_reverted(bid, AD.load_plist(data)[0])
        except AD.AppDataError as exc:
            raise _ad(exc) from None
        res = {"bundleId": bid, "path": path, "exists": True, "running": running, **out}
        if waited:
            res["waited"] = waited
        if reverted:
            res["reverted"] = reverted
        return res

    async def edit(d, a, change, action: str):
        s = d.need_session(a)
        info = await _app(d, s, a)
        bid = info["bundleId"]
        key = str(a.get("key") or "")
        dry = bool(a.get("dryRun"))
        async with lock(bid):
            stopped = False if dry else await _ensure_stopped(d, s, a, bid, "changing its settings")
            waited = 0.0 if dry else await _wait_flush(s, bid)

            async def current(afc):
                path = await _defaults_path(afc, bid) or AD.defaults_candidates(bid)[0]
                if not dry:
                    await _settle(afc, path)
                return (await _read(afc, path, AD.MAX_PLIST) if await afc.exists(path) else None), path
            data, path = await _call(s, bid, current, "the app's settings")
            try:
                new, res = change(data)
            except AD.AppDataError as exc:
                raise _ad(exc) from None
            out = {"bundleId": bid, "key": key, "path": path, **res}
            if dry:
                out.update(ok=False, dryRun=True, running=bool(await _running(d, a, bid)))
                return out
            if new is None:                       # delete of a key that isn't there
                return {**out, "ok": True, "stopped": stopped, "method": "none", "refreshed": False,
                        "verified": True, "needsRelaunch": False}
            backup = AD.backup_plist(bid, data or b"")
            want = AD.coerce(a.get("value"), a.get("type")) if action == "set" else None
            check = (lambda o: key in o and AD.same_value(o[key], want)) if action == "set" else (lambda o: key not in o)

            async def put(afc):
                await _write(afc, path, new)
            await _call(s, bid, put, "the app's settings", retry=False)
            dlog(f"appdata: wrote {path} of {bid} ({len(new)} bytes)")
            out.update(ok=True, stopped=stopped, waited=waited, backup=backup, needsRelaunch=True, attempts=1)
            if a.get("refresh", True):
                out.update(await _refresh(d, s, a, bid))
            else:
                out["refreshed"] = False
            out["verified"] = await _verify(s, bid, path, check)
            while out["verified"] is False and out["attempts"] < WRITE_TRIES:
                # cfprefsd wrote its copy over ours (it flushes a just-stopped app's settings late): apply
                # the change again to the file as it is now. After a refresh its cache is gone, so this sticks.
                await asyncio.sleep(2.0)
                again, _ = await _call(s, bid, current, "the app's settings")
                try:
                    new2, _ = change(again)
                except AD.AppDataError as exc:
                    raise _ad(exc) from None
                if new2 is not None:
                    async def put2(afc, data=new2):
                        await _write(afc, path, data)
                    await _call(s, bid, put2, "the app's settings", retry=False)
                out["attempts"] += 1
                await asyncio.sleep(1.0)
                out["verified"] = await _verify(s, bid, path, check)
            out["method"] = "reinstall" if out["refreshed"] else "file"
            AD.remember_write(bid, key, res.get("previous"), out.get("value"), action)
            if not out["refreshed"]:
                out["warning"] = AD.CACHED_WARNING
                if action == "set":
                    args = AD.launch_args(key, want)
                    if args:
                        out["launchArgs"] = args
            return out

    @reg.command("appdata.defaults.set")
    async def c_defaults_set(d, a):
        if "value" not in a:
            raise err("bad-args", "Which value? (value missing)")
        return await edit(d, a, lambda data: AD.set_key(data, a.get("key"), a.get("value"), a.get("type")), "set")

    @reg.command("appdata.defaults.delete")
    async def c_defaults_delete(d, a):
        return await edit(d, a, lambda data: AD.delete_key(data, a.get("key")), "delete")

    @reg.command("appdata.defaults.refresh")
    async def c_defaults_refresh(d, a):
        """After edits made with refresh: false, make the app read them on its next launch."""
        s = d.need_session(a)
        info = await _app(d, s, a)
        bid = info["bundleId"]
        async with lock(bid):
            stopped = await _ensure_stopped(d, s, a, bid, "refreshing its settings")
            await _wait_flush(s, bid)     # else the app's last unsaved settings are dropped with the cache
            r = await _refresh(d, s, a, bid)
        out = {"ok": r["refreshed"], "bundleId": bid, "stopped": stopped, **r}
        if not r["refreshed"]:
            out["warning"] = AD.CACHED_WARNING
        return out

    # ------------------------------------------------ stores
    async def pull_store(s, afc, bid: str, group: dict) -> tuple:
        stamp = {}
        for f, st in zip(group["files"], await F.stat_many(afc, group["files"])):
            if st is not None:
                stamp[f] = [int(st.get("st_size") or 0), F._mtime(st)]
        hit = AD.cached_store(bid, group, stamp)
        if hit:
            return hit, True
        if group["size"] > AD.MAX_STORE:
            raise err("too-big", f"{group['name']} is {group['size'] >> 20} MB, too big to copy here.")
        tmp = AD.new_pull_dir(bid)
        try:
            prog = df.Progress(lambda *_: None, "appdata", "appdata.pull", group["size"], len(group["files"]))
            for f in group["files"]:
                if f in stamp:
                    await F.read_to(afc, f, tmp / posixpath.basename(f), stamp[f][0], prog)
        except BaseException:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        db = await asyncio.to_thread(AD.commit_pull, bid, group, tmp, stamp)
        return db, False

    @reg.command("appdata.stores")
    async def c_stores(d, a):
        s = d.need_session(a)
        info = await _app(d, s, a)
        bid = info["bundleId"]
        async with lock(bid):
            async def go(afc):
                files, _ = await _listing(afc, AD.STORE_DIRS)
                groups = AD.store_groups(files)
                out = []
                for g in groups:
                    entry = {"name": g["name"], "path": g["path"], "size": g["size"], "kind": "sqlite", "tables": []}
                    try:
                        db, cached = await pull_store(s, afc, bid, g)
                        entry.update(await asyncio.to_thread(AD.summarize, db, g["name"]), cached=cached)
                    except AD.AppDataError as exc:
                        entry["error"] = {"code": exc.code, "message": exc.message}
                    except AfcException as exc:
                        e = F.afc_error(exc, g["name"])
                        entry["error"] = {"code": getattr(e, "code", "failed"), "message": str(e)}
                    except Exception as exc:  # noqa: BLE001 - one bad store doesn't hide the others
                        if F.is_connection_error(exc):
                            raise
                        code = getattr(exc, "code", "failed")
                        entry["error"] = {"code": code, "message": getattr(exc, "message", None) or str(exc)}
                    out.append(entry)
                return AD.sort_stores(out)
            stores = await _call(s, bid, go, "the app's data")
            if not stores and _recent_stop(s, bid):     # right after a kill or install the container lags
                await asyncio.sleep(1.5)
                stores = await _call(s, bid, go, "the app's data")
            await asyncio.to_thread(AD.prune_cache, bid, [x["path"] for x in stores])
        return {"bundleId": bid, "running": bool(await _running(d, a, bid)), "stores": stores}

    @reg.command("appdata.rows")
    async def c_rows(d, a):
        s = d.need_session(a)
        info = await _app(d, s, a)
        bid = info["bundleId"]
        store = str(a.get("store") or "")
        table = str(a.get("table") or "")
        if not store or not table:
            raise err("bad-args", "Which store and table? (store and table are needed)")
        try:
            store = df.clean_phone_path(store, "/")
        except df.PathError as exc:
            raise err("bad-args", str(exc))
        db = AD.any_cached(bid, store)
        if db is None:                              # not pulled yet (or pruned): pull just this one
            async with lock(bid):
                async def go(afc):
                    names = await afc.listdir(posixpath.dirname(store))
                    folder = posixpath.dirname(store)
                    paths = [posixpath.join(folder, n) for n in names]
                    sts = await F.stat_many(afc, paths)
                    files = [(p, int(st.get("st_size") or 0)) for p, st in zip(paths, sts) if st]
                    g = next((x for x in AD.store_groups(files) if x["path"] == store), None)
                    if g is None:
                        raise err("no-store", "That store isn't in the app's container anymore.")
                    return (await pull_store(s, afc, bid, g))[0]
                db = await _call(s, bid, go, "that store")
        try:
            return {"bundleId": bid, "store": store,
                    **await asyncio.to_thread(AD.rows, db, table, a.get("offset") or 0, a.get("limit") or 50)}
        except AD.AppDataError as exc:
            raise _ad(exc) from None

    # ------------------------------------------------ reset
    @reg.command("appdata.reset")
    async def c_reset(d, a):
        s = d.need_session(a)
        info = await _app(d, s, a)
        bid = info["bundleId"]
        mode = str(a.get("mode") or "wipe")
        if mode not in ("wipe", "reinstall"):
            raise err("bad-args", "mode is wipe or reinstall.")
        build = None
        if mode == "reinstall":
            art = str(a.get("artifact") or "")
            build = {"artifact": art, "project": a.get("project"), "path": None} if art else \
                await asyncio.to_thread(AD.find_build, bid)
            if not build:
                raise err("no-build", "No signed build of this app was found on this computer, so it can't be "
                          "installed again. Build it (Projects › Run, or ios_build_start action=ipa) first.",
                          BUILD_FIX)
        async with lock(bid):
            async def plan(afc):
                files, dirs = await _listing(afc, AD.RESET_TOPS)
                return AD.reset_plan(files, dirs)
            p = await _call(s, bid, plan, "the app's data")
            running = bool(await _running(d, a, bid))
            if not a.get("confirm"):
                out = {"dryRun": True, "mode": mode, "bundleId": bid, "running": running,
                       **{k: v for k, v in p.items() if k != "all"}}
                if build:
                    out.update(project=build.get("project"), projectPath=build.get("path"),
                               artifact=build.get("artifact"))
                return out
            stopped = await _ensure_stopped(d, s, a, bid, "resetting its data")
            t0 = time.monotonic()
            if mode == "wipe":
                await _wait_flush(s, bid)  # a late cfprefsd save would put the settings file back
            if mode == "wipe":
                async def wipe(afc):
                    removed, failed = 0, []
                    for path in p["all"]:
                        try:
                            left = await afc.rm(path, force=True)
                        except AfcFileNotFoundError:
                            left = []
                        except AfcException:
                            left = [path]
                        if left:
                            failed.append(path)
                        else:
                            removed += 1
                    return removed, failed
                removed, failed = await _call(s, bid, wipe, "the app's data", retry=False)
                await F.forget_container(s, bid, False)
                AD.forget_cache(bid)
                AD.forget_writes(bid)
                out = {"dryRun": False, "mode": mode, "bundleId": bid, "removed": removed, "failed": failed[:50],
                       "bytes": p["bytes"], "stopped": stopped, "reinstalled": False}
                if a.get("refresh", True):        # else cfprefsd hands the app its old settings again
                    out.update(await _refresh(d, s, a, bid))
                else:
                    out["refreshed"] = False
                if not out["refreshed"]:
                    out["warning"] = AD.CACHED_WARNING
                out["seconds"] = round(time.monotonic() - t0, 1)
                return out
            await F.forget_container(s, bid, False)
            await d.registry.lookup("app.uninstall")(d, {"bundleId": bid, **_udid(a)})
            s.tunnel_data.get("files.apps", {}).pop(bid, None)
            AD.forget_cache(bid)
            AD.forget_writes(bid)
            res = await d.registry.lookup("app.install")(d, {"path": build["artifact"], **_udid(a)})
            s.tunnel_data.setdefault("appdata.installed", {})[bid] = time.monotonic()
            return {"dryRun": False, "mode": mode, "bundleId": res.get("bundleId") or bid, "removed": p["count"],
                    "bytes": p["bytes"], "stopped": stopped, "reinstalled": True, "artifact": build["artifact"],
                    "project": build.get("project"), "seconds": round(time.monotonic() - t0, 1)}
