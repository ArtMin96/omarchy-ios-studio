"""Setup pieces the toolchain updater relies on: backups that survive a process (to_json/from_json, the in-progress
marker an interrupted update leaves), IOSSTUDIO_GITHUB_DL for release downloads, and GROUPS loaded on first use.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from iosstudio.features.setup import channel, installers
from iosstudio.features.updates import apply, model

HELPER = Path(__file__).resolve().parents[2] / "helper"


@pytest.mark.parametrize("b", [
    channel.Backup("ipsw"),
    channel._BinaryBackup("ipsw", dest=Path("/h/.local/bin/ipsw"), saved=Path("/c/updates/prev/ipsw")),
    channel._BinaryBackup("rcodesign", dest=Path("/h/.local/bin/rcodesign"), saved=None),
    channel._VenvBackup("pymobiledevice3", existed=True),
    channel._XtoolBackup("xtool", target=Path("/h/.local/lib/omarchy-apple-dev/xtool-abc/xtool")),
    channel._XtoolBackup("xtool", target=None),
    channel._AppleDevBackup("apple-dev", checkout=Path("/h/ad"), head="a" * 40, bundle=Path("/h/sdk"),
                            actool_mtime=12.5, stale_before=True),
])
def test_a_backup_round_trips_through_json(b):
    d = json.loads(json.dumps(b.to_json()))
    assert d["kind"] == b.kind and "type" not in d
    back = channel.Backup.from_json(d)
    assert type(back) is type(b) and back == b


def test_from_json_refuses_what_it_does_not_know():
    assert channel.Backup.from_json({"kind": "nope", "component": "ipsw"}) is None
    assert channel.Backup.from_json({"type": "_VenvBackup", "component": "x"}) is None   # the kind names it, not a class
    assert channel.Backup.from_json({"kind": "venv"}) is None                            # no component


def test_each_kind_is_distinct():
    kinds = [c.kind for c in channel.BACKUP_KINDS]
    assert len(set(kinds)) == len(kinds)


def test_the_venv_plan_names_what_backup_will_move(tmp_path, monkeypatch):
    monkeypatch.setattr(channel, "VENV_DIR", tmp_path / "venv")
    assert channel.venv_backup_plan().to_json() == {"kind": "venv", "component": "pymobiledevice3", "existed": False}
    (tmp_path / "venv").mkdir()
    assert channel.venv_backup_plan().to_json()["existed"] is True


def test_an_interrupted_update_is_put_back_from_its_marker(tmp_path, monkeypatch, capsys):
    """A marker written by a process that's gone: recover() restores the saved binary from the marker's JSON."""
    dest, saved = tmp_path / "bin/ipsw", tmp_path / "prev/ipsw"
    dest.parent.mkdir()
    saved.parent.mkdir()
    dest.write_text("new")
    saved.write_text("old")
    b = channel._BinaryBackup("ipsw", dest=dest, saved=saved)
    monkeypatch.setattr(model, "marker_path", lambda: tmp_path / "updates-inprogress.json")
    (tmp_path / "updates-inprogress.json").write_text(json.dumps(
        {"pid": 999999999, "component": "ipsw", "target": "9.9.9", "since": 1, "backups": [b.to_json()]}))
    assert apply.recover() == ["ipsw"]
    assert dest.read_text() == "old" and not saved.exists()
    assert not (tmp_path / "updates-inprogress.json").exists()
    assert any('"rollback"' in ln for ln in capsys.readouterr().out.splitlines())


# ---------------------------------------------------------------- IOSSTUDIO_GITHUB_DL

def _urls(monkeypatch, fn) -> list[str]:
    seen: list[str] = []
    monkeypatch.setattr(installers, "_install_tarball_binary", lambda url, *a: seen.append(url))
    monkeypatch.setattr(installers, "arch", lambda: "x86_64")
    pins = channel.tested_pins()
    pins = channel.replace(pins, ipsw_sha=pins.ipsw_sha or "0" * 64, rcodesign_sha=pins.rcodesign_sha or "0" * 64)
    fn(type("Ctx", (), {"pins": pins})(), "x")
    return seen


@pytest.mark.parametrize("fn", [installers.i_ipsw, installers.i_rcodesign])
def test_release_downloads_come_from_github_by_default(monkeypatch, fn):
    monkeypatch.delenv("IOSSTUDIO_GITHUB_DL", raising=False)
    (url,) = _urls(monkeypatch, fn)
    assert url.startswith("https://github.com/") and "/releases/download/" in url


@pytest.mark.parametrize("fn", [installers.i_ipsw, installers.i_rcodesign])
def test_github_dl_overrides_the_download_base(monkeypatch, fn):
    monkeypatch.setenv("IOSSTUDIO_GITHUB_DL", "http://127.0.0.1:9/gh/")
    (url,) = _urls(monkeypatch, fn)
    assert url.startswith("http://127.0.0.1:9/gh/") and "gh//" not in url and "/releases/download/" in url


# ---------------------------------------------------------------- GROUPS on first use

def test_the_updates_group_does_not_load_setups_commands():
    code = ("import sys, io, contextlib; from iosstudio import cli\n"
            "with contextlib.redirect_stdout(io.StringIO()): cli.main(['updates', 'status'])\n"
            "print(sorted(m for m in sys.modules if m.startswith('iosstudio.features.setup')))")
    out = subprocess.run([sys.executable, "-c", code], cwd=HELPER, capture_output=True, text=True, timeout=30, check=True)
    loaded = set(eval(out.stdout.strip().splitlines()[-1]))
    assert "iosstudio.features.setup.channel" in loaded
    assert not loaded & {"iosstudio.features.setup.commands", "iosstudio.features.setup.steps",
                         "iosstudio.features.setup.xip"}


def test_groups_still_resolve_for_cli_and_debugger():
    from iosstudio.features import setup
    from iosstudio.features.setup import toolchain_root
    from iosstudio.features.setup.facts import toolchain_root as real
    assert {"setup", "toolchain"} <= set(setup.GROUPS) and toolchain_root is real
    with pytest.raises(AttributeError):
        setup.nope  # noqa: B018
