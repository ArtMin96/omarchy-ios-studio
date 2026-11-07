#!/usr/bin/env python3
"""Writes the app data fixtures next to this file (tests/test_app_data.py reads them):

  swiftdata/default.store (+ -wal, -shm)  SwiftData-shaped: ZITEM (520 rows: long text, blobs, dates),
                                          ZFOLDER, ZTAG, Z_2TAGS, Z_PRIMARYKEY, Z_METADATA, Z_MODELCACHE,
                                          ATRANSACTION. 20 of ZITEM's rows live only in the -wal file.
  coredata/Model.sqlite                   Core Data-shaped, rollback journal: ZNOTE (3 rows)
  plain/cache.db                          a plain SQLite file without Core Data tables
  not-a-db/broken.sqlite                  text, not SQLite
  prefs/binary.plist, prefs/xml.plist     the same UserDefaults in both formats

Deterministic: running it again gives the same rows (the -wal salts differ). Usage: python3 tests/fixtures/app-data/make_fixtures.py
"""
from __future__ import annotations

import datetime as dt
import plistlib
import shutil
import sqlite3
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
T0 = dt.datetime(2001, 1, 1)


def ts(d: dt.datetime) -> float:
    return (d - T0).total_seconds()


def coredata_tables(con: sqlite3.Connection, entities: list[tuple[str, str]]) -> None:
    con.execute("CREATE TABLE Z_PRIMARYKEY (Z_ENT INTEGER PRIMARY KEY, Z_NAME VARCHAR, Z_SUPER INTEGER, Z_MAX INTEGER)")
    con.execute("CREATE TABLE Z_METADATA (Z_VERSION INTEGER PRIMARY KEY, Z_UUID VARCHAR(255), Z_PLIST BLOB)")
    con.execute("CREATE TABLE Z_MODELCACHE (Z_CONTENT BLOB)")
    for i, (name, cols) in enumerate(entities, 1):
        con.execute(f"CREATE TABLE Z{name.upper()} (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER, {cols})")
        con.execute("INSERT INTO Z_PRIMARYKEY VALUES (?, ?, 0, 0)", (i, name))
    con.execute("INSERT INTO Z_METADATA VALUES (1, 'FIXTURE-UUID', ?)", (plistlib.dumps({"NSStoreType": "SQLite"}),))
    con.execute("INSERT INTO Z_MODELCACHE VALUES (?)", (b"\x00" * 64,))


def swiftdata(dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "default.store"
        con = sqlite3.connect(db)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA wal_autocheckpoint=0")
        coredata_tables(con, [("Folder", "ZNAME VARCHAR"),
                              ("Item", "ZTITLE VARCHAR, ZBODY VARCHAR, ZCREATED TIMESTAMP, ZFOLDER INTEGER, "
                                       "ZTHUMB BLOB, ZSCORE FLOAT"),
                              ("Tag", "ZNAME VARCHAR")])
        con.execute("CREATE TABLE Z_2TAGS (Z_2ITEMS INTEGER, Z_3TAGS INTEGER, PRIMARY KEY (Z_2ITEMS, Z_3TAGS))")
        con.execute("CREATE TABLE ATRANSACTION (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER, ZTIMESTAMP FLOAT)")
        con.executemany("INSERT INTO ZFOLDER VALUES (?, 1, 1, ?)", [(1, "Inbox"), (2, "Archive")])
        con.executemany("INSERT INTO ZTAG VALUES (?, 3, 1, ?)", [(1, "red"), (2, "blue")])
        con.executemany("INSERT INTO Z_2TAGS VALUES (?, ?)", [(1, 1), (2, 2)])
        start = dt.datetime(2026, 1, 1, 12, 0, 0)

        def item(n: int) -> tuple:
            body = ("long " * 200) if n == 3 else f"body {n}"
            thumb = bytes(range(256)) * 4 if n % 100 == 1 else (b"\x01\x02" if n == 2 else None)
            return (n, 2, 1, f"Item {n}", body, ts(start + dt.timedelta(hours=n)), 1 + n % 2, thumb, n / 4)
        con.executemany("INSERT INTO ZITEM VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", [item(n) for n in range(1, 501)])
        con.commit()
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")       # the first 500 rows in the main file
        con.executemany("INSERT INTO ZITEM VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", [item(n) for n in range(501, 521)])
        con.execute("UPDATE Z_PRIMARYKEY SET Z_MAX = 520 WHERE Z_NAME = 'Item'")
        con.commit()                                          # ...the last 20 only in the -wal file
        for suffix in ("", "-wal", "-shm"):
            shutil.copyfile(str(db) + suffix, dest / ("default.store" + suffix))
        con.close()


def coredata(dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    db = dest / "Model.sqlite"
    db.unlink(missing_ok=True)
    con = sqlite3.connect(db)
    coredata_tables(con, [("Note", "ZTEXT VARCHAR, ZEDITED TIMESTAMP")])
    con.executemany("INSERT INTO ZNOTE VALUES (?, 1, 1, ?, ?)", [(1, "Buy milk", 815000000.0),
                                                               (2, "Call Sam", 815003600.0), (3, None, None)])
    con.commit()
    con.close()


def plain(dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    db = dest / "cache.db"
    db.unlink(missing_ok=True)
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE responses (url TEXT, body BLOB, fetched TIMESTAMP)")   # no primary key: rowid order
    con.executemany("INSERT INTO responses VALUES (?, ?, ?)",
                    [(f"https://example.com/{i}", b"x" * i, 1700000000 + i) for i in range(5)])
    con.commit()
    con.close()


DEFAULTS = {
    "hasSeenOnboarding": True, "launchCount": 7, "volume": 0.5, "userName": "Alex",
    "lastOpened": dt.datetime(2026, 11, 6, 8, 30, 0), "recent": ["a", "b"],
    "nested": {"inner": {"deep": [1, 2.5, False, b"\x00\xff"]}, "when": dt.datetime(2020, 2, 29, 23, 59, 59)},
    "blob": bytes(range(200)), "empty": "", "uid": plistlib.UID(3),
}


def prefs(dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "binary.plist").write_bytes(plistlib.dumps(DEFAULTS, fmt=plistlib.FMT_BINARY, sort_keys=True))
    (dest / "xml.plist").write_bytes(plistlib.dumps({k: v for k, v in DEFAULTS.items() if k != "uid"},
                                                    fmt=plistlib.FMT_XML, sort_keys=True))


def main(root: Path = HERE) -> None:
    swiftdata(root / "swiftdata")
    coredata(root / "coredata")
    plain(root / "plain")
    (root / "not-a-db").mkdir(parents=True, exist_ok=True)
    (root / "not-a-db" / "broken.sqlite").write_text("This is not a database.\n")
    prefs(root / "prefs")


if __name__ == "__main__":
    main()
