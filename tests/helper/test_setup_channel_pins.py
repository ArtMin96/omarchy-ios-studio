"""Toolchain updates, Setup's side: the update channel's pins (one place resolves them; installers, rows and
`toolchain info` read ctx.pins) and the backups an update rolls back to.

Runs in process against tests/support/setup_upgrade.py: real git on local remotes, fake swift, ldd, xtool and
install-toolchain.sh. The binaries' installers run as dry runs (their downloads are GitHub URLs), which goes
through the same code and logs the exact URL, checksum and pip pin.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from iosstudio.core import appdirs
from iosstudio.core.output import HelperError
from iosstudio.features.setup import channel, facts, installers, pins, steps, toolchain
from iosstudio.features.setup import runtime as rt
from support.setup_upgrade import Machine, git, setup_machine

NEW_SHA = "ab" * 32


def events(capsys) -> list[dict]:
    return [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.strip()]


def logs(capsys) -> list[str]:
    return [e["text"] for e in events(capsys) if e.get("event") == "log"]


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    """Private settings.json and setup state; this machine's architecture is x86_64."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))     # updates/prev
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))  # updates.lock
    monkeypatch.setattr(appdirs, "SETTINGS_FILE", tmp_path / "config/settings.json")
    monkeypatch.setattr(facts, "SETUP_STATE", tmp_path / "state/setup.json")
    monkeypatch.setattr(channel, "arch", lambda: "x86_64")
    monkeypatch.setattr(installers, "arch", lambda: "x86_64")
    return tmp_path


@pytest.fixture
def machine(tmp_path, monkeypatch, home) -> Machine:
    m = setup_machine(tmp_path / "m", monkeypatch)
    monkeypatch.setattr(channel, "APPLE_DEV_COMMIT", m.ad_old)    # tested = the old commit, latest = the new one
    return m


def save_latest(**entries) -> None:
    channel.set_channel("latest")
    facts.save_state({"channelPins": entries})


# ================================================================ effective_pins

def test_tested_is_pins_py(home):
    p = channel.effective_pins()
    assert p == channel.tested_pins() == channel.effective_pins("tested")
    assert (p.apple_dev_commit, p.ipsw_version, p.rcodesign_version, p.pymd3_version) == \
        (pins.APPLE_DEV_COMMIT, pins.IPSW_VERSION, pins.RCODESIGN_VERSION, pins.PYMD3_VERSION)
    assert p.ipsw_sha == pins.IPSW_SHA["x86_64"][1] and p.rcodesign_sha == pins.RCODESIGN_SHA["x86_64"]
    assert all(p.is_tested(c) for c in channel.COMPONENTS) and p.word("ipsw") == "tested"


def test_latest_lays_channel_pins_over_tested(home):
    facts.save_state({"channelPins": {"ipsw": {"version": "3.1.752", "sha256": NEW_SHA, "at": 1}}})
    assert channel.effective_pins() == channel.tested_pins()        # saved channel is tested: ignored
    channel.set_channel("latest")
    p = channel.effective_pins()
    assert (p.ipsw_version, p.ipsw_sha) == ("3.1.752", NEW_SHA)
    assert p.rcodesign_version == pins.RCODESIGN_VERSION and p.apple_dev_commit == pins.APPLE_DEV_COMMIT
    assert not p.is_tested("ipsw") and p.word("ipsw") == "latest" and p.is_tested("rcodesign")
    assert channel.effective_pins("tested") == channel.tested_pins()  # an explicit channel wins over the saved one


def test_unverifiable_channel_pins_are_ignored(home):
    save_latest(rcodesign={"version": "0.30.0"},                       # no checksum
                ipsw={"version": "3.1.752", "sha256": "nothex"},
                **{"apple-dev": {"commit": "abc1234"}, "pymobiledevice3": {"version": "../evil"}, "xtool": "x"})
    assert channel.effective_pins() == channel.tested_pins()


def test_override_wins_on_any_channel(home):
    save_latest(pymobiledevice3={"version": "12.0.0", "sha256": NEW_SHA})
    over = {"pymobiledevice3": {"version": "12.1.0"}, "apple-dev": {"commit": "c" * 40}}
    for ch in ("tested", "latest", None):
        p = channel.effective_pins(ch, over)
        assert p.pymd3_version == "12.1.0" and p.apple_dev_commit == "c" * 40
    assert channel.effective_pins().pymd3_version == "12.0.0"


def test_channel_setting_and_recorded_pins(home):
    appdirs.SETTINGS_FILE.parent.mkdir(parents=True)
    appdirs.SETTINGS_FILE.write_text('{"projectRoots": ["~/x"], "toolchainChannel": "bogus"}')
    assert channel.current_channel() == "tested"
    channel.set_channel("latest")
    assert json.loads(appdirs.SETTINGS_FILE.read_text()) == {"projectRoots": ["~/x"], "toolchainChannel": "latest"}
    with pytest.raises(HelperError):
        channel.set_channel("nightly")
    channel.record_pin("ipsw", version="3.1.752", sha256=NEW_SHA, at=5)
    channel.record_pin("apple-dev", commit="d" * 40, at=6)
    assert channel.channel_pins() == {"ipsw": {"version": "3.1.752", "sha256": NEW_SHA, "at": 5},
                                      "apple-dev": {"commit": "d" * 40, "at": 6}}
    assert channel.effective_pins().apple_dev_commit == "d" * 40


# ================================================================ the installers and rows read ctx.pins

def test_binary_installers_download_the_pinned_release(machine, capsys, monkeypatch):
    monkeypatch.setattr(rt, "DRY", True)
    ctx = steps.Ctx({}, pins=channel.effective_pins(override={"ipsw": {"version": "3.1.752", "sha256": NEW_SHA},
                                                              "rcodesign": {"version": "0.30.0", "sha256": NEW_SHA}}))
    installers.i_ipsw(ctx, "ipsw")
    installers.i_rcodesign(ctx, "rcodesign")
    text = "\n".join(logs(capsys))
    assert "releases/download/v3.1.752/ipsw_3.1.752_linux_x86_64.tar.gz" in text
    assert "apple-codesign%2F0.30.0/apple-codesign-0.30.0-x86_64-unknown-linux-musl.tar.gz" in text
    assert text.count(f"sha256 {NEW_SHA[:12]}") == 2
    installers.i_ipsw(steps.Ctx({}), "ipsw")                   # the default: the tested pin
    assert f"v{pins.IPSW_VERSION}/" in "\n".join(logs(capsys))


def test_pymd3_pin_and_keep_previous(machine, capsys, monkeypatch, tmp_path):
    venv = tmp_path / "data/venv"
    (venv / "bin").mkdir(parents=True)
    monkeypatch.setattr(installers, "VENV_DIR", venv)
    monkeypatch.setattr(rt, "ALLOWED_DELETE_ROOTS", (venv,))
    monkeypatch.setattr(rt, "DRY", True)
    over = channel.effective_pins(override={"pymobiledevice3": {"version": "12.1.0"}})
    installers.i_pymd3(steps.Ctx({}, pins=over), "pymobiledevice3")
    text = "\n".join(logs(capsys))
    assert "pymobiledevice3==12.1.0" in text and "would delete" in text
    installers.i_pymd3(steps.Ctx({}, pins=over, keep_previous=True), "pymobiledevice3")
    text = "\n".join(logs(capsys))
    assert "pymobiledevice3==12.1.0" in text and "would delete" not in text   # the backup holds the old venv


def test_rows_and_info_name_the_latest_pin(machine, monkeypatch):
    save_latest(**{"apple-dev": {"commit": machine.ad_new}, "pymobiledevice3": {"version": "12.1.0"},
                   "ipsw": {"version": "3.1.752", "sha256": NEW_SHA}})
    s = {r["id"]: r for r in steps.Ctx({}).steps}
    ad = s["apple-dev"]
    assert ad["state"] == "broken" and ad["update"] is True
    assert f"; the Latest channel has {machine.ad_new[:7]} (untested). " in ad["detail"]
    assert "(latest: 12.1.0)" in s["pymobiledevice3"]["detail"]
    info = {i["id"]: i for i in toolchain.info()["items"]}
    assert info["apple-dev"]["pinned"] == machine.ad_new[:7] and info["ipsw"]["pinned"] == "3.1.752"
    assert info["pymobiledevice3"]["pinned"] == "12.1.0" and info["rcodesign"]["pinned"] == pins.RCODESIGN_VERSION
    assert info["apple-dev"]["status"] == "update"           # an older checkout: works, the channel's pin is ready
    assert (info["apple-dev"]["pinWord"], info["pymobiledevice3"]["pinWord"], info["ipsw"]["pinWord"]) == \
        ("latest", "latest", "latest")
    assert info["rcodesign"]["pinWord"] == "tested"
    channel.set_channel("tested")
    s = {r["id"]: r for r in steps.Ctx({}).steps}
    assert s["apple-dev"]["state"] == "done" and "At the tested version" in s["apple-dev"]["detail"]


def test_install_on_latest_moves_the_scripts_to_the_latest_commit(machine, capsys):
    save_latest(**{"apple-dev": {"commit": machine.ad_new}})
    assert installers.install({"step": "apple-dev", "yes": True}) == 0
    assert events(capsys)[-1]["data"]["installed"] == ["apple-dev", "xtool", "sdk"]
    assert git("-C", str(machine.apple_dev), "rev-parse", "HEAD", cwd=machine.root) == machine.ad_new
    assert not any(p.exists() for p in machine.legacy_dirs())   # not an update run: pruned at once


# ================================================================ backups

def test_binary_backup_restore_and_discard(machine):
    ipsw = machine.local_bin / "ipsw"
    ipsw.write_text("old")
    b = channel.backup("ipsw")
    saved = channel.prev_dir() / "ipsw"
    assert saved.read_text() == "old" and channel.backup_paths() == [saved]
    ipsw.write_text("new")
    b.restore()
    assert ipsw.read_text() == "old" and os.access(ipsw, os.X_OK) and not saved.exists()

    channel.backup("ipsw").discard()
    assert ipsw.read_text() == "old" and channel.backup_paths() == []

    rc = machine.local_bin / "rcodesign"                     # nothing installed before: restore removes the new one
    b = channel.backup("rcodesign")
    rc.write_text("new")
    b.restore()
    assert not rc.exists()


def test_venv_backup_renames_and_restores(machine, monkeypatch, tmp_path):
    venv = tmp_path / "data/ios-studio/venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin/old").write_text("1")
    monkeypatch.setattr(channel, "VENV_DIR", venv)
    b = channel.backup("pymobiledevice3")
    assert not venv.exists() and (channel.venv_prev() / "bin/old").exists()
    assert channel.venv_prev() == venv.with_name("venv.prev") and channel.venv_prev() in channel.backup_paths()
    (venv / "bin").mkdir(parents=True)                        # the update's new, broken venv
    (venv / "bin/new").write_text("1")
    b.restore()
    assert (venv / "bin/old").exists() and not (venv / "bin/new").exists() and not channel.venv_prev().exists()

    b = channel.backup("pymobiledevice3")
    (venv / "bin").mkdir(parents=True)
    b.discard()
    assert venv.is_dir() and not channel.venv_prev().exists()


def move_scripts(m: Machine, commit: str) -> None:
    git("-C", str(m.apple_dev), "fetch", "-q", "--depth", "1", f"file://{m.root / 'remotes/omarchy-apple-dev'}",
        commit, cwd=m.root)
    git("-C", str(m.apple_dev), "checkout", "-q", "--detach", commit, cwd=m.root)


def test_xtool_update_keeps_the_old_build_until_discard(machine, capsys):
    move_scripts(machine, machine.ad_new)
    old = machine.legacy_dirs()[0]
    new = machine.lib / f"xtool-{machine.x_new}"
    latest = channel.effective_pins(override={"apple-dev": {"commit": machine.ad_new}})
    b = channel.backup("xtool")
    assert installers.run_chain(steps.Ctx({"yes": True}, pins=latest, keep_previous=True), "xtool") == ["xtool"]
    assert os.path.realpath(machine.local_bin / "xtool") == str(new / "xtool")
    assert all(p.exists() for p in machine.legacy_dirs())      # pruning waits for the post-update check
    b.restore()
    assert os.path.realpath(machine.local_bin / "xtool") == str(old / "xtool")
    assert not new.exists() and not (machine.cache / new.name).exists() and old.exists()

    b = channel.backup("xtool")
    installers.run_chain(steps.Ctx({"yes": True}, pins=latest, keep_previous=True), "xtool")
    b.discard()
    assert new.exists() and not any(p.exists() for p in machine.legacy_dirs())
    capsys.readouterr()


def test_apple_dev_restore_reruns_the_repair_only_when_it_ran(machine, capsys):
    over = channel.effective_pins(override={"apple-dev": {"commit": machine.ad_new}})
    b_ad, b_xt = channel.backup("apple-dev"), channel.backup("xtool")
    assert installers.run_chain(steps.Ctx({"yes": True}, pins=over, keep_previous=True), "apple-dev") == \
        ["apple-dev", "xtool", "sdk"]
    assert machine.calls("install-toolchain.sh") == ["install-toolchain.sh --mode full --repair"]
    b_xt.restore()                                            # newest first: xtool, then the scripts
    b_ad.restore()
    assert git("-C", str(machine.apple_dev), "rev-parse", "HEAD", cwd=machine.root) == machine.ad_old
    assert len(machine.calls("install-toolchain.sh")) == 2     # the old darwin tools rebuilt at the old commit
    s = {r["id"]: r["state"] for r in steps.Ctx({}).steps}
    assert s["apple-dev"] == s["xtool"] == s["sdk"] == "done"
    capsys.readouterr()


def test_apple_dev_restore_without_repair_keeps_the_sdk_done(machine, capsys):
    b = channel.backup("apple-dev")
    move_scripts(machine, machine.ad_new)                      # the update moved the scripts, then failed
    b.restore()
    assert git("-C", str(machine.apple_dev), "rev-parse", "HEAD", cwd=machine.root) == machine.ad_old
    assert not machine.calls("install-toolchain.sh")
    assert not facts.darwin_tools_stale(machine.apple_dev, machine.bundle)
    assert {r["id"]: r["state"] for r in steps.Ctx({}).steps}["sdk"] == "done"
    channel.backup("apple-dev").restore()                      # at the old HEAD already: nothing to do
    assert git("-C", str(machine.apple_dev), "rev-parse", "HEAD", cwd=machine.root) == machine.ad_old
    capsys.readouterr()


def test_dry_run_and_unknown_backups(machine, capsys, monkeypatch):
    with pytest.raises(HelperError):
        channel.backup("swift")
    monkeypatch.setattr(rt, "DRY", True)
    (machine.local_bin / "ipsw").write_text("old")
    b = channel.backup("ipsw")
    assert type(b) is channel.Backup and channel.backup_paths() == []
    b.restore()
    assert any("would keep the previous ipsw" in t for t in logs(capsys))


# ================================================================ leftovers: the disk item, clean, the lock

def leftovers(monkeypatch, tmp_path) -> Path:
    venv = tmp_path / "data/ios-studio/venv"
    monkeypatch.setattr(channel, "VENV_DIR", venv)
    (channel.venv_prev() / "bin").mkdir(parents=True)
    (channel.venv_prev() / "bin/python").write_bytes(b"x" * 5000)
    channel.prev_dir().mkdir(parents=True)
    (channel.prev_dir() / "ipsw").write_bytes(b"x" * 5000)
    return venv


def test_disk_item_and_clean(machine, capsys, monkeypatch, tmp_path):
    leftovers(monkeypatch, tmp_path)
    item = {i["id"]: i for i in toolchain.disk_items()}["update-backups"]
    assert item["cleanable"] and item["safe"]
    assert item["paths"] == [channel.prev_dir() / "ipsw", channel.venv_prev()]
    assert toolchain.clean({"item": "update-backups"}) == 0
    assert events(capsys)[-1]["data"]["freed"] > 0
    assert channel.backup_paths() == [] and {i["id"]: i for i in toolchain.disk_items()}["update-backups"]["paths"] == []


def test_clear_backups_waits_for_a_running_update(machine, capsys, monkeypatch, tmp_path):
    leftovers(monkeypatch, tmp_path)
    with channel.updates_lock():
        with pytest.raises(HelperError) as cm:
            channel.clear_backups()
        assert cm.value.code == "busy" and len(channel.backup_paths()) == 2
        assert channel.clear_backups(take_lock=False) > 0      # the update itself, lock held
    assert channel.backup_paths() == []
    capsys.readouterr()
