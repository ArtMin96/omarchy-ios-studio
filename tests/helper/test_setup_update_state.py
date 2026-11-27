"""An older omarchy-apple-dev checkout of our own is an update, not a fault.

The row stays state "broken" (every install path repairs it as before) with update=True and a calm detail;
summarize() keeps it in essentialMissing/autoInstallable but lists it under "updates", not "broken", and words an
update-only Setup as "1 update ready" (level info); `toolchain info` reports status "update" with the version.
Runs on tests/support/setup_upgrade.py's old machine (real git checkouts, fake tools).
"""
from __future__ import annotations

import pytest

from iosstudio.features.setup import steps, toolchain
from support.setup_upgrade import facts_for, old_machine, relocate


def row(id_: str, state: str, **kw) -> dict:
    return {"id": id_, "state": state, "canAutoInstall": state in ("missing", "broken"), "essential": True, **kw}


@pytest.fixture
def machine(tmp_path, monkeypatch):
    m = old_machine(tmp_path)
    relocate(monkeypatch, m)
    return m


def apple_dev(F: dict) -> dict:
    return next(r for r in steps.build_steps(F)[0] if r["id"] == "apple-dev")


def test_old_checkout_is_an_update(machine):
    ad = apple_dev(facts_for(machine))
    assert (ad["state"], ad["update"], ad["action"]["label"], ad["canAutoInstall"]) == ("broken", True, "Update", True)
    assert ad["detail"] == (f"You have {machine.ad_old[:7]}; iOS Studio is tested with {machine.ad_new[:7]}. "
                            "Updating downloads it and rebuilds only what changed.")
    assert ad["detail"] == steps.apple_dev_update_detail(machine.ad_old)


def test_local_changes_are_a_sentence_after_the_update_detail(machine):
    ad = apple_dev(facts_for(machine, ad_dirty=(0, " M install-toolchain.sh\n", "")))
    assert ad["detail"].endswith("rebuilds only what changed. It has local changes.")


def test_current_checkout_is_not_an_update(machine):
    ad = apple_dev(facts_for(machine, ad_head=(0, machine.ad_new + "\n", "")))
    assert ad["state"] == "done" and ad["update"] is False


def test_summary_of_an_update_only_setup():
    s = steps.summarize([row("swift", "done"), row("apple-dev", "broken", update=True)])
    assert (s["level"], s["title"], s["badge"]) == ("info", "1 update ready", "1")
    assert s["ready"] is False and s["essentialMissing"] == ["apple-dev"] and s["autoInstallable"] == ["apple-dev"]
    assert s["updates"] == ["apple-dev"] and s["broken"] == []


def test_summary_with_an_update_and_other_work():
    s = steps.summarize([row("apple-dev", "broken", update=True), row("sdk", "missing")])
    assert (s["level"], s["title"]) == ("warn", "2 steps left")
    s = steps.summarize([row("apple-dev", "broken", update=True), row("xtool", "broken")])
    assert (s["level"], s["title"], s["broken"], s["updates"]) == ("error", "Something needs repair", ["xtool"], ["apple-dev"])
    s = steps.summarize([row("apple-dev", "done", update=True)])     # the flag alone means nothing once it's done
    assert s["ready"] and s["updates"] == []


def test_toolchain_info_shows_the_version_and_an_update(machine, monkeypatch):
    monkeypatch.setattr(toolchain, "gather", lambda: facts_for(machine))
    item = next(i for i in toolchain.info()["items"] if i["id"] == "apple-dev")
    assert (item["status"], item["version"], item["pinned"]) == ("update", machine.ad_old[:7], machine.ad_new[:7])
