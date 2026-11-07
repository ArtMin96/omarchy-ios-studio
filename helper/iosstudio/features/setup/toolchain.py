"""The `toolchain` group's logic: versions (info), disk usage, cleaning allow-listed items, removing everything."""
from __future__ import annotations

import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from iosstudio.core import appdirs, output
from iosstudio.core.appdirs import CACHE_DIR, HOME, LOCAL_BIN, VENV_DIR
from iosstudio.core.output import HelperError

from . import runtime as rt
from .facts import (
    apple_dev_pins,
    find_apple_dev,
    first_version,
    gather,
    load_state,
    save_state,
    sdk_caches,
    sdk_ios_versions,
    sdk_stamp,
    toolchain_root,
)
from .pins import (
    APPLE_DEV_CACHE,
    APPLE_DEV_COMMIT,
    APPLE_DEV_HOME,
    APPLE_DEV_LIB,
    AUR_DIR,
    DOWNLOADS,
    IPSW_VERSION,
    LEGACY_VENV,
    OMARCHY_HOOK,
    PYMD3_VERSION,
    RCODESIGN_VERSION,
    SDK_CACHE,
    XTOOL_CONFIG,
)
from .runtime import confirm, do_rmtree, do_unlink, du, free_bytes, log, progress, run_root, run_stream
from .steps import build_steps, summarize
from .xip import find_xips

# ------------------------------------------------------------------ toolchain info

def info() -> dict:
    """Versions and state of every piece (from the same facts as `setup check`)."""
    F = gather()
    steps, c = build_steps(F)
    by = {s["id"]: s for s in steps}

    def item(id_: str, name: str, version: str | None, detail: str, status: str, **kw) -> dict:
        d = {"id": id_, "name": name, "version": version, "detail": detail, "status": status}
        d.update(kw)
        return d

    def st_of(sid: str) -> str:
        return {"done": "ok", "missing": "missing", "broken": "error", "optional": "missing", "unknown": "warn"}[by[sid]["state"]]
    xv = (F["xtool_ver"][1].strip().splitlines() or [""])[0]
    xt_real = os.path.realpath(F["xtool"]) if F["xtool"] else None
    swift_ver = c.get("swift_ver")
    qi = F["swift_qi"][1]
    pkgv = re.search(r"^Version\s*:\s*(\S+)", qi, re.M)
    items = [
        item("xtool", "xtool", xv.split()[-1][:7] if xv else None, appdirs.tilde(xt_real) if xt_real else "Not installed",
             st_of("xtool"), hasFix=F["xtool_old_ua"] is False, step="xtool"),
        item("swift", "Swift", swift_ver, f"swift-bin {pkgv.group(1)}" if pkgv else "Not installed", st_of("swift"),
             held="swift-bin" in F["ignore"][1].split(), step="swift"),
        item("sdk", "iOS SDK", (sdk_ios_versions(c["bundle"]) or [None])[-1] if c.get("bundle") else None,
             by["sdk"]["detail"], st_of("sdk"), path=appdirs.tilde(c["bundle"]) if c.get("bundle") else None,
             stamp=sdk_stamp(c["bundle"]) if c.get("bundle") else None, wantedStamp=c["stamp"], step="sdk"),
        item("pymobiledevice3", "pymobiledevice3", c["pymd3"][1] if c.get("pymd3") else None,
             (f"Python {c['pymd3'][2]} · {appdirs.tilde(Path(c['pymd3'][0]).parent.parent)}" if c.get("pymd3") else by["pymobiledevice3"]["detail"]),
             st_of("pymobiledevice3"), pinned=PYMD3_VERSION, step="pymobiledevice3"),
        item("rcodesign", "rcodesign", first_version(F["rcodesign"][1]) if F["rcodesign"][0] == 0 else None,
             "Signs App Store builds", st_of("rcodesign"), pinned=RCODESIGN_VERSION, step="rcodesign"),
        item("ipsw", "ipsw", first_version(F["ipsw"][1]) if F["ipsw"][0] == 0 else None, "Debugger helper (optional)",
             "ok" if F["ipsw"][0] == 0 else "missing", pinned=IPSW_VERSION, step="ipsw", optional=True),
        item("apple-dev", "omarchy-apple-dev", (c.get("apple_dev_head") or "")[:7] or None,
             appdirs.tilde(c["apple_dev"]) if c.get("apple_dev") else "Not downloaded", st_of("apple-dev"),
             pinned=APPLE_DEV_COMMIT[:7], step="apple-dev"),
    ]
    caches = [dict(x, path=appdirs.tilde(x["path"])) for x in c.get("caches", [])]
    return {"items": items, "sdks": {"installed": sdk_ios_versions(c["bundle"]) if c.get("bundle") else [],
                                     "caches": caches, "xcodeNeeded": c["xcode"]},
            "summary": summarize(steps)}


# ------------------------------------------------------------------ toolchain disk / clean / remove-all

PRUNE = {"node_modules", "Pods", "vendor", "build", "dist", "target", "DerivedData", "venv", "__pycache__"}


def project_build_dirs(max_depth: int = 5) -> list[Path]:
    """`.build` folders of xtool projects (Package.swift + xtool.yml) under the configured project roots."""
    roots = appdirs.project_roots()
    out: set[Path] = set()
    deadline = time.time() + 3
    for r in roots:
        r = Path(os.path.expanduser(r))
        base = len(r.parts)
        for dirpath, dirnames, filenames in os.walk(r):
            if time.time() > deadline:
                break
            here = Path(dirpath)
            if "xtool.yml" in filenames and "Package.swift" in filenames and (here / ".build").is_dir():
                out.add(here / ".build")
            if len(here.parts) - base >= max_depth:
                dirnames[:] = []
            else:
                dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in PRUNE]
    return sorted(out)


def _stale_pid_dirs() -> list[Path]:
    out = []
    for p in SDK_CACHE.glob("pid-*"):
        m = re.match(r"pid-(\d+)-", p.name)
        if m and not Path(f"/proc/{m.group(1)}").exists():
            out.append(p)
    return out


def disk_items() -> list[dict]:
    """Every piece with its paths. `clean` deletes only items with cleanable=True, and only their paths."""
    xt_real = Path(os.path.realpath(LOCAL_BIN / "xtool")) if (LOCAL_BIN / "xtool").exists() else None
    active_lib = xt_real.parent if xt_real else None
    ad = find_apple_dev()
    stamp = apple_dev_pins(ad)["XTOOL_SDK_VERSION"]
    bundle = appdirs.sdk_bundle()
    reg_vers = sdk_ios_versions(bundle) if bundle else []
    caches = sdk_caches(stamp)
    good_caches = [Path(x["path"]) for x in caches if x["current"] and x["complete"] and (not reg_vers or x["ios"] in reg_vers)]
    old_caches = [Path(x["path"]) for x in caches if Path(x["path"]) not in good_caches]
    old_caches += _stale_pid_dirs() + [p for p in SDK_CACHE.glob(".build.*")]
    sdk_ok_now = bool(bundle and sdk_stamp(bundle) == stamp and reg_vers)
    xips = find_xips()
    superseded_libs = [p for p in APPLE_DEV_LIB.glob("xtool-*") if p.is_dir() and p != active_lib]
    # source trees of superseded builds (same naming: lib/xtool-X <-> cache/xtool-X)
    superseded_src = [APPLE_DEV_CACHE / p.name for p in superseded_libs if (APPLE_DEV_CACHE / p.name).is_dir()]
    build_trees = [p / ".build" for p in APPLE_DEV_CACHE.glob("*") if (p / ".build").is_dir() and p not in superseded_src]
    if ad and (ad / "tools/darwin-tools/.build").is_dir():
        build_trees.append(ad / "tools/darwin-tools/.build")
    venvs = [p for p in (VENV_DIR,) if p.exists()] + ([LEGACY_VENV] if LEGACY_VENV.exists() and not LEGACY_VENV.is_symlink() else [])
    items = [
        dict(id="sdk", name="iOS SDK (in use)", paths=[bundle] if bundle else [], cleanable=False,
             note="Needed to build apps. Remove it with “Remove everything”."),
        dict(id="sdk-cache", name="Spare SDK copy", paths=good_caches, cleanable=bool(good_caches),
             safe=False, note="Lets iOS Studio repair the SDK without the Xcode download. Deleting it is safe, "
             "but a later repair needs Xcode again."),
        dict(id="sdk-cache-old", name="Old SDK copies and leftovers", paths=old_caches, cleanable=True, safe=True,
             note="Built for another Xcode or by an older xtool. Not used."),
        dict(id="xip", name="Xcode download (.xip)", paths=xips, cleanable=bool(xips), safe=sdk_ok_now and bool(good_caches),
             note=("Not needed any more: the SDK is built and a spare copy exists." if sdk_ok_now and good_caches else
                   "Still needed until the SDK is built." if not sdk_ok_now else
                   "The SDK is built, but there's no spare copy: a repair would need this file.")),
        dict(id="build-trees", name="Build leftovers (xtool, macro server)", paths=build_trees, cleanable=bool(build_trees), safe=True,
             note="Compiler output kept from installing xtool. Only needed to rebuild faster."),
        dict(id="xtool-old", name="Old xtool builds", paths=superseded_libs + superseded_src, cleanable=True, safe=True,
             note="xtool versions that are no longer used."),
        dict(id="aur", name="Swift download and package", paths=[AUR_DIR] if AUR_DIR.exists() else [], cleanable=True, safe=True,
             note="Kept from installing Swift. Not needed once Swift is installed."),
        dict(id="project-builds", name="Project build folders", paths=project_build_dirs(), cleanable=True, safe=True,
             note="Your apps' .build folders. Safe to delete; the next build takes a few minutes longer."),
        dict(id="xtool", name="xtool", paths=[p for p in (active_lib,) if p], cleanable=False,
             note="In use. (Its source code and build leftovers are listed separately.)"),
        dict(id="venv", name="pymobiledevice3", paths=venvs, cleanable=False, note="In use."),
        dict(id="tools", name="rcodesign and ipsw", paths=[p for p in (LOCAL_BIN / "rcodesign", LOCAL_BIN / "ipsw") if p.exists()],
             cleanable=False, note="Small helper programs."),
        dict(id="swift", name="Swift toolchain", paths=[toolchain_root()] if toolchain_root() else [], cleanable=False,
             note="System package (swift-bin)."),
    ]
    return items


def disk_usage() -> dict:
    """Disk usage per piece (measured in parallel) and what is safe to clean; also cached for `--cached`."""
    items = disk_items()
    all_paths = {str(p) for it in items for p in it["paths"]}
    with ThreadPoolExecutor(max_workers=8) as ex:
        sizes = dict(zip(all_paths, ex.map(lambda s: du(Path(s)), all_paths)))
    out = []
    for it in items:
        ps = [{"path": appdirs.tilde(p), "size": sizes.get(str(p), 0)} for p in it["paths"]]
        out.append({"id": it["id"], "name": it["name"], "size": sum(x["size"] for x in ps), "paths": ps,
                    "cleanable": bool(it["cleanable"] and ps and sum(x["size"] for x in ps) > 0), "safe": it.get("safe", False),
                    "note": it["note"]})
    data = {"items": out, "total": sum(i["size"] for i in out),
            "reclaimable": sum(i["size"] for i in out if i["cleanable"] and i["safe"]),
            "free": free_bytes(HOME)}
    appdirs.cache_put("toolchain-disk", data)
    return data


def clean(flags: dict) -> int:
    """Delete one cleanable item's paths (each re-checked against the item's shape and the allow-list)."""
    item_id = flags.get("item")
    items = {i["id"]: i for i in disk_items()}
    it = items.get(item_id)
    if not it or not it["cleanable"]:
        raise HelperError("usage", "That item can't be cleaned.")
    if item_id == "xip":
        extra_roots = (DOWNLOADS,) + tuple(p.parent for p in it["paths"])
    elif item_id in ("project-builds", "build-trees"):
        extra_roots = tuple(p.parent for p in it["paths"])
    else:
        extra_roots = ()
    if rt.DRY:
        log(None, "Dry run: nothing will be deleted.")
    output.step(item_id, it["name"], "running")
    freed = 0
    for i, p in enumerate(it["paths"]):
        # each path must still match its item's shape
        if item_id == "xip" and not (p.suffix == ".xip" and p.name.lower().startswith("xcode")):
            continue
        if item_id in ("project-builds", "build-trees") and p.name != ".build":
            continue
        progress(item_id, i / max(1, len(it["paths"])), f"Deleting {appdirs.tilde(p)}")
        freed += do_rmtree(p, item_id, inside=extra_roots)
    if item_id == "xip" and not rt.DRY:
        st = load_state()
        if st.get("xip") and not Path(st["xip"]).exists():
            save_state({"xip": None, "xipInfo": None})
    output.step(item_id, it["name"], "done")
    try:
        (CACHE_DIR / "toolchain-disk.json").unlink()
    except OSError:
        pass
    return output.done(True, {"item": item_id, "freed": freed, "dryRun": rt.DRY})


def remove_all(flags: dict) -> int:
    """Mirror of `omarchy-remove-dev-env ios` (PR #14255), complete: also ipsw, our venv, caches, the hold,
    the hook and the usbmuxd drop-in. Keeps Apple sign-in (~/.config/xtool) unless --remove-auth, keeps
    pairing records always, keeps system packages that other software uses."""
    if rt.DRY:
        log(None, "Dry run: nothing will be removed.")
    confirm("Remove the whole iOS toolchain (Swift, xtool, the iOS SDK, caches)? Your projects, your iPhone pairing"
            + ("" if flags.get("remove_auth") else " and your Apple sign-in") + " stay.", "Remove everything", flags)
    sid = "remove-all"
    output.step(sid, "Removing the iOS toolchain", "running")
    xt = LOCAL_BIN / "xtool"
    progress(sid, 0.05, "Unregistering the iOS SDK")
    if Path("/usr/bin/swift").exists():
        run_stream(["/usr/bin/swift", "sdk", "remove", "darwin"], sid)
    for b in appdirs.sdk_bundles():
        do_rmtree(b, sid)
    steps = [
        ("SDK copies", [SDK_CACHE]),
        ("xtool builds", [APPLE_DEV_LIB, APPLE_DEV_CACHE]),
        ("iOS build scripts", [APPLE_DEV_HOME]),
        ("pymobiledevice3", [VENV_DIR]),
        ("iOS Studio downloads", [AUR_DIR]),
    ]
    for i, (label, paths) in enumerate(steps):
        progress(sid, 0.1 + 0.5 * i / len(steps), f"Removing {label}")
        for p in paths:
            do_rmtree(p, sid)
    # ~/pymobile3-venv (omarchy-apple-dev's venv path): its uninstall removes it too. Our symlink always; a real one only if it isn't someone else's.
    if LEGACY_VENV.is_symlink():
        do_unlink(LEGACY_VENV, sid)
    elif LEGACY_VENV.is_dir():
        do_rmtree(LEGACY_VENV, sid, exact=(LEGACY_VENV,))
    for b in ("xtool", "rcodesign", "ipsw"):
        p = LOCAL_BIN / b
        if p.is_symlink() or p.is_file():
            do_unlink(p, sid)
    do_unlink(OMARCHY_HOOK, sid)
    if flags.get("remove_auth"):
        progress(sid, 0.65, "Signing out of Apple")
        if xt.exists():
            run_stream([xt, "auth", "logout"], sid)
        do_rmtree(XTOOL_CONFIG, sid, exact=(XTOOL_CONFIG,))
    root_args = ["--remove-usbmuxd-dropin"]
    if not flags.get("keep_swift"):
        root_args = ["--unhold-swift", "--remove-swift"] + root_args
    progress(sid, 0.7, "Removing system pieces (asks for your password)")
    run_root(root_args, sid, "Removing Swift")
    save_state({"xip": None, "xipInfo": None})
    output.step(sid, "Removing the iOS toolchain", "done")
    return output.done(True, {"removed": True, "keptAuth": not flags.get("remove_auth"), "dryRun": rt.DRY})
