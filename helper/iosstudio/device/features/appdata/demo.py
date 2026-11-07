"""Demo: appdata.defaults / defaults.set / defaults.delete, appdata.stores / rows, appdata.reset.

The made-up developer apps (HabitTracker, stopped; WeatherGlance, running) get realistic saved data in their
demo containers (helper/iosstudio/device/features/files/demo.py) the first time this daemon touches them: a binary
preferences plist with strings, numbers, a bool, a date, an array, a nested dictionary and a Data blob, and a
SwiftData store (HabitTracker: Habit, Completion with 640 rows, Tag, plus Core Data's Z_* tables) or a
Core Data store (WeatherGlance). Edits and resets change those files, so they show in Files too, and last
until the demo daemon restarts. The same helper/iosstudio/features/app_data.py code as the real daemon reads and writes them.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import plistlib
import random
import shutil
import sqlite3
import threading
import time
from pathlib import Path

from iosstudio.core.appdirs import HOME
from iosstudio.core.output import HelperError
from iosstudio.core.subproc import demo_delay
from iosstudio.device.demo.core import DIR, event, installed, running
from iosstudio.device.features.files import demo as FD
from iosstudio.features import app_data as AD

AD.CACHE = DIR / "appdata-cache"     # the demo daemon never touches ~/.cache/ios-studio/appdata



def stop_fix(bid: str) -> dict:
    return {"label": "Stop the app", "page": "device", "tab": "apps", "app": bid, "section": "data",
            "action": "stop-app"}


NOT_DEV = ("Only your own apps (built and installed here) share their saved data. App Store apps keep "
           "theirs private.")
_seeded: set[str] = set()
INSTALL_STEP = 0.4          # seconds per fake install progress step


def err(code: str, message: str, fix_=None, detail=None) -> HelperError:
    return HelperError(code, message, fix_, detail)


# ---------------------------------------------------------------- made-up data

def _defaults(name: str) -> dict:
    now = dt.datetime(2026, 11, 6, 8, 30, 0)
    if name == "WeatherGlance":
        return {"unit": "celsius", "refreshMinutes": 30, "showFeelsLike": True, "lastRefresh": now,
                "places": ["Lisbon", "Porto", "Faro"], "selectedPlace": 0, "radarOpacity": 0.65,
                "widget": {"style": "compact", "showHighLow": True, "accent": "#3A8DDE"},
                "cachedForecast": bytes(random.Random(5).randrange(256) for _ in range(1536))}
    return {
        "hasSeenOnboarding": True, "launchCount": 42, "dailyGoal": 3, "reminderHour": 20.5,
        "userName": "Alex", "theme": "system", "lastOpened": now, "streakStart": dt.datetime(2026, 10, 1, 7, 0, 0),
        "enabledWeekdays": [1, 2, 3, 4, 5], "recentSearches": ["read", "run", "water"],
        "notifications": {"enabled": True, "sound": "chime", "quietHours": {"from": 22, "to": 7}},
        "avatarPNG": bytes(random.Random(9).randrange(256) for _ in range(2048)),
        "AppleLanguages": ["en-PT", "pt-PT"],
        "NSWindow Frame Main": "0 0 393 852",
        "WebKitDiskImageCacheSavedCacheDirectory": "",
        "lastExportNote": "Exported 12 habits and 640 completions. " * 80,
    }


def _coredata(path: Path, entities: list[tuple[str, list[tuple[str, str]], list[list]]], history: bool) -> None:
    """A SQLite file shaped like Core Data / SwiftData: Z<ENTITY> tables with Z_PK/Z_ENT/Z_OPT, Z_PRIMARYKEY,
    Z_METADATA, Z_MODELCACHE and (history=True, as SwiftData does) ATRANSACTION / ACHANGE."""
    path.parent.mkdir(parents=True, exist_ok=True)
    for p in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        p.unlink(missing_ok=True)
    con = sqlite3.connect(str(path))
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE Z_PRIMARYKEY (Z_ENT INTEGER PRIMARY KEY, Z_NAME VARCHAR, Z_SUPER INTEGER, Z_MAX INTEGER)")
        con.execute("CREATE TABLE Z_METADATA (Z_VERSION INTEGER PRIMARY KEY, Z_UUID VARCHAR(255), Z_PLIST BLOB)")
        con.execute("CREATE TABLE Z_MODELCACHE (Z_CONTENT BLOB)")
        for i, (ent, cols, data) in enumerate(entities, 1):
            t = "Z" + ent.upper()
            spec = ", ".join(f"{c} {ty}" for c, ty in cols)
            con.execute(f"CREATE TABLE {t} (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER, {spec})")
            marks = ", ".join("?" * (len(cols) + 3))
            con.executemany(f"INSERT INTO {t} VALUES ({marks})", [[n, i, 1, *r] for n, r in enumerate(data, 1)])
            con.execute("INSERT INTO Z_PRIMARYKEY VALUES (?, ?, 0, ?)", (i, ent, len(data)))
        con.execute("INSERT INTO Z_METADATA VALUES (1, ?, ?)", ("8C1F0D2E-5B7A-4C3E-9E21-6F0A1B2C3D4E",
                                                                plistlib.dumps({"NSStoreType": "SQLite"}, fmt=plistlib.FMT_BINARY)))
        con.execute("INSERT INTO Z_MODELCACHE VALUES (?)", (bytes(range(256)) * 8,))
        if history:
            con.execute("CREATE TABLE ATRANSACTION (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER, "
                        "ZAUTHORTS INTEGER, ZTIMESTAMP FLOAT)")
            con.execute("CREATE TABLE ACHANGE (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER, ZCHANGETYPE "
                        "INTEGER, ZENTITY INTEGER, ZENTITYPK INTEGER, ZTRANSACTIONID INTEGER)")
            con.executemany("INSERT INTO ATRANSACTION VALUES (?, 16001, 1, 0, ?)",
                            [(n, 815000000.0 + n * 60) for n in range(1, 25)])
            con.executemany("INSERT INTO ACHANGE VALUES (?, 16002, 1, 0, 2, ?, ?)",
                            [(n, n, 1 + n // 30) for n in range(1, 700)])
        con.commit()
    finally:
        con.close()


def _ts(d: dt.datetime) -> float:
    return (d - dt.datetime(2001, 1, 1)).total_seconds()


def _habits_store(path: Path) -> None:
    rnd = random.Random(27)
    names = ["Read 20 pages", "Run 5 km", "Drink water", "Meditate", "Stretch", "No phone after 22:00",
             "Practice Portuguese", "Journal", "Walk the dog", "Floss", "Cook at home", "Sleep by 23:30"]
    start = dt.datetime(2026, 9, 1, 7, 0)
    habits = [[1 if i in (5, 10) else 0, i, _ts(start + dt.timedelta(days=i)), f"#{rnd.randrange(1 << 24):06X}",
               n, bytes(rnd.randrange(256) for _ in range(16)), None if i % 3 else f"Started after a chat with Sam ({i})."]
              for i, n in enumerate(names)]
    comps = []
    for n in range(640):
        day = start + dt.timedelta(days=n // 7, hours=rnd.randrange(14))
        note = None
        if n % 97 == 0:
            note = "Felt great today. " * 30
        elif n % 11 == 0:
            note = "Short one."
        comps.append([_ts(day), 1 + n % len(names), rnd.randrange(1, 4), note,
                      bytes(rnd.randrange(256) for _ in range(900)) if n % 53 == 0 else None])
    tags = [[t, f"#{rnd.randrange(1 << 24):06X}"] for t in ("health", "mind", "home", "fitness", "learning", "sleep")]
    _coredata(path, [
        ("Habit", [("ZISARCHIVED", "INTEGER"), ("ZSORTORDER", "INTEGER"), ("ZCREATEDAT", "TIMESTAMP"),
                   ("ZCOLORHEX", "VARCHAR"), ("ZNAME", "VARCHAR"), ("ZID", "BLOB"), ("ZNOTES", "VARCHAR")], habits),
        ("Completion", [("ZDATE", "TIMESTAMP"), ("ZHABIT", "INTEGER"), ("ZCOUNT", "INTEGER"), ("ZNOTE", "VARCHAR"),
                        ("ZPHOTO", "BLOB")], comps),
        ("Tag", [("ZNAME", "VARCHAR"), ("ZCOLORHEX", "VARCHAR")], tags),
    ], history=True)
    con = sqlite3.connect(str(path))       # a many-to-many join table, as Core Data names them
    con.execute("CREATE TABLE Z_1TAGS (Z_1HABITS INTEGER, Z_3TAGS INTEGER, PRIMARY KEY (Z_1HABITS, Z_3TAGS))")
    con.executemany("INSERT INTO Z_1TAGS VALUES (?, ?)", [(h, 1 + h % 6) for h in range(1, 13)])
    con.commit()
    con.close()


def _weather_store(path: Path) -> None:
    rnd = random.Random(3)
    places = [["Lisbon", 38.72, -9.14], ["Porto", 41.15, -8.61], ["Faro", 37.02, -7.93]]
    base = dt.datetime(2026, 10, 25)
    fc = [[_ts(base + dt.timedelta(hours=3 * n)), 1 + n % 3, round(rnd.uniform(11, 24), 1), rnd.choice(
        ["sun", "cloud", "rain", "wind"])] for n in range(168)]
    _coredata(path, [("Place", [("ZNAME", "VARCHAR"), ("ZLATITUDE", "FLOAT"), ("ZLONGITUDE", "FLOAT")], places),
                     ("Forecast", [("ZTIME", "TIMESTAMP"), ("ZPLACE", "INTEGER"), ("ZTEMPERATURE", "FLOAT"),
                                   ("ZSYMBOL", "VARCHAR")], fc)], history=False)


def _legacy_db(path: Path) -> None:
    path.unlink(missing_ok=True)
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE entries (id INTEGER PRIMARY KEY, habit TEXT, done_on TEXT)")
    con.executemany("INSERT INTO entries VALUES (?, ?, ?)",
                    [(i, ["read", "run", "water"][i % 3], f"2026-08-{1 + i % 28:02d}") for i in range(1, 58)])
    con.commit()
    con.close()


def _seed(info: dict, base: Path) -> None:
    """Fresh made-up data, once per demo daemon run (edits and resets stick until it restarts)."""
    bid = info["bundleId"]
    if bid in _seeded:
        return
    _seeded.add(bid)
    name = info["name"]
    plain = AD.plain_bundle_id(bid)
    prefs = base / "Library" / "Preferences"
    prefs.mkdir(parents=True, exist_ok=True)
    for old in prefs.glob("*.plist"):
        old.unlink()
    (prefs / f"{plain}.plist").write_bytes(plistlib.dumps(_defaults(name), fmt=plistlib.FMT_BINARY))
    support = base / "Library" / "Application Support"
    if name == "HabitTracker":
        _habits_store(support / "default.store")
        _legacy_db(base / "Documents" / "habits.sqlite")
    else:
        _weather_store(support / f"{name}.sqlite")
    (base / "Library" / "Caches").mkdir(parents=True, exist_ok=True)
    (base / "Library" / "Caches" / "com.apple.nsurlsessiond").mkdir(exist_ok=True)
    (base / "Library" / "Caches" / "com.apple.nsurlsessiond" / "downloads.plist").write_bytes(b"\0" * 4096)
    (base / "Library" / "Saved Application State").mkdir(exist_ok=True)
    (base / "tmp").mkdir(exist_ok=True)
    (base / "tmp" / "upload-7F3A.tmp").write_bytes(b"\0" * 12000)


def _app(a: dict) -> tuple[dict, Path]:
    bid = str(a.get("bundleId") or "")
    if not bid:
        raise err("bad-args", "Which app? (bundleId missing)")
    info = FD._app(FD._bid(AD.plain_bundle_id(bid)) if bid not in installed else bid)
    if not info["developer"]:
        raise err("not-developer", NOT_DEV)
    _, _, _, base = FD._access({"bundleId": info["bundleId"]})
    _seed(info, base)
    return info, base


# The real daemon waits until 10 s after an app stopped before it touches the app's settings (iOS saves them
# 2 to 7 s after the stop), and a change takes 10 to 20 s. The demo keeps that shape, shorter, so the
# window's busy states show: a stop is noticed by a small watcher (app.kill lives in apps.py), reads and
# changes wait out DEMO_GRACE after it, and a change settles for DEMO_SETTLE before the install.
DEMO_GRACE = 4.0
DEMO_SETTLE = 1.5
_stopped_at: dict[str, float] = {}


def _watch_stops() -> None:
    seen = set(running)
    while True:
        now = set(running)
        for bid in seen - now:
            _stopped_at[bid] = time.monotonic()
        seen = now
        time.sleep(0.25)


def _wait_grace(bid: str) -> float:
    """Sleeps out what's left of DEMO_GRACE since the app stopped; returns the seconds waited."""
    at = _stopped_at.get(bid)
    left = DEMO_GRACE - (time.monotonic() - at) if at else 0.0
    if left <= 0:
        return 0.0
    time.sleep(demo_delay(left))
    return round(left, 1)


def _ensure_stopped(a: dict, bid: str, what: str) -> bool:
    if bid not in running:
        return False
    if not a.get("stopApp"):
        raise err("app-running", f"Stop the app before {what}: it keeps its own copy of this data in memory and "
                  "would write it back over the change.", stop_fix(bid))
    running.discard(bid)
    _stopped_at[bid] = time.monotonic()
    return True


def _local(base: Path, phone: str) -> Path:
    return base / phone.lstrip("/")


def _defaults_path(base: Path, bid: str) -> str | None:
    d = base / "Library" / "Preferences"
    return AD.pick_defaults(bid, [p.name for p in d.iterdir()] if d.is_dir() else [])


def _listing(base: Path, tops) -> tuple[list[tuple[str, int]], list[str]]:
    files, dirs = [], []
    for top in tops:
        root = _local(base, top)
        if not root.is_dir():
            continue
        for p in sorted(root.rglob("*")):
            if p.name in (".keep",) or p.name.endswith(".part"):
                continue
            phone = "/" + p.relative_to(base).as_posix()
            if p.is_dir():
                dirs.append(phone)
            elif p.is_file():
                files.append((phone, p.stat().st_size))
    return files, dirs


def _pull(bid: str, base: Path, group: dict) -> tuple[Path, bool]:
    stamp = {}
    for f in group["files"]:
        st = _local(base, f).stat()
        stamp[f] = [st.st_size, st.st_mtime]
    hit = AD.cached_store(bid, group, stamp)
    if hit:
        return hit, True
    tmp = AD.new_pull_dir(bid)
    try:
        for f in group["files"]:
            shutil.copy2(_local(base, f), tmp / Path(f).name)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return AD.commit_pull(bid, group, tmp, stamp), False


def _build(info: dict) -> dict:
    name = info["name"]
    path = f"{HOME}/Developer/{name}"
    return {"project": name, "path": path, "artifact": f"{path}/xtool/{name}.ipa"}


def register(reg):
    threading.Thread(target=_watch_stops, name="demo-appdata-stops", daemon=True).start()

    @reg.command("appdata.defaults")
    def defaults(ctx, a):
        info, base = _app(a)
        bid = info["bundleId"]
        waited = _wait_grace(bid)
        path = _defaults_path(base, bid)
        common = {"bundleId": bid, "running": bid in running, **({"waited": waited} if waited else {})}
        if not path:
            return {**common, "path": AD.defaults_candidates(bid)[0], "exists": False, "keys": 0, "values": {},
                    "types": {}, "truncated": False, "size": 0}
        data = _local(base, path).read_bytes()
        try:
            out = AD.read_defaults(data, full=bool(a.get("full")))
            reverted = AD.check_reverted(bid, AD.load_plist(data)[0])
        except AD.AppDataError as exc:
            raise err(exc.code, exc.message) from None
        res = {**common, "path": path, "exists": True, **out}
        if reverted:
            res["reverted"] = reverted
        return res

    def refresh(a, info) -> dict:
        """The real daemon installs the last build over the app so iOS drops its cached settings; here the
        made-up build always exists and the install only shows progress."""
        build = {"artifact": a["artifact"]} if a.get("artifact") else _build(info)
        for pct in (0, 20, 40, 60, 80, 100):
            event("installProgress", {"path": build["artifact"], "percent": pct,
                                      "status": "done" if pct == 100 else ("uploading" if pct == 0 else "installing")})
            time.sleep(demo_delay(INSTALL_STEP))
        return {"refreshed": True, "artifact": build["artifact"]}

    def edit(a, change, action):
        info, base = _app(a)
        bid = info["bundleId"]
        path = _defaults_path(base, bid) or AD.defaults_candidates(bid)[0]
        f = _local(base, path)
        data = f.read_bytes() if f.is_file() else None
        try:
            new, res = change(data)
        except AD.AppDataError as exc:
            raise err(exc.code, exc.message) from None
        out = {"bundleId": bid, "key": str(a.get("key") or ""), "path": path, **res}
        if a.get("dryRun"):
            return {**out, "ok": False, "dryRun": True, "running": bid in running}
        if new is None:
            return {**out, "ok": True, "stopped": False, "method": "none", "refreshed": False, "verified": True,
                    "needsRelaunch": False}
        stopped = _ensure_stopped(a, bid, "changing its settings")
        waited = _wait_grace(bid)
        time.sleep(demo_delay(DEMO_SETTLE))
        backup = AD.backup_plist(bid, data or b"")
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_name(f.name + ".iosstudio-new")
        tmp.write_bytes(new)
        tmp.replace(f)
        out.update(ok=True, stopped=stopped, waited=waited, backup=backup, needsRelaunch=True, attempts=1)
        out.update(refresh(a, info) if a.get("refresh", True) else {"refreshed": False})
        obj = AD.load_plist(f.read_bytes())[0]
        want = AD.coerce(a.get("value"), a.get("type")) if action == "set" else None
        out["verified"] = (a.get("key") in obj and AD.same_value(obj[a.get("key")], want)) if action == "set" \
            else a.get("key") not in obj
        out["method"] = "reinstall" if out["refreshed"] else "file"
        AD.remember_write(bid, str(a.get("key")), res.get("previous"), out.get("value"), action)
        if not out["refreshed"]:
            out["warning"] = AD.CACHED_WARNING
            args = AD.launch_args(str(a.get("key")), want) if action == "set" else None
            if args:
                out["launchArgs"] = args
        return out

    @reg.command("appdata.defaults.set")
    def defaults_set(ctx, a):
        if "value" not in a:
            raise err("bad-args", "Which value? (value missing)")
        return edit(a, lambda data: AD.set_key(data, a.get("key"), a.get("value"), a.get("type")), "set")

    @reg.command("appdata.defaults.delete")
    def defaults_delete(ctx, a):
        return edit(a, lambda data: AD.delete_key(data, a.get("key")), "delete")

    @reg.command("appdata.defaults.refresh")
    def defaults_refresh(ctx, a):
        info, _ = _app(a)
        bid = info["bundleId"]
        stopped = _ensure_stopped(a, bid, "refreshing its settings")
        return {"ok": True, "bundleId": bid, "stopped": stopped, **refresh(a, info)}

    @reg.command("appdata.stores")
    def stores(ctx, a):
        info, base = _app(a)
        bid = info["bundleId"]
        files, _ = _listing(base, AD.STORE_DIRS)
        out = []
        for g in AD.store_groups(files):
            entry = {"name": g["name"], "path": g["path"], "size": g["size"], "kind": "sqlite", "tables": []}
            try:
                db, cached = _pull(bid, base, g)
                entry.update(AD.summarize(db, g["name"]), cached=cached)
            except AD.AppDataError as exc:
                entry["error"] = {"code": exc.code, "message": exc.message}
            out.append(entry)
        AD.prune_cache(bid, [x["path"] for x in out])
        return {"bundleId": bid, "running": bid in running, "stores": AD.sort_stores(out)}

    @reg.command("appdata.rows")
    def rows(ctx, a):
        info, base = _app(a)
        bid = info["bundleId"]
        store, table = str(a.get("store") or ""), str(a.get("table") or "")
        if not store or not table:
            raise err("bad-args", "Which store and table? (store and table are needed)")
        db = AD.any_cached(bid, store)
        if db is None:
            files, _ = _listing(base, AD.STORE_DIRS)
            g = next((x for x in AD.store_groups(files) if x["path"] == store), None)
            if g is None:
                raise err("no-store", "That store isn't in the app's container anymore.")
            db = _pull(bid, base, g)[0]
        try:
            return {"bundleId": bid, "store": store, **AD.rows(db, table, a.get("offset") or 0, a.get("limit") or 50)}
        except AD.AppDataError as exc:
            raise err(exc.code, exc.message) from None

    @reg.command("appdata.reset")
    async def reset(ctx, a):
        info, base = await asyncio.to_thread(_app, a)
        bid = info["bundleId"]
        mode = str(a.get("mode") or "wipe")
        if mode not in ("wipe", "reinstall"):
            raise err("bad-args", "mode is wipe or reinstall.")
        build = None
        if mode == "reinstall":
            build = {"artifact": a["artifact"], "project": a.get("project"), "path": None} \
                if a.get("artifact") else _build(info)
        files, dirs = await asyncio.to_thread(_listing, base, AD.RESET_TOPS)
        plan = AD.reset_plan(files, dirs)
        if not a.get("confirm"):
            out = {"dryRun": True, "mode": mode, "bundleId": bid, "running": bid in running,
                   **{k: v for k, v in plan.items() if k != "all"}}
            if build:
                out.update(project=build.get("project"), projectPath=build.get("path"), artifact=build["artifact"])
            return out
        stopped = _ensure_stopped(a, bid, "resetting its data")
        t0 = time.monotonic()
        await asyncio.to_thread(_wait_grace, bid)
        await asyncio.sleep(demo_delay(DEMO_SETTLE))
        for path in plan["all"]:
            p = _local(base, path)
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink(missing_ok=True)
        AD.forget_cache(bid)
        AD.forget_writes(bid)
        out = {"dryRun": False, "mode": mode, "bundleId": bid, "removed": plan["count"], "failed": [],
               "bytes": plan["bytes"], "stopped": stopped, "reinstalled": False}
        if mode == "wipe":
            if a.get("refresh", True):
                out.update(await asyncio.to_thread(refresh, a, info))
            else:
                out.update(refreshed=False, warning=AD.CACHED_WARNING)
        if mode == "reinstall":
            installed.discard(bid)
            for pct in (0, 35, 70, 100):
                event("installProgress", {"path": build["artifact"], "percent": pct,
                                          "status": "done" if pct == 100 else ("uploading" if pct == 0 else "installing")})
                await asyncio.sleep(demo_delay(INSTALL_STEP))
            installed.add(bid)
            out.update(reinstalled=True, artifact=build["artifact"], project=build.get("project"))
        out["seconds"] = round(time.monotonic() - t0, 1)
        return out
