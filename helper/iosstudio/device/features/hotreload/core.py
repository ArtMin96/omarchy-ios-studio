"""Shared pieces of the hot reload device feature (hotreload.push / clean / runtime) for real.py and demo.py.
Stdlib only, no side effects on import.

The app's hot reload runtime (templates/hot-reload/, compiled into DEBUG builds) watches
`Library/Caches/HotReload/` in the app's container. The helper's session pushes each signed patch there as
`patch-<seq>.dylib` (written as `.part`, then renamed, so the runtime never sees half a file); the runtime loads it
and answers with `ack-<seq>.json`. At each launch it writes `runtime.json` and deletes old patches and acks.

A push only sends a file from a helper's hot-reload patch store (<cache>/hot-reload/<key>/patches/patch-<n>.dylib,
see helper/iosstudio/features/hotreload/buildinfo.py), never any other local file. The daemon doesn't work out
the store from its own environment (it may have been started with another XDG_CACHE_HOME); it takes the path the
helper sends when it (a) is absolute with no symlink in it (src == its realpath), (b) has exactly that shape (and
the request's `key`, when given, as <key>) and (c) is a regular file owned by the daemon's user, in a `patches`
folder owned by it too. The file is then opened with O_NOFOLLOW | O_CLOEXEC and the open file checked to be the
same one (device and inode), so it can't be swapped between the check and the read.
"""
from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from iosstudio.core.output import HelperError

DIR = "/Library/Caches/HotReload"
RUNTIME = f"{DIR}/runtime.json"
WAIT_DEFAULT_MS = 5000
WAIT_MAX_MS = 15000
POLL_S = 0.05                 # ack poll interval
ACK_MAX = 64 << 10            # bytes of an ack (or runtime.json) read at most
# Biggest patch pushed. A patch holds only the changed files' code (no DWARF: the linker leaves it in the objects),
# typically 50-500 KB per file; CrashLab's whole app executable is ~5 MB. 64 MB is >10x any real patch and ~3 s of AFC.
PATCH_MAX = 64 << 20
PATCH_NAME = re.compile(r"^patch-\d+\.dylib$")
KEY = re.compile(r"^[0-9a-f]{12}$")        # buildinfo.project_key()
STORE_DIR = "hot-reload"
NOT_OURS = "Only hot reload patches made by iOS Studio can be sent."
SEQ_MAX = 10 ** 9
OURS = re.compile(r"^(patch-\d+\.dylib(\.part)?|ack-\d+\.json(\.tmp)?)$")   # what hotreload.clean removes
ACK_INTS = ("swapped", "kept", "vtable", "reverted", "generation")

NOT_DEV = ("Hot reload works with your own apps only (built and installed from iOS Studio). "
           "This one is from the App Store.")
NOT_INSTALLED = "The app isn't installed on your iPhone. Run it once from iOS Studio, then try again."


def patch_path(seq: int) -> str:
    return f"{DIR}/patch-{seq}.dylib"


def ack_path(seq: int) -> str:
    return f"{DIR}/ack-{seq}.json"


def is_ours(name: str) -> bool:
    """A patch or ack file (also half-written ones); never runtime.json."""
    return bool(OURS.match(name))


def store_shaped(path: Path, key: str | None = None) -> bool:
    """path is shaped like a patch in a store: …/hot-reload/<key>/patches/patch-<n>.dylib (that key, if given)."""
    p = path.parts
    return (len(p) >= 5 and p[-4] == STORE_DIR and bool(KEY.match(p[-3])) and (key is None or p[-3] == key)
            and p[-2] == "patches" and bool(PATCH_NAME.match(p[-1])))


@dataclass(frozen=True)
class Patch:
    """A checked patch file (push_args): open it with open_patch()."""
    path: Path
    dev: int
    ino: int
    size: int


def _ours(st: os.stat_result) -> bool:
    return st.st_uid == os.getuid()


def check_patch(raw: str, key: str | None = None) -> Patch:
    """The local patch file a push names, checked (see the module doc); not-found / bad-args / too-big."""
    given = Path(raw.removeprefix("file://")).expanduser()
    try:
        real = Path(os.path.realpath(given, strict=True))
    except OSError:
        raise HelperError("not-found", "The patch file isn't there anymore.", None, str(given)) from None
    if not given.is_absolute() or real != Path(os.path.normpath(given)) or not store_shaped(real, key):
        raise HelperError("bad-args", NOT_OURS, None, str(given))
    try:
        st, folder = os.lstat(real), os.lstat(real.parent)
    except OSError:
        raise HelperError("not-found", "The patch file isn't there anymore.", None, str(given)) from None
    if not stat.S_ISREG(st.st_mode) or not _ours(st) or not _ours(folder):
        raise HelperError("bad-args", NOT_OURS, None, str(given))
    if st.st_size > PATCH_MAX:
        raise HelperError("too-big", too_big(st.st_size))
    return Patch(real, st.st_dev, st.st_ino, st.st_size)


def open_patch(patch: Patch) -> BinaryIO:
    """The checked patch opened for reading (O_NOFOLLOW | O_CLOEXEC), the same file check_patch looked at, or
    HelperError (not-found when it's gone, bad-args when another file is there now)."""
    try:
        fd = os.open(patch.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        raise HelperError("not-found", "The patch file isn't there anymore.", None, str(patch.path)) from None
    except OSError:          # ELOOP: a symlink took its place
        raise HelperError("bad-args", NOT_OURS, None, str(patch.path)) from None
    try:
        st = os.fstat(fd)
        if (st.st_dev, st.st_ino) != (patch.dev, patch.ino) or not stat.S_ISREG(st.st_mode) or not _ours(st):
            raise HelperError("bad-args", NOT_OURS, None, str(patch.path))
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def bundle_id(a: dict) -> str:
    bid = str(a.get("bundleId") or "")
    if not bid:
        raise HelperError("bad-args", "Which app? (bundleId missing)")
    return bid


def wait_ms(a: dict) -> int:
    raw = a.get("waitMs")
    try:
        ms = WAIT_DEFAULT_MS if raw is None else int(raw)
    except (TypeError, ValueError):
        raise HelperError("bad-args", "waitMs must be a number of milliseconds.") from None
    return max(0, min(ms, WAIT_MAX_MS))


def push_args(a: dict) -> tuple[str, int, Patch, int]:
    """(bundle id, seq, checked local patch file, wait ms) of a hotreload.push request; bad-args / not-found /
    too-big on errors."""
    bid = bundle_id(a)
    try:
        seq = int(a.get("seq"))
    except (TypeError, ValueError):
        raise HelperError("bad-args", "Which patch? (seq missing)") from None
    if not 1 <= seq <= SEQ_MAX:
        raise HelperError("bad-args", "seq must be a positive number.")
    raw = str(a.get("src") or "")
    if not raw:
        raise HelperError("bad-args", "Which patch file? (src missing)")
    key = a.get("key")
    if key is not None and not (isinstance(key, str) and KEY.match(key)):
        raise HelperError("bad-args", "key must be a hot reload project key.")
    return bid, seq, check_patch(raw, key), wait_ms(a)


def too_big(size: int) -> str:
    return f"The patch is {size >> 20} MB, too big to send (at most {PATCH_MAX >> 20} MB)."


def _num(v, kind):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return kind(v)


def parse_ack(data: bytes, seq: int) -> dict:
    """The runtime's ack-<seq>.json as {seq, ok, ms, swapped, kept, vtable, error, generation}. An ack that can't
    be read (too big, not JSON) is a failed one with a plain error, never an exception."""
    bad = {"seq": seq, "ok": False, "ms": None, "swapped": 0, "kept": 0, "vtable": 0, "reverted": 0, "generation": None,
           "error": "The app's answer to this patch couldn't be read."}
    if len(data) > ACK_MAX:
        return bad
    try:
        obj = json.loads(data.decode("utf-8", "replace"))
    except ValueError:
        return bad
    if not isinstance(obj, dict):
        return bad
    out = {"seq": _num(obj.get("seq"), int) or seq, "ok": obj.get("ok") is True, "ms": _num(obj.get("ms"), float)}
    for k in ACK_INTS:
        out[k] = _num(obj.get(k), int)
        if out[k] is None and k != "generation":
            out[k] = 0
    e = obj.get("error")
    out["error"] = str(e)[:2000] if e not in (None, "") else None
    if not out["ok"] and not out["error"]:
        out["error"] = "The app couldn't load this patch."
    return out


def parse_runtime(data: bytes) -> dict | None:
    """runtime.json as {version, pid, started}, or None when it isn't one."""
    if len(data) > ACK_MAX:
        return None
    try:
        obj = json.loads(data.decode("utf-8", "replace"))
    except ValueError:
        return None
    if not isinstance(obj, dict) or _num(obj.get("version"), int) is None:
        return None
    return {"version": _num(obj.get("version"), int), "pid": _num(obj.get("pid"), int),
            "started": _num(obj.get("started"), float)}
