"""Demo mode of the `updates` group (IOSSTUDIO_DEMO): toolchain updates with the real reply shapes.

  updates status                          the cached state: channel, components, notices (instant)
  updates check [--force]                 JSON lines: progress, one component event per tool, done {status}
  updates apply (--component ID | --all) [--channel tested|latest] [--dry-run] [--yes]
                                          JSON lines: step, progress, verified, (rollback), done {applied, rolledBack, status}
  updates channel --set tested|latest     {channel, reinstall: [ids]}

The story: on "tested" (the default) everything is current; ipsw has a newer, untested release 3.1.752 that shows up
as an update on "latest". omarchy-apple-dev, xtool, pymobiledevice3 and rcodesign are at their tested pins on both
channels, and one info notice says upstream xtool has a newer release. States follow the real model: once 3.1.752 is
installed it is "newer-than-tested" on latest and an "update" (back to the tested pin) on tested.

IOSSTUDIO_DEMO_UPDATES=rollback makes every update fail its post-update check (verify-failed) and roll back.
IOSSTUDIO_DEMO_UPDATES=interrupted starts with an ipsw update that was interrupted (status.interrupted) until an apply
puts it back (`updates apply` with no component or --all does only that). Nothing is downloaded or installed: the
channel, the "installed" versions, the last check and the interrupted marker live in
$XDG_STATE_HOME/ios-studio/demo-updates.json. Cancel ({"cancel": true} on stdin, or SIGTERM) rolls back the tool in
progress, as the real apply does.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass

from iosstudio.core import appdirs, fileio, output, subproc
from iosstudio.core.output import HelperError
from iosstudio.demo import demo
from iosstudio.features.setup import pins

CHANNELS = ("tested", "latest")
DAY = 86400.0
APPLE_DEV_TAG = "v0.4.8"  # the release tag of the pinned build scripts (real: the newest release's tag)
STEP_PAUSE = 0.35       # seconds per pretend stage (IOSSTUDIO_DEMO_DELAY scales it)
SLICE = 0.05            # cancel is checked at least this often while pausing


@dataclass(frozen=True)
class Tool:
    id: str
    name: str
    tested: str
    latest: str
    source: str                    # checksum source
    notes: str
    restarts: tuple[str, ...] = ()


def _tools() -> tuple[Tool, ...]:
    apple_dev = "https://github.com/joshuaswarren/omarchy-apple-dev"
    return (
        Tool("apple-dev", "iOS build scripts", pins.APPLE_DEV_COMMIT, pins.APPLE_DEV_COMMIT, "git-commit",
             f"{apple_dev}/releases"),
        Tool("xtool", "xtool", pins.DEFAULT_XTOOL_SHA, pins.DEFAULT_XTOOL_SHA, "git-commit",
             f"{pins.DEFAULT_XTOOL_REPO}/commits/main"),
        Tool("pymobiledevice3", "pymobiledevice3", pins.PYMD3_VERSION, pins.PYMD3_VERSION, "pypi",
             "https://pypi.org/project/pymobiledevice3/#history", ("iphone-connection",)),
        Tool("ipsw", "ipsw", pins.IPSW_VERSION, "3.1.752", "checksums.txt",
             "https://github.com/blacktop/ipsw/releases/tag/v3.1.752"),
        Tool("rcodesign", "rcodesign", pins.RCODESIGN_VERSION, pins.RCODESIGN_VERSION, "SHA256SUMS",
             "https://github.com/indygreg/apple-platform-rs/releases"),
    )


TOOLS = _tools()
BY_ID = {t.id: t for t in TOOLS}
NOTICES = [{"id": "xtool-upstream", "level": "info", "fix": None,
            "message": "xtool 1.25.0 is out upstream. iOS Studio uses the xtool that omarchy-apple-dev pins, "
                       "so it arrives with an omarchy-apple-dev update."}]


# ---------------------------------------------------------------- demo state

def _state_path():
    return appdirs.state_dir() / "demo-updates.json"


def _variant() -> str:
    return os.environ.get("IOSSTUDIO_DEMO_UPDATES", "").strip().lower()


def _load() -> dict:
    s = fileio.load_json(_state_path(), {}) or {}
    if "interrupted" not in s:                      # the marker of a killed apply (only in the "interrupted" story)
        s["interrupted"] = ({"component": "ipsw", "since": time.time() - 20 * 60}
                            if _variant() == "interrupted" else None)
    if s.get("channel") not in CHANNELS:
        s["channel"] = "tested"
    if not isinstance(s.get("installed"), dict):
        s["installed"] = {}
    if not isinstance(s.get("checkedAt"), (int, float)):
        s["checkedAt"] = time.time() - 3 * 3600      # "checked 3 h ago"
    return s


def _save(s: dict) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with fileio.lock(path.with_suffix(".lock")):
        data = {"channel": s["channel"], "installed": s["installed"], "checkedAt": s["checkedAt"],
                "interrupted": s.get("interrupted")}
        fileio.atomic_write(path, json.dumps(data, indent=2) + "\n")


def _installed(s: dict, t: Tool) -> str | None:
    if demo.FRESH:                                  # a new machine has none of the tools yet
        return None
    if t.id == "apple-dev" and demo.apple_dev_outdated():   # IOSSTUDIO_DEMO_APPLE_DEV=outdated: the older checkout
        return demo.OLD_APPLE_DEV[:7]
    return str(s["installed"].get(t.id) or t.tested)


# ---------------------------------------------------------------- status

def _iso(ts: float) -> str:
    """A release time as GitHub and PyPI give it (the real latestAt)."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _progress(step: str, fraction: float, detail: str) -> None:
    """Setup's progress event, which names its step (setup/runtime.progress)."""
    output.event("progress", fraction=fraction, detail=detail, eta=None, step=step)


def _component(t: Tool, installed: str | None, channel: str) -> dict:
    target = t.tested if channel == "tested" else t.latest
    if installed is None:
        state = "unknown"
    elif installed == target:                       # as the real model: current, or on latest newer than tested
        state = "current" if target == t.tested else "newer-than-tested"
    else:
        state = "update"
    return {"id": t.id, "name": t.name, "installed": installed, "tested": t.tested, "latest": t.latest,
            "target": target, "state": state, "untested": target != t.tested, "notesUrl": t.notes,
            "latestAt": _iso(time.time() - (2 if t.latest != t.tested else 9) * DAY),
            "applicable": True, "blockedReason": None, "checksum": {"source": t.source}, "restarts": list(t.restarts),
            **({"latestTag": APPLE_DEV_TAG} if t.id == "apple-dev" else {})}


def status(s: dict | None = None, channel: str | None = None) -> dict:
    s = s or _load()
    ch = channel or s["channel"]
    comps = [_component(t, _installed(s, t), ch) for t in TOOLS]
    next_at = s["checkedAt"] + DAY
    return {"channel": ch, "checkedAt": s["checkedAt"], "nextCheckAt": next_at, "due": time.time() >= next_at,
            "checking": False, "error": None, "count": sum(1 for c in comps if c["state"] == "update"),
            "components": comps, "notices": list(NOTICES), "interrupted": s.get("interrupted")}


# ---------------------------------------------------------------- streams

def _pause(runner: subproc.Runner, seconds: float) -> None:
    end = time.monotonic() + subproc.demo_delay(seconds)
    while True:
        runner.poll()                               # Cancelled on {"cancel": true}
        left = end - time.monotonic()
        if left <= 0:
            return
        time.sleep(min(SLICE, left))


def check(flags: dict) -> int:
    s = _load()
    if not flags.get("force") and time.time() < s["checkedAt"] + DAY:
        return output.done(True, status(s))
    with subproc.Runner(watch_stdin=True) as runner:
        try:
            for i, t in enumerate(TOOLS):
                output.progress(i / len(TOOLS), f"Checking {t.name}")
                _pause(runner, STEP_PAUSE / 2)
                output.event("component", id=t.id, latest=t.latest)
        except subproc.Cancelled:
            return output.done(False, status(s), HelperError("cancelled", "Check cancelled."))
    s["checkedAt"] = time.time()
    _save(s)
    return output.done(True, status(s))


def _channel_flag(flags: dict, key: str) -> str | None:
    v = flags.get(key)
    if v in (None, False):
        return None
    if v not in CHANNELS:
        raise HelperError("usage", "The update channel is either tested or latest.")
    return str(v)


def _targets(flags: dict, s: dict, channel: str) -> list[dict]:
    comps = status(s, channel)["components"]
    want = flags.get("component")
    if want and want is not True:
        if want not in BY_ID:
            raise HelperError("usage", f"There's no component '{want}'. Use one of: {', '.join(BY_ID)}.")
        comps = [c for c in comps if c["id"] == want]
    # on tested, a newer untested install is an "update" back to the tested pin, as in the real model
    return [c for c in comps if c["state"] == "update" and c["applicable"]]


def _recover(s: dict) -> list[str]:
    """Put back what an interrupted update set aside (the real apply does this first, under its lock)."""
    m = s.get("interrupted")
    if not m:
        return []
    cid = str(m.get("component"))
    name = BY_ID[cid].name if cid in BY_ID else cid
    output.log(f"An earlier update of {name} was interrupted: putting the previous version back")
    output.event("rollback", id=cid, reason="interrupted")
    s["interrupted"] = None
    _save(s)
    return [cid]


def apply(flags: dict) -> int:
    s = _load()
    dry = bool(flags.get("dry_run"))
    try:
        channel = _channel_flag(flags, "channel")
    except HelperError as exc:
        return output.done(False, error=exc)
    rolled: list[str] = [] if dry else _recover(s)
    if not flags.get("component") and not flags.get("all"):
        if rolled or dry:                          # only put back what an interrupted update set aside
            return output.done(True, {"applied": [], "rolledBack": rolled, "status": status(s)})
        return output.done(False, error=HelperError("usage", "What should be updated? Use --component ID or --all."))
    try:
        todo = _targets(flags, s, channel or s["channel"])
    except HelperError as exc:
        return output.done(False, error=exc)
    if not todo:
        if channel and not dry:
            s["channel"] = channel
            _save(s)
        return output.done(True, {"applied": [], "rolledBack": rolled, "status": status(s),
                                  "message": "Nothing to update.", **({"dryRun": True, "would": []} if dry else {})})
    if dry:                                        # like the real dry run: each step says what it would do
        for c in todo:
            title = f"Updating {c['name']}"
            output.step(c["id"], title, "running")
            _progress(c["id"], 0.0, f"Would download and check {c['name']} {c['target']}")
            output.step(c["id"], title, "done")
        would = [{"id": c["id"], "installed": c["installed"], "target": c["target"]} for c in todo]
        return output.done(True, {"applied": [], "rolledBack": [], "status": status(s), "dryRun": True,
                                  "would": would})
    if any(c["restarts"] for c in todo) and not flags.get("yes"):
        try:
            answer = output.prompt("confirm", "Updating pymobiledevice3 restarts the iPhone connection for a moment. "
                                   "A debug, mirror or hot reload session would stop.",
                                   [{"value": "yes", "label": "Update"}, {"value": "no", "label": "Not now"}])
        except HelperError as exc:
            return output.done(False, {"applied": [], "rolledBack": [], "status": status(s)}, exc)
        if answer != "yes":
            return output.done(False, {"applied": [], "rolledBack": [], "status": status(s)},
                               HelperError("cancelled", "Update cancelled."))
    fail = _variant() == "rollback"
    applied: list[str] = []
    with subproc.Runner(watch_stdin=True) as runner:
        for c in todo:
            title = f"Updating {c['name']}"
            output.step(c["id"], title, "running")
            try:
                _progress(c["id"], 0.1, f"Downloading {c['name']} {c['target']}")
                _pause(runner, STEP_PAUSE)
                _progress(c["id"], 0.45, "Checking the download")
                _pause(runner, STEP_PAUSE / 2)
                output.event("verified", id=c["id"], source=c["checksum"]["source"])
                _progress(c["id"], 0.7, f"Installing {c['name']} {c['target']}")
                _pause(runner, STEP_PAUSE)
                _progress(c["id"], 0.9, f"Checking that {c['name']} works")
                _pause(runner, STEP_PAUSE / 2)
            except subproc.Cancelled:
                output.event("rollback", id=c["id"], reason="Cancelled")
                output.step(c["id"], title, "failed")
                rolled.append(c["id"])
                _save(s)
                return output.done(False, {"applied": applied, "rolledBack": rolled, "status": status(s)},
                                   HelperError("cancelled", "Update cancelled. The previous version is back."))
            if fail:
                reason = f"The new {c['name']} {c['target']} didn't pass its check. The previous version is back."
                output.event("rollback", id=c["id"], reason=reason)
                output.step(c["id"], title, "failed")
                rolled.append(c["id"])
                _save(s)
                return output.done(False, {"applied": applied, "rolledBack": rolled, "status": status(s)},
                                   HelperError("verify-failed", reason, output.fix("Open Toolchain", "toolchain")))
            s["installed"][c["id"]] = c["target"]
            applied.append(c["id"])
            output.step(c["id"], title, "done")
    if channel:
        s["channel"] = channel
    _save(s)
    return output.done(True, {"applied": applied, "rolledBack": rolled, "status": status(s)})


def channel(flags: dict) -> int:
    try:
        new = _channel_flag(flags, "set")
    except HelperError as exc:
        return output.fail(exc.code, exc.message)
    s = _load()
    if not new:
        return output.ok({"channel": s["channel"], "reinstall": []})
    s["channel"] = new
    _save(s)
    reinstall = [c["id"] for c in status(s)["components"] if c["state"] == "update"]
    return output.ok({"channel": new, "reinstall": reinstall})


def run(action: str, flags: dict) -> int:
    if action == "status":
        return output.ok(status())
    if action == "check":
        return check(flags)
    if action == "apply":
        return apply(flags)
    if action == "channel":
        return channel(flags)
    return output.fail("usage", f"Unknown command 'updates {action}'.")
