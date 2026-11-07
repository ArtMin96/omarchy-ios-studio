"""App data: an app's saved state on the iPhone (UserDefaults, SwiftData / Core Data stores) and resets.

Pure logic shared by the real daemon feature (helper/iosstudio/device/features/appdata.py), its demo twin and the
tests. Stdlib only; never talks to a phone. See docs/features/app-data.md.

UserDefaults: `Library/Preferences/<bundle id>.plist` (binary or XML) <-> JSON-safe values. Plist types
that JSON lacks are tagged objects, so a value read can be written back unchanged:
    bytes     {"$type": "data", "base64": "...", "size": n}       (+ "truncated": true when cut for display)
    datetime  {"$type": "date", "iso": "2026-11-07T10:00:00Z"}
    UID       {"$type": "uid", "value": n}
    inf/nan   {"$type": "float", "value": "inf" | "-inf" | "nan"}
    a dict whose own keys include "$type": {"$type": "dict", "value": {...}}
Display copies clip long strings, data and big containers into
    {"$type": "string" | "array" | "dict" | "data", "truncated": true, "preview": ..., "length" | "count" | "size": n}
which are refused on write (edit the full value instead).

Stores: `*.store` / `*.sqlite` (+ `-wal`, `-shm`) under Library/Application Support and Documents. A pulled
copy is checkpointed (WAL folded into the main file) and then only opened read-only and immutable.
"""
from __future__ import annotations

import base64
import binascii
import contextlib
import datetime as dt
import json
import math
import os
import plistlib
import posixpath
import re
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable

from iosstudio.core import appdirs, fileio

CACHE = appdirs.CACHE_DIR / "appdata"
KEEP_APPS = 5              # apps whose pulled stores stay cached
BACKUPS_KEPT = 10          # plist backups per app made before each write
MAX_PLIST = 8 << 20        # biggest preferences file read
MAX_STORE = 1 << 30        # biggest store pulled (main + wal)
MAX_ROWS = 200             # appdata.rows limit cap
CELL_TEXT = 256            # characters of a text cell
CELL_BLOB = 16             # bytes of a blob shown as hex
VIEW_STRING = 2000         # characters of a defaults string shown before it is clipped
VIEW_DATA = 48             # bytes of a defaults Data value shown
VIEW_ITEMS = 200           # array items / dict keys shown per container
VIEW_BYTES = 256 << 10     # JSON bytes of values in one appdata.defaults reply (full=false)
RESET_ITEMS = 200          # items listed in a reset dry run
TYPES = ("string", "int", "float", "bool", "date", "data", "json")
EPOCH_2001 = 978307200     # Core Data timestamps count seconds from 2001-01-01 UTC

STORE_EXT = (".store", ".sqlite", ".sqlite3", ".db")
SIDE_EXT = ("-wal", "-shm")
STORE_DIRS = ("/Library/Application Support", "/Documents")
RESET_TOPS = ("/Documents", "/Library", "/tmp")
# folders iOS expects to exist: a wipe empties them instead of deleting them
RESET_KEEP = ("/Documents", "/Library", "/tmp", "/Library/Preferences", "/Library/Caches",
              "/Library/Application Support")
META_TABLES = {"Z_PRIMARYKEY", "Z_METADATA", "Z_MODELCACHE", "ATRANSACTION", "ACHANGE", "ATRANSACTIONSTRING"}
META_COLUMNS = {"Z_PK", "Z_ENT", "Z_OPT"}


class AppDataError(Exception):
    """Bad input (code + plain message); the daemon turns it into err(code, message)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ================================================================ bundle ids

def plain_bundle_id(bid: str) -> str:
    """'XTL-<TEAM>.com.example.app' (how free teams install it) -> 'com.example.app'."""
    return bid.split(".", 1)[1] if bid.startswith("XTL-") and "." in bid else bid


def defaults_candidates(bid: str) -> list[str]:
    """Preferences paths to try, best first (UserDefaults uses the installed bundle id)."""
    out = [f"/Library/Preferences/{bid}.plist"]
    plain = plain_bundle_id(bid)
    if plain != bid:
        out.append(f"/Library/Preferences/{plain}.plist")
    return out


def pick_defaults(bid: str, names: Iterable[str]) -> str | None:
    """The preferences file for `bid` among the names in Library/Preferences (None: none yet)."""
    names = list(names)
    for c in defaults_candidates(bid):
        if posixpath.basename(c) in names:
            return c
    plain = plain_bundle_id(bid)
    for n in sorted(names):
        if n.endswith("." + plain + ".plist"):
            return "/Library/Preferences/" + n
    return None


# ================================================================ plist <-> JSON

def _iso(d: dt.datetime) -> str:
    if d.tzinfo is not None:
        d = d.astimezone(dt.timezone.utc).replace(tzinfo=None)
    s = d.isoformat(timespec="microseconds" if d.microsecond else "seconds")
    return s + "Z"


def parse_date(v: Any) -> dt.datetime:
    """ISO 8601 (Z or an offset; a bare date is midnight UTC) or Unix seconds -> naive UTC datetime."""
    if isinstance(v, bool):
        raise AppDataError("bad-value", "A date needs an ISO 8601 text like 2026-11-07T10:00:00Z.")
    if isinstance(v, (int, float)):
        return dt.datetime.fromtimestamp(float(v), dt.timezone.utc).replace(tzinfo=None)
    s = str(v or "").strip()
    if not s:
        raise AppDataError("bad-value", "A date needs an ISO 8601 text like 2026-11-07T10:00:00Z.")
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    try:
        d = dt.datetime.fromisoformat(s)
    except ValueError:
        raise AppDataError("bad-value", f"'{v}' isn't a date. Use ISO 8601, e.g. 2026-11-07T10:00:00Z.") from None
    if d.tzinfo is not None:
        d = d.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return d


def to_json(v: Any) -> Any:
    """A plist value -> a JSON-safe value (tagged where JSON lacks the type). Lossless: from_json() inverts it."""
    if isinstance(v, bool) or v is None or isinstance(v, (int, str)):
        return v
    if isinstance(v, float):
        if math.isfinite(v):
            return v
        return {"$type": "float", "value": "nan" if math.isnan(v) else ("inf" if v > 0 else "-inf")}
    if isinstance(v, (bytes, bytearray)):
        return {"$type": "data", "base64": base64.b64encode(bytes(v)).decode(), "size": len(v)}
    if isinstance(v, dt.datetime):
        return {"$type": "date", "iso": _iso(v)}
    if isinstance(v, plistlib.UID):
        return {"$type": "uid", "value": int(v.data)}
    if isinstance(v, dict):
        out = {str(k): to_json(x) for k, x in v.items()}
        return {"$type": "dict", "value": out} if "$type" in out else out
    if isinstance(v, (list, tuple)):
        return [to_json(x) for x in v]
    return str(v)


def from_json(v: Any) -> Any:
    """A JSON value (as to_json() makes them) -> a plist value. Display-only clipped values are refused."""
    if v is None:
        raise AppDataError("bad-value", "Property lists can't store null. Delete the key instead.")
    if isinstance(v, bool) or isinstance(v, (int, str)):
        return v
    if isinstance(v, float):
        return v
    if isinstance(v, list):
        return [from_json(x) for x in v]
    if isinstance(v, dict):
        t = v.get("$type")
        if t is None:
            return {str(k): from_json(x) for k, x in v.items()}
        if v.get("truncated"):
            raise AppDataError("truncated-value", "That value was shortened for display. Read the full value "
                                                  "(full=true) and edit that instead.")
        if t == "data":
            return _b64(v.get("base64", ""))
        if t == "date":
            return parse_date(v.get("iso"))
        if t == "uid":
            return plistlib.UID(int(v.get("value") or 0))
        if t == "float":
            return float(v.get("value"))
        if t == "dict":
            inner = v.get("value")
            if not isinstance(inner, dict):
                raise AppDataError("bad-value", "A tagged dict needs a 'value' object.")
            return {str(k): from_json(x) for k, x in inner.items()}
        raise AppDataError("bad-value", f"Unknown value type '{t}'.")
    raise AppDataError("bad-value", f"Can't store a {type(v).__name__} in a property list.")


def _b64(s: Any) -> bytes:
    if isinstance(s, (bytes, bytearray)):
        return bytes(s)
    try:
        return base64.b64decode(str(s or "").strip(), validate=True)
    except (binascii.Error, ValueError):
        raise AppDataError("bad-value", "Data must be base64 text.") from None


def coerce(value: Any, type_: str | None = None) -> Any:
    """The plist value to store for a JSON value. No type: from the JSON type (text stays text, a tagged
    object keeps its tag). type: string, int, float, bool, date (ISO 8601), data (base64) or json (text
    holding JSON: an array or object typed into a text field)."""
    t = (type_ or "").strip().lower() or None
    if t is None:
        return from_json(value)
    if t not in TYPES:
        raise AppDataError("bad-type", f"Unknown type '{type_}'. Use one of: {', '.join(TYPES)}.")
    if t == "json":
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError as exc:
                raise AppDataError("bad-value", f"That isn't valid JSON ({exc.msg} at character {exc.pos}).") from None
        return from_json(value)
    if isinstance(value, dict) and value.get("$type"):
        value = from_json(value)
        if t == "date" and isinstance(value, dt.datetime) or t == "data" and isinstance(value, bytes):
            return value
    if t == "string":
        if isinstance(value, (dict, list)):
            raise AppDataError("bad-value", "A string can't hold an array or dictionary (use type json).")
        return value if isinstance(value, str) else json.dumps(value) if isinstance(value, bool) else str(value)
    if t == "bool":
        if isinstance(value, bool):
            return value
        s = str(value).strip().lower()
        if s in ("true", "yes", "1", "on"):
            return True
        if s in ("false", "no", "0", "off"):
            return False
        raise AppDataError("bad-value", f"'{value}' isn't true or false.")
    if t == "int":
        if isinstance(value, bool):
            raise AppDataError("bad-value", "Use type bool for true/false.")
        if isinstance(value, float):
            if not value.is_integer():
                raise AppDataError("bad-value", f"{value} isn't a whole number (use type float).")
            return int(value)
        try:
            return int(str(value).strip())
        except ValueError:
            raise AppDataError("bad-value", f"'{value}' isn't a whole number.") from None
    if t == "float":
        if isinstance(value, bool):
            raise AppDataError("bad-value", "Use type bool for true/false.")
        try:
            return float(str(value).strip()) if not isinstance(value, (int, float)) else float(value)
        except ValueError:
            raise AppDataError("bad-value", f"'{value}' isn't a number.") from None
    if t == "date":
        return parse_date(value)
    if t == "data":
        return _b64(value)
    raise AppDataError("bad-type", f"Unknown type '{type_}'.")  # pragma: no cover


def type_name(v: Any) -> str:
    """The plist type of a value, as `type` arguments name them (plus array / dict / uid)."""
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, str):
        return "string"
    if isinstance(v, (bytes, bytearray)):
        return "data"
    if isinstance(v, dt.datetime):
        return "date"
    if isinstance(v, plistlib.UID):
        return "uid"
    if isinstance(v, dict):
        return "dict"
    if isinstance(v, (list, tuple)):
        return "array"
    return type(v).__name__


def view(v: Any, depth: int = 0) -> tuple[Any, bool]:
    """(display JSON, clipped?): like to_json() but long strings, data and big containers are cut."""
    if isinstance(v, str) and len(v) > VIEW_STRING:
        return {"$type": "string", "truncated": True, "preview": v[:VIEW_STRING], "length": len(v)}, True
    if isinstance(v, (bytes, bytearray)) and len(v) > VIEW_DATA:
        return {"$type": "data", "truncated": True, "base64": base64.b64encode(bytes(v[:VIEW_DATA])).decode(),
                "size": len(v)}, True
    if isinstance(v, dict):
        if depth > 12:
            return {"$type": "dict", "truncated": True, "count": len(v)}, True
        cut = False
        out = {}
        for i, (k, x) in enumerate(v.items()):
            if i >= VIEW_ITEMS:
                return {"$type": "dict", "truncated": True, "preview": out, "count": len(v)}, True
            out[str(k)], c = view(x, depth + 1)
            cut = cut or c
        return ({"$type": "dict", "value": out} if "$type" in out else out), cut
    if isinstance(v, (list, tuple)):
        if depth > 12:
            return {"$type": "array", "truncated": True, "count": len(v)}, True
        items, cut = [], False
        for x in list(v)[:VIEW_ITEMS]:
            j, c = view(x, depth + 1)
            items.append(j)
            cut = cut or c
        if len(v) > VIEW_ITEMS:
            return {"$type": "array", "truncated": True, "preview": items, "count": len(v)}, True
        return items, cut
    return to_json(v), False


def load_plist(data: bytes) -> tuple[dict, Any]:
    """(top-level dict, format) of a preferences file. Empty data is an empty dict (binary)."""
    if not data.strip():
        return {}, plistlib.FMT_BINARY
    fmt = plistlib.FMT_BINARY if data.startswith(b"bplist") else plistlib.FMT_XML
    try:
        obj = plistlib.loads(data)
    except Exception as exc:  # noqa: BLE001 - plistlib raises several types
        raise AppDataError("bad-plist", f"The preferences file isn't a valid property list ({exc}).") from None
    if not isinstance(obj, dict):
        raise AppDataError("bad-plist", "The preferences file doesn't hold a dictionary.")
    return obj, fmt


def dump_plist(obj: dict, fmt: Any = plistlib.FMT_BINARY) -> bytes:
    try:
        return plistlib.dumps(obj, fmt=fmt, sort_keys=False)
    except (TypeError, OverflowError, ValueError) as exc:
        raise AppDataError("bad-value", f"That value can't be stored in a property list ({exc}).") from None


def read_defaults(data: bytes, full: bool = False, max_bytes: int = VIEW_BYTES) -> dict:
    """{keys, values, types, truncated, size} of a preferences file. Without full, long values are clipped
    (view()) and keys stop once the values pass max_bytes of JSON."""
    obj, fmt = load_plist(data)
    values: dict = {}
    types: dict = {}
    truncated = False
    used = 0
    for k, v in obj.items():
        j, cut = (to_json(v), False) if full else view(v)
        b = len(json.dumps(j, ensure_ascii=False, separators=(",", ":"))) + len(k) + 4
        if not full and values and used + b > max_bytes:
            truncated = True
            break
        values[str(k)] = j
        types[str(k)] = type_name(v)
        used += b
        truncated = truncated or cut
    return {"keys": len(obj), "values": values, "types": types, "truncated": truncated, "size": len(data),
            "format": "binary" if fmt == plistlib.FMT_BINARY else "xml"}


def set_key(data: bytes | None, key: str, value: Any, type_: str | None = None) -> tuple[bytes, dict]:
    """(new file bytes, {existed, previous?, value, type}) with `key` set. The file keeps its format."""
    key = check_key(key)
    obj, fmt = load_plist(data or b"")
    new = coerce(value, type_)
    out = {"existed": key in obj, "value": to_json(new), "type": type_name(new)}
    if key in obj:
        out["previous"] = view(obj[key])[0]
        out["previousType"] = type_name(obj[key])
    obj[key] = new
    return dump_plist(obj, fmt), out


def delete_key(data: bytes | None, key: str) -> tuple[bytes | None, dict]:
    """(new file bytes or None when unchanged, {existed, previous?})."""
    key = check_key(key)
    obj, fmt = load_plist(data or b"")
    if key not in obj:
        return None, {"existed": False}
    prev = obj.pop(key)
    return dump_plist(obj, fmt), {"existed": True, "previous": view(prev)[0], "previousType": type_name(prev)}


def check_key(key: Any) -> str:
    k = str(key if key is not None else "")
    if not k:
        raise AppDataError("bad-args", "Which key? (key missing)")
    if len(k) > 1024 or "\x00" in k:
        raise AppDataError("bad-args", "That key isn't allowed.")
    return k


# ---------------------------------------------------------------- cfprefsd's cached copy

CACHED_WARNING = ("The new value is in the app's settings file, but iOS may still hand the app its cached copy "
                  "(cfprefsd) and write that back. Without a build to reinstall it can't be cleared from here: "
                  "restart the iPhone, or pass the value as a launch argument for one launch.")
NO_BUILD_REFRESH = ("No signed build of this app was found on this computer, so iOS's cached copy of its settings "
                    "couldn't be cleared. Build the app once (Projects › Run) so the next change can.")
WRITES_TTL = 7 * 86400     # how long a write is remembered for the reverted check


def same_value(a: Any, b: Any) -> bool:
    """Plist values equal after a round trip through the file (dates to the millisecond)."""
    if isinstance(a, dt.datetime) and isinstance(b, dt.datetime):
        return abs((a - b).total_seconds()) < 0.001
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(same_value(a[k], b[k]) for k in a)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(same_value(x, y) for x, y in zip(a, b))
    return type(a) is type(b) and a == b if isinstance(a, bool) or isinstance(b, bool) else a == b


def launch_args(key: str, value: Any) -> list[str] | None:
    """["-key", "value"]: arguments that override one default for a single launch (NSArgumentDomain). Only for
    strings, numbers and bools; None for other types."""
    if isinstance(value, bool):
        return ["-" + key, "YES" if value else "NO"]
    if isinstance(value, (int, float)):
        return ["-" + key, repr(value) if isinstance(value, float) else str(value)]
    if isinstance(value, str):
        return ["-" + key, value]
    return None


def _writes_file(bid: str) -> Path:
    return app_cache(bid) / "writes.json"


def _load_writes(bid: str) -> dict:
    try:
        w = json.loads(_writes_file(bid).read_text())
        return w if isinstance(w, dict) else {}
    except (OSError, ValueError):
        return {}


def remember_write(bid: str, key: str, previous: Any, value: Any, action: str) -> None:
    """Note a write so a later read can tell when iOS put the old value back (check_reverted)."""
    w = {k: v for k, v in _load_writes(bid).items() if time.time() - float(v.get("time", 0)) < WRITES_TTL}
    w[key] = {"previous": previous, "value": value, "action": action, "time": time.time()}
    with contextlib.suppress(OSError):
        _writes_file(bid).parent.mkdir(parents=True, exist_ok=True)
        _writes_file(bid).write_text(json.dumps(w))


def forget_writes(bid: str) -> None:
    with contextlib.suppress(OSError):
        _writes_file(bid).unlink()


def check_reverted(bid: str, obj: dict) -> list[dict]:
    """[{key, expected, actual}] for remembered writes whose key holds its old value again (iOS's cached copy
    won). A key the app changed to something else is not flagged. Reported once, then forgotten."""
    w = _load_writes(bid)
    if not w:
        return []
    out, keep = [], {}
    for key, e in w.items():
        if time.time() - float(e.get("time", 0)) >= WRITES_TTL:
            continue
        if e.get("action") == "delete":
            back = key in obj and e.get("previous") is not None and view(obj[key])[0] == e["previous"]
            expected = None
        else:
            back = key in obj and e.get("previous") is not None and view(obj[key])[0] == e["previous"] \
                and e["previous"] != e.get("value")
            expected = e.get("value")
        if back:
            out.append({"key": key, "expected": expected, "actual": view(obj[key])[0]})
        else:
            keep[key] = e
    with contextlib.suppress(OSError):
        if keep:
            _writes_file(bid).write_text(json.dumps(keep))
        else:
            _writes_file(bid).unlink(missing_ok=True)
    return out


# ================================================================ stores

def store_groups(files: Iterable[tuple[str, int]]) -> list[dict]:
    """Group a file listing [(phone path, size)] into stores: [{name, path, size, files: [path…]}]. A store
    is a *.store / *.sqlite / *.sqlite3 / *.db file; its -wal / -shm siblings belong to it."""
    sizes = dict(files)
    out = []
    for p in sorted(sizes):
        low = p.lower()
        if not low.endswith(STORE_EXT):
            continue
        group = [p] + [p + s for s in SIDE_EXT if p + s in sizes]
        out.append({"name": posixpath.basename(p), "path": p, "size": sum(sizes[f] for f in group),
                    "files": group, "mainSize": sizes[p]})
    return out


def kind_of(name: str, tables: Iterable[str]) -> str:
    """swiftdata (a Core Data schema in a .store file, SwiftData's default.store), coredata or sqlite."""
    t = set(tables)
    if {"Z_PRIMARYKEY", "Z_METADATA"} <= t:
        return "swiftdata" if name.lower().endswith(".store") else "coredata"
    return "sqlite"


def sort_stores(stores: list[dict]) -> list[dict]:
    """SwiftData first, then Core Data, then other SQLite files; by path within a kind."""
    order = {"swiftdata": 0, "coredata": 1, "sqlite": 2}
    return sorted(stores, key=lambda x: (order.get(x.get("kind"), 3), x.get("path") or ""))


def is_meta_table(name: str) -> bool:
    return name in META_TABLES or name.startswith("Z_")


def checkpoint(db: Path) -> None:
    """Fold a pulled copy's -wal into the main file and drop -wal/-shm, so later reads can be immutable.
    Only ever run on our own copy."""
    con = sqlite3.connect(str(db), timeout=5)
    try:
        mode = con.execute("PRAGMA journal_mode").fetchone()[0]
        if str(mode).lower() == "wal":
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            con.execute("PRAGMA journal_mode=DELETE")
    finally:
        con.close()
    for s in SIDE_EXT:
        with contextlib.suppress(OSError):
            Path(str(db) + s).unlink()


@contextlib.contextmanager
def open_ro(db: Path):
    """A read-only, immutable connection to a (checkpointed) local copy."""
    uri = "file:" + _uri_path(db) + "?mode=ro&immutable=1"
    con = sqlite3.connect(uri, uri=True, timeout=5)
    con.text_factory = lambda b: b.decode("utf-8", "replace")
    try:
        yield con
    finally:
        con.close()


def _uri_path(p: Path) -> str:
    from urllib.parse import quote
    return quote(str(p.resolve()), safe="/")


def _qi(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def summarize(db: Path, name: str | None = None) -> dict:
    """{kind, tables: [{name, entity?, rows, columns: [{name, type, meta?}], meta}]} of a local copy.
    Entities first (by name), then metadata tables. sqlite_* tables are left out."""
    name = name or db.name
    try:
        with open_ro(db) as con:
            names = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            names = [n for n in names if not n.startswith("sqlite_")]
            kind = kind_of(name, names)
            entities = {}
            if "Z_PRIMARYKEY" in names:
                with contextlib.suppress(sqlite3.Error):
                    for (ename,) in con.execute("SELECT Z_NAME FROM Z_PRIMARYKEY"):
                        if ename:
                            entities["Z" + str(ename).upper()] = str(ename)
            tables = []
            for n in names:
                cols = [{"name": c[1], "type": c[2] or ""} for c in con.execute(f"PRAGMA table_info({_qi(n)})")]
                core = kind != "sqlite"
                if core:
                    for c in cols:
                        if c["name"] in META_COLUMNS:
                            c["meta"] = True
                try:
                    rows = con.execute(f"SELECT COUNT(*) FROM {_qi(n)}").fetchone()[0]
                except sqlite3.Error:
                    rows = None
                t = {"name": n, "rows": rows, "columns": cols, "meta": core and is_meta_table(n)}
                if core and not t["meta"]:
                    t["entity"] = entities.get(n) or (n[1:].capitalize() if n.startswith("Z") and len(n) > 1 else n)
                tables.append(t)
    except sqlite3.DatabaseError as exc:
        raise AppDataError("bad-store", f"{name} isn't a readable SQLite database ({exc}).") from None
    tables.sort(key=lambda t: (t["meta"], (t.get("entity") or t["name"]).lower()))
    return {"kind": kind, "tables": tables}


def cell(v: Any, date: bool = False) -> Any:
    """A bounded, JSON-safe cell. Text over CELL_TEXT characters and blobs become tagged objects; Core
    Data TIMESTAMP columns (seconds since 2001) get an ISO date next to the raw number."""
    if isinstance(v, (bytes, bytearray, memoryview)):
        b = bytes(v)
        out = {"$type": "blob", "size": len(b), "hex": b[:CELL_BLOB].hex()}
        if len(b) > CELL_BLOB:
            out["truncated"] = True
        return out
    if isinstance(v, str) and len(v) > CELL_TEXT:
        return {"$type": "text", "preview": v[:CELL_TEXT], "length": len(v), "truncated": True}
    if isinstance(v, float) and not math.isfinite(v):
        return {"$type": "float", "value": "nan" if math.isnan(v) else ("inf" if v > 0 else "-inf")}
    if date and isinstance(v, (int, float)) and not isinstance(v, bool):
        with contextlib.suppress(OverflowError, OSError, ValueError):
            d = dt.datetime.fromtimestamp(EPOCH_2001 + float(v), dt.timezone.utc).replace(tzinfo=None)
            return {"$type": "date", "iso": _iso(d), "raw": v}
    return v


def rows(db: Path, table: str, offset: int = 0, limit: int = 50, kind: str | None = None) -> dict:
    """{table, columns, rows: [[cell…]], offset, limit, total, more} of one table in a local copy."""
    offset = max(0, int(offset or 0))
    limit = max(1, min(int(limit or 50), MAX_ROWS))
    try:
        with open_ro(db) as con:
            names = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            if table not in names:
                low = {n.lower(): n for n in names}
                table = low.get(str(table).lower()) or low.get("z" + str(table).lower())
                if not table:
                    raise AppDataError("no-table", "That table isn't in this store.")
            kind = kind or kind_of(db.name, names)
            info = list(con.execute(f"PRAGMA table_info({_qi(table)})"))
            cols = [{"name": c[1], "type": c[2] or ""} for c in info]
            dates = [kind != "sqlite" and (c["type"] or "").upper() == "TIMESTAMP" for c in cols]
            pk = [c[1] for c in info if c[5]]
            order = f" ORDER BY {', '.join(_qi(p) for p in pk)}" if pk else ""
            if not pk:
                with contextlib.suppress(sqlite3.Error):
                    con.execute(f"SELECT rowid FROM {_qi(table)} LIMIT 0")
                    order = " ORDER BY rowid"
            total = con.execute(f"SELECT COUNT(*) FROM {_qi(table)}").fetchone()[0]
            cur = con.execute(f"SELECT * FROM {_qi(table)}{order} LIMIT ? OFFSET ?", (limit, offset))
            out = [[cell(v, dates[i]) for i, v in enumerate(r)] for r in cur]
    except sqlite3.DatabaseError as exc:
        raise AppDataError("bad-store", f"This store isn't a readable SQLite database ({exc}).") from None
    for c, d in zip(cols, dates):
        if d:
            c["date"] = True
    return {"table": table, "columns": cols, "rows": out, "offset": offset, "limit": limit, "total": total,
            "more": offset + len(out) < total}


# ---------------------------------------------------------------- the local cache of pulled stores

def app_cache(bid: str) -> Path:
    return CACHE / re.sub(r"[^A-Za-z0-9._-]", "_", bid)[:200]


def cached_store(bid: str, group: dict, stamp: dict) -> Path | None:
    """The checkpointed local copy of a store when its phone files still match `stamp` ({path: [size, mtime]})."""
    d = app_cache(bid) / fileio.path_key(group["path"])
    db = d / posixpath.basename(group["path"])
    try:
        man = json.loads((d / "manifest.json").read_text())
    except (OSError, ValueError):
        return None
    return db if man.get("stamp") == stamp and db.is_file() else None


def any_cached(bid: str, path: str) -> Path | None:
    """The last copy of a store, whatever its stamp (appdata.rows after appdata.stores)."""
    d = app_cache(bid) / fileio.path_key(path)
    db = d / posixpath.basename(path)
    return db if db.is_file() else None


def new_pull_dir(bid: str) -> Path:
    base = app_cache(bid)
    base.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=".pull-", dir=base))


def commit_pull(bid: str, group: dict, tmp: Path, stamp: dict) -> Path:
    """Checkpoint a finished pull in `tmp` and move it into place (replacing the older copy). On any error
    the temp folder is removed."""
    try:
        db = tmp / posixpath.basename(group["path"])
        checkpoint(db)
        (tmp / "manifest.json").write_text(json.dumps({"path": group["path"], "stamp": stamp,
                                                        "pulledAt": time.time()}))
        final = app_cache(bid) / fileio.path_key(group["path"])
        if final.exists():
            shutil.rmtree(final, ignore_errors=True)
        os.replace(tmp, final)
        return final / db.name
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def prune_cache(bid: str, keep_paths: Iterable[str] = ()) -> None:
    """Drop copies of stores the app no longer has, stale temp pulls, and apps beyond the newest KEEP_APPS."""
    base = app_cache(bid)
    keep = {fileio.path_key(p) for p in keep_paths}
    if base.is_dir():
        for d in base.iterdir():
            if d.name in ("backups", "writes.json"):
                continue
            if d.name.startswith(".pull-"):
                with contextlib.suppress(OSError):
                    if time.time() - d.stat().st_mtime > 3600:
                        shutil.rmtree(d, ignore_errors=True)
                continue
            if d.is_dir() and d.name not in keep:
                shutil.rmtree(d, ignore_errors=True)
        with contextlib.suppress(OSError):
            os.utime(base)
    if CACHE.is_dir():
        fileio.prune([p for p in CACHE.iterdir() if p.is_dir()], KEEP_APPS, min_keep=1, protect=[base])


def forget_cache(bid: str) -> None:
    """After a wipe or reinstall: the cached copies describe data that is gone."""
    base = app_cache(bid)
    if base.is_dir():
        for d in base.iterdir():
            if d.name not in ("backups", "writes.json"):
                shutil.rmtree(d, ignore_errors=True) if d.is_dir() else d.unlink(missing_ok=True)


def backup_plist(bid: str, data: bytes) -> str | None:
    """Keep the preferences file as it was before a write (the newest BACKUPS_KEPT per app)."""
    if not data:
        return None
    d = app_cache(bid) / "backups"
    try:
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{time.strftime('%Y%m%d-%H%M%S')}-{int(time.time() * 1000) % 1000:03d}.plist"
        p.write_bytes(data)
        fileio.prune(list(d.glob("*.plist")), BACKUPS_KEPT, min_keep=1)
        return str(p)
    except OSError:
        return None


# ================================================================ reset

def _category(path: str) -> str:
    if path.startswith("/Documents"):
        return "documents"
    if path.startswith("/tmp"):
        return "tmp"
    if path.startswith("/Library/Preferences"):
        return "preferences"
    if path.startswith("/Library/Caches"):
        return "caches"
    if path.startswith("/Library/Application Support"):
        return "app-support"
    return "library"


def reset_item(path: str) -> str | None:
    """The path a wipe deletes to remove `path`: its first component below the deepest RESET_KEEP folder
    (None for a kept folder itself or anything outside Documents, Library and tmp)."""
    path = posixpath.normpath("/" + path.lstrip("/"))
    if not any(path == t or path.startswith(t + "/") for t in RESET_TOPS):
        return None
    if path in RESET_KEEP:
        return None
    parent = max((k for k in RESET_KEEP if path.startswith(k + "/")), key=len)
    rest = path[len(parent) + 1:]
    return parent + "/" + rest.split("/", 1)[0]


def reset_plan(files: Iterable[tuple[str, int]], dirs: Iterable[str] = (), limit: int = RESET_ITEMS) -> dict:
    """What a wipe removes, from a container listing (files with sizes, plus folders so empty ones count):
    {items: [{path, size, kind: file|dir, files, category}] biggest first (≤ limit), count, files, bytes,
    truncated}. Folders iOS expects (RESET_KEEP) are emptied, not deleted."""
    items: dict[str, dict] = {}
    for p, size in files:
        top = reset_item(p)
        if top is None:
            continue
        it = items.setdefault(top, {"path": top, "size": 0, "kind": "file" if top == posixpath.normpath(p) else "dir",
                                    "files": 0, "category": _category(top)})
        if top != posixpath.normpath(p):
            it["kind"] = "dir"
        it["size"] += int(size or 0)
        it["files"] += 1
    for d in dirs:
        top = reset_item(d)
        if top is not None:
            items.setdefault(top, {"path": top, "size": 0, "kind": "dir", "files": 0, "category": _category(top)})
            items[top]["kind"] = "dir"
    ordered = sorted(items.values(), key=lambda x: (-x["size"], x["path"]))
    return {"items": ordered[:limit], "count": len(ordered), "files": sum(x["files"] for x in ordered),
            "bytes": sum(x["size"] for x in ordered), "truncated": len(ordered) > limit,
            "all": [x["path"] for x in ordered]}


# ================================================================ the last build of an app

def _matches(bid: str, other: Any) -> bool:
    o = str(other or "")
    return bool(o) and (o == bid or o == plain_bundle_id(bid) or bid.endswith("." + o))


def find_build(bid: str, history: list | None = None) -> dict | None:
    """{project, path, artifact} for the newest signed build (.ipa) that produced `bid`: the build history
    first, then renewals, then the projects' own xtool/<product>.ipa. None when there is none."""
    hist = history if history is not None else fileio.load_json(appdirs.STATE_DIR / "builds.json", [])
    for e in reversed(hist if isinstance(hist, list) else []):
        if isinstance(e, dict) and e.get("result") == "ok" and e.get("ipa") and _matches(bid, e.get("bundleId")) \
                and Path(str(e["ipa"])).is_file():
            return {"project": e.get("name") or e.get("project"), "path": e.get("path"), "artifact": e["ipa"],
                    "time": e.get("time")}
    ren = fileio.load_json(appdirs.STATE_DIR / "renewals.json", {}) or {}
    for path, r in (ren.items() if isinstance(ren, dict) else []):
        if isinstance(r, dict) and _matches(bid, r.get("bundleId")):
            ipas = sorted(Path(path, "xtool").glob("*.ipa"), key=lambda p: p.stat().st_mtime, reverse=True) \
                if Path(path, "xtool").is_dir() else []
            if ipas:
                return {"project": Path(path).name, "path": path, "artifact": str(ipas[0])}
    try:
        from iosstudio.features import projects as P
        found, _ = P._scan()
        state = P._state()
        for p in list(found) + [Path(a) for a in state.get("added") or []]:
            if not (p.is_dir() and P._is_project(p)):
                continue
            info = P._project_info(p, [], {}, state)
            if _matches(bid, info.get("bundleId")) and info.get("ipa"):
                return {"project": info.get("name"), "path": info.get("path"), "artifact": info["ipa"]}
    except Exception:  # noqa: BLE001 - the scan is a last resort
        pass
    return None
