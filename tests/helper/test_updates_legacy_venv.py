"""Toolchain updates find pymobiledevice3 in the venv in use: ours (VENV_DIR) or omarchy-apple-dev's ~/pymobile3-venv.

`updates status` used to read only our venv, so a machine with just ~/pymobile3-venv (what omarchy-apple-dev's
scripts make) showed pymobiledevice3 as "Not installed yet". It now uses Setup's lookup (facts.pymd3_venvs: ours,
then ~/pymobile3-venv, symlinks followed). An update installs ours and leaves ~/pymobile3-venv alone, so a rollback
that removes ours goes back to it.

    .venv/bin/pytest -q tests/helper/test_updates_legacy_venv.py
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from iosstudio.core import appdirs
from iosstudio.features.setup import channel, facts
from iosstudio.features.setup.pins import PYMD3_VERSION
from iosstudio.features.updates import model
from support.setup_upgrade import Machine, setup_machine

OLD, NEW = "11.40.0", "11.50.0"


def make_venv(v: Path, version: str) -> Path:
    (v / "bin").mkdir(parents=True)
    (v / "bin/python").write_text("#!/bin/sh\nexit 0\n")
    (v / f"lib/python3.14/site-packages/pymobiledevice3-{version}.dist-info").mkdir(parents=True)
    return v


@pytest.fixture
def venvs(tmp_path, monkeypatch) -> tuple[Path, Path]:
    """(ours, legacy): neither exists yet."""
    ours, legacy = tmp_path / "data/ios-studio/venv", tmp_path / "home/pymobile3-venv"
    legacy.parent.mkdir(parents=True)
    monkeypatch.setattr(facts, "VENV_DIR", ours)
    monkeypatch.setattr(facts, "LEGACY_VENV", legacy)
    monkeypatch.setattr(channel, "VENV_DIR", ours)
    return ours, legacy


# ================================================================ detection

def test_nothing_installed(venvs):
    assert facts.pymd3_venvs() == []
    assert model.pymd3_installed() is None


def test_only_the_legacy_venv(venvs):
    ours, legacy = venvs
    make_venv(legacy, OLD)
    assert facts.pymd3_venvs() == [legacy]
    assert model.pymd3_installed() == OLD


def test_only_ours(venvs):
    ours, _ = venvs
    make_venv(ours, NEW)
    assert facts.pymd3_venvs() == [ours]
    assert model.pymd3_installed() == NEW


def test_both_ours_comes_first(venvs):
    ours, legacy = venvs
    make_venv(ours, NEW)
    make_venv(legacy, OLD)
    assert facts.pymd3_venvs() == [ours, legacy]
    assert model.pymd3_installed() == NEW


def test_a_broken_venv_of_ours_falls_back_to_the_legacy_one(venvs):
    ours, legacy = venvs
    (ours / "bin").mkdir(parents=True)            # no pymobiledevice3 in it
    make_venv(legacy, OLD)
    assert model.pymd3_installed() == OLD


def test_the_setup_symlink_to_ours_is_listed_once(venvs):
    ours, legacy = venvs
    make_venv(ours, NEW)
    legacy.symlink_to(ours)
    assert facts.pymd3_venvs() == [ours]
    assert model.pymd3_installed() == NEW


def test_a_legacy_symlink_to_another_venv_is_followed(venvs, tmp_path):
    _, legacy = venvs
    legacy.symlink_to(make_venv(tmp_path / "elsewhere/venv", OLD))
    assert facts.pymd3_venvs() == [legacy]
    assert model.pymd3_installed() == OLD


def test_a_dangling_legacy_symlink_is_ignored(venvs, tmp_path):
    _, legacy = venvs
    legacy.symlink_to(tmp_path / "gone")
    assert facts.pymd3_venvs() == []
    assert model.pymd3_installed() is None


def test_detection_is_cheap(venvs):
    ours, legacy = venvs
    make_venv(legacy, OLD)
    t = time.perf_counter()
    for _ in range(10):
        model.pymd3_installed()
    assert (time.perf_counter() - t) / 10 < 0.02


# ================================================================ status

@pytest.fixture
def machine(tmp_path, monkeypatch) -> Machine:
    """setup_upgrade's machine with private settings, state and cache; no pymobiledevice3 venv of ours."""
    m = setup_machine(tmp_path / "m", monkeypatch)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    monkeypatch.setattr(appdirs, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(appdirs, "SETTINGS_FILE", tmp_path / "config/settings.json")
    monkeypatch.setattr(facts, "SETUP_STATE", tmp_path / "state/setup.json")
    monkeypatch.setattr(facts, "VENV_DIR", tmp_path / "data/venv")
    monkeypatch.setattr(channel, "VENV_DIR", tmp_path / "data/venv")
    monkeypatch.setattr(model, "PACMAN_LOCAL", tmp_path / "pacman")
    return m


def pymd3_row(status: dict) -> dict:
    return next(c for c in status["components"] if c["id"] == "pymobiledevice3")


def test_status_reads_the_legacy_venv(machine):
    """The owner's machine: pymobiledevice3 only in ~/pymobile3-venv (a real folder, not our symlink)."""
    import shutil
    shutil.rmtree(machine.legacy_venv)
    make_venv(machine.legacy_venv, PYMD3_VERSION)
    row = pymd3_row(model.status("tested", model.empty_cache()))
    assert row["installed"] == PYMD3_VERSION
    assert row["state"] == "current"
    assert row["applicable"] is True and row["blockedReason"] is None


def test_status_offers_the_tested_version_over_an_older_legacy_venv(machine):
    import shutil
    shutil.rmtree(machine.legacy_venv)
    make_venv(machine.legacy_venv, OLD)
    row = pymd3_row(model.status("tested", model.empty_cache()))
    assert (row["installed"], row["target"], row["state"]) == (OLD, PYMD3_VERSION, "update")


# ================================================================ apply's backup and rollback

def test_backup_with_only_the_legacy_venv_rolls_back_to_it(venvs):
    """The update installs ours beside ~/pymobile3-venv; a rollback removes ours, and the legacy one is in use again."""
    ours, legacy = venvs
    make_venv(legacy, OLD)
    b = channel.backup("pymobiledevice3")
    assert b.to_json() == channel.venv_backup_plan().to_json() == \
        {"kind": "venv", "component": "pymobiledevice3", "existed": False}
    assert model.pymd3_installed() == OLD            # the legacy venv isn't moved
    make_venv(ours, NEW)                             # what i_pymd3 installs
    assert model.pymd3_installed() == NEW            # ours comes first
    b.restore()
    assert not ours.exists()
    assert model.pymd3_installed() == OLD
    assert facts.pymd3_venvs() == [legacy]


def test_backup_with_only_the_legacy_venv_keeps_the_update(venvs):
    ours, legacy = venvs
    make_venv(legacy, OLD)
    b = channel.backup("pymobiledevice3")
    make_venv(ours, NEW)
    b.discard()
    assert model.pymd3_installed() == NEW
    assert (legacy / "bin/python").exists()          # omarchy-apple-dev's own venv stays


def test_backup_with_both_moves_ours_and_puts_it_back(venvs):
    ours, legacy = venvs
    make_venv(ours, OLD)
    make_venv(legacy, "11.0.0")
    b = channel.backup("pymobiledevice3")
    assert channel.venv_prev().is_dir() and not ours.exists()
    make_venv(ours, NEW)
    assert model.pymd3_installed() == NEW
    b.restore()
    assert model.pymd3_installed() == OLD
    assert not channel.venv_prev().exists()


def test_backup_with_the_setup_symlink(venvs):
    """~/pymobile3-venv -> ours: the backup moves ours (the link dangles meanwhile) and the rollback brings it back."""
    ours, legacy = venvs
    make_venv(ours, OLD)
    legacy.symlink_to(ours)
    b = channel.backup("pymobiledevice3")
    assert facts.pymd3_venvs() == []
    make_venv(ours, NEW)
    assert facts.pymd3_venvs() == [ours] and model.pymd3_installed() == NEW
    b.restore()
    assert model.pymd3_installed() == OLD
    assert legacy.is_symlink() and facts.pymd3_venvs() == [ours]
