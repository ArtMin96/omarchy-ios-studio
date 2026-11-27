"""`updates apply`: install one component's (or every) update, verify it, keep or put back the previous version.

For each component (apple-dev carries xtool with it: xtool follows the checkout's XTOOL_SHA):

  marker   state/updates-inprogress.json {pid, component, target, since, backups} written before anything changes
           and after each backup, removed once the backups are discarded or restored. A marker whose process is gone
           (SIGKILL, the app closed mid-update) is reported by `updates status` as "interrupted"; the next apply
           restores its backups first (an apply without --component/--all does only that).
  backup   setup/channel.backup(): binaries copied to updates/prev, the venv renamed to venv.prev, the old xtool build
           kept, the old checkout commit remembered.
  install  Setup's own installers (installers.run_chain) with ctx.pins = the target and keep_previous.
  check    the tool reports the target version (run_cmd with timeouts), xtool has no old user agent, and no Setup
           step that was "done" before is anything else now (build_steps(gather())).
  then     Backup.discard() (nothing big left behind) and channel.record_pin(); on a failure, a cancel ({"cancel":
           true} on stdin or SIGTERM) or any exception: Backup.restore() newest first (xtool before apple-dev).

pymobiledevice3 is refused while a debug, mirror or hot reload session uses the iPhone; otherwise it asks once (the
iPhone connection restarts), stops the device daemon before the venv moves and again once the new one is in place
(a daemon the app restarted meanwhile ran from the moved venv); the daemon starts again on demand.
"""
from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

from iosstudio.core import fileio, output, subproc
from iosstudio.core.output import HelperError, fix

from ..setup import channel as ch
from ..setup import facts
from ..setup import runtime as rt
from ..setup.runtime import confirm, log
from . import model
from .model import NAMES, NOT_INSTALLED, Row

ORDER = ["apple-dev", "xtool", "rcodesign", "pymobiledevice3", "ipsw"]
DAEMON_EXIT_WAIT = 20.0
TOOLCHAIN_FIX = fix("Open Toolchain", "toolchain")
PYMD3_CHECK = "import importlib.metadata as m, pymobiledevice3, cryptography; print(m.version('pymobiledevice3'))"


# ------------------------------------------------------------------ events

def verified(cid: str, source: str, version: str | None) -> None:
    output.event("verified", id=cid, source=source)
    log(cid, f"Checked: {NAMES[cid]} {model.short(cid, version) or ''} works ({source})".replace("  ", " "))


def rolled_back(cid: str, reason: str) -> None:
    output.event("rollback", id=cid, reason=reason)
    log(cid, f"Put the previous {NAMES.get(cid, cid)} back: {reason}")


# ------------------------------------------------------------------ the in-progress marker

def write_marker(cid: str, target: str | None, backups: list[dict]) -> None:
    if rt.DRY:
        return
    pid = os.getpid()
    old = model.read_marker() or {}
    fileio.atomic_write(model.marker_path(), json.dumps(
        {"pid": pid, "pidStart": model.proc_start(pid), "component": cid, "target": target,
         "since": old.get("since") if old.get("component") == cid else time.time(), "backups": backups}))


def clear_marker() -> None:
    try:
        model.marker_path().unlink()
    except FileNotFoundError:
        pass


def recover() -> list[str]:
    """Put back what an interrupted apply left set aside (called with the updates lock held: a marker found then
    belongs to a process that's gone). Returns the ids put back."""
    m = model.read_marker()
    if not m:
        return []
    cid = str(m.get("component") or "")
    if rt.DRY:
        log(cid or None, f"would put back the previous {NAMES.get(cid, cid)} (an earlier update was interrupted)")
        return []
    log(cid or None, f"An earlier update of {NAMES.get(cid, cid)} was interrupted: putting the previous version back")
    with _quiet_signals():
        for d in reversed(m.get("backups") or []):
            b = ch.Backup.from_json(d) if isinstance(d, dict) else None
            if b is None:
                continue
            try:
                b.restore(cid)
            except Exception as exc:  # noqa: BLE001 - keep restoring the rest
                log(cid, f"couldn't put {b.component} back: {exc}")
        clear_marker()
    rolled_back(cid, "interrupted")
    return [cid]


class _quiet_signals:
    """Ignore SIGTERM/SIGINT while files are put back (a second cancel must not cut a rollback short)."""

    def __enter__(self):
        self.old = {s: signal.signal(s, signal.SIG_IGN) for s in (signal.SIGTERM, signal.SIGINT)}

    def __exit__(self, *exc):
        for s, h in self.old.items():
            signal.signal(s, h)


# ------------------------------------------------------------------ cancel

def watch_cancel() -> None:
    """{"cancel": true} on stdin becomes a SIGTERM to ourselves: the main thread unwinds (rolling back) wherever
    it is. Started after the last prompt, so it never competes with output.prompt for stdin."""
    def run() -> None:
        try:
            for line in sys.stdin:
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if isinstance(msg, dict) and msg.get("cancel"):
                    os.kill(os.getpid(), signal.SIGTERM)
                    return
        except (OSError, ValueError):     # no usable stdin: SIGTERM still cancels
            return
    threading.Thread(target=run, daemon=True, name="cancel").start()


# ------------------------------------------------------------------ the iPhone connection (pymobiledevice3)

def _daemon(fn):
    """Run `fn(client)` against the live device daemon without starting one; None when none runs."""
    import asyncio

    from iosstudio.device.client import DeviceClient, socket_path
    if not socket_path(False).exists():
        return None

    async def go():
        dev = DeviceClient(name="toolchain update", events=[], demo=False, spawn=False, reconnect=False,
                           connect_timeout=3)
        try:
            await asyncio.wait_for(dev.connect(), 5)
            return await fn(dev)
        finally:
            await dev.close()
    try:
        return asyncio.run(go())
    except (HelperError, OSError, TimeoutError):
        return None


def phone_sessions() -> list[str]:
    """What uses the iPhone connection right now: hot reload sessions (their locks) and the daemon's held
    resources (the mirror, a recording, a debugger's port forward)."""
    found: list[str] = []
    try:
        from iosstudio.features.hotreload import state as hr
        if hr.live_keys():
            found.append("hot reload")
    except Exception:  # noqa: BLE001 - no runtime folder: no session
        pass

    async def features(dev):
        return await dev.request("daemon.features", {}, timeout=5)
    res = _daemon(features) or {}
    for r in res.get("resources") or []:
        key = str((r or {}).get("key") or "")
        name = "screen mirroring" if key in ("mirror", "record") else "debugging" if key.startswith("debug:") else None
        if name and name not in found:
            found.append(name)
    return found


def stop_daemon(step_id: str) -> None:
    """Stop the device daemon (for every client) and wait for its process to exit; it starts again on demand."""
    async def stop(dev):
        pid = (dev.hello or {}).get("pid")
        try:
            await dev.request("daemon.shutdown", {"all": True}, timeout=10)
        except HelperError:
            pass                                 # it closed the connection on its way out
        return pid
    pid = _daemon(stop)
    if pid is None:
        return
    end = time.monotonic() + DAEMON_EXIT_WAIT
    while time.monotonic() < end:
        try:
            os.kill(int(pid), 0)
        except (ProcessLookupError, ValueError, TypeError):
            break
        except PermissionError:
            break
        time.sleep(0.1)
    log(step_id, "Stopped the iPhone connection; it starts again when something needs the iPhone")


# ------------------------------------------------------------------ verification

def _tool_version(argv: list, timeout: float = 15) -> str | None:
    r = subproc.run_cmd(argv, timeout=timeout, check=False)
    return facts.first_version(r.stdout) if r.returncode == 0 else None


def verify_xtool() -> None:
    ad = facts.find_apple_dev()
    want = facts.apple_dev_pins(ad)["XTOOL_SHA"]
    link = ch.LOCAL_BIN / "xtool"
    real = Path(os.path.realpath(link))
    built = facts.xtool_built_from(real) if link.exists() else None
    if not built or not want.startswith(built):
        raise HelperError("verify-failed", f"xtool isn't the build the new iOS build scripts ask for ({want[:7]}).",
                          TOOLCHAIN_FIX)
    if facts.binary_has_old_ua(real) is not False:
        raise HelperError("verify-failed", "The new xtool can't sign in to Apple (it still has the old sign-in code).",
                          TOOLCHAIN_FIX)
    if subproc.run_cmd([link, "--version"], timeout=15, check=False).returncode != 0:
        raise HelperError("verify-failed", "The new xtool doesn't start.", TOOLCHAIN_FIX)
    verified("xtool", "git-commit", want)


def verify(row: Row, ran: list[str], xtool_was: str) -> None:
    """The installed tool is the target (raises HelperError "verify-failed")."""
    cid, want = row.id, row.target or ""
    if cid == "apple-dev":
        ad = facts.find_apple_dev()
        rc, out, _ = rt.probe(["git", "-C", ad, "rev-parse", "HEAD"]) if ad else (1, "", "")
        if rc != 0 or out.strip() != want:
            raise HelperError("verify-failed", f"The iOS build scripts aren't at {want[:7]} after the update.",
                              TOOLCHAIN_FIX)
        verified(cid, "git-commit", want)
        if "xtool" in ran or xtool_was == "done":
            verify_xtool()
        return
    if cid == "xtool":
        verify_xtool()
        return
    if cid == "pymobiledevice3":
        r = subproc.run_cmd([ch.VENV_DIR / "bin/python", "-I", "-c", PYMD3_CHECK], timeout=30, check=False)
        got = (r.stdout.strip().splitlines() or [""])[-1] if r.returncode == 0 else None
    else:
        got = _tool_version([ch.LOCAL_BIN / cid, *model.PROBED[cid]])
    if got != want:
        raise HelperError("verify-failed", f"The new {NAMES[cid]} reports {('version ' + got) if got else 'no version'}, "
                          f"not {want}.", TOOLCHAIN_FIX)
    verified(cid, model.CHECKSUM[cid], want)


def regressions(before: dict[str, str], ctx) -> list[str]:
    """Setup steps that were done before the update and aren't now."""
    return [sid for sid, st in before.items() if st == "done" and ctx.state(sid) != "done"]


# ------------------------------------------------------------------ one component

def apply_one(row: Row, channel: str) -> None:
    """Back up, install, verify, then discard; on any failure or cancel restore and re-raise."""
    from ..setup.installers import run_chain
    from ..setup.steps import Ctx
    cid = row.id
    comps = ["apple-dev", "xtool"] if cid == "apple-dev" else [cid]
    pins = ch.effective_pins(channel, {cid: row.pin})
    ctx = Ctx({"yes": True}, pins=pins, keep_previous=True)
    before = {s["id"]: s["state"] for s in ctx.steps}
    planned = [ch.venv_backup_plan().to_json()] if cid == "pymobiledevice3" else []
    write_marker(cid, row.target, planned)    # before the venv moves: a kill mid-rename still finds venv.prev
    backups: list[ch.Backup] = []
    try:
        if cid == "pymobiledevice3" and not rt.DRY:
            stop_daemon(cid)
        for c in comps:
            backups.append(ch.backup(c, cid))
            write_marker(cid, row.target, [b.to_json() for b in backups])
        ran = run_chain(ctx, cid)
        if rt.DRY:
            return
        if cid == "pymobiledevice3":
            stop_daemon(cid)                   # one the app restarted meanwhile runs from the moved venv
        verify(row, ran, before.get("xtool", ""))
        ctx.refresh()
        bad = regressions(before, ctx)
        if bad:
            names = ", ".join(ctx.by_id.get(s, {}).get("title", s) for s in bad)
            raise HelperError("verify-failed", f"After the update, Setup reports a problem with: {names}.",
                              TOOLCHAIN_FIX)
    except BaseException as exc:
        reason = ("cancelled" if isinstance(exc, (SystemExit, KeyboardInterrupt)) else
                  exc.message if isinstance(exc, HelperError) else "something unexpected went wrong")
        with _quiet_signals():
            for b in reversed(backups):
                try:
                    b.restore(cid)
                except Exception as rexc:  # noqa: BLE001 - keep restoring the rest
                    log(cid, f"couldn't put the previous {b.component} back: {rexc}")
            clear_marker()
        if backups and not rt.DRY:
            rolled_back(cid, reason)
        raise
    for b in backups:
        b.discard(cid)
    clear_marker()
    if not same_as_tested(row):
        _record(row)


def same_as_tested(row: Row) -> bool:
    return model.same(row.id, row.target, row.tested)


def _record(row: Row) -> None:
    p = row.pin
    if row.id == "apple-dev":
        ch.record_pin("apple-dev", commit=p["commit"])
        ch.record_pin("xtool", commit=facts.apple_dev_pins(facts.find_apple_dev())["XTOOL_SHA"])
    elif row.id == "xtool":
        ch.record_pin("xtool", commit=p.get("commit"))
    else:
        ch.record_pin(row.id, version=p.get("version"), sha256=p.get("sha256"))


def refresh_probes() -> None:
    with model.cache_lock():
        cache = model.load_cache()
        model.refresh_probes(cache["probes"])
        model.save_cache(cache)


# ------------------------------------------------------------------ the command

def plan(rows: list[Row], component: str | None, all_: bool) -> list[Row]:
    by = {r.id: r for r in rows}
    if component:
        if component not in by:
            raise HelperError("usage", f"There's no component '{component}'. Use one of: {', '.join(ORDER)}.")
        r = by[component]
        if component == "xtool" and by["apple-dev"].state == "update":
            return [by["apple-dev"]]             # xtool follows the scripts: updating them rebuilds it
        if r.state == "update" or (r.state == "unknown" and r.blocked is None):
            return [r]
        if r.blocked and r.state in ("blocked", "unknown"):
            f = fix("Open Setup", "setup", step=component) if r.blocked == NOT_INSTALLED else \
                fix("Get Xcode", "setup", step="xcode") if r.blocked == model.SDK_BLOCK else TOOLCHAIN_FIX
            raise HelperError("blocked", r.blocked, f)
        return []
    units = [by[c] for c in ORDER if by[c].state == "update"]
    if any(u.id == "apple-dev" for u in units):
        units = [u for u in units if u.id != "xtool"]
    return units


def apply(flags: dict) -> int:
    channel = str(flags.get("channel") or ch.current_channel())
    if channel not in ch.CHANNELS:
        raise HelperError("usage", f"Unknown update channel '{channel}'. Use tested or latest.")
    component = flags.get("component") if isinstance(flags.get("component"), str) else None
    applied: list[str] = []
    rolled: list[str] = []

    def finish(ok_: bool, error: HelperError | None = None, **extra) -> int:
        data = {"applied": applied, "rolledBack": rolled, "status": model.status(channel), **extra}
        return output.done(True, data) if ok_ else output.done(False, data, error)

    if rt.DRY:
        log(None, "Dry run: nothing will be changed; every command is printed instead.")
    with ch.updates_lock(blocking=False):
        rolled += recover()
        if not component and not flags.get("all"):
            if rolled or rt.DRY:
                return finish(True)
            raise HelperError("usage", "What should be updated? Use --component ID or --all.")
        ch.clear_backups(take_lock=False)
        refresh_probes()                          # Setup may have replaced a binary since the last check
        units = plan(model.rows(channel, model.load_cache()), component, bool(flags.get("all")))
        would = [{"id": u.id, "installed": model.short(u.id, u.installed), "target": model.short(u.id, u.target)}
                 for u in units]
        if not units:
            if flags.get("channel") and not rt.DRY:
                ch.set_channel(channel)
            return finish(True, message="Nothing to update.", **({"dryRun": True, "would": []} if rt.DRY else {}))
        if any(u.id == "pymobiledevice3" for u in units):
            busy = phone_sessions()
            if busy:
                raise HelperError("iphone-busy", f"The iPhone connection is in use ({', '.join(busy)}). Stop that "
                                  "first: updating pymobiledevice3 restarts the connection.",
                                  fix("Open iPhone", "device"))
            u = next(u for u in units if u.id == "pymobiledevice3")
            confirm(f"Update pymobiledevice3 to {u.target}? The iPhone connection restarts: anything using the "
                    "iPhone pauses for a few seconds.", "Update", flags)
        if not rt.DRY:
            watch_cancel()
        for u in units:
            try:
                apply_one(u, channel)
            except HelperError as exc:
                rolled.append(u.id)
                retry = not exc.fix or exc.fix.get("label") == "Try again"   # Setup's retry installs the channel's pin
                return finish(False, HelperError(exc.code, f"{exc.message} The previous version is back.",
                                                 TOOLCHAIN_FIX if retry else exc.fix, exc.detail))
            except (SystemExit, KeyboardInterrupt):
                rolled.append(u.id)
                return finish(False, HelperError("cancelled", "Cancelled. The previous version is back."))
            except Exception:
                import traceback
                rolled.append(u.id)
                return finish(False, HelperError("internal", "Something unexpected went wrong. The previous version "
                                                 "is back.", TOOLCHAIN_FIX, traceback.format_exc()))
            if not rt.DRY:
                applied.append(u.id)
        if rt.DRY:
            return finish(True, dryRun=True, would=would)
        refresh_probes()
        if flags.get("channel"):
            ch.set_channel(channel)
        return finish(True)
