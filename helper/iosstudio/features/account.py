"""Account slice: Apple ID / developer team details via xtool.  Group: `account`.

Commands
  account status                     fast, local (`xtool auth status`)
  account details [--cached] [--refresh] [--sections a,b]
                                     aggregated Apple Developer Services listings (slow, cached)
  account login --email X [--mode password|key]
                                     STREAMING. Drives `xtool auth login` and asks the UI for
                                     secrets through prompt events (password / code / team / text)
  account logout                     one-shot (`xtool auth logout`)

xtool has no JSON output; everything here parses its plain text.  The xtool binary can be
overridden with IOSSTUDIO_XTOOL (used only to test the login state machine with a fake).
xtool has no command to revoke a certificate, so there is no `revoke-cert`.
"""
from __future__ import annotations

import json
import os
import queue
import random
import re
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

from iosstudio.core import appdirs, output, subproc
from iosstudio.core.output import HelperError, fix

CACHE_NAME = "account-details"
STALE_AFTER = 15 * 60          # seconds: cached details older than this are "stale"
SECTIONS = ("teams", "certificates", "identifiers", "devices", "profiles")
DS_TIMEOUT = 60
FREE_LIMITS = {"profileDays": 7, "appIdsPerWeek": 10, "devices": 3, "appsPerDevice": 3}
AGREEMENT_URL = "https://developer.apple.com/account"
CODE_HELP = ("No code? On your iPhone, iPad or Mac open Settings › [your name] › "
             "Sign-In & Security › Get Verification Code, and type that code here.")


# ---------------------------------------------------------------- xtool plumbing

def xtool_path() -> str:
    override = os.environ.get("IOSSTUDIO_XTOOL")
    if override:
        return override
    found = subproc.which("xtool")
    if found:
        return found
    raise HelperError("missing-tool", "xtool isn't installed yet. iOS Studio needs it to talk to Apple.",
                      fix("Install xtool", "setup", step="xtool"))


def using_fake_xtool() -> bool:
    return bool(os.environ.get("IOSSTUDIO_XTOOL"))


def xtool_env() -> dict:
    env = {"NO_COLOR": "1"}
    if not os.environ.get("XDG_CONFIG_HOME"):
        env["XDG_CONFIG_HOME"] = str(appdirs.config_home())
    return env


def xtool(args: list[str], timeout: float = DS_TIMEOUT) -> subprocess.CompletedProcess:
    return subproc.run_cmd([xtool_path(), *args], timeout=timeout, env=xtool_env(),
                          input_text="")   # never let xtool wait on our stdin


# ---------------------------------------------------------------- error classification

def error_line(text: str) -> str:
    """The most useful `Error: …` text xtool printed (or the last non-empty line)."""
    lines = [l.strip() for l in text.replace("\r", "\n").splitlines() if l.strip()]
    for i, l in enumerate(lines):
        if l.startswith("Error:"):
            return " ".join([l[6:].strip(), *lines[i + 1:i + 3]]).strip()
    return lines[-1] if lines else ""


def classify(text: str, during: str = "ds") -> HelperError:
    """Map xtool output to a plain-language HelperError."""
    t = text or ""
    low = t.lower()
    detail = t.strip()[-4000:] or None
    if "server busy" in low or ("503" in t and ("anisette" in low or "get_headers" in low or "http" in low)):
        return HelperError("busy", "Apple's sign-in helper is busy right now. Wait a minute and try again.",
                           detail=detail)
    if "please log in" in low or "not logged in" in low or low.strip() == "logged out":
        return HelperError("not-signed-in", "You're not signed in to your Apple ID.",
                           fix("Sign in", "account", action="signin"), detail)
    if "requires password-based authentication" in low:
        return HelperError("key-mode", "You're signed in with an App Store Connect API key. "
                           "Account details need an Apple ID sign-in.",
                           fix("Sign in with Apple ID", "account", action="signin"), detail)
    if "incorrectverificationcode" in low or "-21669" in t or "twofactorauthenticateoperation.error error 0" in low:
        return HelperError("code-wrong", "That verification code didn't work. It may have expired. "
                           "Sign in again to get a fresh code.", detail=detail)
    if "usercancelled" in low or "twofactorauthenticateoperation.error error 1" in low:
        return HelperError("code-missing", "Sign-in stopped before a verification code was entered.",
                           detail=detail)
    if "failedsecondaryauth" in low or "could not complete 2-factor" in low:
        return HelperError("code-wrong", "Two-factor verification didn't complete. Try signing in again.",
                           detail=detail)
    if "-22406" in t or "incorrect" in low and ("password" in low or "apple id" in low):
        return HelperError("wrong-password", "That Apple ID or password isn't right. Check them and try again.",
                           detail=detail)
    if "-20209" in t or "locked" in low:
        return HelperError("account-locked", "Apple has locked this Apple ID for security reasons. "
                           "Unlock it at iforgot.apple.com, then sign in again.",
                           {"label": "Open iforgot.apple.com", "url": "https://iforgot.apple.com"}, detail)
    if "agreement" in low or "pla update" in low or "program license" in low:
        return HelperError("agreement", "Apple needs you to accept the latest developer agreement. "
                           "Sign in at developer.apple.com, accept it, then come back.",
                           {"label": "Open developer.apple.com", "url": AGREEMENT_URL}, detail)
    if "no development teams" in low:
        return HelperError("no-team", "This Apple ID has no developer team yet. Sign in once at "
                           "developer.apple.com to create a free one, then try again.",
                           {"label": "Open developer.apple.com", "url": AGREEMENT_URL}, detail)
    if "connecttimeout" in low or "timed out" in low or "timeout" in low:
        return HelperError("timeout", "Apple took too long to answer. Check your internet connection and try again.",
                           detail=detail)
    if any(k in low for k in ("could not connect", "network is unreachable", "couldn't connect",
                              "connection refused", "name resolution", "nsurlerrordomain", "-1009",
                              "not connected to the internet", "dns", "no route to host",
                              "connection reset")):
        return HelperError("network", "iOS Studio couldn't reach Apple. Check your internet connection.",
                           detail=detail)
    if "key is invalid" in low or ("no such file" in low and during == "login"):
        return HelperError("bad-key", "That API key file couldn't be read. Choose the .p8 file you "
                           "downloaded from App Store Connect.", detail=detail)
    if "password cannot be empty" in low or "input cannot be empty" in low:
        return HelperError("empty", "Please fill in every field.", detail=detail)
    msg = error_line(t)
    what = "Signing in" if during == "login" else "Talking to Apple"
    return HelperError("xtool-failed", f"{what} didn't work." + (f" Apple said: {msg}" if msg else ""),
                       detail=detail)


TRANSIENT = {"busy", "timeout", "network"}


def error_tail(text: str) -> str:
    """Only the part of xtool's output that describes the failure (skips the banner)."""
    i = text.rfind("Error:")
    if i >= 0:
        return text[i:]
    lines = [l for l in text.splitlines() if l.strip()]
    return "\n".join(lines[-4:])


# ---------------------------------------------------------------- date parsing

_DATE_RE = re.compile(
    r"(\d{1,4})[/.-](\d{1,2})[/.-](\d{1,4}),?\s+(\d{1,2}):(\d{2})(?::(\d{2}))?\s*([AaPp]\.?[Mm]\.?)?")


def parse_date(text: str | None) -> dict | None:
    """xtool prints Foundation `.dateTime` dates, e.g. `31/10/2027, 2:33 PM` (day first on this
    build regardless of LANG). Returns {"text", "ts" (epoch s), "iso"} or {"text"} if unparseable."""
    if not text:
        return None
    raw = text.strip().replace("\u202f", " ").replace("\xa0", " ")
    s = raw
    m = _DATE_RE.search(s)
    if not m:
        return {"text": raw}
    a, b, c, hh, mm, ss, ap = m.groups()
    a, b, c = int(a), int(b), int(c)
    if a > 31:                    # ISO-ish Y-M-D
        y, mo, d = a, b, c
    else:
        y = c if c > 99 else 2000 + c
        if a > 12:
            d, mo = a, b
        elif b > 12:
            mo, d = a, b
        else:
            d, mo = a, b          # observed default: day first
    h, mi, sec = int(hh), int(mm), int(ss or 0)
    if ap:
        pm = ap.lower().startswith("p")
        if h == 12:
            h = 0
        if pm:
            h += 12
    try:
        dt = datetime(y, mo, d, h, mi, sec)
    except ValueError:
        return {"text": raw}
    ts = time.mktime(dt.timetuple())
    return {"text": raw, "ts": ts, "iso": dt.astimezone().isoformat()}


def days_left(ts: float | None) -> float | None:
    if ts is None:
        return None
    return round((ts - time.time()) / 86400, 2)


def short_id(value: str | None, n: int = 8) -> str:
    if not value:
        return ""
    return value if len(value) <= n + 2 else f"{value[:4]}…{value[-(n - 4):]}"


# ---------------------------------------------------------------- text parsing

def parse_status(text: str) -> dict:
    lines = [l.rstrip() for l in text.splitlines()]
    first = next((l.strip() for l in lines if l.strip()), "")
    out: dict[str, Any] = {"signedIn": first == "Logged in."}
    fields: dict[str, str] = {}
    for l in lines:
        m = re.match(r"\s*-\s*([^:]+):\s*(.*)$", l)
        if m:
            fields[m.group(1).strip().lower()] = m.group(2).strip()
    if not out["signedIn"]:
        out["mode"] = None
        return out
    if "asc key id" in fields:
        out.update(mode="key", keyId=fields.get("asc key id"), issuerId=fields.get("issuer id"),
                   appleId=None, teamId=None, tokenExpiry=None)
    else:
        exp = parse_date(fields.get("token expiry"))
        out.update(mode="password", appleId=fields.get("apple id"), teamId=fields.get("team id"),
                   tokenExpiry=exp, tokenDaysLeft=days_left(exp.get("ts") if exp else None))
    return out


def parse_items(text: str) -> list[dict]:
    """Parse xtool's YAML-ish `- key: value` listings into dicts.
    Nested `key:` followed by deeper `- value` lines become lists; other nesting is flattened."""
    items: list[dict] = []
    cur: dict | None = None
    list_key: str | None = None
    list_indent = -1
    for raw in text.splitlines():
        if not raw.strip():
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        line = raw.strip()
        if indent == 0 and line.startswith("- "):
            cur = {}
            items.append(cur)
            list_key, list_indent = None, -1
            line = line[2:]
        elif cur is None:
            continue
        if list_key and line.startswith("- ") and indent >= list_indent:
            cur.setdefault(list_key, []).append(line[2:].strip())
            continue
        m = re.match(r"([A-Za-z][A-Za-z0-9 _-]*):\s?(.*)$", line)
        if not m:
            continue
        key, val = m.group(1).strip().lower(), m.group(2).strip()
        if val == "":
            list_key, list_indent = key, indent
            cur.setdefault(key, [])
        else:
            list_key = None
            if key in cur and not isinstance(cur[key], list):
                key = f"{key} ({len([k for k in cur if k.startswith(key)])})"
            cur[key] = val
    return items


def parse_teams(text: str) -> list[dict]:
    teams: list[dict] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = re.match(r"(.+?)\s*\[([^\]]*)\]:\s*(\S+)$", line)
        if m and not raw.startswith((" ", "-")):
            teams.append({"name": m.group(1), "status": m.group(2), "id": m.group(3), "memberships": []})
            continue
        if line.startswith("- ") and teams:
            mm = re.match(r"-\s*(.+?)(?:\s*\(([^)]*)\))?$", line)
            if mm:
                teams[-1]["memberships"].append({"name": mm.group(1), "platform": mm.group(2) or ""})
    for t in teams:
        names = " ".join(m["name"].lower() for m in t["memberships"])
        if "free provisioning" in names:
            t["type"] = "free"
        elif "developer program" in names or "enterprise" in names:
            t["type"] = "paid"
        else:
            t["type"] = "unknown"
        t["typeLabel"] = {"free": "Free", "paid": "Paid", "unknown": ""}[t["type"]]
        t["active"] = t["status"].lower() == "active"
    return teams


def strip_xtl(bundle: str, team: str | None) -> str:
    """`XTL-TEAM.app.foo` → `app.foo` (xtool prefixes App IDs on free teams)."""
    m = re.match(r"XTL-[A-Z0-9]+\.(.+)$", bundle or "")
    return m.group(1) if m else (bundle or "")


def shape_certificates(items: list[dict]) -> list[dict]:
    out = []
    for c in items:
        exp = parse_date(c.get("expiry"))
        serial = c.get("serial number", "")
        kind = c.get("type", "")
        out.append({
            "id": c.get("id", ""), "name": c.get("name", ""), "displayName": c.get("display name", ""),
            "kind": kind, "kindLabel": {"DEVELOPMENT": "Development", "DISTRIBUTION": "Distribution",
                                        "IOS_DEVELOPMENT": "Development", "IOS_DISTRIBUTION": "Distribution"}
            .get(kind.upper(), kind.title()),
            "serial": serial, "serialShort": short_id(serial, 10),
            "commonName": c.get("common name", ""), "teamId": c.get("team id", ""),
            "expires": exp, "daysLeft": days_left(exp.get("ts") if exp else None),
        })
    return out


def shape_identifiers(items: list[dict]) -> list[dict]:
    out = []
    for i in items:
        ident = i.get("identifier", "")
        out.append({"id": i.get("id", ""), "name": i.get("name", ""), "bundleId": ident,
                    "displayBundleId": strip_xtl(ident, i.get("seedid")),
                    "platform": i.get("platform", ""), "seedId": i.get("seedid", "")})
    return out


_CLASS = {"IPHONE": "iPhone", "IPAD": "iPad", "IPOD": "iPod touch", "WATCH": "Apple Watch",
          "APPLE_TV": "Apple TV", "APPLE_VISION": "Apple Vision Pro", "MAC": "Mac"}


def shape_devices(items: list[dict]) -> list[dict]:
    out = []
    for d in items:
        added = parse_date(d.get("added date"))
        cls = d.get("device class", "")
        udid = d.get("udid", "")
        out.append({"id": d.get("id", ""), "name": d.get("name", ""), "udid": udid,
                    "udidShort": short_id(udid, 10), "deviceClass": cls,
                    "model": _CLASS.get(cls.upper(), cls.title()), "platform": d.get("platform", ""),
                    "status": d.get("status", ""), "enabled": d.get("status", "").upper() == "ENABLED",
                    "added": added})
    return out


def shape_profiles(items: list[dict]) -> list[dict]:
    out = []
    for p in items:
        exp = parse_date(p.get("expiration date"))
        created = parse_date(p.get("created date"))
        ents: dict = {}
        try:
            ents = json.loads(p.get("entitlements", "") or "{}")
        except json.JSONDecodeError:
            pass
        app_ident = ents.get("application-identifier", "")
        bundle = app_ident.split(".", 1)[1] if "." in app_ident else app_ident
        state = p.get("profile state", "")
        out.append({
            "id": p.get("id", ""), "name": p.get("name", ""), "uuid": p.get("uuid", ""),
            "platform": p.get("platform", ""), "type": p.get("profile type", ""),
            "state": state, "active": state.upper() == "ACTIVE",
            "created": created, "expires": exp, "daysLeft": days_left(exp.get("ts") if exp else None),
            "appIdentifier": app_ident, "bundleId": bundle, "displayBundleId": strip_xtl(bundle, None),
            "devices": p.get("devices", []) if isinstance(p.get("devices"), list) else [],
            "devicesCount": len(p.get("devices", [])) if isinstance(p.get("devices"), list) else 0,
            "certificates": p.get("certificates", []) if isinstance(p.get("certificates"), list) else [],
            "teamIds": p.get("team identifiers", []) if isinstance(p.get("team identifiers"), list) else [],
            "entitlements": sorted(ents.keys()),
        })
    out.sort(key=lambda x: (x["expires"] or {}).get("ts") or 0)
    return out


def parse_section(name: str, text: str) -> list[dict]:
    if name == "teams":
        return parse_teams(text)
    items = parse_items(text)
    return {"certificates": shape_certificates, "identifiers": shape_identifiers,
            "devices": shape_devices, "profiles": shape_profiles}[name](items)


# ---------------------------------------------------------------- status

def get_status() -> dict:
    cp = xtool(["auth", "status"], timeout=20)
    text = (cp.stdout or "") + (cp.stderr or "")
    if cp.returncode != 0 and "Logged" not in text:
        raise classify(text, "status")
    st = parse_status(cp.stdout or "")
    team = st.get("teamId")
    st["hasLocalSigningKey"] = bool(team) and (xtool_data_dir() / team).exists()
    return st


def xtool_data_dir() -> Path:
    return appdirs.config_home() / "xtool" / "data"


def cmd_status(flags: dict) -> int:
    return output.ok(get_status())


# ---------------------------------------------------------------- details

def fetch_section(name: str, attempts: int = 4) -> dict:
    """Run `xtool ds <name> list` with retry/backoff on transient errors."""
    started = time.time()
    last: HelperError | None = None
    for attempt in range(attempts):
        if attempt:
            time.sleep(min(2 ** attempt, 10) + random.uniform(0, 1.5))
        try:
            cp = xtool(["ds", name, "list"])
        except HelperError as exc:
            last = exc
            if exc.code in TRANSIENT:
                continue
            break
        if cp.returncode == 0:
            try:
                items = parse_section(name, cp.stdout or "")
            except Exception as exc:  # parser bug: never fail the whole result
                return {"ok": False, "items": [], "error": HelperError(
                    "parse", "iOS Studio couldn't read this list.", detail=f"{exc!r}\n{cp.stdout}").to_json()}
            return {"ok": True, "items": items, "seconds": round(time.time() - started, 1),
                    "attempts": attempt + 1}
        last = classify((cp.stdout or "") + "\n" + (cp.stderr or ""))
        if last.code not in TRANSIENT:
            break
    return {"ok": False, "items": [], "error": (last or HelperError("failed", "Couldn't load this list.")).to_json(),
            "seconds": round(time.time() - started, 1)}


def summarize(status: dict, sections: dict) -> dict:
    """Derived facts the UI shows: team, type, limits, signing health."""
    teams = sections.get("teams", {}).get("items", [])
    team_id = status.get("teamId")
    team = next((t for t in teams if t.get("id") == team_id), None) or (teams[0] if teams else None)
    ttype = (team or {}).get("type", "unknown")
    free = ttype == "free"
    profiles = [p for p in sections.get("profiles", {}).get("items", []) if p.get("active")]
    soonest = min((p for p in profiles if p.get("daysLeft") is not None),
                  key=lambda p: p["daysLeft"], default=None)
    health: dict[str, Any]
    if soonest is None:
        health = {"level": "info", "title": "No apps signed yet",
                  "detail": "When you run an app on your iPhone, its signing profile shows up here."}
    else:
        d = soonest["daysLeft"]
        app = soonest.get("displayBundleId") or soonest.get("name")
        if d <= 0:
            health = {"level": "error", "title": "Signing has expired",
                      "detail": f"{app} won't open on your iPhone until you renew it."}
        elif d < 2:
            health = {"level": "warn", "title": "Signing expires soon",
                      "detail": f"{app} stops opening in {fmt_left(d)}. Renew it to keep it working."}
        else:
            health = {"level": "ok", "title": "Signing is healthy",
                      "detail": f"{app} keeps working for {fmt_left(d)}."}
        health.update(profileId=soonest.get("id"), bundleId=soonest.get("bundleId"),
                      expires=soonest.get("expires"), daysLeft=d)
    certs = sections.get("certificates", {}).get("items", [])
    devices = sections.get("devices", {}).get("items", [])
    idents = sections.get("identifiers", {}).get("items", [])
    week_ago = time.time() - 7 * 86400
    # App IDs used this week: an App ID whose oldest known profile was created in the last 7 days
    # is (most likely) new this week.  It's an estimate; Apple doesn't expose the real counter.
    first_profile: dict[str, float] = {}
    for p in sections.get("profiles", {}).get("items", []):
        ts = (p.get("created") or {}).get("ts")
        if ts and p.get("bundleId"):
            first_profile[p["bundleId"]] = min(first_profile.get(p["bundleId"], ts), ts)
    new_this_week = sum(1 for i in idents if first_profile.get(i["bundleId"], 0) >= week_ago)
    return {
        "team": team, "teamType": ttype, "free": free,
        "limits": FREE_LIMITS if free else None,
        "counts": {"certificates": len(certs), "appIds": len(idents), "appIdsThisWeek": new_this_week,
                   "devices": len(devices), "profiles": len(profiles)},
        "health": health,
    }


def fmt_left(days: float) -> str:
    if days >= 2:
        return f"{int(days)} days"
    hours = max(1, int(round(days * 24)))
    return f"{hours} hour{'s' if hours != 1 else ''}"


def cmd_details(flags: dict) -> int:
    cached = appdirs.cache_get(CACHE_NAME)
    if flags.get("cached"):
        if not cached:
            return output.ok({"cached": True, "empty": True, "stale": True, "fetchedAt": None})
        data = dict(cached.get("data") or {})
        age = time.time() - float(cached.get("at", 0))
        data.update(cached=True, empty=False, stale=age > STALE_AFTER, fetchedAt=cached.get("at"),
                    ageSeconds=round(age))
        return output.ok(data)

    status = get_status()
    if not status.get("signedIn"):
        raise HelperError("not-signed-in", "You're not signed in to your Apple ID.",
                          fix("Sign in", "account", action="signin"))
    if status.get("mode") == "key":
        raise classify("requires password-based authentication")

    wanted = [s for s in str(flags.get("sections") or ",".join(SECTIONS)).split(",") if s in SECTIONS]
    # Two at a time: the anisette server answers "503 busy" when hit with five requests at once.
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {}
        for i, name in enumerate(wanted):
            futures[name] = pool.submit(lambda n=name, d=i: (time.sleep(0.4 * d), fetch_section(n))[1])
        results = {name: f.result() for name, f in futures.items()}

    prev = (cached or {}).get("data") or {}
    same_account = prev.get("status", {}).get("teamId") == status.get("teamId") and \
        prev.get("status", {}).get("appleId") == status.get("appleId")
    sections: dict[str, dict] = {}
    for name in SECTIONS:
        if name in results:
            r = results[name]
            if not r["ok"] and same_account and prev.get("sections", {}).get(name, {}).get("items"):
                # keep the last good list, flagged stale, with the fresh error attached
                old = prev["sections"][name]
                r = {**r, "items": old["items"], "stale": True, "fetchedAt": old.get("fetchedAt")}
            else:
                r = {**r, "stale": False, "fetchedAt": time.time() if r["ok"] else None}
            sections[name] = r
        elif same_account and name in prev.get("sections", {}):
            sections[name] = prev["sections"][name]
        else:
            sections[name] = {"ok": False, "items": [], "error": None, "stale": True, "fetchedAt": None}

    data = {"status": status, "sections": sections, "summary": summarize(status, sections),
            "errors": [{"section": n, **(s.get("error") or {})} for n, s in sections.items()
                       if not s.get("ok") and s.get("error")]}
    if any(r["ok"] for r in results.values()):
        entry = appdirs.cache_put(CACHE_NAME, data)
        at = entry["at"]
    else:
        at = (cached or {}).get("at")
        if not prev:
            # nothing at all: report the first error as the result
            first = next(iter(results.values()), {}).get("error") or {}
            if first and all(not r["ok"] for r in results.values()):
                return output.fail(first.get("code", "failed"), first.get("message", "Couldn't reach Apple."),
                                   first.get("fix"), first.get("detail"))
    data.update(cached=False, empty=False, stale=False, fetchedAt=at, ageSeconds=0)
    return output.ok(data)


# ---------------------------------------------------------------- logout

def cmd_logout(flags: dict) -> int:
    args = ["auth", "logout"] + (["--reset-2fa"] if flags.get("reset_2fa") else [])
    cp = xtool(args, timeout=30)
    if cp.returncode != 0:
        raise classify((cp.stdout or "") + (cp.stderr or ""), "logout")
    try:
        (appdirs.CACHE_DIR / f"{CACHE_NAME}.json").unlink()
    except OSError:
        pass
    return output.ok({"signedIn": False})


# ---------------------------------------------------------------- login (streaming)

PROMPTS = [
    # (regex on the unconsumed tail, kind)
    (re.compile(r"Password:\s*$"), "password"),
    (re.compile(r"Code:\s*$"), "code"),
    (re.compile(r"Choice \((\d+)-(\d+)\):\s*$"), "team"),
    (re.compile(r"Key ID:\s*$"), "key-id"),
    (re.compile(r"Issuer ID:\s*$"), "issuer-id"),
    (re.compile(r"Key path:\s*$"), "key-path"),
    (re.compile(r"Apple ID:\s*$"), "apple-id"),
    (re.compile(r"\(yes/no\):\s*$"), "confirm"),
]
TEAM_LINE = re.compile(r"^\s*(\d+):\s*(.+?)\s*\(([A-Z0-9]+)\)\s*$")


class Login:
    """State machine around one `xtool auth login` process.  One event queue carries both xtool
    output chunks and UI answers, so we never block on one while the other has news."""

    def __init__(self, email: str, mode: str):
        self.email, self.mode = email, mode
        self.q: queue.Queue = queue.Queue()
        self.out = ""            # everything xtool printed (never contains secrets: pipes don't echo)
        self.tail = ""           # text since the last answered prompt
        self.logged_upto = 0
        self.pid_counter = 0
        self.waiting: dict | None = None
        self.teams: list[dict] = []
        self.code_tries = 0
        self.manual_code = False
        self.proc: subprocess.Popen | None = None

    # -- io threads
    def _read_proc(self):
        assert self.proc and self.proc.stdout
        fd = self.proc.stdout.fileno()
        while True:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                chunk = b""
            if not chunk:
                self.q.put(("eof", None))
                return
            self.q.put(("out", chunk.decode("utf-8", "replace")))

    def _read_stdin(self):
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                self.q.put(("answer", json.loads(line)))
            except json.JSONDecodeError:
                continue
        self.q.put(("stdin-closed", None))

    # -- helpers
    def _emit_log_lines(self):
        """Forward complete output lines as log events (prompt text included, answers never)."""
        while True:
            nl = self.out.find("\n", self.logged_upto)
            if nl < 0:
                return
            line = self.out[self.logged_upto:nl].rstrip("\r")
            self.logged_upto = nl + 1
            if line.strip():
                output.log(line)
                self._watch_line(line)

    def _watch_line(self, line: str):
        low = line.lower()
        if line.strip() == "Logging in...":
            output.step("login", "Checking your Apple ID with Apple", "running")
        elif "could not send verification code via sms" in low:
            output.event("notice", id="sms-fallback", level="info",
                         message="Apple couldn't text you a code, so it's sending one to your other Apple devices instead.")
        elif "could not send verification code" in low:
            self.manual_code = True
            output.event("notice", id="code-manual", level="warn",
                         message="Apple couldn't send the code automatically. " + CODE_HELP)
        m = TEAM_LINE.match(line)
        if m:
            self.teams.append({"value": m.group(1), "label": m.group(2), "teamId": m.group(3)})

    def _ask(self, kind: str, message: str, **extra):
        self.pid_counter += 1
        pid = f"p{self.pid_counter}"
        self.waiting = {"id": pid, "kind": kind}
        output.event("prompt", id=pid, kind=kind, message=message, options=extra.pop("options", []), **extra)

    def _write(self, value: str):
        assert self.proc and self.proc.stdin
        # exactly one line per prompt: xtool drops anything after the first newline
        value = value.replace("\r", "").split("\n", 1)[0]
        try:
            self.proc.stdin.write((value + "\n").encode())
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass

    def _kill(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.send_signal(signal.SIGTERM)
                self.proc.wait(timeout=3)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass

    def _on_prompt(self, kind: str, m: re.Match):
        self.tail = ""
        if kind == "password":
            self._ask("password", f"Enter the password for {self.email}.", email=self.email)
        elif kind == "code":
            self.code_tries += 1
            self._ask("code", "Enter the 6-digit code Apple sent to your iPhone, iPad or Mac.",
                      digits=6, hint=CODE_HELP, manual=self.manual_code, attempt=self.code_tries)
        elif kind == "team":
            teams, self.teams = self.teams, []
            if not teams:
                teams = getattr(self, "last_teams", [])
            if teams:
                self.last_teams = teams
            if not teams:
                lo, hi = int(m.group(1)), int(m.group(2))
                teams = [{"value": str(i), "label": f"Team {i}"} for i in range(lo, hi + 1)]
            self._ask("team", "This Apple ID belongs to more than one team. Which one should iOS Studio use?",
                      options=teams)
        elif kind == "key-id":
            self._ask("text", "Key ID", field="keyId", placeholder="e.g. 2X9R4HXF34")
        elif kind == "issuer-id":
            self._ask("text", "Issuer ID", field="issuerId", placeholder="e.g. 57246542-96fe-1a63-e053-0824d011072a")
        elif kind == "key-path":
            self._ask("text", "API key file (.p8)", field="keyPath", file=True)
        elif kind == "apple-id":
            self._write(self.email)
        elif kind == "confirm":
            # not expected during login; answer "no" so nothing gets revoked by accident
            self._write("no")

    # -- main loop
    def run(self) -> int:
        argv = [xtool_path(), "auth", "login", "-m", self.mode]
        if self.mode == "password":
            argv += ["-u", self.email]
        env = subproc.tool_env("plain", xtool_env())
        output.step("start", "Starting sign-in", "running")
        try:
            self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, env=env, bufsize=0)
        except FileNotFoundError:
            raise HelperError("missing-tool", "xtool isn't installed yet.", fix("Install xtool", "setup", step="xtool"))
        threading.Thread(target=self._read_proc, daemon=True).start()
        threading.Thread(target=self._read_stdin, daemon=True).start()
        last_activity = time.time()
        try:
            while True:
                try:
                    kind, payload = self.q.get(timeout=1.0)
                except queue.Empty:
                    # #414: a TLS stall after the code; give up after 2 minutes of silence
                    if self.waiting is None and time.time() - last_activity > 120:
                        self._kill()
                        return output.done(False, error=HelperError(
                            "timeout", "Apple stopped answering. Check your internet connection and try again."))
                    continue
                if kind == "out":
                    last_activity = time.time()
                    self.out += payload
                    self.tail += payload
                    self._emit_log_lines()
                    for rx, pkind in PROMPTS:
                        m = rx.search(self.tail)
                        if m:
                            self._on_prompt(pkind, m)
                            break
                elif kind == "answer":
                    if not self.waiting or payload.get("prompt") != self.waiting["id"]:
                        continue
                    if payload.get("cancel"):
                        self._kill()
                        return output.done(False, error=HelperError("cancelled", "Sign-in was cancelled."))
                    value = str(payload.get("value", ""))
                    if self.waiting["kind"] == "code":
                        value = re.sub(r"\D", "", value)
                    self.waiting = None
                    last_activity = time.time()
                    self._write(value)
                    del value
                elif kind == "stdin-closed":
                    if self.proc.poll() is None and self.waiting:
                        self._kill()
                        return output.done(False, error=HelperError("cancelled", "Sign-in was cancelled."))
                elif kind == "eof":
                    break
        finally:
            self._kill()
        try:
            rc = self.proc.wait(timeout=5)     # _kill() ended it; never hang on one that ignores even SIGKILL
        except subprocess.TimeoutExpired:
            rc = -9
        if self.out[self.logged_upto:].strip():
            self.out += "\n"
            self._emit_log_lines()
        if rc == 0 and "Logged in." in self.out:
            output.step("login", "Checking your Apple ID with Apple", "done")
            self._tighten_permissions()
            try:
                st = get_status()
            except HelperError:
                st = parse_status(self.out[self.out.find("Logged in."):])
            try:
                (appdirs.CACHE_DIR / f"{CACHE_NAME}.json").unlink()
            except OSError:
                pass
            return output.done(True, {"status": st})
        err = classify(error_tail(self.out), "login")
        if err.code == "code-wrong" and self.manual_code:
            err.message += " " + CODE_HELP
        output.step("login", "Checking your Apple ID with Apple", "failed")
        return output.done(False, error=err)

    def _tighten_permissions(self):
        if using_fake_xtool():
            return
        base = xtool_data_dir().parent
        try:
            os.chmod(base, 0o700)
            for f in xtool_data_dir().iterdir():
                if f.is_file():
                    os.chmod(f, 0o600)
        except OSError:
            pass


def cmd_login(flags: dict) -> int:
    mode = str(flags.get("mode") or "password")
    if mode not in ("password", "key"):
        output.done(False, error=HelperError("usage", "mode must be password or key"))   # streaming command
        return output.EXIT_USAGE
    email = str(flags.get("email") or "").strip()
    try:
        if mode == "password" and not email:
            # ask for it the same way as the other fields
            output.event("prompt", id="p0", kind="text", field="email", message="Apple ID (email)", options=[])
            for line in sys.stdin:
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if msg.get("prompt") == "p0":
                    if msg.get("cancel"):
                        raise HelperError("cancelled", "Sign-in was cancelled.")
                    email = str(msg.get("value", "")).strip()
                    break
            if not email:
                raise HelperError("empty", "Enter your Apple ID email to sign in.")
        return Login(email, mode).run()
    except HelperError as exc:   # streaming command: errors go out as a done event
        return output.done(False, error=exc)


GROUPS = {
    "account": {
        "status": cmd_status,
        "details": cmd_details,
        "login": cmd_login,
        "logout": cmd_logout,
    }
}
