"""Account slice (helper/iosstudio/features/account.py): xtool text parsing, error classification, the
details aggregation and caching, logout, against a fake xtool (tests/support/xtool_account.py)."""
from __future__ import annotations

import json
import subprocess
import time
import types
from datetime import datetime, timedelta

import pytest

from iosstudio.core import appdirs
from iosstudio.core.output import HelperError
from iosstudio.features import account
from support import write_exe
from support.xtool_account import FAKE_XTOOL


def fmt(dt: datetime) -> str:
    """How xtool prints a date (day first, 12-hour clock)."""
    return dt.strftime("%d/%m/%Y, %I:%M %p")


def lines(capsys) -> list[dict]:
    return [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.strip()]


@pytest.fixture
def fake(tmp_path, monkeypatch):
    """A fake xtool + scenario dir, an empty details cache, and no real sleeping between retries."""
    d = tmp_path / "scenario"
    d.mkdir()
    exe = write_exe(tmp_path / "xtool", FAKE_XTOOL)
    monkeypatch.setenv("IOSSTUDIO_XTOOL", str(exe))
    monkeypatch.setenv("FAKE_XTOOL_DIR", str(d))
    monkeypatch.setattr(appdirs, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(account, "time", types.SimpleNamespace(time=time.time, mktime=time.mktime,
                                                               sleep=lambda s: None))
    return d


# ================================================================ dates and ids

@pytest.mark.parametrize("text, expect", [
    ("31/10/2027, 2:33 PM", (2027, 10, 31, 14, 33)),
    ("10/31/2027, 2:33 PM", (2027, 10, 31, 14, 33)),          # second field > 12: month first
    ("05/04/27, 12:05 AM", (2027, 4, 5, 0, 5)),               # ambiguous: day first; 12 AM is midnight
    ("1/2/2027, 12:00 p.m.", (2027, 2, 1, 12, 0)),
    ("2027-10-31 14:33:07", (2027, 10, 31, 14, 33)),           # ISO-ish
    ("31.10.2027 09:01", (2027, 10, 31, 9, 1)),
])
def test_parse_date(text, expect):
    d = account.parse_date(text)
    got = datetime.fromtimestamp(d["ts"])
    assert (got.year, got.month, got.day, got.hour, got.minute) == expect
    assert d["iso"].startswith(f"{expect[0]}-")


def test_parse_date_unparseable():
    assert account.parse_date(None) is None and account.parse_date("") is None
    assert account.parse_date("tomorrow") == {"text": "tomorrow"}
    assert account.parse_date("31/31/2027, 1:00") == {"text": "31/31/2027, 1:00"}


def test_days_left_and_short_id():
    assert account.days_left(None) is None
    assert 0.99 < account.days_left(time.time() + 86400) <= 1.0
    assert account.short_id(None) == "" and account.short_id("ABCDEFGHIJ") == "ABCDEFGHIJ"
    assert account.short_id("ABCDEFGHIJKL") == "ABCD…IJKL"
    assert account.fmt_left(3.7) == "3 days" and account.fmt_left(1.0) == "24 hours"
    assert account.fmt_left(0.01) == "1 hour"


def test_strip_xtl():
    assert account.strip_xtl("XTL-ABC123.com.example.App", None) == "com.example.App"
    assert account.strip_xtl("com.example.App", "X") == "com.example.App"
    assert account.strip_xtl("", None) == ""


# ================================================================ errors

@pytest.mark.parametrize("text, code", [
    ("Error: server busy", "busy"),
    ("get_headers failed: HTTP 503", "busy"),
    ("Please log in first", "not-signed-in"),
    ("Logged out", "not-signed-in"),
    ("This command requires password-based authentication", "key-mode"),
    ("Error: IncorrectVerificationCode (-21669)", "code-wrong"),
    ("TwoFactorAuthenticateOperation.Error error 1", "code-missing"),
    ("failedSecondaryAuth", "code-wrong"),
    ("Error: -22406 incorrect", "wrong-password"),
    ("Your Apple ID or password was incorrect", "wrong-password"),
    ("This Apple ID has been locked (-20209)", "account-locked"),
    ("You must accept the Program License Agreement", "agreement"),
    ("no development teams found", "no-team"),
    ("The request timed out.", "timeout"),
    ("NSURLErrorDomain -1009", "network"),
    ("Error: The key is invalid", "bad-key"),
    ("Password cannot be empty", "empty"),
])
def test_classify(text, code):
    assert account.classify(text).code == code


def test_classify_fallback_quotes_apple():
    e = account.classify("banner\nError: Something odd\n  more context\n  and more\nignored")
    assert e.code == "xtool-failed"
    assert e.message == "Talking to Apple didn't work. Apple said: Something odd more context and more"
    assert account.classify("", "login").message == "Signing in didn't work."
    assert account.classify("no such file", "login").code == "bad-key"
    assert account.classify("no such file", "ds").code == "xtool-failed"


def test_error_line_and_tail():
    assert account.error_line("a\r\nb\n\n") == "b"
    assert account.error_line("") == ""
    assert account.error_tail("banner\nmore\nError: x\ny") == "Error: x\ny"
    assert account.error_tail("1\n2\n3\n4\n5\n") == "2\n3\n4\n5"


# ================================================================ text parsing

def test_parse_status_modes():
    pw = account.parse_status("Logged in.\n - Apple ID: a@b.c\n - Team ID: T1\n - Token expiry: 31/10/2099, 2:33 PM\n")
    assert pw["signedIn"] and pw["mode"] == "password" and pw["appleId"] == "a@b.c" and pw["teamId"] == "T1"
    assert pw["tokenDaysLeft"] > 1000
    key = account.parse_status("Logged in.\n - ASC Key ID: K1\n - Issuer ID: I1\n")
    assert key["mode"] == "key" and key["keyId"] == "K1" and key["issuerId"] == "I1" and key["appleId"] is None
    assert account.parse_status("\nNot logged in\n") == {"signedIn": False, "mode": None}


def test_parse_items_lists_and_duplicates():
    items = account.parse_items("noise before\n- id: A\n  name: One\n  devices:\n    - D1\n    - D2\n  name: Two\n"
                                "  junk line\n- id: B\n  certificates:\n")
    assert items[0]["devices"] == ["D1", "D2"]
    assert items[0]["name"] == "One" and items[0]["name (1)"] == "Two"
    assert items[1] == {"id": "B", "certificates": []}


TEAMS = """Jane Doe [Active]: TEAM123456
- Free Provisioning (ios)
Acme Inc [Inactive]: ACME000001
  - Apple Developer Program
Other [active]: OTHER00001
"""


def test_parse_teams():
    t = account.parse_teams(TEAMS)
    assert [(x["id"], x["type"], x["typeLabel"], x["active"]) for x in t] == [
        ("TEAM123456", "free", "Free", True), ("ACME000001", "paid", "Paid", False), ("OTHER00001", "unknown", "", True)]
    assert t[0]["memberships"] == [{"name": "Free Provisioning", "platform": "ios"}]


def profiles_text(days_left: float, created_days_ago: float = 1) -> str:
    now = datetime.now()
    return (f"- id: P1\n  name: XTL profile\n  uuid: U-1\n  profile state: ACTIVE\n"
            f"  expiration date: {fmt(now + timedelta(days=days_left))}\n"
            f"  created date: {fmt(now - timedelta(days=created_days_ago))}\n"
            '  entitlements: {"application-identifier": "TEAM123456.XTL-TEAM123456.com.example.App", "get-task-allow": true}\n'
            "  devices:\n    - 00008110-0001\n  team identifiers:\n    - TEAM123456\n"
            f"- id: P0\n  name: Old\n  profile state: EXPIRED\n  expiration date: {fmt(now - timedelta(days=30))}\n"
            "  entitlements: not json\n")


def test_shape_sections():
    certs = account.parse_section("certificates", "- id: C1\n  type: IOS_DEVELOPMENT\n  serial number: 0123456789ABCDEF\n"
                                                   "  expiry: 31/10/2099, 1:00 PM\n- id: C2\n  type: weird_kind\n")
    assert certs[0]["kindLabel"] == "Development" and certs[0]["serialShort"] == "0123…ABCDEF"
    assert certs[0]["daysLeft"] > 0 and certs[1]["kindLabel"] == "Weird_Kind" and certs[1]["expires"] is None
    ids = account.parse_section("identifiers", "- id: I1\n  identifier: XTL-T1.com.example.App\n  seedid: T1\n")
    assert ids[0]["displayBundleId"] == "com.example.App" and ids[0]["bundleId"] == "XTL-T1.com.example.App"
    devs = account.parse_section("devices", "- id: D1\n  name: Phone\n  udid: 00008110-000A1B2C3D4E\n"
                                            "  device class: IPHONE\n  status: ENABLED\n- id: D2\n  device class: toaster\n")
    assert devs[0]["model"] == "iPhone" and devs[0]["enabled"] and devs[1]["model"] == "Toaster"
    profs = account.parse_section("profiles", profiles_text(3))
    assert [p["id"] for p in profs] == ["P0", "P1"]                       # soonest expiry first
    p1 = profs[1]
    assert p1["active"] and p1["bundleId"] == "XTL-TEAM123456.com.example.App"
    assert p1["displayBundleId"] == "com.example.App" and p1["devicesCount"] == 1
    assert p1["entitlements"] == ["application-identifier", "get-task-allow"] and p1["teamIds"] == ["TEAM123456"]
    assert profs[0]["entitlements"] == [] and profs[0]["devices"] == []


@pytest.mark.parametrize("days, level, title", [
    (5, "ok", "Signing is healthy"), (1, "warn", "Signing expires soon"), (-1, "error", "Signing has expired")])
def test_summarize_health(days, level, title):
    sections = {"teams": {"items": account.parse_teams(TEAMS)},
                "profiles": {"items": account.parse_section("profiles", profiles_text(days))},
                "identifiers": {"items": [{"bundleId": "XTL-TEAM123456.com.example.App"}, {"bundleId": "old"}]}}
    s = account.summarize({"teamId": "TEAM123456"}, sections)
    assert s["health"]["level"] == level and s["health"]["title"] == title
    assert s["health"]["profileId"] == "P1" and "com.example.App" in s["health"]["detail"]
    assert s["free"] and s["limits"] == account.FREE_LIMITS and s["team"]["id"] == "TEAM123456"
    assert s["counts"]["profiles"] == 1 and s["counts"]["appIds"] == 2 and s["counts"]["appIdsThisWeek"] == 1


def test_summarize_without_anything():
    s = account.summarize({}, {})
    assert s["team"] is None and s["teamType"] == "unknown" and not s["free"] and s["limits"] is None
    assert s["health"]["level"] == "info"
    paid = account.summarize({"teamId": "nope"}, {"teams": {"items": account.parse_teams(TEAMS)[1:2]}})
    assert paid["team"]["id"] == "ACME000001" and paid["teamType"] == "paid"   # falls back to the first team


# ================================================================ xtool plumbing

def test_xtool_path(monkeypatch):
    monkeypatch.setenv("IOSSTUDIO_XTOOL", "/x/xtool")
    assert account.xtool_path() == "/x/xtool" and account.using_fake_xtool()
    monkeypatch.delenv("IOSSTUDIO_XTOOL")
    monkeypatch.setattr(account.subproc, "which", lambda name: None)
    with pytest.raises(HelperError) as ei:
        account.xtool_path()
    assert ei.value.code == "missing-tool" and ei.value.fix["step"] == "xtool"


def test_xtool_env_defaults_config_home(monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", "/cfg")
    assert account.xtool_env() == {"NO_COLOR": "1"}
    monkeypatch.delenv("XDG_CONFIG_HOME")
    assert account.xtool_env()["XDG_CONFIG_HOME"].endswith("/.config")


def test_get_status_against_fake(fake, tmp_path, monkeypatch):
    monkeypatch.setattr(account, "xtool_data_dir", lambda: tmp_path / "data")
    st = account.get_status()
    assert st["signedIn"] and st["teamId"] == "TEAM123456" and st["hasLocalSigningKey"] is False
    (tmp_path / "data" / "TEAM123456").mkdir(parents=True)
    assert account.get_status()["hasLocalSigningKey"] is True


def cp(rc: int, out: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], rc, out, "")


def test_fetch_section_retries_transient_then_succeeds(fake, monkeypatch):
    replies = [cp(1, "Error: server busy"), cp(1, "The request timed out."), cp(0, TEAMS)]
    monkeypatch.setattr(account, "xtool", lambda args, timeout=60: replies.pop(0))
    r = account.fetch_section("teams")
    assert r["ok"] and r["attempts"] == 3 and len(r["items"]) == 3


def test_fetch_section_stops_on_a_permanent_error(fake, monkeypatch):
    calls = []
    monkeypatch.setattr(account, "xtool", lambda args, timeout=60: calls.append(args) or cp(1, "Please log in"))
    r = account.fetch_section("devices")
    assert not r["ok"] and r["error"]["code"] == "not-signed-in" and len(calls) == 1


def test_fetch_section_gives_up_after_attempts(fake, monkeypatch):
    def raise_busy(args, timeout=60):
        raise HelperError("timeout", "slow")
    monkeypatch.setattr(account, "xtool", raise_busy)
    r = account.fetch_section("devices", attempts=3)
    assert not r["ok"] and r["error"]["code"] == "timeout"


def test_fetch_section_parser_bug_is_contained(fake, monkeypatch):
    monkeypatch.setattr(account, "xtool", lambda args, timeout=60: cp(0, "x"))
    monkeypatch.setattr(account, "parse_section", lambda n, t: 1 / 0)
    r = account.fetch_section("teams")
    assert not r["ok"] and r["error"]["code"] == "parse" and "ZeroDivisionError" in r["error"]["detail"]


# ================================================================ details

def scenario(d, **sections):
    for name, text in sections.items():
        (d / f"{name}.txt").write_text(text)


def test_details_cached_empty(fake, capsys):
    assert account.cmd_details({"cached": True}) == 0
    assert lines(capsys)[0]["data"] == {"cached": True, "empty": True, "stale": True, "fetchedAt": None}


def test_details_fetch_cache_and_keep_last_good(fake, capsys):
    scenario(fake, teams=TEAMS, profiles=profiles_text(5), certificates="", identifiers="", devices="")
    assert account.cmd_details({}) == 0
    data = lines(capsys)[0]["data"]
    assert data["cached"] is False and data["errors"] == []
    assert data["summary"]["health"]["level"] == "ok" and data["summary"]["teamType"] == "free"
    assert all(s["ok"] and not s["stale"] for s in data["sections"].values())
    calls = (fake / "calls").read_text().splitlines()
    assert sorted(c for c in calls if c.startswith("ds")) == [f"ds {s} list" for s in sorted(account.SECTIONS)]

    # Apple fails for profiles now: the last good list stays, flagged stale, with the error attached
    (fake / "profiles.txt").unlink()
    assert account.cmd_details({"sections": "profiles,bogus"}) == 0
    data = lines(capsys)[0]["data"]
    prof = data["sections"]["profiles"]
    assert prof["stale"] and not prof["ok"] and [p["id"] for p in prof["items"]] == ["P0", "P1"]
    assert data["errors"] == [{"section": "profiles", **prof["error"]}] and prof["error"]["code"] == "busy"
    assert data["sections"]["teams"]["ok"]                                  # not asked for: kept from the cache

    assert account.cmd_details({"cached": True}) == 0
    cached = lines(capsys)[0]["data"]
    assert cached["cached"] and not cached["stale"] and cached["ageSeconds"] >= 0


def test_details_first_error_when_nothing_works(fake, capsys):
    assert account.cmd_details({"sections": "teams,devices"}) == 1
    err = lines(capsys)[0]["error"]
    assert err["code"] == "busy"


def test_details_need_password_sign_in(fake, capsys):
    (fake / "status").write_text("Logged in.\n - ASC Key ID: K\n - Issuer ID: I\n")
    with pytest.raises(HelperError) as ei:
        account.cmd_details({})
    assert ei.value.code == "key-mode"
    (fake / "status").write_text("Not logged in\n")
    with pytest.raises(HelperError) as ei:
        account.cmd_details({})
    assert ei.value.code == "not-signed-in"


def test_logout_drops_the_cache(fake, capsys):
    appdirs.cache_put(account.CACHE_NAME, {"x": 1})
    assert account.cmd_logout({"reset_2fa": True}) == 0
    assert lines(capsys)[0]["data"] == {"signedIn": False}
    assert appdirs.cache_get(account.CACHE_NAME) is None
    assert "auth logout --reset-2fa" in (fake / "calls").read_text()


def test_login_rejects_bad_mode(capsys):
    assert account.cmd_login({"mode": "magic"}) == 2
    ev = lines(capsys)[0]      # a streaming command: the error is its done event
    assert ev["event"] == "done" and ev["ok"] is False and ev["error"]["code"] == "usage"
