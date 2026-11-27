"""`updates check`: ask every upstream source what's new, at most once a day, and cache the answer.

Skipped (done at once with the cached status) when the last check is under 24 h old (unless --force) and always
before GitHub's rate-limit reset. A failed or partial check still moves nextCheckAt (3 h later, or the reset
time), so the app's hourly timer doesn't retry a persistent network error every hour. Each run refreshes the
installed-version probes of ipsw, rcodesign and xtool (only for binaries that changed).
"""
from __future__ import annotations

import time

from iosstudio.core import output
from iosstudio.core.output import HelperError

from ..setup.channel import COMPONENTS
from ..setup.runtime import log, progress
from . import model, upstream


def _when(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


def run(force: bool) -> int:
    with model.cache_lock():
        cache = model.load_cache()
        model.refresh_probes(cache["probes"])
        now = time.time()
        reset = (cache.get("rate") or {}).get("resetAt")
        if reset and now < float(reset):
            model.save_cache(cache)
            log(None, f"GitHub asked iOS Studio to wait: the next check can run after {_when(float(reset))}.")
            return output.done(True, model.status(cache=cache))
        if not force and not model.is_due(cache, now):
            model.save_cache(cache)
            log(None, "Checked less than a day ago; using that answer.")
            return output.done(True, model.status(cache=cache))
        ok_, err = _check(cache, now)
        model.save_cache(cache)
    st = model.status(cache=cache)
    return output.done(True, st) if ok_ else output.done(False, st, err)


def _check(cache: dict, now: float) -> tuple[bool, HelperError | None]:
    """One run over every source, merged into `cache` in place. (any source answered, the error if one failed)."""
    progress(None, 0.0, "Checking for toolchain updates")
    fetcher = upstream.Fetcher(etags=dict(cache.get("etags") or {}))
    comps = {c["id"]: c for c in cache.get("components") or [] if isinstance(c, dict) and c.get("id")}
    unverified = {n["id"]: n for n in cache.get("notices") or [] if isinstance(n, dict) and n.get("id")}
    failed: list[str] = []
    messages: list[str] = []
    reset_at: float | None = None
    n = 0
    total = len(upstream.SOURCES)

    def on_result(src: str, found: list[dict] | None, exc: Exception | None) -> None:
        nonlocal n, reset_at
        n += 1
        ids = ["apple-dev", "xtool"] if src == "apple-dev" else [src]
        if found:
            for c in found:
                comps[c["id"]] = c
            unverified.pop(src, None)
        elif isinstance(exc, upstream.Unverifiable):
            unverified[src] = {"id": src, "version": exc.version, "reason": exc.reason}
        else:
            failed.append(src)
            messages.append(str(exc) or "it didn't answer")
            if isinstance(exc, upstream.RateLimited):
                reset_at = max(reset_at or 0, exc.reset_at)
        for cid in ids:
            if cid in COMPONENTS:
                latest = (comps.get(cid) or {}).get("latest")
                output.event("component", id=cid, latest=model.short(cid, latest))
        progress(None, n / total, f"Checked {src}")

    upstream.check_all(fetcher, on_result)
    cache["etags"] = {u: fetcher.etags[u] for u in fetcher.used if u in fetcher.etags}
    cache["components"] = list(comps.values())
    cache["notices"] = list(unverified.values())
    cache["rate"] = {"remaining": fetcher.rate.get("remaining"), "resetAt": reset_at}
    answered = len(failed) < total
    if answered:
        cache["checkedAt"] = now
    if not failed:
        cache["error"] = None
        cache["nextCheckAt"] = now + model.DAY
        return True, None
    cache["nextCheckAt"] = max(now + model.RETRY_AFTER_ERROR, reset_at or 0)
    if reset_at:
        code, msg = "rate-limited", f"GitHub asked iOS Studio to wait; it checks again after {_when(reset_at)}."
    elif answered:
        code, msg = "partial", (f"Some sources couldn't be checked ({', '.join(failed)}): {messages[0]} "
                                "iOS Studio tries again in a few hours.")
    else:
        code, msg = "network", f"Couldn't check for updates: {messages[0]}"
    cache["error"] = {"code": code, "message": msg}
    return answered, HelperError(code, msg)
