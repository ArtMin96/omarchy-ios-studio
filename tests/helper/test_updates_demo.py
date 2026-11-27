"""Demo mode of the `updates` group (helper/iosstudio/demo/updates_demo.py), in process: the contract's reply shapes,
the channel switch both ways, apply (one tool, all, back to tested, dry run, rollback, cancel, the iPhone-connection
question), the interrupted-update story and putting it back, check (skipped while fresh, forced) and the usage errors.
States and error codes are the real model's (features/updates/model.py, apply.py)."""
from __future__ import annotations

import dataclasses
import json
import time

import pytest

from iosstudio.core import subproc
from iosstudio.demo import demo, updates_demo

STATUS_KEYS = {"channel", "checkedAt", "nextCheckAt", "due", "checking", "error", "count", "components", "notices",
               "interrupted"}
COMPONENT_KEYS = {"id", "name", "installed", "tested", "latest", "target", "state", "untested", "notesUrl", "latestAt",
                  "applicable", "blockedReason", "checksum", "restarts"}
IDS = ["apple-dev", "xtool", "pymobiledevice3", "ipsw", "rcodesign"]


@pytest.fixture(autouse=True)
def private_state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("IOSSTUDIO_DEMO_DELAY", "0")
    monkeypatch.delenv("IOSSTUDIO_DEMO_UPDATES", raising=False)
    monkeypatch.setattr(demo, "FRESH", False)


def lines(capsys) -> list[dict]:
    return [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.strip()]


def call(capsys, action: str, **flags) -> dict:
    """A one-shot command's {"ok", "data"|"error"}."""
    updates_demo.run(action, flags)
    return lines(capsys)[-1]


def stream(capsys, action: str, **flags) -> tuple[list[dict], dict]:
    updates_demo.run(action, flags)
    out = lines(capsys)
    assert out[-1]["event"] == "done"
    return out[:-1], out[-1]


def comp(st: dict, cid: str) -> dict:
    return next(c for c in st["components"] if c["id"] == cid)


def status(capsys) -> dict:
    out = call(capsys, "status")
    assert out["ok"], out
    return out["data"]


def test_status_has_the_contract_shape(capsys):
    st = status(capsys)
    assert set(st) == STATUS_KEYS
    assert st["channel"] == "tested" and st["checking"] is False and st["error"] is None
    assert st["interrupted"] is None
    assert [c["id"] for c in st["components"]] == IDS
    for c in st["components"]:
        assert set(c) == COMPONENT_KEYS | ({"latestTag"} if c["id"] == "apple-dev" else set())
        assert isinstance(c["latestAt"], str) and c["latestAt"].endswith("Z")     # ISO, as GitHub/PyPI give it
        assert c["state"] == "current" and c["untested"] is False and c["applicable"] is True
        assert c["checksum"]["source"] in ("checksums.txt", "SHA256SUMS", "git-commit", "pypi")
    assert comp(st, "ipsw")["latest"] == "3.1.752" and comp(st, "ipsw")["tested"] != "3.1.752"
    assert comp(st, "pymobiledevice3")["restarts"] == ["iphone-connection"]
    assert st["count"] == 0
    assert [n["id"] for n in st["notices"]] == ["xtool-upstream"]
    assert st["notices"][0]["level"] == "info"
    # checked 3 h ago, so the next check is tomorrow
    assert not st["due"] and st["nextCheckAt"] == st["checkedAt"] + 86400
    assert 2.9 * 3600 < time.time() - st["checkedAt"] < 3.1 * 3600


def test_the_router_sends_the_group_here(capsys):
    demo.run("updates", "status", {})
    assert lines(capsys)[-1]["data"]["channel"] == "tested"


def test_latest_offers_the_untested_ipsw_and_back_to_tested_lists_it(capsys):
    out = call(capsys, "channel", set="latest")
    assert out == {"ok": True, "data": {"channel": "latest", "reinstall": ["ipsw"]}}   # what apply --all installs
    st = status(capsys)
    ipsw = comp(st, "ipsw")
    assert (st["channel"], st["count"]) == ("latest", 1)
    assert (ipsw["state"], ipsw["target"], ipsw["untested"]) == ("update", "3.1.752", True)
    assert comp(st, "rcodesign")["state"] == "current"

    events, done = stream(capsys, "apply", component="ipsw")
    assert done["ok"] and done["data"]["applied"] == ["ipsw"] and done["data"]["rolledBack"] == []
    assert [e["state"] for e in events if e["event"] == "step"] == ["running", "done"]
    assert {"event": "verified", "id": "ipsw", "source": "checksums.txt"} in events
    assert all(0 <= e["fraction"] <= 1 and e["step"] == "ipsw" for e in events if e["event"] == "progress")
    after = comp(done["data"]["status"], "ipsw")
    assert (after["installed"], after["state"]) == ("3.1.752", "newer-than-tested")   # at the target, untested

    # on latest the installed 3.1.752 is newer than tested; back on tested (without reinstalling) it's an update
    # back to the tested pin, listed for reinstall, as the real model reports it
    assert comp(status(capsys), "ipsw")["state"] == "newer-than-tested"
    assert call(capsys, "channel", set="tested")["data"] == {"channel": "tested", "reinstall": ["ipsw"]}
    st = status(capsys)
    ipsw = comp(st, "ipsw")
    assert (ipsw["state"], ipsw["installed"], ipsw["target"], st["count"]) == ("update", "3.1.752", ipsw["tested"], 1)


def test_apply_all_with_channel_tested_reinstalls_the_tested_pin_and_saves_the_channel(capsys):
    call(capsys, "channel", set="latest")
    stream(capsys, "apply", all=True)
    _events, done = stream(capsys, "apply", all=True, channel="tested")
    assert done["ok"] and done["data"]["applied"] == ["ipsw"]
    st = done["data"]["status"]
    assert st["channel"] == "tested"
    assert comp(st, "ipsw")["installed"] == comp(st, "ipsw")["tested"]
    assert status(capsys)["channel"] == "tested"


def test_apply_with_nothing_to_do_and_dry_run(capsys):
    events, done = stream(capsys, "apply", all=True)
    assert events == [] and done["ok"] and done["data"]["applied"] == []
    assert done["data"]["message"] == "Nothing to update."
    _e, done = stream(capsys, "apply", all=True, dry_run=True)
    assert (done["data"]["dryRun"], done["data"]["would"]) == (True, [])
    call(capsys, "channel", set="latest")
    events, done = stream(capsys, "apply", all=True, dry_run=True)
    assert done["data"]["dryRun"] is True
    # like the real dry run, each step says what it would do
    assert [(e["event"], e.get("state")) for e in events] == [("step", "running"), ("progress", None), ("step", "done")]
    assert events[1]["step"] == "ipsw" and events[1]["detail"].startswith("Would download")
    # the real dry run's items: {id, installed, target}
    assert done["data"]["would"] == [{"id": "ipsw", "installed": comp(done["data"]["status"], "ipsw")["tested"],
                                      "target": "3.1.752"}]
    assert comp(status(capsys), "ipsw")["state"] == "update"     # nothing changed


def test_a_failed_check_rolls_back_and_keeps_the_channel(capsys, monkeypatch):
    monkeypatch.setenv("IOSSTUDIO_DEMO_UPDATES", "rollback")
    _events, done = stream(capsys, "apply", all=True, channel="latest")
    assert not done["ok"] and done["error"]["code"] == "verify-failed"
    assert done["error"]["message"].endswith("The previous version is back.")
    assert done["error"]["fix"]["page"] == "toolchain"
    assert done["data"]["applied"] == [] and done["data"]["rolledBack"] == ["ipsw"]
    assert status(capsys)["channel"] == "tested"      # --channel is saved only once the apply succeeds


def test_cancel_rolls_back_the_tool_in_progress(capsys, monkeypatch):
    call(capsys, "channel", set="latest")

    def cancelled(_runner, _seconds):
        raise subproc.Cancelled()
    monkeypatch.setattr(updates_demo, "_pause", cancelled)
    events, done = stream(capsys, "apply", component="ipsw")
    assert {"event": "rollback", "id": "ipsw", "reason": "Cancelled"} in events
    assert not done["ok"] and done["error"]["code"] == "cancelled" and done["data"]["rolledBack"] == ["ipsw"]
    assert comp(status(capsys), "ipsw")["state"] == "update"


@pytest.mark.parametrize(("answer", "ok"), [("yes", True), ("no", False)])
def test_pymobiledevice3_asks_once_unless_yes(capsys, monkeypatch, answer, ok):
    newer = dataclasses.replace(updates_demo.BY_ID["pymobiledevice3"], latest="11.50.0")
    tools = tuple(newer if t.id == newer.id else t for t in updates_demo.TOOLS)
    monkeypatch.setattr(updates_demo, "TOOLS", tools)
    monkeypatch.setattr(updates_demo, "BY_ID", {t.id: t for t in tools})
    asked = []
    monkeypatch.setattr(updates_demo.output, "prompt", lambda kind, message, options: asked.append(message) or answer)
    call(capsys, "channel", set="latest")
    _events, done = stream(capsys, "apply", component="pymobiledevice3")
    assert len(asked) == 1 and "iPhone connection" in asked[0]
    assert done["ok"] is ok
    if not ok:
        assert done["error"]["code"] == "cancelled"
    asked.clear()
    stream(capsys, "apply", all=True, yes=True)
    assert asked == []


def test_check_is_skipped_while_fresh_and_runs_when_forced_or_due(capsys):
    events, done = stream(capsys, "check")
    assert events == [] and done["ok"]
    before = done["data"]["checkedAt"]
    events, done = stream(capsys, "check", force=True)
    assert [e["id"] for e in events if e["event"] == "component"] == IDS
    assert {"event": "component", "id": "ipsw", "latest": "3.1.752"} in events
    assert done["data"]["checkedAt"] > before and not done["data"]["due"]
    # a day later it's due, and a plain check runs
    s = updates_demo._load()
    s["checkedAt"] = time.time() - 25 * 3600
    updates_demo._save(s)
    assert status(capsys)["due"] is True
    events, _done = stream(capsys, "check")
    assert sum(1 for e in events if e["event"] == "component") == len(IDS)


def test_usage_errors(capsys):
    _e, done = stream(capsys, "apply")
    assert done["error"]["code"] == "usage"
    _e, done = stream(capsys, "apply", component="nope")
    assert done["error"]["code"] == "usage" and "nope" in done["error"]["message"]
    _e, done = stream(capsys, "apply", all=True, channel="beta")
    assert done["error"]["code"] == "usage"
    assert call(capsys, "channel", set="beta")["error"]["code"] == "usage"
    assert call(capsys, "frobnicate")["error"]["code"] == "usage"


def test_a_fresh_machine_has_nothing_to_update(capsys, monkeypatch):
    monkeypatch.setattr(demo, "FRESH", True)
    call(capsys, "channel", set="latest")
    st = status(capsys)
    assert st["count"] == 0 and {c["state"] for c in st["components"]} == {"unknown"}


def test_an_interrupted_update_is_reported_until_it_is_put_back(capsys, monkeypatch):
    monkeypatch.setenv("IOSSTUDIO_DEMO_UPDATES", "interrupted")
    st = status(capsys)
    assert st["interrupted"]["component"] == "ipsw" and 0 < time.time() - st["interrupted"]["since"] < 3600
    # a dry run changes nothing, and with no component only says it would put it back
    _e, done = stream(capsys, "apply", dry_run=True)
    assert done["ok"] and done["data"]["rolledBack"] == []
    assert status(capsys)["interrupted"]["component"] == "ipsw"
    # `updates apply` with neither --component nor --all only puts it back
    events, done = stream(capsys, "apply")
    assert {"event": "rollback", "id": "ipsw", "reason": "interrupted"} in events
    assert done["ok"] and done["data"]["applied"] == [] and done["data"]["rolledBack"] == ["ipsw"]
    assert done["data"]["status"]["interrupted"] is None
    assert status(capsys)["interrupted"] is None                 # stays gone, even with the variant still set
    _e, done = stream(capsys, "apply")
    assert done["error"]["code"] == "usage"


def test_any_update_puts_an_interrupted_one_back_first(capsys, monkeypatch):
    monkeypatch.setenv("IOSSTUDIO_DEMO_UPDATES", "interrupted")
    call(capsys, "channel", set="latest")
    events, done = stream(capsys, "apply", component="ipsw")
    assert [e["id"] for e in events if e["event"] == "rollback"] == ["ipsw"]
    assert done["ok"] and done["data"]["applied"] == ["ipsw"] and done["data"]["rolledBack"] == ["ipsw"]
    assert done["data"]["status"]["interrupted"] is None
