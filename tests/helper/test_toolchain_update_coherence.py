"""An older omarchy-apple-dev checkout is one update, worded the same everywhere: the Setup row (update: true, compared
with the update channel's pin), the Toolchain page's Installed item (status "update", pinWord) and Toolchain › Updates
(an "update" row for the same checkout). The demo variant IOSSTUDIO_DEMO_APPLE_DEV=outdated tells the same story until
a demo update installs the scripts.
"""
from __future__ import annotations

import pytest

from iosstudio.demo import demo, updates_demo
from iosstudio.features.setup import pins, steps

OLD = demo.OLD_APPLE_DEV[:7]


def test_the_detail_names_the_channels_pin():
    assert steps.apple_dev_update_detail("a" * 40, "b" * 40) == (
        "You have aaaaaaa; iOS Studio is tested with bbbbbbb. Updating downloads it and rebuilds only what changed.")
    assert steps.apple_dev_update_detail("a" * 40, "b" * 40, "latest") == (
        "You have aaaaaaa; the Latest channel has bbbbbbb (untested). Updating downloads it and rebuilds only what "
        "changed.")
    assert pins.APPLE_DEV_COMMIT[:7] in steps.apple_dev_update_detail("a" * 40)


@pytest.fixture
def outdated(tmp_path, monkeypatch):
    """The ready demo with IOSSTUDIO_DEMO_APPLE_DEV=outdated, its state in a private folder."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("IOSSTUDIO_DEMO_APPLE_DEV", "outdated")
    monkeypatch.setattr(demo, "FRESH", False)
    items = [{"id": i, "name": i, "version": "0", "detail": "", "status": "ok", "pinned": "0"}
             for i in ("xtool", "ipsw", "apple-dev")]
    rows = [{"id": i, "state": "done", "detail": "", "essential": True, "canAutoInstall": False}
            for i in ("apple-dev", "xtool", "ipsw")]
    monkeypatch.setattr(demo, "_real", lambda *a: {"items": [dict(x) for x in items], "sdks": {},
                                                   "steps": [dict(r) for r in rows], "groups": []})


def views() -> tuple[dict, dict, dict]:
    """(Setup's apple-dev row, the Installed item, the Updates component) as the demo answers them now."""
    row = next(s for s in demo._setup_check({})["steps"] if s["id"] == "apple-dev")
    item = next(i for i in demo._toolchain_info({})["items"] if i["id"] == "apple-dev")
    comp = next(c for c in updates_demo.status()["components"] if c["id"] == "apple-dev")
    return row, item, comp


@pytest.mark.parametrize("channel", ["tested", "latest"])
def test_outdated_demo_is_one_update_in_every_view(outdated, channel, capsys):
    assert updates_demo.run("channel", {"set": channel}) == 0
    capsys.readouterr()
    row, item, comp = views()
    # the demo's latest build scripts are the tested ones, so both channels compare with the tested pin
    assert row["update"] is True and row["detail"].startswith(f"You have {OLD}; iOS Studio is tested with "
                                                              f"{pins.APPLE_DEV_COMMIT[:7]}.")
    assert (item["status"], item["version"], item["pinned"], item["pinWord"]) == (
        "update", OLD, pins.APPLE_DEV_COMMIT[:7], "tested")
    assert (comp["state"], comp["installed"], comp["untested"]) == ("update", OLD, False)
    assert updates_demo.status()["count"] == (2 if channel == "latest" else 1)   # + ipsw 3.1.752 on latest


def test_a_demo_update_of_the_scripts_ends_the_outdated_story(outdated, capsys):
    assert updates_demo.run("apply", {"component": "apple-dev", "yes": True}) == 0
    capsys.readouterr()
    row, item, comp = views()
    assert row["state"] == "done" and not row.get("update")
    assert item["status"] == "ok" and comp["state"] == "current"
