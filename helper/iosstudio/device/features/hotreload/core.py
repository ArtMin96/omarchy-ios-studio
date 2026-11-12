"""Shared pieces of the hot reload device feature (hotreload.push / clean / runtime) for real.py and demo.py.
Stdlib only, no side effects on import.

The app's hot reload runtime (templates/hot-reload/, compiled into DEBUG builds) watches
`Library/Caches/HotReload/` in the app's container. The helper's session pushes each signed patch there as
`patch-<seq>.dylib` (written as `.part`, then renamed, so the runtime never sees half a file); the runtime loads it
and answers with `ack-<seq>.json`. At each launch it writes `runtime.json` and deletes old patches and acks.

A push only sends a file from the helper's hot-reload patch store (<cache>/hot-reload/<key>/patches/patch-<n>.dylib,
see helper/iosstudio/features/hotreload/buildinfo.py), never any other local file.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from iosstudio.core import appdirs
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


def patch_store() -> Path:
    """The helper's hot-reload cache (buildinfo.cache_root()); patches live in <store>/<key>/patches/."""
    return appdirs.cache_dir() / "hot-reload"


def in_store(path: Path) -> bool:
    """A resolved path that is a patch in the store: <store>/<key>/patches/patch-<n>.dylib."""
    try:
        rel = path.relative_to(patch_store().resolve())
    except (ValueError, OSError):
        return False
    return len(rel.parts) == 3 and rel.parts[1] == "patches" and bool(PATCH_NAME.match(rel.parts[2]))


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


def push_args(a: dict) -> tuple[str, int, Path, int]:
    """(bundle id, seq, local patch file, wait ms) of a hotreload.push request; bad-args / not-found on errors."""
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
    given = Path(raw.removeprefix("file://")).expanduser()
    try:
        src = given.resolve(strict=True)
    except OSError:
        src = None
    if src is None or not src.is_file():
        raise HelperError("not-found", "The patch file isn't there anymore.", None, str(given))
    if not in_store(src):
        raise HelperError("bad-args", "Only hot reload patches made by iOS Studio can be sent.", None, str(given))
    size = src.stat().st_size
    if size > PATCH_MAX:
        raise HelperError("too-big", too_big(size))
    return bid, seq, src, wait_ms(a)


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
