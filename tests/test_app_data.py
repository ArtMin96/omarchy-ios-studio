"""App data (helper/app_data.py): plist <-> JSON round trips, typed values, sqlite summaries and paging on
the fixture stores in tests/fixtures/app-data/ (written by its make_fixtures.py), cell bounds, the store
cache, reset plans, finding the last build; plus the demo daemon's appdata.* commands.
"""
from __future__ import annotations

import asyncio
import base64
import datetime as dt
import json
import os
import plistlib
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "helper"))
FIX = ROOT / "tests" / "fixtures" / "app-data"

import app_data as AD  # noqa: E402


class PlistJson(unittest.TestCase):
    def roundtrip(self, obj):
        j = json.loads(json.dumps(AD.to_json(obj)))          # through real JSON text
        self.assertEqual(AD.from_json(j), obj)
        return j

    def test_scalars_and_containers(self):
        self.roundtrip({"s": "x", "i": -3, "f": 2.5, "b": True, "a": [1, "two", [False]], "d": {"k": {"z": 0}}})

    def test_tagged_types(self):
        j = self.roundtrip({"data": b"\x00\x01\xff", "date": dt.datetime(2026, 11, 7, 10, 0, 0),
                            "frac": dt.datetime(2026, 1, 2, 3, 4, 5, 678000), "uid": plistlib.UID(9)})
        self.assertEqual(j["data"], {"$type": "data", "base64": "AAH/", "size": 3})
        self.assertEqual(j["date"], {"$type": "date", "iso": "2026-11-07T10:00:00Z"})
        self.assertEqual(j["uid"], {"$type": "uid", "value": 9})
        inf = AD.to_json(float("inf"))
        self.assertEqual(inf, {"$type": "float", "value": "inf"})
        self.assertEqual(AD.from_json(inf), float("inf"))

    def test_dict_with_type_key_is_escaped(self):
        j = self.roundtrip({"$type": "mine", "n": 1})
        self.assertEqual(j["$type"], "dict")

    def test_binary_and_xml_fixtures(self):
        for name in ("binary.plist", "xml.plist"):
            data = (FIX / "prefs" / name).read_bytes()
            obj, fmt = AD.load_plist(data)
            self.assertEqual(fmt, plistlib.FMT_BINARY if name == "binary.plist" else plistlib.FMT_XML)
            full = AD.read_defaults(data, full=True)
            self.assertFalse(full["truncated"])
            self.assertEqual(full["format"], "binary" if name.startswith("binary") else "xml")
            back = {k: AD.from_json(v) for k, v in json.loads(json.dumps(full["values"])).items()}
            self.assertEqual(back, obj)
            self.assertEqual(full["types"]["lastOpened"], "date")
            self.assertEqual(full["types"]["nested"], "dict")
            self.assertEqual(full["types"]["blob"], "data")
            self.assertEqual(full["values"]["nested"]["inner"]["deep"][3], {"$type": "data", "base64": "AP8=", "size": 2})

    def test_view_clips_but_full_does_not(self):
        data = (FIX / "prefs" / "binary.plist").read_bytes()
        v = AD.read_defaults(data)
        self.assertTrue(v["truncated"])
        self.assertEqual(v["values"]["blob"]["size"], 200)
        self.assertTrue(v["values"]["blob"]["truncated"])
        self.assertEqual(len(base64.b64decode(v["values"]["blob"]["base64"])), AD.VIEW_DATA)
        with self.assertRaises(AD.AppDataError) as c:            # a clipped value can't be written back
            AD.from_json(v["values"]["blob"])
        self.assertEqual(c.exception.code, "truncated-value")
        big = plistlib.dumps({"s": "x" * 5000, "a": list(range(500)), "ok": 1})
        v = AD.read_defaults(big)
        self.assertEqual(v["values"]["s"]["length"], 5000)
        self.assertEqual(v["values"]["a"]["count"], 500)
        self.assertEqual(len(v["values"]["a"]["preview"]), AD.VIEW_ITEMS)
        self.assertEqual(v["values"]["ok"], 1)

    def test_reply_bytes_are_bounded(self):
        obj = {f"k{i:04d}": "y" * 1500 for i in range(400)}
        v = AD.read_defaults(plistlib.dumps(obj), max_bytes=50_000)
        self.assertTrue(v["truncated"])
        self.assertLess(len(json.dumps(v["values"])), 52_000)
        self.assertEqual(v["keys"], 400)

    def test_bad_plists(self):
        self.assertEqual(AD.read_defaults(b"")["keys"], 0)
        for bad in (b"not a plist", plistlib.dumps([1, 2])):
            with self.assertRaises(AD.AppDataError) as c:
                AD.load_plist(bad)
            self.assertEqual(c.exception.code, "bad-plist")


class Typing(unittest.TestCase):
    def test_inference_from_json(self):
        self.assertEqual(AD.coerce("true"), "true")             # text stays text without a type
        self.assertIs(AD.coerce(True), True)
        self.assertEqual(AD.coerce(3), 3)
        self.assertIsInstance(AD.coerce(3.0), float)
        self.assertEqual(AD.coerce([1, {"a": "b"}]), [1, {"a": "b"}])
        self.assertEqual(AD.coerce({"$type": "date", "iso": "2026-11-07T10:00:00+02:00"}), dt.datetime(2026, 11, 7, 8))

    def test_explicit_types(self):
        self.assertEqual(AD.coerce("42", "int"), 42)
        self.assertEqual(AD.coerce(42.0, "int"), 42)
        self.assertEqual(AD.coerce("2.5", "float"), 2.5)
        self.assertIs(AD.coerce("yes", "bool"), True)
        self.assertIs(AD.coerce(0, "bool"), False)
        self.assertEqual(AD.coerce(12, "string"), "12")
        self.assertEqual(AD.coerce(True, "string"), "true")
        self.assertEqual(AD.coerce("2026-11-07", "date"), dt.datetime(2026, 11, 7))
        self.assertEqual(AD.coerce("2026-11-07T10:00:00Z", "date"), dt.datetime(2026, 11, 7, 10))
        self.assertEqual(AD.coerce("AAH/", "data"), b"\x00\x01\xff")
        self.assertEqual(AD.coerce('{"a": [1, 2]}', "json"), {"a": [1, 2]})
        self.assertEqual(AD.coerce({"$type": "data", "base64": "AA=="}, "data"), b"\x00")

    def test_type_errors(self):
        for value, t, code in [("x", "int", "bad-value"), (1.5, "int", "bad-value"), (True, "int", "bad-value"),
                               ("maybe", "bool", "bad-value"), ("soon", "date", "bad-value"),
                               ("!!", "data", "bad-value"), ("{", "json", "bad-value"), ([1], "string", "bad-value"),
                               (1, "uuid", "bad-type"), (None, None, "bad-value")]:
            with self.subTest(value=value, t=t), self.assertRaises(AD.AppDataError) as c:
                AD.coerce(value, t)
            self.assertEqual(c.exception.code, code)

    def test_set_and_delete_keep_format_and_other_keys(self):
        for name in ("binary.plist", "xml.plist"):
            data = (FIX / "prefs" / name).read_bytes()
            new, res = AD.set_key(data, "launchCount", "8", "int")
            self.assertEqual((res["existed"], res["previous"], res["value"], res["type"]), (True, 7, 8, "int"))
            self.assertEqual(new.startswith(b"bplist"), name == "binary.plist")
            obj, _ = AD.load_plist(new)
            self.assertEqual(obj["launchCount"], 8)
            self.assertEqual(obj["blob"], bytes(range(200)))      # untouched, not the clipped view
            new2, res = AD.delete_key(new, "blob")
            self.assertTrue(res["existed"])
            self.assertNotIn("blob", AD.load_plist(new2)[0])
            self.assertEqual(AD.delete_key(new2, "blob"), (None, {"existed": False}))
        fresh, res = AD.set_key(None, "first", {"a": 1})
        self.assertFalse(res["existed"])
        self.assertTrue(fresh.startswith(b"bplist"))
        with self.assertRaises(AD.AppDataError):
            AD.set_key(None, "", 1)

    def test_defaults_file_names(self):
        bid = "XTL-ABCDE12345.com.example.habits"
        self.assertEqual(AD.plain_bundle_id(bid), "com.example.habits")
        self.assertEqual(AD.pick_defaults(bid, ["com.example.habits.plist", "other.plist"]),
                         "/Library/Preferences/com.example.habits.plist")
        self.assertEqual(AD.pick_defaults(bid, [bid + ".plist", "com.example.habits.plist"]),
                         f"/Library/Preferences/{bid}.plist")
        self.assertIsNone(AD.pick_defaults(bid, ["com.apple.thing.plist"]))


class Stores(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cache = mock.patch.object(AD, "CACHE", self.tmp / "cache")
        self.cache.start()
        self.addCleanup(self.cache.stop)

    def copy_swiftdata(self) -> Path:
        d = self.tmp / "sd"
        shutil.copytree(FIX / "swiftdata", d)
        return d / "default.store"

    def test_groups(self):
        files = [("/Library/Application Support/default.store", 10), ("/Library/Application Support/default.store-wal", 5),
                 ("/Library/Application Support/default.store-shm", 3), ("/Documents/notes.txt", 9),
                 ("/Documents/a.sqlite", 4), ("/Documents/b.DB", 1)]
        g = AD.store_groups(files)
        self.assertEqual([x["name"] for x in g], ["a.sqlite", "b.DB", "default.store"])
        self.assertEqual(g[2]["size"], 18)
        self.assertEqual(len(g[2]["files"]), 3)

    def test_checkpoint_folds_the_wal(self):
        db = self.copy_swiftdata()
        AD.checkpoint(db)
        self.assertFalse(Path(str(db) + "-wal").exists())
        s = AD.summarize(db)
        self.assertEqual(s["kind"], "swiftdata")
        item = next(t for t in s["tables"] if t["name"] == "ZITEM")
        self.assertEqual(item["rows"], 520)                   # the 20 rows that lived only in the -wal

    def test_summary_entities_and_meta(self):
        db = self.copy_swiftdata()
        AD.checkpoint(db)
        tables = AD.summarize(db)["tables"]
        names = [t["name"] for t in tables]
        self.assertEqual(names[:3], ["ZFOLDER", "ZITEM", "ZTAG"])          # entities first
        self.assertEqual({t["entity"] for t in tables[:3]}, {"Folder", "Item", "Tag"})
        meta = {t["name"] for t in tables if t["meta"]}
        self.assertEqual(meta, {"ATRANSACTION", "Z_2TAGS", "Z_METADATA", "Z_MODELCACHE", "Z_PRIMARYKEY"})
        self.assertFalse(any(n.startswith("sqlite_") for n in names))
        cols = {c["name"]: c for c in tables[1]["columns"]}
        self.assertTrue(cols["Z_PK"]["meta"])
        self.assertNotIn("meta", cols["ZTITLE"])
        self.assertEqual(cols["ZCREATED"]["type"], "TIMESTAMP")
        cd = AD.summarize(FIX / "coredata" / "Model.sqlite")
        self.assertEqual(cd["kind"], "coredata")
        self.assertEqual(cd["tables"][0]["entity"], "Note")
        plain = AD.summarize(FIX / "plain" / "cache.db")
        self.assertEqual(plain["kind"], "sqlite")
        self.assertEqual(plain["tables"], [{"name": "responses", "rows": 5, "meta": False, "columns": [
            {"name": "url", "type": "TEXT"}, {"name": "body", "type": "BLOB"}, {"name": "fetched", "type": "TIMESTAMP"}]}])
        with self.assertRaises(AD.AppDataError) as c:
            AD.summarize(FIX / "not-a-db" / "broken.sqlite")
        self.assertEqual(c.exception.code, "bad-store")

    def test_read_only_never_writes(self):
        src = FIX / "coredata" / "Model.sqlite"
        before = src.read_bytes()
        with AD.open_ro(src) as con, self.assertRaises(sqlite3.OperationalError):
            con.execute("DELETE FROM ZNOTE")
        self.assertEqual(src.read_bytes(), before)

    def test_paging(self):
        db = self.copy_swiftdata()
        AD.checkpoint(db)
        p1 = AD.rows(db, "ZITEM", 0, 50)
        self.assertEqual((p1["total"], len(p1["rows"]), p1["more"]), (520, 50, True))
        self.assertEqual(p1["rows"][0][0], 1)
        last = AD.rows(db, "ZITEM", 500, 50)
        self.assertEqual((len(last["rows"]), last["more"]), (20, False))
        self.assertEqual(last["rows"][-1][0], 520)
        self.assertEqual(AD.rows(db, "ZITEM", 0, 10_000)["limit"], AD.MAX_ROWS)
        self.assertEqual(AD.rows(db, "item", 0, 1)["table"], "ZITEM")    # entity name / any case
        with self.assertRaises(AD.AppDataError) as c:
            AD.rows(db, "nope")
        self.assertEqual(c.exception.code, "no-table")
        plain = AD.rows(FIX / "plain" / "cache.db", "responses", 3, 10)  # no primary key: rowid order
        self.assertEqual([r[0] for r in plain["rows"]], ["https://example.com/3", "https://example.com/4"])
        self.assertNotIn("date", plain["columns"][2])                    # not a Core Data store: raw numbers

    def test_cells_are_bounded(self):
        db = self.copy_swiftdata()
        AD.checkpoint(db)
        r = AD.rows(db, "ZITEM", 0, 3)
        cols = [c["name"] for c in r["columns"]]
        row1, row2, row3 = r["rows"]
        thumb = row1[cols.index("ZTHUMB")]
        self.assertEqual((thumb["$type"], thumb["size"], thumb["truncated"]), ("blob", 1024, True))
        self.assertEqual(len(bytes.fromhex(thumb["hex"])), AD.CELL_BLOB)
        small = row2[cols.index("ZTHUMB")]
        self.assertEqual(small, {"$type": "blob", "size": 2, "hex": "0102"})
        body = row3[cols.index("ZBODY")]
        self.assertEqual((body["$type"], body["length"], len(body["preview"])), ("text", 1000, AD.CELL_TEXT))
        created = row1[cols.index("ZCREATED")]
        self.assertEqual(created["iso"], "2026-01-01T13:00:00Z")
        self.assertTrue(r["columns"][cols.index("ZCREATED")]["date"])
        self.assertLess(len(json.dumps(AD.rows(db, "ZITEM", 0, 200))), 200 * 700)

    def test_cache_and_pruning(self):
        bid = "XTL-T.com.example.app"
        group = {"name": "default.store", "path": "/Library/Application Support/default.store",
                 "files": ["/Library/Application Support/default.store", "/Library/Application Support/default.store-wal"]}
        stamp = {group["files"][0]: [1, 2.0]}
        self.assertIsNone(AD.cached_store(bid, group, stamp))
        tmp = AD.new_pull_dir(bid)
        for suffix in ("", "-wal", "-shm"):
            shutil.copyfile(FIX / "swiftdata" / ("default.store" + suffix), tmp / ("default.store" + suffix))
        db = AD.commit_pull(bid, group, tmp, stamp)
        self.assertEqual(AD.cached_store(bid, group, stamp), db)
        self.assertIsNone(AD.cached_store(bid, group, {group["files"][0]: [2, 2.0]}))   # changed on the phone
        self.assertEqual(AD.any_cached(bid, group["path"]), db)
        self.assertFalse(tmp.exists())
        bad = AD.new_pull_dir(bid)                                          # a failed pull leaves nothing
        (bad / "default.store").write_text("garbage")
        with self.assertRaises(sqlite3.DatabaseError):
            AD.commit_pull(bid, group, bad, stamp)
        self.assertFalse(bad.exists())
        self.assertEqual(AD.any_cached(bid, group["path"]), db)             # the older copy survives
        AD.prune_cache(bid, [])                                             # the app no longer has that store
        self.assertIsNone(AD.any_cached(bid, group["path"]))
        for i in range(AD.KEEP_APPS + 3):                                   # only the newest apps stay
            d = AD.app_cache(f"app{i}")
            d.mkdir(parents=True)
            os.utime(d, (1000 + i, 1000 + i))
        AD.prune_cache("app0")
        left = {p.name for p in AD.CACHE.iterdir()}
        self.assertIn("app0", left)                                         # the one in use is protected
        self.assertLessEqual(len(left), AD.KEEP_APPS + 1)

    def test_backups_are_capped(self):
        for i in range(AD.BACKUPS_KEPT + 4):
            with mock.patch.object(AD.time, "strftime", return_value=f"2026-{i:03d}"):
                self.assertTrue(AD.backup_plist("b", b"x"))
        self.assertEqual(len(list((AD.app_cache("b") / "backups").glob("*.plist"))), AD.BACKUPS_KEPT)
        self.assertIsNone(AD.backup_plist("b", b""))


class Reset(unittest.TestCase):
    FILES = [("/Documents/a.json", 10), ("/Documents/Exports/x.csv", 100), ("/Documents/Exports/y.csv", 50),
             ("/Library/Preferences/com.example.app.plist", 300), ("/Library/Caches/com.example.app/Cache.db", 4000),
             ("/Library/Application Support/default.store", 9000), ("/Library/Application Support/default.store-wal", 1),
             ("/Library/SplashBoard/Snapshots/a.ktx", 70), ("/tmp/up.tmp", 5),
             ("/.com.apple.mobile_container_manager.metadata.plist", 400), ("/SystemData/x", 1)]

    def test_items(self):
        self.assertIsNone(AD.reset_item("/Library/Preferences"))
        self.assertIsNone(AD.reset_item("/SystemData/x"))
        self.assertEqual(AD.reset_item("/Library/Caches/a/b/c"), "/Library/Caches/a")
        self.assertEqual(AD.reset_item("/Library/SplashBoard/Snapshots/a.ktx"), "/Library/SplashBoard")
        self.assertEqual(AD.reset_item("Documents/x"), "/Documents/x")

    def test_plan(self):
        p = AD.reset_plan(self.FILES, dirs=["/Documents/Empty", "/Library/Preferences"])
        paths = [x["path"] for x in p["items"]]
        self.assertEqual(paths[0], "/Library/Application Support/default.store")       # biggest first
        self.assertIn("/Library/Application Support/default.store-wal", paths)
        self.assertIn("/Documents/Exports", paths)
        self.assertIn("/Documents/Empty", paths)
        self.assertNotIn("/Library/Preferences", paths)                                  # kept, emptied
        self.assertFalse(any(x.startswith(("/.com.apple", "/SystemData")) for x in paths))
        exports = next(x for x in p["items"] if x["path"] == "/Documents/Exports")
        self.assertEqual((exports["kind"], exports["files"], exports["size"], exports["category"]),
                         ("dir", 2, 150, "documents"))
        self.assertEqual(p["bytes"], 10 + 150 + 300 + 4000 + 9001 + 70 + 5)
        self.assertEqual((p["count"], p["files"], p["truncated"]), (9, 9, False))
        small = AD.reset_plan(self.FILES, limit=3)
        self.assertEqual((len(small["items"]), small["count"], small["truncated"]), (3, 8, True))
        self.assertEqual(len(small["all"]), 8)


class FindBuild(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.ipa = self.tmp / "App" / "xtool" / "App.ipa"
        self.ipa.parent.mkdir(parents=True)
        self.ipa.write_bytes(b"PK")
        self.state = mock.patch.object(AD.common, "STATE_DIR", self.tmp / "state")
        self.state.start()
        self.addCleanup(self.state.stop)

    def test_history_newest_ok_build(self):
        hist = [{"name": "App", "path": str(self.tmp / "App"), "result": "ok", "bundleId": "com.example.app",
                 "ipa": str(self.ipa), "time": 1},
                {"name": "App", "path": str(self.tmp / "App"), "result": "failed", "bundleId": "com.example.app",
                 "ipa": str(self.ipa), "time": 2},
                {"name": "Gone", "result": "ok", "bundleId": "com.example.app", "ipa": str(self.tmp / "missing.ipa")}]
        b = AD.find_build("XTL-ABCDE12345.com.example.app", hist)
        self.assertEqual((b["project"], b["artifact"], b["time"]), ("App", str(self.ipa), 1))
        self.assertIsNone(AD.find_build("com.example.other", hist))

    def test_renewals_fallback(self):
        (self.tmp / "state").mkdir()
        (self.tmp / "state" / "renewals.json").write_text(json.dumps({str(self.tmp / "App"): {"bundleId": "com.example.app"}}))
        with mock.patch.dict(sys.modules, {"projects": None}):
            b = AD.find_build("com.example.app", [])
        self.assertEqual(b["artifact"], str(self.ipa))


class DemoCommands(unittest.TestCase):
    """The demo feature module through a registry, on containers in a temp folder."""

    HABITS = "XTL-A1B2C3D4E5.com.example.habits"
    WEATHER = "XTL-A1B2C3D4E5.com.example.weatherglance"

    def setUp(self):
        from deviced_kit import FeatureRegistry
        import deviced_demo_core as core
        from deviced_demo_features import appdata as demo
        from deviced_demo_features import files as demo_files
        self.core, self.demo = core, demo
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        for target, attr, val in [(demo_files, "ROOT", self.tmp / "containers"), (AD, "CACHE", self.tmp / "cache"),
                                  (demo, "INSTALL_STEP", 0)]:
            p = mock.patch.object(target, attr, val)
            p.start()
            self.addCleanup(p.stop)
        saved = (set(core.running), set(core.installed), set(demo._seeded))
        demo._seeded.clear()

        def restore():
            core.running.clear()
            core.running.update(saved[0])
            core.installed.clear()
            core.installed.update(saved[1])
            demo._seeded.clear()
            demo._seeded.update(saved[2])
        self.addCleanup(restore)
        core.running.discard(self.HABITS)
        core.running.add(self.WEATHER)
        self.reg = FeatureRegistry(log=lambda *_: None)
        m = self.reg.module("appdata")
        demo.register(m)
        m.commit()

    def call(self, cmd, a):
        r = self.reg.lookup(cmd)(None, a)
        return asyncio.run(r) if asyncio.iscoroutine(r) else r

    def code(self, cmd, a) -> str:
        from common import HelperError
        with self.assertRaises(HelperError) as c:
            self.call(cmd, a)
        return c.exception.code

    def test_defaults_round_trip(self):
        d = self.call("appdata.defaults", {"bundleId": "com.example.habits"})       # plain form accepted
        self.assertTrue(d["exists"])
        self.assertFalse(d["running"])
        self.assertEqual(d["bundleId"], self.HABITS)
        self.assertGreaterEqual(d["keys"], 10)
        self.assertEqual(d["values"]["lastOpened"]["$type"], "date")
        self.assertEqual(d["types"]["notifications"], "dict")
        self.assertTrue(d["values"]["avatarPNG"]["truncated"])
        r = self.call("appdata.defaults.set", {"bundleId": self.HABITS, "key": "launchCount", "value": 43})
        self.assertEqual((r["ok"], r["previous"], r["value"]), (True, 42, 43))
        self.assertEqual((r["method"], r["refreshed"], r["verified"], r["needsRelaunch"]), ("reinstall", True, True, True))
        self.assertTrue(r["artifact"].endswith("HabitTracker.ipa"))
        self.assertTrue(Path(r["backup"]).is_file())
        r = self.call("appdata.defaults.set", {"bundleId": self.HABITS, "key": "userName", "value": "Sam", "refresh": False})
        self.assertEqual((r["method"], r["launchArgs"]), ("file", ["-userName", "Sam"]))
        self.assertTrue(self.call("appdata.defaults.refresh", {"bundleId": self.HABITS})["refreshed"])
        r = self.call("appdata.defaults.set", {"bundleId": self.HABITS, "key": "dailyGoal", "value": "5", "type": "int",
                                               "dryRun": True})
        self.assertEqual((r["dryRun"], r["previous"], r["value"]), (True, 3, 5))
        r = self.call("appdata.defaults.delete", {"bundleId": self.HABITS, "key": "theme"})
        self.assertTrue(r["existed"])
        d = self.call("appdata.defaults", {"bundleId": self.HABITS, "full": True})
        self.assertEqual(d["values"]["launchCount"], 43)
        self.assertEqual(d["values"]["dailyGoal"], 3)                                 # the dry run changed nothing
        self.assertNotIn("theme", d["values"])
        self.assertFalse(d["truncated"])
        self.assertEqual(len(base64.b64decode(d["values"]["avatarPNG"]["base64"])), 2048)
        self.assertFalse(self.call("appdata.defaults.delete", {"bundleId": self.HABITS, "key": "theme"})["existed"])

    def test_running_app_refuses_writes(self):
        self.assertEqual(self.code("appdata.defaults.set", {"bundleId": self.WEATHER, "key": "unit", "value": "f"}),
                         "app-running")
        self.assertTrue(self.call("appdata.defaults", {"bundleId": self.WEATHER})["running"])
        r = self.call("appdata.defaults.set", {"bundleId": self.WEATHER, "key": "unit", "value": "f", "stopApp": True})
        self.assertTrue(r["stopped"])
        self.assertNotIn(self.WEATHER, self.core.running)

    def test_errors(self):
        self.assertEqual(self.code("appdata.defaults", {"bundleId": "com.example.podcasts"}), "not-developer")
        self.assertEqual(self.code("appdata.defaults", {}), "bad-args")
        self.assertEqual(self.code("appdata.defaults.set", {"bundleId": self.HABITS, "key": "k", "value": "x",
                                                            "type": "int"}), "bad-value")
        self.assertEqual(self.code("appdata.rows", {"bundleId": self.HABITS, "store": "/Documents/none.store",
                                                    "table": "ZHABIT"}), "no-store")

    def test_stores_and_rows(self):
        s = self.call("appdata.stores", {"bundleId": self.HABITS})
        self.assertEqual([x["kind"] for x in s["stores"]], ["swiftdata", "sqlite"])
        sd = s["stores"][0]
        self.assertEqual(sd["path"], "/Library/Application Support/default.store")
        ents = {t["entity"]: t["rows"] for t in sd["tables"] if not t["meta"]}
        self.assertEqual(ents, {"Completion": 640, "Habit": 12, "Tag": 6})
        self.assertTrue(any(t["meta"] for t in sd["tables"]))
        self.assertFalse(sd["cached"])
        self.assertTrue(self.call("appdata.stores", {"bundleId": self.HABITS})["stores"][0]["cached"])
        r = self.call("appdata.rows", {"bundleId": self.HABITS, "store": sd["path"], "table": "ZCOMPLETION",
                                       "offset": 600, "limit": 50})
        self.assertEqual((r["total"], len(r["rows"]), r["more"]), (640, 40, False))
        w = self.call("appdata.stores", {"bundleId": self.WEATHER})
        self.assertEqual(w["stores"][0]["kind"], "coredata")

    def test_reset(self):
        dry = self.call("appdata.reset", {"bundleId": self.HABITS, "mode": "wipe"})
        self.assertTrue(dry["dryRun"])
        self.assertGreater(dry["count"], 3)
        self.assertGreater(dry["bytes"], 50_000)
        paths = [x["path"] for x in dry["items"]]
        self.assertIn("/Library/Application Support/default.store", paths)
        self.assertTrue(self.call("appdata.defaults", {"bundleId": self.HABITS})["exists"])   # dry run: still there
        self.assertEqual(self.code("appdata.reset", {"bundleId": self.WEATHER, "mode": "wipe", "confirm": True}),
                         "app-running")
        done = self.call("appdata.reset", {"bundleId": self.HABITS, "mode": "wipe", "confirm": True})
        self.assertEqual((done["dryRun"], done["removed"], done["reinstalled"], done["refreshed"]),
                         (False, dry["count"], False, True))
        self.assertFalse(self.call("appdata.defaults", {"bundleId": self.HABITS})["exists"])
        self.assertEqual(self.call("appdata.stores", {"bundleId": self.HABITS})["stores"], [])
        self.assertEqual(self.call("appdata.reset", {"bundleId": self.HABITS, "mode": "wipe"})["count"], 0)

    def test_reinstall(self):
        dry = self.call("appdata.reset", {"bundleId": self.HABITS, "mode": "reinstall"})
        self.assertEqual(dry["project"], "HabitTracker")
        self.assertTrue(dry["artifact"].endswith("/HabitTracker/xtool/HabitTracker.ipa"))
        done = self.call("appdata.reset", {"bundleId": self.HABITS, "mode": "reinstall", "confirm": True})
        self.assertTrue(done["reinstalled"])
        self.assertIn(self.HABITS, self.core.installed)


class FakeAfc:
    """AFC over a local folder: just the calls deviced_features/appdata.py and files.py make."""

    def __init__(self, base: Path):
        self.base = base
        self.handles: dict = {}

    def _p(self, path: str) -> Path:
        return self.base / path.lstrip("/")

    async def listdir(self, path):
        from pymobiledevice3.exceptions import AfcFileNotFoundError
        p = self._p(path)
        if not p.is_dir():
            raise AfcFileNotFoundError("missing", 8, path)
        return sorted(x.name for x in p.iterdir())

    async def stat(self, path):
        from pymobiledevice3.exceptions import AfcFileNotFoundError
        p = self._p(path)
        if not p.exists():
            raise AfcFileNotFoundError("missing", 8, path)
        st = p.stat()
        return {"st_ifmt": "S_IFDIR" if p.is_dir() else "S_IFREG", "st_size": st.st_size,
                "st_mtime": dt.datetime.fromtimestamp(st.st_mtime)}

    async def exists(self, path):
        return self._p(path).exists()

    async def makedirs(self, path):
        self._p(path).mkdir(parents=True, exist_ok=True)

    async def fopen(self, path, mode="r"):
        h = len(self.handles) + 1
        self.handles[h] = open(self._p(path), "rb" if mode == "r" else "wb")
        return h

    async def fread(self, h, n):
        return self.handles[h].read(n)

    async def fwrite(self, h, data):
        self.handles[h].write(data)

    async def fclose(self, h):
        self.handles.pop(h).close()

    async def rename(self, a, b):
        os.replace(self._p(a), self._p(b))

    async def rm(self, path, force=False):
        p = self._p(path)
        shutil.rmtree(p) if p.is_dir() else p.unlink(missing_ok=True)
        return []


try:
    import pymobiledevice3  # noqa: F401
    HAVE_PMD3 = True
except ImportError:
    HAVE_PMD3 = False


@unittest.skipUnless(HAVE_PMD3, "needs pymobiledevice3 (the device daemon's venv)")
class RealModule(unittest.IsolatedAsyncioTestCase):
    """deviced_features/appdata.py against a fake container (FakeAfc) and a fake daemon. `prefs_hook` plays
    cfprefsd: it runs after each write and may put the old file back."""

    BID = "XTL-T.com.example.app"
    PREFS = "Library/Preferences/com.example.app.plist"

    async def asyncSetUp(self):
        from deviced_kit import FeatureRegistry
        from deviced_features import appdata as real
        from deviced_features import apps as A
        from deviced_features import files as F
        self.real = real
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        base = self.tmp / "container"
        (base / "Library" / "Preferences").mkdir(parents=True)
        (base / self.PREFS).write_bytes((FIX / "prefs" / "binary.plist").read_bytes())
        shutil.copytree(FIX / "swiftdata", base / "Library" / "Application Support")
        (base / "Documents").mkdir()
        (base / "Documents" / "a.txt").write_text("hi")
        (base / "tmp").mkdir()
        self.base, self.afc = base, FakeAfc(base)
        self.pids: list[int] = []
        self.calls: list = []
        self.prefs_hook = None
        self.slept: list[float] = []
        real_rename = self.afc.rename

        async def rename(a, b):
            await real_rename(a, b)
            if self.prefs_hook and b.endswith(".plist"):
                self.prefs_hook()
        self.afc.rename = rename

        async def app_info(d, s, bid):
            return {"bundleId": bid, "name": "App", "developer": bid == self.BID, "fileSharing": False}

        async def forget(s, bid, docs):
            return None

        async def pids(d, rsd, bid):
            return list(self.pids)

        async def nosleep(sec):
            self.slept.append(sec)
            await RealModule._real_sleep(0)
        self.build = {"project": "App", "path": "/p", "artifact": "/p/xtool/App.ipa"}
        for target, attr, val in [(F, "app_info", app_info), (F, "forget_container", forget), (A, "app_pids", pids),
                                  (AD, "CACHE", self.tmp / "cache"), (real.asyncio, "sleep", nosleep),
                                  (real, "dlog", lambda *_: None), (AD, "find_build", lambda bid: self.build)]:
            p = mock.patch.object(target, attr, val)
            p.start()
            self.addCleanup(p.stop)
        self.reg = FeatureRegistry(log=lambda *_: None)
        m = self.reg.module("appdata")
        real.register(m)
        m.commit()

        async def kill(d, a):
            self.calls.append("kill")
            self.pids.clear()
            session.tunnel_data.setdefault("app.killed", {})[a["bundleId"]] = time.monotonic()
            return {}

        async def uninstall(d, a):
            self.calls.append("uninstall")
            return {}

        async def install(d, a):
            self.calls.append(("install", a["path"]))
            if self.install_error:
                raise self.install_error
            return {"bundleId": self.BID}
        self.install_error = None
        afc = self.afc

        class HA:
            async def __aenter__(self):
                return afc

            async def __aexit__(self, *exc):
                return False
        session = SimpleNamespaceLike(tunnel_data={}, house_arrest=lambda bid, documents=False: HA())
        self.session = session
        lookups = {"app.kill": kill, "app.uninstall": uninstall, "app.install": install}

        async def rsd(a):
            return session, object()
        self.d = SimpleNamespaceLike(need_session=lambda a: session, rsd=rsd,
                                     registry=SimpleNamespaceLike(lookup=lookups.get))

    _real_sleep = asyncio.sleep

    async def call(self, cmd, a):
        return await self.reg.lookup(cmd)(self.d, a)

    def prefs(self) -> dict:
        return plistlib.loads((self.base / self.PREFS).read_bytes())

    async def test_set_reinstalls_and_verifies(self):
        r = await self.call("appdata.defaults", {"bundleId": self.BID})
        self.assertEqual((r["exists"], r["path"], r["values"]["launchCount"]),
                         (True, "/" + self.PREFS, 7))
        r = await self.call("appdata.defaults.set", {"bundleId": self.BID, "key": "launchCount", "value": 8})
        self.assertEqual((r["method"], r["refreshed"], r["verified"], r["attempts"], r["needsRelaunch"]),
                         ("reinstall", True, True, 1, True))
        self.assertEqual(self.calls, [("install", "/p/xtool/App.ipa")])
        self.assertEqual(self.prefs()["launchCount"], 8)
        self.assertEqual(self.prefs()["blob"], bytes(range(200)))
        self.assertFalse(list((self.base / "Library/Preferences").glob("*.iosstudio-new")))
        r = await self.call("appdata.defaults.set", {"bundleId": self.BID, "key": "when", "value": "2026-11-07T10:00:00.5Z",
                                                     "type": "date", "refresh": False})
        self.assertEqual((r["method"], r["verified"]), ("file", True))       # dates compare after the round trip
        self.assertIn("cached copy", r["warning"])
        self.assertNotIn("launchArgs", r)                                     # a date has no -key form here
        r = await self.call("appdata.defaults.set", {"bundleId": self.BID, "key": "flag", "value": True, "refresh": False})
        self.assertEqual(r["launchArgs"], ["-flag", "YES"])

    async def test_stop_waits_for_cfprefsd(self):
        self.pids[:] = [812]
        from common import HelperError
        with self.assertRaises(HelperError) as c:
            await self.call("appdata.defaults.delete", {"bundleId": self.BID, "key": "blob"})
        self.assertEqual(c.exception.code, "app-running")
        self.assertEqual(c.exception.fix["section"], "data")
        r = await self.call("appdata.defaults.delete", {"bundleId": self.BID, "key": "blob", "stopApp": True})
        self.assertTrue(r["stopped"])
        self.assertGreater(r["waited"], 9)                                    # the flush grace after the kill
        self.assertIn("kill", self.calls)
        self.assertNotIn("blob", self.prefs())
        self.session.tunnel_data["appdata.stopped"].pop(self.BID)           # killed by someone else 4 s ago
        self.session.tunnel_data["app.killed"][self.BID] = time.monotonic() - 4
        d = await self.call("appdata.defaults", {"bundleId": self.BID})
        self.assertAlmostEqual(d["waited"], 6, delta=0.5)
        with self.assertRaises(HelperError) as c:
            await self.call("appdata.defaults", {"bundleId": "com.other"})
        self.assertEqual(c.exception.code, "not-developer")

    async def test_rewrites_when_cfprefsd_puts_the_old_file_back(self):
        old = (self.base / self.PREFS).read_bytes()
        hits = []

        def cfprefsd():
            if not hits:                      # its late save lands over our first write
                hits.append(1)
                (self.base / self.PREFS).write_bytes(old)
        self.prefs_hook = cfprefsd
        r = await self.call("appdata.defaults.set", {"bundleId": self.BID, "key": "userName", "value": "Sam"})
        self.assertEqual((r["verified"], r["attempts"]), (True, 2))
        self.assertEqual(self.prefs()["userName"], "Sam")
        self.prefs_hook = lambda: (self.base / self.PREFS).write_bytes(old)   # it always wins
        r = await self.call("appdata.defaults.set", {"bundleId": self.BID, "key": "userName", "value": "Kim"})
        self.assertEqual((r["verified"], r["attempts"]), (False, self.real.WRITE_TRIES))

    async def test_reverted_is_reported_once(self):
        r = await self.call("appdata.defaults.set", {"bundleId": self.BID, "key": "userName", "value": "Sam",
                                                     "refresh": False})
        self.assertTrue(r["verified"])
        obj = self.prefs()
        obj["userName"] = "Alex"                                              # iOS's cached copy came back
        (self.base / self.PREFS).write_bytes(plistlib.dumps(obj, fmt=plistlib.FMT_BINARY))
        d = await self.call("appdata.defaults", {"bundleId": self.BID})
        self.assertEqual(d["reverted"], [{"key": "userName", "expected": "Sam", "actual": "Alex"}])
        self.assertNotIn("reverted", await self.call("appdata.defaults", {"bundleId": self.BID}))

    async def test_no_build_keeps_the_write(self):
        self.build = None
        r = await self.call("appdata.defaults.set", {"bundleId": self.BID, "key": "volume", "value": 0.75})
        self.assertEqual((r["method"], r["refreshed"], r["refreshError"]["code"]), ("file", False, "no-build"))
        self.assertEqual(r["launchArgs"], ["-volume", "0.75"])
        self.assertEqual(self.prefs()["volume"], 0.75)
        from common import HelperError
        self.build = {"artifact": "/p/App.ipa"}
        self.install_error = HelperError("profile-expired", "The app's profile expired.")
        r = await self.call("appdata.defaults.refresh", {"bundleId": self.BID})
        self.assertEqual((r["ok"], r["refreshError"]["code"]), (False, "profile-expired"))

    async def test_stores_rows_and_cache(self):
        s = await self.call("appdata.stores", {"bundleId": self.BID})
        st = s["stores"][0]
        self.assertEqual((st["kind"], st["cached"]), ("swiftdata", False))
        self.assertEqual(next(t for t in st["tables"] if t["name"] == "ZITEM")["rows"], 520)
        again = await self.call("appdata.stores", {"bundleId": self.BID})
        self.assertTrue(again["stores"][0]["cached"])
        r = await self.call("appdata.rows", {"bundleId": self.BID, "store": st["path"], "table": "Item", "offset": 510})
        self.assertEqual((len(r["rows"]), r["more"]), (10, False))
        self.assertTrue((self.base / "Library/Application Support/default.store-wal").read_bytes())   # untouched

    async def test_reset(self):
        dry = await self.call("appdata.reset", {"bundleId": self.BID})
        self.assertTrue(dry["dryRun"])
        self.assertIn("/Documents/a.txt", [x["path"] for x in dry["items"]])
        self.assertTrue((self.base / "Documents/a.txt").exists())
        done = await self.call("appdata.reset", {"bundleId": self.BID, "confirm": True})
        self.assertEqual((done["removed"], done["refreshed"]), (dry["count"], True))
        self.assertFalse((self.base / "Documents/a.txt").exists())
        self.assertTrue((self.base / "Library/Preferences").is_dir())          # kept, emptied
        from common import HelperError
        self.build = None
        with self.assertRaises(HelperError) as c:
            await self.call("appdata.reset", {"bundleId": self.BID, "mode": "reinstall"})
        self.assertEqual(c.exception.code, "no-build")
        self.calls.clear()
        r = await self.call("appdata.reset", {"bundleId": self.BID, "mode": "reinstall", "confirm": True,
                                              "artifact": "/x/App.ipa"})
        self.assertTrue(r["reinstalled"])
        self.assertEqual(self.calls, ["uninstall", ("install", "/x/App.ipa")])


class SimpleNamespaceLike:
    def __init__(self, **kw):
        self.__dict__.update(kw)


if __name__ == "__main__":
    unittest.main()
