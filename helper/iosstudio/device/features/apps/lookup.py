"""Shared helpers for features that need one installed app or a process's executable path.

Not a feature module: the loader only imports features/<name>/real.py and demo.py.

* exe_path(proc): a process from AppServiceService.list_processes() -> its executable path.
* find_app(): "the installed app for this bundle id" through the cached AppIndex, never the full
  app list with every attribute.
"""
from __future__ import annotations

from iosstudio.device.features.apps.index import INDEX, Fetch


def exe_path(proc: dict) -> str:
    """executableURL ({'relative': 'file:///…'} or a plain string) -> '/…/X.app/X' ('' if none)."""
    url = proc.get("executableURL")
    path = url.get("relative", "") if isinstance(url, dict) else str(url or "")
    return path.removeprefix("file://")


async def find_app(udid: str, bundle_id: str, fetch: Fetch) -> dict | None:
    """The AppIndex entry for `bundle_id`, plain or XTL-<TEAM>. form, or None if it isn't installed.

    1. INDEX.lookup: a cached exact match, else one Lookup of just that bundle id.
    2. A miss (a free team's app installed as XTL-<TEAM>.<id>, or an XTL- id from another team):
       the user app list (limited attributes, cached afterwards), then bundle_matches().
    """
    bundle_id = str(bundle_id or "").strip()
    if not bundle_id:
        return None
    hit = await INDEX.lookup(udid, bundle_id, fetch)
    if hit is not None:
        return hit
    plain = bundle_id.split(".", 1)[-1] if bundle_id.startswith("XTL-") else bundle_id
    await INDEX.list(udid, "User", fetch, sync=True)
    return INDEX.cached(udid, bundle_id) or (INDEX.cached(udid, plain) if plain != bundle_id else None)
