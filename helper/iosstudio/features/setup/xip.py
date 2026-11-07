"""Apple's Xcode .xip: find it in Downloads, check it (cheap magic/size, then the version inside), watch for it."""
from __future__ import annotations

import re
import subprocess
import threading
import time
from pathlib import Path

from iosstudio.core import output
from iosstudio.core.output import HelperError, event, fix, ok

from .facts import load_state, save_state
from .pins import DIRECT_XIP, DOWNLOAD_PAGE, DOWNLOADS, XIP_MIN_BYTES
from .runtime import fmt_bytes, fmt_dur, progress


def xip_quick(path: Path) -> dict:
    """Cheap checks only (magic + size). Version is read separately (about a minute)."""
    info: dict = {"path": str(path), "name": path.name}
    try:
        stt = path.stat()
        info["size"] = stt.st_size
        info["mtime"] = stt.st_mtime
        with open(path, "rb") as f:
            magic = f.read(4)
    except OSError as exc:
        info.update(valid=False, problem="unreadable", message=f"Can't read that file ({exc.strerror}).")
        return info
    if magic != b"xar!" or info["size"] < XIP_MIN_BYTES:
        small = info["size"] < 5_000_000
        info.update(valid=False, problem="stub" if small else "not-xip",
                    message=("This file is only " + fmt_bytes(info["size"]) + ": it's Apple's sign-in page, not Xcode. "
                             "Sign in on the download page and download it again.") if small else
                    "This file isn't an Xcode .xip archive (or the download is incomplete). Download it again.")
        return info
    info["valid"] = True
    cached = (load_state().get("xipInfo") or {})
    if cached.get("path") == str(path) and cached.get("size") == info["size"] and cached.get("mtime") == info["mtime"]:
        info["version"] = cached.get("version")
        info["build"] = cached.get("build")
    else:
        m = re.search(r"Xcode[_ -]?(\d+(?:\.\d+)*)", path.name)
        if m:
            info["nameVersion"] = m.group(1)
    return info


def xip_read_version(path: Path, step_id: str | None = "xcode") -> tuple[str, str]:
    from iosstudio.features import xip_version  # vendored, stdlib only
    result: dict = {}

    def work():
        try:
            _data, v, b = xip_version.read_version(str(path))
            result["v"], result["b"] = v, b
        except Exception as exc:  # noqa: BLE001 - any parse failure means "not a usable xip"
            result["err"] = str(exc)

    th = threading.Thread(target=work, daemon=True)
    start = time.time()
    th.start()
    while th.is_alive():
        th.join(1.5)
        el = time.time() - start
        progress(step_id, min(0.95, el / 110), f"Checking the Xcode version inside the file · {fmt_dur(el)}, usually about 2 min",
              max(0, 110 - el))
    if "err" in result:
        raise HelperError("bad-xip", "This file isn't a complete Xcode download. Download it again.",
                          fix("Open download page", "setup", step="xcode"), detail=result["err"])
    st = path.stat()
    save_state({"xip": str(path), "xipInfo": {"path": str(path), "size": st.st_size, "mtime": st.st_mtime,
                                              "version": result["v"], "build": result["b"]}})
    return result["v"], result["b"]


def find_xips() -> list[Path]:
    seen, out = set(), []
    st = load_state().get("xip")
    cands = ([Path(st)] if st else []) + sorted(DOWNLOADS.glob("Xcode*.xip"), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in cands:
        if p.is_file() and str(p) not in seen:
            seen.add(str(p))
            out.append(p)
    return out


def best_xip(major: str) -> dict | None:
    """The most promising local .xip for the needed Xcode major: valid, matching version first."""
    infos = [xip_quick(p) for p in find_xips()]
    if not infos:
        return None

    def score(i):
        ver = i.get("version") or i.get("nameVersion") or ""
        return (bool(i.get("valid")), ver.split(".")[0] == major, i.get("mtime", 0))
    return sorted(infos, key=score, reverse=True)[0]


# ------------------------------------------------------------------ setup xip --open / --use / --watch

def open_download_page(rec: str, flags: dict) -> int:
    url = DIRECT_XIP.format(v=rec) if "." in rec and not flags.get("page") else DOWNLOAD_PAGE.format(v=rec)
    if flags.get("dry_run"):
        return ok({"url": url, "opened": False})
    subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return ok({"url": url, "pageUrl": DOWNLOAD_PAGE.format(v=rec), "opened": True, "xcode": rec})


def validate(path: Path, major: str, rec: str) -> dict:
    """xip_quick plus the version inside (read once, then cached); valid only for the needed Xcode major."""
    info = xip_quick(path)
    if not info.get("valid"):
        return info
    if not info.get("version"):
        v, b = xip_read_version(path, "xcode")
        info["version"], info["build"] = v, b
    info["matches"] = info["version"].split(".")[0] == major
    if not info["matches"]:
        info["valid"] = False
        info["problem"] = "wrong-version"
        info["message"] = f"This is Xcode {info['version']}, but your Swift needs Xcode {rec}. Download Xcode {rec} instead."
    return info


def use(p: Path, major: str, rec: str) -> int:
    """Validate a .xip the user picked and remember it."""
    output.step("xcode", "Checking Xcode", "running")
    info = validate(p, major, rec)
    if not info.get("valid"):
        raise HelperError(info.get("problem", "bad-xip"), info.get("message", "That file can't be used."),
                          fix("Open download page", "setup", step="xcode"))
    save_state({"xip": str(p)})
    output.step("xcode", "Checking Xcode", "done")
    return output.done(True, {"xip": info})


def watch(major: str, rec: str, expected: int, interval: float) -> int:
    """Stream download progress of an Xcode .xip into ~/Downloads; validate each finished one until one fits."""
    event("watching", dir=str(DOWNLOADS), needed=rec, expectedSize=expected)
    checked: dict[str, tuple] = {}
    sizes: dict[str, tuple[int, float]] = {}
    last_emit = 0.0
    # A file already there counts right away.
    while True:
        now = time.time()
        partials = []
        for pat in ("Xcode*.xip.part", "Xcode*.xip.crdownload", "*.crdownload", "Xcode*.xip.download", "Xcode*.xip.tmp"):
            partials += [p for p in DOWNLOADS.glob(pat) if p.is_file()]
        partials = sorted(set(partials), key=lambda p: p.stat().st_mtime, reverse=True)
        if partials:
            p = partials[0]
            size = p.stat().st_size
            prev = sizes.get(str(p))
            rate = (size - prev[0]) / max(0.1, now - prev[1]) if prev else 0
            sizes[str(p)] = (size, now)
            if now - last_emit > 1:
                last_emit = now
                eta = (expected - size) / rate if rate > 0 and expected > size else None
                event("download", file=p.name, size=size, expected=expected, fraction=min(0.99, size / expected) if expected else None,
                      rate=rate, eta=None if eta is None else int(eta))
                progress("xcode", min(0.99, size / expected) if expected else None,
                         f"Downloading {p.name.split('.xip')[0]}: {fmt_bytes(size)} of about {fmt_bytes(expected)}", eta)
        for p in find_xips():
            st = p.stat()
            key = (st.st_size, st.st_mtime)
            if checked.get(str(p)) == key:
                continue
            # wait until the size is stable (browser finished writing)
            prev = sizes.get("final:" + str(p))
            sizes["final:" + str(p)] = (st.st_size, now)
            if not prev or prev[0] != st.st_size or now - st.st_mtime < 2:
                continue
            checked[str(p)] = key
            event("found", path=str(p), file=p.name, size=st.st_size)
            output.step("xcode", "Checking Xcode", "running")
            try:
                info = validate(p, major, rec)
            except HelperError as exc:
                info = {"path": str(p), "name": p.name, "valid": False, "problem": "bad-xip", "message": exc.message}
            if info.get("valid"):
                save_state({"xip": str(p)})
                output.step("xcode", "Checking Xcode", "done")
                return output.done(True, {"xip": info})
            output.step("xcode", "Checking Xcode", "failed", error={"code": info.get("problem"), "message": info.get("message")})
            event("rejected", xip=info, message=info.get("message"))
        time.sleep(interval)
