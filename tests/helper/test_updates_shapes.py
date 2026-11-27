"""Demo mode keeps the real reply shapes: `updates status/check/apply/channel` from helper/iosstudio/demo/updates_demo.py
against the real group run in process on the fake upstream (tests/support/updates_fakes.py) and test_updates_check's
fake machine. Compared: every key at every level, each value's JSON type, the events a stream sends with their keys,
the fixed per-component values (name, checksum source, restarts), and the states one ipsw story walks through
(ipsw 3.1.752 is the untested latest in both worlds).

DEMO_GAPS is empty: any difference fails.

    .venv/bin/pytest -q tests/helper/test_updates_shapes.py
"""
from __future__ import annotations

import json

import pytest
from test_updates_check import machine, up  # noqa: F401 - the fake machine and upstream fixtures

from iosstudio.demo import updates_demo
from iosstudio.features.updates import commands

# Known demo differences, as the diff functions word them. Empty: the demo has the real shapes. A difference
# updates_demo.py can't avoid goes here with a reason; anything else fails the test.
DEMO_GAPS: set[str] = set()


# ---------------------------------------------------------------- shapes

def _type(v) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, (int, float)):
        return "number"
    return {str: "string", list: "array", dict: "object"}[type(v)]


def flatten(v, path: str = "$", out: dict | None = None) -> dict[str, set[str]]:
    """{path: JSON types seen there}; list items share one path (`[]`), so every component is merged."""
    out = {} if out is None else out
    out.setdefault(path, set()).add(_type(v))
    if isinstance(v, dict):
        for k, x in v.items():
            flatten(x, f"{path}.{k}", out)
    elif isinstance(v, list):
        for x in v:
            flatten(x, f"{path}[]", out)
    return out


def _parent_has_room(path: str, types: dict[str, set[str]]) -> bool:
    """Would this side have shown `path`? Only when its parent is an object (or a non-empty list) there."""
    parent = path[:-2] if path.endswith("[]") else path.rsplit(".", 1)[0]
    want = "array" if path.endswith("[]") else "object"
    return want in types.get(parent, set()) and not (want == "array" and parent + "[]" not in types)


def shape_diffs(label: str, real, demo) -> set[str]:
    r, d = flatten(real), flatten(demo)
    out = set()
    for p in sorted(r.keys() | d.keys()):
        if p not in d:
            if _parent_has_room(p, d):
                out.add(f"{label}: demo lacks {p} (real: {'/'.join(sorted(r[p]))})")
        elif p not in r:
            if _parent_has_room(p, r):
                out.add(f"{label}: demo has extra {p}")
        else:
            rt_, dt = r[p] - {"null"}, d[p] - {"null"}
            if rt_ and dt and not rt_ & dt:
                out.add(f"{label}: {p} is {'/'.join(sorted(dt))} in demo, {'/'.join(sorted(rt_))} in real")
    return out


def _lines(capsys) -> list[dict]:
    return [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.strip()]


def event_keys(evs: list[dict]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for e in evs:
        if "event" in e and e["event"] != "done":
            out.setdefault(e["event"], set()).update(e)
    return out


def event_diffs(label: str, real: list[dict], demo: list[dict], optional: set[str] = frozenset()) -> set[str]:
    """Event kinds and their keys. `optional`: kinds only one side may send (Setup's log lines, say)."""
    r, d = event_keys(real), event_keys(demo)
    out = set()
    for kind in sorted(r.keys() | d.keys()):
        if kind not in d or kind not in r:
            if kind not in optional:
                out.add(f"{label}: only {'real' if kind in r else 'demo'} sends {kind} events")
            continue
        # A done step may add "elapsed" (Setup's runner); everything else must match.
        miss, extra = r[kind] - d[kind] - {"elapsed"}, d[kind] - r[kind]
        if miss:
            out.add(f"{label}: demo {kind} events lack {sorted(miss)}")
        if extra:
            out.add(f"{label}: demo {kind} events have extra {sorted(extra)}")
    return out


FIXED = ("name", "checksum", "restarts")


def fixed_diffs(label: str, real_status: dict, demo_status: dict) -> set[str]:
    r = {c["id"]: c for c in real_status["components"]}
    d = {c["id"]: c for c in demo_status["components"]}
    out = {f"{label}: component ids differ: real {sorted(r)}, demo {sorted(d)}"} if r.keys() != d.keys() else set()
    for cid in sorted(r.keys() & d.keys()):
        for k in FIXED:
            if r[cid].get(k) != d[cid].get(k):
                out.add(f"{label}: {cid}.{k} is {d[cid].get(k)!r} in demo, {r[cid].get(k)!r} in real")
    return out


# ---------------------------------------------------------------- the two worlds, same story

STORY = [("status", {}), ("check", {"force": True}), ("channel", {"set": "latest"}),
         ("apply", {"component": "ipsw", "dry_run": True}), ("apply", {"component": "ipsw", "yes": True}),
         ("status", {}), ("channel", {"set": "tested"}), ("status", {})]


def _run_real(action: str, flags: dict) -> int:
    return {"status": commands.cmd_status, "check": commands.cmd_check, "apply": commands.cmd_apply,
            "channel": commands.cmd_channel}[action](dict(flags))


@pytest.fixture
def worlds(up, capsys, monkeypatch):  # noqa: F811 - pytest fixture from test_updates_check
    """[(action, real lines, demo lines)] for STORY: the real group first, then the demo in fresh state."""
    monkeypatch.setattr(updates_demo.demo, "FRESH", False)
    real = []
    for action, flags in STORY:
        assert _run_real(action, flags) == 0, (action, capsys.readouterr().out[-2000:])
        real.append(_lines(capsys))
    updates_demo._state_path().unlink(missing_ok=True)
    demo = []
    for action, flags in STORY:
        assert updates_demo.run(action, dict(flags)) == 0, action
        demo.append(_lines(capsys))
    return [(a, r, d) for (a, _f), r, d in zip(STORY, real, demo)]


def _data(lines: list[dict]) -> dict:
    return lines[-1]["data"]


def _status(action: str, data: dict) -> dict:
    """The status a reply carries: status and check answer with it, apply has it under "status"."""
    return data["status"] if action == "apply" else data


def all_diffs(worlds) -> set[str]:
    out: set[str] = set()
    for (action, flags), (_a, real, demo) in zip(STORY, worlds):
        label = action + (" --dry-run" if flags.get("dry_run") else "")
        out |= shape_diffs(label, _data(real), _data(demo))
        if action in ("check", "apply"):
            out |= event_diffs(label, real, demo, optional={"log"})
            assert real[-1]["event"] == demo[-1]["event"] == "done"
        if action != "channel":
            out |= fixed_diffs(label, _status(action, _data(real)), _status(action, _data(demo)))
    return out


def ipsw_story(worlds) -> list:
    """What the UI shows for ipsw at each step: its state and untested flag, and whether a channel switch lists it."""
    out = []
    for action, lines in worlds:
        d = _data(lines)
        if action == "channel":
            out.append((action, "ipsw" in d["reinstall"]))
            continue
        c = next(c for c in _status(action, d)["components"] if c["id"] == "ipsw")
        out.append((action, c["state"], c["untested"], c["target"]))
    return out


def test_demo_replies_have_the_real_shapes(worlds):
    diffs = all_diffs(worlds)
    assert diffs <= DEMO_GAPS, "\n".join(sorted(diffs - DEMO_GAPS))


def test_demo_toolchain_info_shows_what_a_demo_update_installed(tmp_path, monkeypatch, capsys):
    """After a demo update the Toolchain page's Installed list (demo `toolchain info`) and the Setup rows show the new
    version with the channel's pin, as real mode's effective_pins() does on Latest; back on Tested the pin is tested."""
    from iosstudio.demo import demo
    from iosstudio.features.setup import pins
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(demo, "FRESH", False)
    items = [{"id": i, "name": i, "version": "0", "detail": "", "status": "ok", "pinned": "0"}
             for i in ("xtool", "pymobiledevice3", "rcodesign", "ipsw", "apple-dev")]
    rows = [{"id": i, "state": "done", "detail": ""} for i in ("apple-dev", "xtool", "ipsw")]
    monkeypatch.setattr(demo, "_real", lambda *a: {"items": [dict(x) for x in items], "sdks": {},
                                                   "steps": [dict(r) for r in rows], "groups": []})

    def info() -> dict:
        return {i["id"]: i for i in demo._toolchain_info({})["items"]}
    assert (info()["ipsw"]["version"], info()["ipsw"]["pinned"]) == (pins.IPSW_VERSION, pins.IPSW_VERSION)
    assert updates_demo.run("apply", {"component": "ipsw", "channel": "latest", "yes": True}) == 0
    capsys.readouterr()
    ipsw = info()["ipsw"]
    assert (ipsw["version"], ipsw["pinned"], ipsw["status"]) == ("3.1.752", "3.1.752", "ok")
    assert info()["rcodesign"]["version"] == "0"                      # not updated: this machine's own
    setup = {s["id"]: s for s in demo._setup_check({})["steps"]}
    assert setup["ipsw"]["detail"] == "ipsw 3.1.752"
    assert setup["apple-dev"]["detail"].startswith(f"At the tested version {pins.APPLE_DEV_COMMIT[:7]}")
    assert updates_demo.run("channel", {"set": "tested"}) == 0
    capsys.readouterr()
    assert (info()["ipsw"]["version"], info()["ipsw"]["pinned"]) == ("3.1.752", pins.IPSW_VERSION)


def test_the_demo_tells_the_same_ipsw_story(worlds):
    # From the check on: before its first check real mode reads ipsw's version as unknown (it comes from the
    # check's --version probe), while the demo starts "checked 3 h ago".
    real = ipsw_story([(a, r) for a, r, _ in worlds[1:]])
    demo = ipsw_story([(a, d) for a, _, d in worlds[1:]])
    assert demo == real
