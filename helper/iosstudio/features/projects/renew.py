"""Auto-renew: re-sign free 7-day apps before they expire, from a systemd user timer (`projects renew-due`)."""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from iosstudio.core import appdirs, fileio, output, subproc
from iosstudio.core.output import HelperError
from iosstudio.features.projects import build, store

UNIT = "ios-studio-renew"
SYSTEMCTL_TIMEOUT = 20
EXIT_TEMP, EXIT_USER = 75, 78


def _notify(title: str, body: str = "", urgency: str = "normal") -> None:
    """A desktop notification (Omarchy's sender, else notify-send); silently nothing without either."""
    exe = subproc.which("omarchy-notification-send")
    argv = [exe, "--app-name", "iOS Studio", "-u", urgency, title] if exe else \
        (["notify-send", "-a", "iOS Studio", "-u", urgency, title] if shutil.which("notify-send") else None)
    if not argv:
        return
    if body:
        argv.append(body)
    try:
        subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        pass


def due_projects(days: float) -> list[dict]:
    """Every auto-renew project with its days left and whether it is due (under `days` left, iPhone known)."""
    state = store.load_state()
    renewals = store.load_renewals()
    due = []
    for sp, meta in state["meta"].items():
        if not meta.get("autoRenew"):
            continue
        rec = renewals.get(sp) or {}
        udid = rec.get("udid") or meta.get("udid")
        exp = rec.get("expires")
        left = (exp - time.time()) / 86400 if exp else None
        due.append({"path": sp, "udid": udid, "daysLeft": round(left, 2) if left is not None else None,
                    "due": bool(udid) and (left is None or left < days), "exists": Path(sp).is_dir()})
    return due


def _deviced_once(cmd: str, args: dict, timeout: float = 300) -> dict | None:
    """Ask the device daemon for one request without the app (background renewals over Wi-Fi).

    Contract request to the device slice: `bin/iosstudio-deviced --once <cmd> '<json args>'` prints one
    reply object {"ok": ..., "data"|"error": ...} and exits. Returns None if unsupported.
    """
    exe = appdirs.REPO / "bin/iosstudio-deviced"
    if not exe.exists():
        return None
    try:
        r = subprocess.run([str(exe), "--once", cmd, json.dumps(args)], capture_output=True, text=True,
                           timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in reversed(r.stdout.strip().splitlines()):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "ok" in obj and "event" not in obj:
            return obj
    return None


def renew_due(days: float, dry: bool) -> int:
    """Background job (systemd timer): renew every auto-renew project whose signing has < N days left.

    Exit 0 ok/nothing due, 75 try later (phone unreachable, Apple busy), 78 needs the user, 1 failed.
    Streams the same JSON lines as a build so `journalctl --user -u ios-studio-renew` is readable.
    """
    items = due_projects(days)
    due = [i for i in items if i["due"] and i["exists"]]
    output.event("renew-plan", days=days, projects=items, dryRun=dry)
    if dry or not due:
        return output.done(True, {"renewed": [], "due": due, "dryRun": dry})
    codes: list[int] = []
    renewed, failed = [], []
    for it in due:
        name = Path(it["path"]).name
        try:
            _rc, data, err = build.do_build({"path": it["path"], "udid": it["udid"]}, "renew", interactive=False)
        except HelperError as exc:
            data, err = None, exc
        if err is None and data and data.get("needsInstall"):
            reply = _deviced_once("app.install", {"udid": it["udid"], "path": data["ipa"]})
            if reply and reply.get("ok"):
                store.record_signing(it["path"], it["udid"], data.get("bundleId"), "wifi", data.get("profile"))
            else:
                err = HelperError("device-unreachable", "Your iPhone wasn't reachable to install the renewed app.")
        if err is None:
            renewed.append(it["path"])
            continue
        failed.append({"path": it["path"], "error": err.to_json()})
        code = EXIT_USER if err.code in ("needs-certificate", "not-signed-in", "signing-failed") else (
            EXIT_TEMP if err.code in ("apple-unreachable", "device-unreachable", "device-not-found", "no-device", "busy")
            else 1)
        codes.append(code)
        if code == EXIT_USER:
            _notify(f"{name} needs you to renew", err.message + " Open iOS Studio › Projects.", "critical")
    if renewed:
        _notify("Apps renewed", ", ".join(Path(p).name for p in renewed) + " will keep opening for another week.", "low")
    output.done(not failed, {"renewed": renewed, "failed": failed},
                None if not failed else HelperError(failed[0]["error"]["code"], failed[0]["error"]["message"]))
    if not failed:
        return 0
    return EXIT_USER if EXIT_USER in codes else (EXIT_TEMP if EXIT_TEMP in codes else 1)


def unit_paths() -> tuple[Path, Path]:
    systemd = appdirs.config_home() / "systemd/user"
    return systemd / f"{UNIT}.service", systemd / f"{UNIT}.timer"


def unit_texts() -> tuple[str, str]:
    helper = appdirs.REPO / "bin/iosstudio-helper"
    esc = str(helper).replace("%", "%%")
    service = f"""# GENERATED by iOS Studio (projects autorenew --enable); removed by --disable. Don't edit.
[Unit]
Description=iOS Studio: renew free 7-day app signing on your iPhone
StartLimitIntervalSec=0

[Service]
Type=oneshot
ExecStart="{esc}" projects renew-due --background
Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin
Environment=XDG_CONFIG_HOME=%h/.config
Nice=10
TimeoutStartSec=45min
# 75: phone unreachable or Apple busy, 1: build failed -> retry in 3 hours. 78: needs you.
Restart=on-failure
RestartSec=3h
RestartPreventExitStatus=78
"""
    timer = """# GENERATED by iOS Studio (projects autorenew --enable); removed by --disable. Don't edit.
[Unit]
Description=iOS Studio: daily check of app signing expiry

[Timer]
OnCalendar=*-*-* 10:00:00
Persistent=true
RandomizedDelaySec=30min

[Install]
WantedBy=timers.target
"""
    return service, timer


SYSTEMCTL_TIMEOUT = 20


def systemctl(*args: str) -> subprocess.CompletedProcess:
    """`systemctl --user ...`, bounded: a wedged user manager must not hang the helper."""
    try:
        return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True,
                              timeout=SYSTEMCTL_TIMEOUT, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise HelperError("systemd-timeout", f"systemctl --user {args[0]} didn't answer in {SYSTEMCTL_TIMEOUT} s. "
                          "Your user systemd may be busy; try again in a moment.")


def timer_status() -> dict:
    """The timer's unit files, enabled state, next/last run and last result, plus the auto-renew projects."""
    svc, tmr = unit_paths()
    out: dict[str, Any] = {"installed": svc.exists() and tmr.exists(), "unit": f"{UNIT}.timer"}
    if shutil.which("systemctl"):
        try:
            r = systemctl("is-enabled", f"{UNIT}.timer")
            out["enabled"] = r.stdout.strip() == "enabled"
            r = systemctl("show", f"{UNIT}.timer", "-p", "NextElapseUSecRealtime", "-p", "LastTriggerUSec")
            for ln in r.stdout.splitlines():
                k, _, v = ln.partition("=")
                out["next" if k == "NextElapseUSecRealtime" else "last"] = v or None
            r = systemctl("show", f"{UNIT}.service", "-p", "Result", "-p", "ExecMainStatus")
            for ln in (r.stdout.splitlines() if out["installed"] else []):
                k, _, v = ln.partition("=")
                out["lastResult" if k == "Result" else "lastExit"] = v
        except HelperError:
            pass    # status is best-effort: show what the unit files say
    out["projects"] = due_projects(float(appdirs.settings().get("autoRenewDays") or 2))
    return out


def enable_timer(dry: bool) -> dict:
    """Write the service + timer units and enable the timer (dry: only the plan). Returns timer_status()."""
    svc, tmr = unit_paths()
    service, timer = unit_texts()
    actions = [f"write {svc}", f"write {tmr}", "systemctl --user daemon-reload",
               f"systemctl --user enable --now {UNIT}.timer"]
    if dry:
        return {"dryRun": True, "actions": actions, "service": service, "timer": timer}
    fileio.atomic_write(svc, service)
    fileio.atomic_write(tmr, timer)
    systemctl("daemon-reload")
    r = systemctl("enable", "--now", f"{UNIT}.timer")
    if r.returncode != 0:
        raise HelperError("systemd", "Couldn't turn on automatic renewal.", detail=r.stderr)
    return timer_status()


def disable_timer(dry: bool) -> dict:
    """Disable the timer and remove its unit files (dry: only the plan). Returns timer_status()."""
    svc, tmr = unit_paths()
    actions = [f"systemctl --user disable --now {UNIT}.timer", f"remove {svc}", f"remove {tmr}",
               "systemctl --user daemon-reload"]
    if dry:
        return {"dryRun": True, "actions": actions}
    systemctl("disable", "--now", f"{UNIT}.timer")
    for f in (svc, tmr):
        try:
            f.unlink()
        except FileNotFoundError:
            pass
    systemctl("daemon-reload")
    return timer_status()
