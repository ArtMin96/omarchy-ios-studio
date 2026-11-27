"""Toolchain updates: `updates check` against a local fake upstream (tests/support/updates_fakes.py) and `updates status`
merging its cache with what's installed, in process.

Covers the once-a-day rule, ETag/304 reuse, rate limits (403 + X-RateLimit-Reset, 429 + Retry-After), network and
partial failures (nextCheckAt still moves), the 40 s run cap, versions that can't be verified (never offered, a
notice instead), the channel targets and states, the SDK-stamp block and the notices. No real network.

    .venv/bin/pytest -q tests/helper/test_updates_check.py
"""
from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path

import pytest

from iosstudio.core import appdirs
from iosstudio.features.setup import channel, facts
from iosstudio.features.updates import check, model, upstream
from iosstudio.features.updates.commands import cmd_channel
from support.setup_upgrade import Machine, make_bundle, setup_machine
from support.updates_fakes import FakeUpstream, Upstream, tool_script

IDS = ["apple-dev", "xtool", "pymobiledevice3", "ipsw", "rcodesign"]


def events(capsys) -> list[dict]:
    return [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.strip()]


def done(evs: list[dict]) -> dict:
    return next(e for e in evs if e.get("event") == "done")


@pytest.fixture
def machine(tmp_path, monkeypatch) -> Machine:
    """setup_upgrade's machine (omarchy-apple-dev, xtool, an SDK) with private settings, state and cache, an ipsw and
    rcodesign at the tested versions and our pymobiledevice3 venv at the tested version; x86_64."""
    m = setup_machine(tmp_path / "m", monkeypatch)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    monkeypatch.setattr(appdirs, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(appdirs, "SETTINGS_FILE", tmp_path / "config/settings.json")
    monkeypatch.setattr(facts, "SETUP_STATE", tmp_path / "state/setup.json")
    monkeypatch.setattr(channel, "VENV_DIR", tmp_path / "data/venv")
    monkeypatch.setattr(model, "PACMAN_LOCAL", tmp_path / "pacman")
    for mod in (channel, upstream):
        monkeypatch.setattr(mod, "arch", lambda: "x86_64")
    for name, ver in (("ipsw", "3.1.743"), ("rcodesign", "0.29.0")):
        p = m.local_bin / name
        p.write_text(tool_script(name, ver))
        p.chmod(0o755)
    (tmp_path / "data/venv/lib/python3.14/site-packages/pymobiledevice3-11.49.0.dist-info").mkdir(parents=True)
    (tmp_path / "pacman/swift-bin-6.4.2-2").mkdir(parents=True)
    return m


@pytest.fixture
def up(tmp_path, monkeypatch, machine):
    with FakeUpstream(tmp_path / "up") as fake:
        for k, v in fake.env().items():
            monkeypatch.setenv(k, v)
        yield fake


def run_check(capsys, force: bool = True) -> tuple[list[dict], dict]:
    check.run(force)
    evs = events(capsys)
    return evs, done(evs)


def comp(status: dict, cid: str) -> dict:
    return next(c for c in status["components"] if c["id"] == cid)


# ================================================================ check

def test_check_finds_the_verified_latest_versions(up, capsys):
    evs, d = run_check(capsys)
    assert d["ok"], d
    assert sorted(e["id"] for e in evs if e.get("event") == "component") == sorted(IDS)
    st = d["data"]
    assert (st["channel"], st["error"], st["due"], st["checking"]) == ("tested", None, False, False)
    assert abs(st["nextCheckAt"] - st["checkedAt"] - model.DAY) < 1
    ad = comp(st, "apple-dev")
    assert (ad["latest"], ad["latestTag"], ad["checksum"]["source"]) == (up.commit[:7], "v0.5.0", "git-commit")
    assert comp(st, "xtool")["latest"] == "c" * 7                      # the XTOOL_SHA the release pins
    assert comp(st, "ipsw")["latest"] == "3.1.752" and comp(st, "rcodesign")["latest"] == "0.30.0"
    pm = comp(st, "pymobiledevice3")
    assert (pm["latest"], pm["restarts"], pm["installed"]) == ("99.1.0", ["iphone-connection"], "11.49.0")
    assert comp(st, "ipsw")["installed"] == "3.1.743"                  # probed by the check, read from cache after
    cache = model.load_cache()
    assert any(u.endswith("/releases/latest") for u in cache["etags"])
    assert cache["rate"]["remaining"] == 57 and cache["rate"]["resetAt"] is None


def test_once_a_day_and_etags(up, capsys):
    run_check(capsys)
    api = "/api/repos/blacktop/ipsw/releases/latest"
    sums = "/gh/dl/v3.1.752/checksums.txt"
    assert up.hits[api] == [200] and up.hits[sums] == [200]
    evs, d = run_check(capsys, force=False)
    assert d["ok"] and up.hits[api] == [200]                           # checked less than a day ago: skipped
    assert any("less than a day" in e.get("text", "") for e in evs)
    assert not [e for e in evs if e.get("event") == "component"]
    _, d = run_check(capsys)                                           # forced: conditional requests
    assert up.hits[api] == [200, 304] and up.hits[sums] == [200]       # a release's checksum file is read once
    assert comp(d["data"], "ipsw")["latest"] == "3.1.752"
    up.up.ipsw = "3.1.760"
    up.publish()
    _, d = run_check(capsys)
    assert up.hits[api][-1] == 200 and comp(d["data"], "ipsw")["latest"] == "3.1.760"


def test_rate_limit_waits_for_the_reset(up, capsys):
    reset = int(time.time()) + 900
    up.rate_limited = reset
    _, d = run_check(capsys)
    st = d["data"]
    assert st["error"]["code"] == "rate-limited" and not st["due"]
    cache = model.load_cache()
    assert cache["rate"]["resetAt"] == reset and cache["nextCheckAt"] >= reset
    assert comp(st, "pymobiledevice3")["latest"] == "99.1.0"           # PyPI isn't GitHub: still answered
    before = sum(len(v) for v in up.hits.values())
    evs, d = run_check(capsys)                                         # even forced: not before the reset
    assert sum(len(v) for v in up.hits.values()) == before
    assert any("wait" in e.get("text", "") for e in evs)


def test_retry_after(up, capsys):
    up.retry_after = 120
    _, d = run_check(capsys)
    assert d["data"]["error"]["code"] == "rate-limited"
    assert 100 < model.load_cache()["rate"]["resetAt"] - time.time() <= 121


def test_offline_keeps_the_last_answer_and_retries_later(up, capsys, monkeypatch):
    run_check(capsys)
    with socket.socket() as s:                                         # a port nobody listens on
        s.bind(("127.0.0.1", 0))
        dead = f"http://127.0.0.1:{s.getsockname()[1]}"
    for var in ("IOSSTUDIO_GITHUB_API", "IOSSTUDIO_GITHUB_RAW", "IOSSTUDIO_PYPI", "IOSSTUDIO_AUR"):
        monkeypatch.setenv(var, dead)
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "https://nothing.invalid/")
    t0 = time.time()
    _, d = run_check(capsys)
    assert not d["ok"] and d["error"]["code"] == "network"
    st = d["data"]
    assert st["error"]["code"] == "network" and not st["due"]
    assert abs(st["nextCheckAt"] - t0 - model.RETRY_AFTER_ERROR) < 5
    assert comp(st, "ipsw")["latest"] == "3.1.752"                     # the last good answer stays


def test_partial_failure(up, capsys):
    up.fail["/pypi/pypi/pymobiledevice3/json"] = 503
    _, d = run_check(capsys)
    st = d["data"]
    assert d["ok"] and st["error"]["code"] == "partial" and "pymobiledevice3" in st["error"]["message"]
    assert comp(st, "ipsw")["latest"] == "3.1.752" and comp(st, "pymobiledevice3")["latest"] is None
    assert st["nextCheckAt"] - time.time() == pytest.approx(model.RETRY_AFTER_ERROR, abs=5)


def test_the_run_is_capped(up, capsys, monkeypatch):
    monkeypatch.setattr(upstream, "RUN_CAP", 1.5)
    up.delay["checksums.txt"] = 4
    t0 = time.monotonic()
    _, d = run_check(capsys)
    assert time.monotonic() - t0 < 3.5
    assert d["data"]["error"]["code"] == "partial" and comp(d["data"], "ipsw")["latest"] is None


def test_unverifiable_versions_are_never_offered(up, capsys):
    up.up = Upstream(ipsw_sums=False, pymd3_wheel_sha=False)
    up.publish()
    _, d = run_check(capsys)
    st = d["data"]
    assert comp(st, "ipsw")["latest"] is None and comp(st, "pymobiledevice3")["latest"] is None
    ids = {n["id"] for n in st["notices"]}
    assert {"ipsw-unverified", "pymobiledevice3-unverified"} <= ids
    channel.set_channel("latest")
    st = model.status()
    assert comp(st, "ipsw")["target"] == "3.1.743" and comp(st, "ipsw")["state"] == "current"
    assert comp(st, "rcodesign")["state"] == "update"


def test_apple_dev_without_a_verifiable_xtool_pin(up, capsys):
    up.up.xtool_sha = "main"                                           # a branch name: no content address
    up.publish()
    _, d = run_check(capsys)
    assert comp(d["data"], "apple-dev")["latest"] is None
    assert any(n["id"] == "apple-dev-unverified" for n in d["data"]["notices"])


# ================================================================ status

def test_targets_and_states_per_channel(up, capsys, machine):
    run_check(capsys)
    st = model.status("tested")
    assert comp(st, "ipsw")["state"] == "current" and comp(st, "ipsw")["target"] == "3.1.743"
    assert comp(st, "apple-dev")["state"] == "update"                  # the checkout is at the old commit
    assert comp(st, "apple-dev")["target"] == machine.ad_new[:7]
    st = model.status("latest")
    ip = comp(st, "ipsw")
    assert (ip["state"], ip["target"], ip["untested"], ip["applicable"]) == ("update", "3.1.752", True, True)
    assert ip["notesUrl"].endswith("/v3.1.752")
    assert comp(st, "apple-dev")["target"] == up.commit[:7] and comp(st, "xtool")["target"] == "c" * 7
    assert st["count"] == sum(1 for c in st["components"] if c["state"] == "update")
    # An applied latest: newer than tested on latest, a reinstall on tested.
    machine.local_bin.joinpath("ipsw").write_text(tool_script("ipsw", "3.1.752"))
    channel.record_pin("ipsw", version="3.1.752", sha256="a" * 64)
    run_check(capsys)
    assert comp(model.status("latest"), "ipsw")["state"] == "newer-than-tested"
    tested = comp(model.status("tested"), "ipsw")
    assert (tested["state"], tested["target"], tested["untested"]) == ("update", "3.1.743", False)


def test_channel_command_lists_what_would_be_reinstalled(up, capsys, machine):
    run_check(capsys)
    machine.local_bin.joinpath("ipsw").write_text(tool_script("ipsw", "3.1.752"))
    run_check(capsys)
    cmd_channel({"set": "tested"})
    out = json.loads(capsys.readouterr().out)["data"]
    assert out["channel"] == "tested" and "ipsw" in out["reinstall"]
    assert appdirs.settings()["toolchainChannel"] == "tested"


def test_new_sdk_stamp_blocks_apple_dev_until_a_spare_copy_has_it(up, capsys, machine):
    up.up.sdk_stamp = "epoch=5,darwinTools=2.0.0,oam=1.5.0"
    up.publish()
    run_check(capsys)
    st = model.status("latest")
    ad, xt = comp(st, "apple-dev"), comp(st, "xtool")
    assert (ad["state"], ad["applicable"], ad["blockedReason"]) == ("blocked", False, model.SDK_BLOCK)
    assert xt["applicable"] is False
    sdk = next(n for n in st["notices"] if n["id"] == "sdk")
    assert sdk["level"] == "warn" and sdk["fix"]["step"] == "xcode"
    assert comp(model.status("tested"), "apple-dev")["applicable"]     # the tested pin is never blocked
    make_bundle(machine.sdk_cache / "darwin-iPhoneOS27.2.xtoolsdk", ios="27.2", stamp=up.up.sdk_stamp)
    assert comp(model.status("latest"), "apple-dev")["state"] == "update"


def test_notices(up, capsys, machine):
    run_check(capsys)
    ids = {n["id"]: n for n in model.status()["notices"]}
    assert "6.5.0" in ids["swift"]["message"] and ids["swift"]["fix"] is None
    assert "1.25.0" in ids["xtool-upstream"]["message"]                # the machine's xtool says 1.24.0
    assert "xcode" not in ids
    sdk = machine.bundle / "Developer/Platforms/iPhoneOS.platform/Developer/SDKs"
    (sdk / "iPhoneOS27.1.sdk").rename(sdk / "iPhoneOS27.0.sdk")
    x = next(n for n in model.status()["notices"] if n["id"] == "xcode")
    assert "27.1" in x["message"] and x["fix"]["step"] == "xcode"


def test_status_reads_files_only_and_fast(up, capsys, monkeypatch):
    run_check(capsys)
    import subprocess

    def no_process(*a, **kw):
        raise AssertionError("updates status started a process")
    monkeypatch.setattr(subprocess, "Popen", no_process)
    model.status()
    t0 = time.perf_counter()
    for _ in range(5):
        st = model.status("latest")
    assert (time.perf_counter() - t0) / 5 < 0.02
    assert [c["id"] for c in st["components"]] == IDS


def test_interrupted_apply_is_reported(machine):
    assert model.status()["interrupted"] is None
    p = model.marker_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"pid": os.getpid(), "pidStart": model.proc_start(os.getpid()), "component": "ipsw",
                             "since": 1000.0, "backups": []}))
    assert model.status()["interrupted"] is None                       # its process is alive: in progress
    p.write_text(json.dumps({"pid": 2 ** 22 + 7, "pidStart": "1", "component": "ipsw", "since": 1000.0}))
    assert model.status()["interrupted"] == {"component": "ipsw", "since": 1000.0}


def test_git_head_reads_refs(tmp_path):
    from support.setup_upgrade import git
    git("init", "-q", "-b", "main", str(tmp_path / "r"), cwd=tmp_path)
    r = tmp_path / "r"
    (r / "f").write_text("x")
    git("add", "f", cwd=r)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "x", cwd=r)
    head = git("rev-parse", "HEAD", cwd=r)
    assert model.git_head(r) == head
    git("pack-refs", "--all", cwd=r)
    assert not (r / ".git/refs/heads/main").exists() and model.git_head(r) == head
    assert model.git_head(Path(tmp_path / "missing")) is None
