"""Which versions Setup installs: the update channel's pins, and backups that let an update roll back.

"tested" (the default) is pins.py, the versions this iOS Studio release was tested with. "latest" lays the newest
upstream versions an update applied and verified (setup state "channelPins") over them. `effective_pins` is the one
place that resolves them; installers and steps read `ctx.pins`.

An update keeps the previous version until its post-update check passes: `backup(component)` sets it aside
before the install, then `Backup.discard()` (the update works) or `Backup.restore()` (it failed or was
cancelled). Binaries are copied to ~/.cache/ios-studio/updates/prev/, the pymobiledevice3 venv is renamed to
venv.prev (same filesystem, instant), xtool keeps its old build (the install delays pruning), omarchy-apple-dev
remembers its old commit. Leftovers show in `toolchain disk` as "update-backups".
"""
from __future__ import annotations

import os
import re
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, fields, replace
from pathlib import Path

from iosstudio.core import appdirs, fileio
from iosstudio.core.appdirs import LOCAL_BIN, VENV_DIR
from iosstudio.core.output import HelperError, fix

from . import runtime as rt
from .facts import ACTOOL_IN_SDK, arch, darwin_tools_stale, find_apple_dev, load_state, save_state
from .pins import (
    APPLE_DEV_COMMIT,
    APPLE_DEV_LIB,
    IPSW_SHA,
    IPSW_VERSION,
    PYMD3_VERSION,
    RCODESIGN_SHA,
    RCODESIGN_VERSION,
)
from .runtime import do_rmtree, do_symlink, log, probe

CHANNELS = ("tested", "latest")
COMPONENTS = ("apple-dev", "xtool", "pymobiledevice3", "ipsw", "rcodesign")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_COMMIT = re.compile(r"[0-9a-f]{40}")
_VERSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+-]{0,40}")


# ------------------------------------------------------------------ channel and pins

@dataclass(frozen=True)
class Pins:
    """The versions to install. The checksums are this machine's architecture's (None: no build for it)."""
    apple_dev_commit: str
    ipsw_version: str
    ipsw_sha: str | None
    rcodesign_version: str
    rcodesign_sha: str | None
    pymd3_version: str

    def is_tested(self, component: str) -> bool:
        """Is this component's pin the one this iOS Studio release was tested with?"""
        t = tested_pins()
        return {"apple-dev": self.apple_dev_commit == t.apple_dev_commit,
                "ipsw": self.ipsw_version == t.ipsw_version,
                "rcodesign": self.rcodesign_version == t.rcodesign_version,
                "pymobiledevice3": self.pymd3_version == t.pymd3_version}.get(component, True)

    def word(self, component: str) -> str:
        """"tested" or "latest", for the rows that name the pin."""
        return "tested" if self.is_tested(component) else "latest"


def tested_pins() -> Pins:
    a = arch()
    return Pins(apple_dev_commit=APPLE_DEV_COMMIT, ipsw_version=IPSW_VERSION,
                ipsw_sha=IPSW_SHA[a][1] if a in IPSW_SHA else None, rcodesign_version=RCODESIGN_VERSION,
                rcodesign_sha=RCODESIGN_SHA.get(a), pymd3_version=PYMD3_VERSION)


def current_channel() -> str:
    """settings.json "toolchainChannel": "tested" (default) or "latest"."""
    ch = appdirs.settings().get("toolchainChannel")
    return ch if ch in CHANNELS else "tested"


def set_channel(channel: str) -> None:
    """Save the channel in settings.json (read-modify-write under a lock, the other keys kept)."""
    if channel not in CHANNELS:
        raise HelperError("usage", f"Unknown update channel '{channel}'. Use tested or latest.")
    path = appdirs.SETTINGS_FILE
    with fileio.lock(path.with_name(".settings.lock")):
        data = fileio.load_json(path, {}) or {}
        data["toolchainChannel"] = channel
        fileio.save_json(path, data)


def channel_pins() -> dict:
    """Setup state "channelPins": {<id>: {version, sha256|commit, at}}, the latest versions an update applied."""
    cp = load_state().get("channelPins")
    return cp if isinstance(cp, dict) else {}


def record_pin(component: str, *, version: str | None = None, sha256: str | None = None,
               commit: str | None = None, at: float | None = None) -> None:
    """Save a latest version an update applied, once its post-update check passed."""
    import time
    entry = {k: v for k, v in (("version", version), ("sha256", sha256), ("commit", commit)) if v}
    entry["at"] = at or time.time()
    save_state({"channelPins": {**channel_pins(), component: entry}})


def _lay_over(base: Pins, entries: dict) -> Pins:
    """`base` with each well-formed entry applied; a malformed one (no checksum, a bad commit) is ignored."""
    upd: dict = {}
    for cid, e in entries.items():
        if not isinstance(e, dict):
            continue
        ver, sha, commit = str(e.get("version") or ""), str(e.get("sha256") or ""), str(e.get("commit") or "")
        if cid == "apple-dev" and _COMMIT.fullmatch(commit):
            upd["apple_dev_commit"] = commit
        elif cid in ("ipsw", "rcodesign") and _VERSION.fullmatch(ver) and _SHA256.fullmatch(sha):
            key = "ipsw" if cid == "ipsw" else "rcodesign"
            upd[f"{key}_version"], upd[f"{key}_sha"] = ver, sha
        elif cid == "pymobiledevice3" and _VERSION.fullmatch(ver):
            upd["pymd3_version"] = ver
    return replace(base, **upd)


def effective_pins(channel: str | None = None, override: dict | None = None) -> Pins:
    """The pins to install: tested -> pins.py; latest -> channelPins laid over them; `override` (the same
    {<id>: {version, sha256|commit}} shape, an update's target) wins. channel None = the saved channel."""
    pins = tested_pins()
    if (channel or current_channel()) == "latest":
        pins = _lay_over(pins, channel_pins())
    return _lay_over(pins, override or {})


# ------------------------------------------------------------------ backups

def prev_dir() -> Path:
    return appdirs.cache_dir() / "updates/prev"


def venv_prev() -> Path:
    return VENV_DIR.with_name(VENV_DIR.name + ".prev")


def updates_lock_path() -> Path:
    return appdirs.state_dir() / "updates.lock"


@contextmanager
def updates_lock(blocking: bool = True) -> Iterator[None]:
    """Held by an update while it runs; non-blocking raises a plain "busy" error when one is running."""
    try:
        with fileio.lock(updates_lock_path(), blocking=blocking):
            yield
    except BlockingIOError:
        raise HelperError("busy", "A toolchain update is running. Wait for it to finish, then try again.",
                          fix("Open Toolchain", "toolchain"))


def backup_paths() -> list[Path]:
    """What earlier updates left behind (stat-only: `toolchain disk` lists it)."""
    d = prev_dir()
    out = sorted(d.iterdir()) if d.is_dir() else []
    return out + ([venv_prev()] if os.path.lexists(venv_prev()) else [])


def clear_backups(step_id: str | None = None, *, take_lock: bool = True) -> int:
    """Delete every kept previous version; returns bytes freed. An update calls it first with its lock held
    (take_lock=False); `toolchain clean --item update-backups` takes the lock (busy while an update runs)."""
    def clear() -> int:
        freed = sum(do_rmtree(p, step_id, inside=(prev_dir(),)) for p in backup_paths() if p != venv_prev())
        if os.path.lexists(venv_prev()):
            freed += do_rmtree(venv_prev(), step_id, exact=(venv_prev(),))
        return freed
    if not take_lock:
        return clear()
    with updates_lock(blocking=False):
        return clear()


@dataclass
class Backup:
    """The previous version of one component, set aside by `backup()`. restore() puts it back (after a failed or
    cancelled update), discard() deletes it (after the post-update check passed). A no-op base for dry runs.
    to_json()/from_json() carry one across processes (an update's in-progress marker: the next run restores it)."""
    component: str
    kind = "none"                    # its name in to_json (not a field: no annotation)
    paths = ()                       # its fields that hold a Path

    def to_json(self) -> dict:
        d = {f.name: getattr(self, f.name) for f in fields(self)}
        return {"kind": self.kind, **{k: str(v) if isinstance(v, Path) else v for k, v in d.items()}}

    @staticmethod
    def from_json(d: dict) -> Backup | None:
        """The backup to_json() gave `d`; None when it names no known kind or its fields don't fit."""
        cls = next((c for c in BACKUP_KINDS if c.kind == d.get("kind")), None)
        if cls is None:
            return None
        kw = {f.name: Path(d[f.name]) if f.name in cls.paths and isinstance(d[f.name], str) else d[f.name]
              for f in fields(cls) if f.name in d}
        try:
            return cls(**kw)
        except TypeError:
            return None

    def restore(self, step_id: str | None = None) -> None:
        pass

    def discard(self, step_id: str | None = None) -> None:
        pass


@dataclass
class _BinaryBackup(Backup):
    kind, paths = "binary", ("dest", "saved")
    dest: Path = Path()
    saved: Path | None = None        # None: there was no binary before

    def restore(self, step_id: str | None = None) -> None:
        if self.saved and self.saved.exists():
            with fileio.replacing(self.dest, 0o755) as tmp:
                shutil.copy2(self.saved, tmp)
            log(step_id, f"put back the previous {self.dest.name}")
        elif self.saved is None and os.path.lexists(self.dest):
            self.dest.unlink()
            log(step_id, f"removed the new {self.dest.name}")
        self.discard(step_id)

    def discard(self, step_id: str | None = None) -> None:
        if self.saved and os.path.lexists(self.saved):
            self.saved.unlink()


@dataclass
class _VenvBackup(Backup):
    kind = "venv"
    existed: bool = False

    def restore(self, step_id: str | None = None) -> None:
        prev = venv_prev()
        if self.existed and not prev.is_dir():
            return                       # nothing to put back (cleared meanwhile): leave the new one
        if os.path.lexists(VENV_DIR):
            do_rmtree(VENV_DIR, step_id, exact=(VENV_DIR,))
        if self.existed:
            os.replace(prev, VENV_DIR)
            log(step_id, "put back the previous pymobiledevice3")

    def discard(self, step_id: str | None = None) -> None:
        if os.path.lexists(venv_prev()):
            do_rmtree(venv_prev(), step_id, exact=(venv_prev(),))


@dataclass
class _XtoolBackup(Backup):
    kind, paths = "xtool", ("target",)
    target: Path | None = None       # the old link target (a build in APPLE_DEV_LIB or elsewhere)

    def restore(self, step_id: str | None = None) -> None:
        from .sources import prune_old_xtool
        link = LOCAL_BIN / "xtool"
        if self.target is None:
            if os.path.lexists(link):
                link.unlink()
            return
        if not self.target.exists():
            return                       # the old build is gone: keep whatever works now
        do_symlink(self.target, link, step_id)
        log(step_id, "put back the previous xtool")
        if self.target.parent.parent == Path(os.path.realpath(APPLE_DEV_LIB)):
            prune_old_xtool(self.target.parent.name, step_id)

    def discard(self, step_id: str | None = None) -> None:
        from .sources import prune_old_xtool
        real = Path(os.path.realpath(LOCAL_BIN / "xtool"))
        if real.parent.parent == Path(os.path.realpath(APPLE_DEV_LIB)) and real.exists():
            prune_old_xtool(real.parent.name, step_id)


@dataclass
class _AppleDevBackup(Backup):
    kind, paths = "apple-dev", ("checkout", "bundle")
    checkout: Path | None = None
    head: str = ""
    bundle: Path | None = None
    actool_mtime: float | None = None  # the SDK's darwin tools as installed before the update
    stale_before: bool = False

    def restore(self, step_id: str | None = None) -> None:
        from .sources import git
        ad = self.checkout
        if not (ad and self.head) or _head(ad) == self.head:
            return
        rc = git(["-C", str(ad), "checkout", "-q", "--detach", self.head], step_id or "apple-dev")
        if rc != 0:
            raise HelperError("rollback", "Couldn't put the previous iOS build scripts back.",
                              fix("Repair", "setup", step="apple-dev"))
        log(step_id, f"put back the iOS build scripts at {self.head[:7]}")
        if not self.bundle:
            return
        if _mtime(self.bundle / ACTOOL_IN_SDK) != self.actool_mtime:
            # The update reinstalled the SDK's darwin tools: build the old ones again at the old commit.
            from .installers import run_sdk_repair
            run_sdk_repair(ad, step_id or "sdk")
        elif not self.stale_before and darwin_tools_stale(ad, self.bundle):
            # Not reinstalled: the SDK still has the old tools, which the checkout now matches again. Git gave the
            # files it rewrote a fresh mtime; set them back so the SDK row doesn't ask for a needless repair.
            _not_newer_than(ad, self.actool_mtime or 0)


BACKUP_KINDS: tuple[type[Backup], ...] = (Backup, _BinaryBackup, _VenvBackup, _XtoolBackup, _AppleDevBackup)


def venv_backup_plan() -> Backup:
    """What backup("pymobiledevice3") is about to set aside, for a marker written before the venv moves (a kill
    mid-rename still finds venv.prev)."""
    return _VenvBackup("pymobiledevice3", existed=VENV_DIR.is_dir())


def _head(ad: Path) -> str:
    rc, out, _ = probe(["git", "-C", ad, "rev-parse", "HEAD"])
    return out.strip() if rc == 0 else ""


def _mtime(p: Path) -> float | None:
    try:
        return p.stat().st_mtime
    except OSError:
        return None


def _not_newer_than(ad: Path, t: float) -> None:
    from .facts import DARWIN_TOOLS_INPUTS
    for rel in DARWIN_TOOLS_INPUTS:
        p = ad / rel
        for f in ([Path(d, n) for d, _, ns in os.walk(p) for n in ns] if p.is_dir() else [p]):
            if (_mtime(f) or 0) > t:
                os.utime(f, (t, t))


def backup(component: str, step_id: str | None = None) -> Backup:
    """Set aside the installed version of `component` before an update installs another one."""
    if component not in COMPONENTS:
        raise HelperError("usage", f"There's no backup for '{component}'.")
    if rt.DRY:
        log(step_id, f"would keep the previous {component} until the update checks out")
        return Backup(component)
    if component in ("ipsw", "rcodesign"):
        dest = LOCAL_BIN / component
        if not dest.is_file():
            return _BinaryBackup(component, dest=dest, saved=None)
        saved = prev_dir() / component
        saved.parent.mkdir(parents=True, exist_ok=True)
        with fileio.replacing(saved, 0o755) as tmp:
            shutil.copy2(dest, tmp)
        return _BinaryBackup(component, dest=dest, saved=saved)
    if component == "pymobiledevice3":
        existed = VENV_DIR.is_dir()
        if existed:
            if os.path.lexists(venv_prev()):
                do_rmtree(venv_prev(), step_id, exact=(venv_prev(),))
            os.replace(VENV_DIR, venv_prev())   # same folder, same filesystem: instant
        return _VenvBackup(component, existed=existed)
    if component == "xtool":
        link = LOCAL_BIN / "xtool"
        return _XtoolBackup(component, target=Path(os.path.realpath(link)) if link.exists() else None)
    ad = find_apple_dev()
    bundle = appdirs.sdk_bundle()
    return _AppleDevBackup(component, checkout=ad, head=_head(ad) if ad else "", bundle=bundle,
                           actool_mtime=_mtime(bundle / ACTOOL_IN_SDK) if bundle else None,
                           stale_before=bool(ad and bundle and darwin_tools_stale(ad, bundle)))
