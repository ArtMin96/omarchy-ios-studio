"""The projects/build state files: projects.json, builds.json + build-logs/, renewals.json (see __init__ for their shape)."""
from __future__ import annotations

import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from iosstudio.core import appdirs, fileio

PROJECTS_FILE = appdirs.STATE_DIR / "projects.json"
BUILDS_FILE = appdirs.STATE_DIR / "builds.json"
RENEWALS_FILE = appdirs.STATE_DIR / "renewals.json"
BUILD_LOGS = appdirs.STATE_DIR / "build-logs"
LOCK_DIR = appdirs.STATE_DIR / "locks"

MAX_HISTORY = 300
LOG_GRACE = 6 * 3600   # an unreferenced build log younger than this may belong to a running build
MAX_STORED_DIAGS = 200   # per builds.json entry (errors first), so Logs can show problems without re-parsing
FREE_PROFILE_DAYS = 7


# ================================================================ projects.json

def load_state() -> dict:
    """projects.json with its three keys always present."""
    data = fileio.load_json(PROJECTS_FILE, {}) or {}
    data.setdefault("added", [])
    data.setdefault("hidden", [])
    data.setdefault("meta", {})
    return data


def save_state(data: dict) -> None:
    fileio.save_json(PROJECTS_FILE, data)


@contextmanager
def file_lock(name: str) -> Iterator[None]:
    """Exclusive flock on STATE_DIR/.<name>.lock for a read-modify-write of one state file (the app,
    the MCP server and the renew timer each run their own helper processes)."""
    with fileio.lock(appdirs.STATE_DIR / f".{name}.lock"):
        yield


@contextmanager
def edit_state() -> Iterator[dict]:
    """Read projects.json under its lock and save it when the block ends without an exception."""
    with file_lock("projects"):
        state = load_state()
        yield state
        save_state(state)


# ================================================================ builds.json + build-logs/

def load_history() -> list:
    """Every recorded build, oldest first."""
    h = fileio.load_json(BUILDS_FILE, [])
    return h if isinstance(h, list) else []


def stored_diags(diags: list[dict]) -> list[dict]:
    """Errors first, then warnings/notes, in build order, capped at MAX_STORED_DIAGS."""
    errs = [d for d in diags if d.get("severity") == "error"]
    rest = [d for d in diags if d.get("severity") != "error"]
    return (errs + rest)[:MAX_STORED_DIAGS]


def append_history(entry: dict) -> None:
    """Add one build (keeping the newest MAX_HISTORY) and delete logs no entry points to any more."""
    with file_lock("builds"):
        h = load_history()
        h.append(entry)
        h = h[-MAX_HISTORY:]
        fileio.save_json(BUILDS_FILE, h)
        prune_logs(h)


def prune_logs(history: list, min_age: float = LOG_GRACE) -> list[str]:
    """Delete build logs no history entry points to any more (history keeps MAX_HISTORY builds).

    A build writes its log before its history entry exists, so a log only goes once it is older than
    `min_age` seconds: a build running right now (here or in another process) keeps its log."""
    keep = set()
    for e in history:
        f = e.get("logFile") if isinstance(e, dict) else None
        if isinstance(f, str):
            keep.add(os.path.realpath(f))
    removed: list[str] = []
    cutoff = time.time() - min_age
    try:
        files = list(BUILD_LOGS.glob("*.log"))
    except OSError:
        return removed
    for f in files:
        try:
            if os.path.realpath(f) in keep or not f.is_file() or f.stat().st_mtime > cutoff:
                continue
            f.unlink()
            removed.append(str(f))
        except OSError:
            continue
    return removed


def last_build(path: str, history: list | None = None) -> dict | None:
    """The newest history entry of the project at `path`."""
    history = history if history is not None else load_history()
    for entry in reversed(history):
        if entry.get("path") == path:
            return entry
    return None


def estimate_duration(path: str, kind: str) -> float | None:
    """Median of the last five successful builds of this kind, in seconds (None without any)."""
    durs = [e["duration"] for e in load_history()
            if e.get("path") == path and e.get("kind") == kind and e.get("result") == "ok" and e.get("duration")]
    if not durs:
        return None
    recent = sorted(durs[-5:])
    return recent[len(recent) // 2]


# ================================================================ renewals.json + provisioning profiles

def load_renewals() -> dict:
    return fileio.load_json(RENEWALS_FILE, {}) or {}


def signing_status(ren: dict | None) -> dict | None:
    """A renewals.json record plus daysLeft and a level (ok / warn under 2 days / error once expired)."""
    if not ren:
        return None
    exp = ren.get("expires")
    left = (exp - time.time()) / 86400 if exp else None
    level = "ok"
    if left is not None:
        level = "error" if left <= 0 else ("warn" if left < 2 else "ok")
    return {**ren, "daysLeft": round(left, 2) if left is not None else None, "level": level}


def record_signing(path: str, udid: str | None, bundle: str | None, via: str, profile: dict | None) -> dict:
    """Remember that the project at `path` was just installed signed (expiry from its profile, else 7 days)."""
    now = time.time()
    rec: dict[str, Any] = {
        "udid": udid,
        "bundleId": bundle,
        "signedAt": now,
        "via": via,
        "expires": (profile or {}).get("expires") or now + FREE_PROFILE_DAYS * 86400,
        "estimated": not (profile and profile.get("expires")),
    }
    if profile:
        rec["profile"] = {k: profile.get(k) for k in ("uuid", "team", "appId", "name")}
    with file_lock("renewals"):
        data = load_renewals()
        data[path] = rec
        fileio.save_json(RENEWALS_FILE, data)
    return rec


def _plist_ts(value: Any) -> float | None:
    """plistlib returns naive datetimes in UTC; .timestamp() on those would read them as local time."""
    if not isinstance(value, datetime):
        return None
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).timestamp()


def profile_from_bytes(data: bytes) -> dict | None:
    """The interesting fields of an embedded.mobileprovision (a CMS envelope around an XML plist)."""
    import plistlib
    start = data.find(b"<?xml")
    end = data.find(b"</plist>")
    if start < 0 or end < 0:
        return None
    try:
        pl = plistlib.loads(data[start:end + len(b"</plist>")])
    except Exception:
        return None
    exp = pl.get("ExpirationDate")
    created = pl.get("CreationDate")
    ents = pl.get("Entitlements") or {}
    return {
        "expires": _plist_ts(exp),
        "created": _plist_ts(created),
        "name": pl.get("Name"),
        "uuid": pl.get("UUID"),
        "team": (pl.get("TeamIdentifier") or [None])[0],
        "appId": ents.get("application-identifier"),
        "devices": pl.get("ProvisionedDevices") or [],
    }


def read_profile(artifact: str | None) -> dict | None:
    """The provisioning profile inside a built .ipa or .app (None if there's none)."""
    import zipfile
    if not artifact:
        return None
    p = Path(artifact)
    try:
        if p.suffix == ".ipa" and p.is_file():
            with zipfile.ZipFile(p) as z:
                for n in z.namelist():
                    if n.count("/") == 2 and n.endswith(".app/embedded.mobileprovision"):
                        return profile_from_bytes(z.read(n))
        elif p.is_dir() and (p / "embedded.mobileprovision").is_file():
            return profile_from_bytes((p / "embedded.mobileprovision").read_bytes())
    except (OSError, zipfile.BadZipFile):
        return None
    return None
