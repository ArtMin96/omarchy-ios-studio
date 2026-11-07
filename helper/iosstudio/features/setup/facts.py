"""Read-only facts about this machine (tools, versions, SDK, saved state), probed in parallel."""
from __future__ import annotations

import mmap
import os
import re
import shutil
import threading
from collections.abc import Callable
from pathlib import Path

from iosstudio.core import appdirs, fileio
from iosstudio.core.appdirs import LOCAL_BIN, STATE_DIR, VENV_DIR

from . import runtime as rt
from .pins import (
    APPLE_DEV_HOME,
    DEFAULT_SDK_STAMP,
    DEFAULT_XTOOL_REPO,
    DEFAULT_XTOOL_SHA,
    LEGACY_VENV,
    OMARCHY_HOOK,
    PACKAGES,
    SDK_CACHE,
    SETUP_STATE,
    XCODE_FOR_SWIFT,
    XCSPEC_PY,
    XTOOL_OLD_UA,
)
from .runtime import probe


def arch() -> str:
    return os.uname().machine


def swift_hold_hook_present() -> bool:
    """Ours, or any other pre-refresh-pacman hook that already re-holds swift-bin."""
    if OMARCHY_HOOK.exists():
        return True
    try:
        for f in OMARCHY_HOOK.parent.iterdir():
            if f.is_file() and not f.name.endswith(".sample") and os.access(f, os.X_OK):
                text = f.read_text(errors="replace")
                if "swift-bin" in text and "IgnorePkg" in text:
                    return True
    except OSError:
        pass
    return False


def is_omarchy() -> bool:
    return Path("/usr/share/omarchy/bin/omarchy-hook").exists() or shutil.which("omarchy-hook") is not None


def xtool_bin() -> Path | None:
    cand = LOCAL_BIN / "xtool"
    if cand.exists():
        return cand
    w = shutil.which("xtool")
    return Path(w) if w else None


def binary_has_old_ua(path: Path) -> bool | None:
    try:
        with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            return mm.find(XTOOL_OLD_UA) != -1
    except (OSError, ValueError):
        return None


def find_apple_dev() -> Path | None:
    s = appdirs.settings().get("appleDevDir")
    cands = ([Path(os.path.expanduser(s))] if s else []) + [APPLE_DEV_HOME]
    for c in cands:
        if (c / "install-toolchain.sh").is_file() and (c / "tools/sdk-state.sh").is_file():
            return c
    return None


def apple_dev_pins(d: Path | None) -> dict:
    pins = {"XTOOL_REPO": DEFAULT_XTOOL_REPO, "XTOOL_SHA": DEFAULT_XTOOL_SHA, "XTOOL_SDK_VERSION": DEFAULT_SDK_STAMP}
    if d:
        try:
            text = (d / "install-toolchain.sh").read_text(errors="replace")
            for k in list(pins):
                m = re.search(rf'^{k}="?([^"\n]*)"?\s*$', text, re.M)
                if m and m.group(1):
                    pins[k] = m.group(1)
        except OSError:
            pass
    return pins


def toolchain_root() -> Path | None:
    sw = Path("/usr/bin/swift")
    if not sw.exists():
        return None
    real = os.path.realpath(sw)
    m = re.match(r"^(.*)/usr/bin/[^/]+$", real)
    return Path(m.group(1)) if m else None


def xcspec_check(root: Path | None) -> bool | None:
    if root is None:
        return None
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("ios_studio_xcspec", XCSPEC_PY)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        for rel, setting, value in mod.PATCHES:
            path = root / "usr/share/pm" / rel
            try:
                text = path.read_text()
            except OSError:
                return False
            if mod.patched(text, setting, value) != text:
                return False
        return True
    except Exception:
        return None


def sdk_stamp(bundle: Path) -> str:
    try:
        return (bundle / "darwin-sdk-version.txt").read_text().strip()
    except OSError:
        return ""


def sdk_ios_versions(bundle: Path) -> list[str]:
    base = bundle / "Developer/Platforms/iPhoneOS.platform/Developer/SDKs"
    out = []
    for p in sorted(base.glob("iPhoneOS[0-9]*.sdk")):
        out.append(p.name[len("iPhoneOS"):-len(".sdk")])
    return out


def sdk_extras_missing(bundle: Path) -> list[str]:
    b = bundle / "Developer/Platforms/iPhoneOS.platform/Developer/usr/bin"
    miss = [t for t in ("actool", "xcstringstool", "ibtool", "momc") if not os.access(b / t, os.X_OK)]
    if not os.access(bundle / "OpenAppleMacrosServer", os.X_OK):
        miss.append("macro server")
    return miss


def sdk_caches(stamp: str) -> list[dict]:
    out = []
    for p in sorted(SDK_CACHE.glob("darwin-*.xtoolsdk"), key=lambda x: x.stat().st_mtime, reverse=True):
        if not p.is_dir():
            continue
        ver = p.name[len("darwin-iPhoneOS"):-len(".xtoolsdk")] if p.name.startswith("darwin-iPhoneOS") else ""
        st = sdk_stamp(p)
        out.append({"path": str(p), "ios": ver, "stamp": st, "current": st == stamp,
                    "complete": bool(sdk_ios_versions(p))})
    return out


def load_state() -> dict:
    return fileio.load_json(SETUP_STATE, {}) or {}


def save_state(upd: dict) -> None:
    if rt.DRY:
        return
    st = load_state()
    st.update(upd)
    fileio.save_json(SETUP_STATE, st)


def parse_swift_version(text: str) -> str | None:
    m = re.search(r"Swift version (\d+\.\d+(?:\.\d+)?)", text)
    return m.group(1) if m else None


def xcode_for(swift_ver: str | None) -> tuple[str, str, int] | None:
    if not swift_ver:
        return None
    mm = ".".join(swift_ver.split(".")[:2])
    return XCODE_FOR_SWIFT.get(mm)


def needed_xcode(swift_ver: str | None) -> tuple[str, str, int]:
    """(recommended version, required major, approx bytes). Defaults to the pinned swift-bin's."""
    override = appdirs.settings().get("xcodeVersion")
    rec = xcode_for(swift_ver) or XCODE_FOR_SWIFT["6.4"]
    if override:
        return (str(override), str(override).split(".")[0], rec[2])
    return rec


def _parallel(jobs: dict[str, Callable]) -> dict:
    """Run every job on its own thread; results in the jobs' order (a job's exception is re-raised)."""
    results: dict = {}
    errors: dict = {}

    def run(key: str, fn: Callable) -> None:
        try:
            results[key] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised below, in the caller's thread
            errors[key] = exc
    threads = [threading.Thread(target=run, args=(k, fn), daemon=True) for k, fn in jobs.items()]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for key in jobs:
        if key in errors:
            raise errors[key]
    return {k: results[k] for k in jobs}


def gather() -> dict:
    """Every fact `build_steps` needs, probed in parallel (read-only; about half a second)."""
    xt = xtool_bin()
    ad = find_apple_dev()
    pins = apple_dev_pins(ad)
    py_cands = [p for p in (VENV_DIR / "bin/python", LEGACY_VENV / "bin/python") if p.exists()]
    pycode = ("import importlib.metadata as m, cryptography, pymobiledevice3, sys; "
              "print(m.version('pymobiledevice3')); print(sys.version.split()[0])")
    jobs = {
        "pacman_T": lambda: probe(["pacman", "-T", *PACKAGES]),
        "swift_qi": lambda: probe(["pacman", "-Qi", "swift-bin"]),
        "swift_ver": lambda: probe(["/usr/bin/swift", "--version"], timeout=10),
        "ignore": lambda: probe(["pacman-conf", "IgnorePkg"]),
        "xtool_ver": (lambda: probe([xt, "--version"])) if xt else (lambda: (127, "", "")),
        "sdk_status": (lambda: probe([xt, "sdk", "status"])) if xt else (lambda: (127, "", "")),
        "auth": (lambda: probe([xt, "auth", "status"])) if xt else (lambda: (127, "", "")),
        "rcodesign": lambda: probe([LOCAL_BIN / "rcodesign", "--version"]) if (LOCAL_BIN / "rcodesign").exists() else probe(["rcodesign", "--version"]),
        "ipsw": lambda: probe([LOCAL_BIN / "ipsw", "version"]) if (LOCAL_BIN / "ipsw").exists() else probe(["ipsw", "version"]),
        "pymd3": lambda: [(str(p), probe([p, "-I", "-c", pycode], timeout=15)) for p in py_cands],
        "ad_head": (lambda: probe(["git", "-C", ad, "rev-parse", "HEAD"])) if ad else (lambda: (127, "", "")),
        "ad_dirty": (lambda: probe(["git", "-C", ad, "status", "--porcelain", "--untracked-files=no"])) if ad else (lambda: (127, "", "")),
        "xtool_old_ua": (lambda: binary_has_old_ua(Path(os.path.realpath(xt)))) if xt else (lambda: None),
        "xcspec": lambda: xcspec_check(toolchain_root()),
        "idevice": lambda: probe(["idevice_id", "-l"], timeout=3),
        "avahi": lambda: probe(["systemctl", "is-active", "avahi-daemon"], timeout=3),
        "py312": lambda: probe(["pacman", "-Q", "python312"]),
    }
    F = _parallel(jobs)   # plain threads: concurrent.futures costs ~45 ms of imports on every `setup check`
    F["xtool"] = xt
    F["apple_dev"] = ad
    F["pins"] = pins
    return F


def last_device_developer_mode() -> bool | None:
    for name in ("device.json", "last-device.json"):
        d = fileio.load_json(STATE_DIR / name)
        if isinstance(d, dict):
            d = d.get("data", d)
            for k in ("developerMode", "developer_mode"):
                if isinstance(d.get(k), bool):
                    return d[k]
    return None


def first_version(text: str) -> str | None:
    m = re.search(r"(\d+\.\d+(?:\.\d+)*)", text)
    return m.group(1) if m else (text.strip().splitlines() or [None])[0]
