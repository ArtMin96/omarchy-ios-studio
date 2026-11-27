"""Setup runs an omarchy-apple-dev step's follow-ups in dependency order: xtool is rebuilt before the SDK's
darwin-tools repair that needs it, both when our checkout moves to the pin and when the scripts are downloaded fresh
next to an older xtool build (the xtool row was outdated before the download, so it never turned done → broken).
In process against tests/support/setup_upgrade.py: real git on local remotes, fake swift, ldd, xtool and
install-toolchain.sh.
"""
from __future__ import annotations

import json

import pytest

from iosstudio.features.setup import installers, steps
from support.setup_upgrade import Machine, fresh_download_machine, git, setup_machine


def events(capsys) -> list[dict]:
    return [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.strip()]


def steps_run(evs: list[dict]) -> list[str]:
    return [e["id"] for e in evs if e.get("event") == "step" and e["state"] == "running"]


def states(*ids: str) -> dict[str, str]:
    rows = {r["id"]: r for r in steps.build_steps(steps.gather())[0]}
    return {i: rows[i]["state"] for i in ids}


def rebuilt_in_order(m: Machine) -> None:
    """xtool was built from the new pin before install-toolchain.sh repaired the SDK, each once."""
    calls = m.calls()
    builds = [i for i, c in enumerate(calls) if c.startswith("swift build -c release --product xtool")]
    repairs = [i for i, c in enumerate(calls) if c.startswith("install-toolchain.sh")]
    assert len(builds) == 1 and calls[builds[0]].endswith(m.x_new)
    assert len(repairs) == 1 and builds[0] < repairs[0]


@pytest.fixture
def updated(tmp_path, monkeypatch) -> Machine:
    return setup_machine(tmp_path, monkeypatch)


@pytest.fixture
def fresh(tmp_path, monkeypatch) -> Machine:
    return fresh_download_machine(tmp_path, monkeypatch)


def test_fresh_download_rows_before(fresh):
    # what the owner's machine showed: scripts missing, xtool outdated (the default pin), SDK done
    assert states("apple-dev", "xtool", "sdk") == {"apple-dev": "missing", "xtool": "broken", "sdk": "done"}


@pytest.mark.parametrize("flags", [{"step": "apple-dev"}, {"all": True}], ids=["step", "all"])
def test_fresh_download_rebuilds_xtool_then_the_sdk(fresh, capsys, flags):
    assert installers.install({**flags, "yes": True}) == 0
    evs = events(capsys)
    assert steps_run(evs) == ["apple-dev", "xtool", "sdk"]
    assert evs[-1]["data"]["installed"] == ["apple-dev", "xtool", "sdk"]
    assert git("-C", str(fresh.apple_dev), "rev-parse", "HEAD", cwd=fresh.root) == fresh.ad_new
    rebuilt_in_order(fresh)
    assert states("apple-dev", "xtool", "sdk") == {"apple-dev": "done", "xtool": "done", "sdk": "done"}


@pytest.mark.parametrize("flags", [{"step": "apple-dev"}, {"all": True}], ids=["step", "all"])
def test_update_rebuilds_xtool_then_the_sdk(updated, capsys, flags):
    assert installers.install({**flags, "yes": True}) == 0
    evs = events(capsys)
    assert steps_run(evs) == ["apple-dev", "xtool", "sdk"]
    assert evs[-1]["data"]["installed"] == ["apple-dev", "xtool", "sdk"]
    rebuilt_in_order(updated)


# ================================================================ follow_ups() on its own

class FakeCtx:
    def __init__(self, rows: dict[str, tuple[str, list[str]]]):
        self.by_id = {k: {"id": k, "state": st, "depends": deps} for k, (st, deps) in rows.items()}

    def state(self, sid: str) -> str:
        return self.by_id.get(sid, {}).get("state", "missing")


ROWS = {"packages": ("done", []), "swift": ("done", []), "apple-dev": ("done", []),
        "xtool": ("broken", ["packages", "swift", "apple-dev"]), "xcode": ("done", []),
        "pymobiledevice3": ("done", []), "sdk": ("broken", ["xtool", "xcode", "pymobiledevice3"])}


def test_follow_ups_take_what_they_depend_on_in_order():
    ctx = FakeCtx(ROWS)
    assert installers.follow_ups(ctx, "apple-dev", {"xtool": "broken", "sdk": "done"}) == ["xtool", "sdk"]
    assert installers.follow_ups(ctx, "apple-dev", {"xtool": "done", "sdk": "done"}) == ["xtool", "sdk"]
    assert installers.follow_ups(ctx, "apple-dev", {"xtool": "done", "sdk": "broken"}) == ["xtool"]
    assert installers.follow_ups(ctx, "apple-dev", {"xtool": "broken", "sdk": "broken"}) == []   # nothing changed


def test_follow_ups_never_add_root_steps_or_page_steps():
    rows = {**ROWS, "swift": ("broken", []), "pymobiledevice3": ("missing", []), "xcode": ("missing", [])}
    got = installers.follow_ups(FakeCtx(rows), "apple-dev", {"xtool": "broken", "sdk": "done"})
    assert got == ["xtool", "pymobiledevice3", "sdk"]          # not swift (root work), not xcode (by hand)


def test_a_follow_up_whose_dependency_stays_broken_is_logged_not_run(fresh, capsys, monkeypatch):
    monkeypatch.setitem(installers.INSTALLERS, "xtool", lambda c, s: None)   # "works" but leaves xtool outdated
    assert installers.install({"step": "apple-dev", "yes": True}) == 0
    evs = events(capsys)
    assert steps_run(evs) == ["apple-dev", "xtool"]
    assert any("needs “xtool” first" in e.get("text", "") for e in evs if e.get("event") == "log")
    assert not fresh.calls("install-toolchain.sh")
