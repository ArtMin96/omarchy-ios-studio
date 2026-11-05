"""Crash report (.ips) helpers for iosstudio-deviced. Pure stdlib, no pymobiledevice3."""
from __future__ import annotations

import json
import posixpath
import re
from datetime import datetime


def _zone(tz: str | None):
    """ZoneInfo for the phone's IANA time zone name, or None when unknown/invalid."""
    if not tz:
        return None
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(tz)
    except Exception:  # noqa: BLE001 - unknown zone, missing tzdata
        return None

# <process>[.<kind>_resource]-YYYY-MM-DD-HHMMSS[.mmm].ips
_NAME_RE = re.compile(r"^(?P<proc>.+?)-(?P<date>\d{4}-\d{2}-\d{2}-\d{6})(?:\.\d+)?\.ips(?:\.\w+)?$")


def classify(path: str, tz: str | None = None) -> dict | None:
    """CrashItem for one device path, or None when it isn't a report file.

    The date in the file name is the phone's local wall-clock time. With tz (the phone's IANA zone, e.g.
    lockdown's TimeZone) it becomes an aware ISO date and an exact epoch ts. Without it the date stays
    naive (no offset; dateLocal=True says it is the phone's local time) and ts guesses the computer's
    zone, so it is off by the difference when the two zones differ."""
    file = posixpath.basename(path)
    if ".ips" not in file:
        return None
    m = _NAME_RE.match(file)
    proc, ts, date, naive = file, 0.0, "", False
    if m:
        proc = m.group("proc")
        try:
            dt = datetime.strptime(m.group("date"), "%Y-%m-%d-%H%M%S")
        except ValueError:
            dt = None
        if dt is not None:
            zone = _zone(tz)
            if zone is not None:
                dt = dt.replace(tzinfo=zone)
                date, ts = dt.isoformat(), dt.timestamp()
            else:
                # Best guess: the phone is usually in the computer's zone.
                date, ts, naive = dt.isoformat(), dt.timestamp(), True
    kind = "crash"
    if proc.startswith("JetsamEvent"):
        kind, proc = "jetsam", ""
    elif re.search(r"\.(cpu|wakeups|diskwrites|memory)_resource$", proc):
        kind = "resource"
        proc = proc.rsplit(".", 1)[0]
    elif proc.startswith("ExcUserFault_"):
        kind, proc = "fault", proc.removeprefix("ExcUserFault_")
    elif proc.startswith(("stacks", "Stackshot", "spin")) or proc.endswith("_hang"):
        kind = "hang"
    elif proc in ("WiFiLQMMetrics", "SiriSearchFeedback", "ResetCounter") or proc.startswith("Analytics"):
        kind = "other"
    return {
        "name": path,
        "file": file,
        "process": proc,
        "date": date,
        "ts": ts,
        "dateLocal": naive,
        "kind": kind,
        "retired": path.startswith("/Retired/"),
    }


def header_time(stamp) -> tuple[str, float] | None:
    """(ISO date with offset, epoch ts) for an .ips header timestamp such as
    '2026-11-01 14:22:33.00 +0100' (the phone's local time plus its UTC offset), or None."""
    if not isinstance(stamp, str):
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S.%f %z", "%Y-%m-%d %H:%M:%S %z"):
        try:
            dt = datetime.strptime(stamp.strip(), fmt)
        except ValueError:
            continue
        return dt.isoformat(), dt.timestamp()
    return None


def _frame_text(frame: dict, images: list) -> str:
    idx = frame.get("imageIndex")
    image = ""
    if isinstance(idx, int) and 0 <= idx < len(images):
        image = images[idx].get("name") or posixpath.basename(images[idx].get("path", "") or "") or "?"
    sym = frame.get("symbol")
    if sym:
        off = frame.get("symbolLocation")
        return f"{image} {sym}" + (f" + {off}" if off else "")
    return f"{image} + 0x{int(frame.get('imageOffset', 0)):x}"


def parse_ips(text: str) -> dict:
    """Summary of a .ips report: JSON header line + JSON body (iOS 15+ format)."""
    out = {"process": None, "bundleId": None, "appVersion": None, "osVersion": None,
           "date": None, "exceptionType": None, "signal": None, "terminationReason": None,
           "crashedThread": None, "frames": [], "bugType": None, "incident": None}
    head, _, body = text.partition("\n")
    try:
        h = json.loads(head)
    except json.JSONDecodeError:
        h = {}
    out.update(process=h.get("app_name") or h.get("name"), bundleId=h.get("bundleID"),
               osVersion=h.get("os_version"), date=h.get("timestamp"),
               bugType=h.get("bug_type"), incident=h.get("incident_id"))
    when = header_time(h.get("timestamp"))
    if when:
        out["dateIso"], out["ts"] = when
    if h.get("app_version"):
        out["appVersion"] = h["app_version"] + (f" ({h['build_version']})" if h.get("build_version") else "")
    try:
        b = json.loads(body)
    except json.JSONDecodeError:
        return out
    if not isinstance(b, dict):
        return out
    out["process"] = out["process"] or b.get("procName")
    exc = b.get("exception") or {}
    out["exceptionType"] = exc.get("type")
    out["signal"] = exc.get("signal")
    term = b.get("termination") or {}
    if term:
        reason = term.get("indicator") or ""
        details = term.get("reasons") or term.get("details") or []
        if isinstance(details, list) and details:
            reason = (reason + ": " if reason else "") + "; ".join(map(str, details))[:600]
        out["terminationReason"] = reason or (f"{term.get('namespace','')} {term.get('code','')}".strip() or None)
    if b.get("asi"):
        asi = "; ".join(" ".join(v) if isinstance(v, list) else str(v) for v in b["asi"].values())
        out["terminationReason"] = out["terminationReason"] or asi[:600]
    images = b.get("usedImages") or []
    threads = b.get("threads") or []
    ft = b.get("faultingThread")
    if isinstance(ft, int):
        out["crashedThread"] = ft
    else:
        for i, t in enumerate(threads):
            if t.get("triggered"):
                out["crashedThread"] = i
                break
    ci = out["crashedThread"]
    if isinstance(ci, int) and 0 <= ci < len(threads):
        frames = threads[ci].get("frames") or []
        out["frames"] = [f"{n} {_frame_text(f, images)}" for n, f in enumerate(frames[:15])]
    return out
