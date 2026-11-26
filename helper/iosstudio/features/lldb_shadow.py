"""A shadow lldb that evaluates Swift expressions (p, po, expression) in an iOS app. Stdlib only.

Why: for an iOS target, Linux LLDB looks for an Xcode Swift resource dir, finds none ("HostInfoError") and
falls back to the toolchain's own usr/lib/swift, whose Linux CoreFoundation module shadows the SDK's. Every Swift
expression then fails with "missing required module 'SwiftShims'" (or, with target.sdk-path set, "malformed or
corrupted precompiled file"); only frame variables work. LLDB reads the resource dir from
<dir of liblldb.so>/swift, symlinks resolved, and has no setting for it. So the shadow toolchain holds a real
liblldb file next to the darwin SDK's resource dir:

  usr/bin/lldb, lldb-dap      the toolchain's (RUNPATH $ORIGIN/../lib: they load the shadow liblldb); the
                              other bin entries are symlinks
  usr/lib/liblldb.so.X.Y.Z    the toolchain's liblldb; its liblldb.so* links point inside the shadow
  usr/lib/swift/              the SDK's XcodeDefault.xctoolchain/usr/lib/swift entries, plus the toolchain's
                              linux/ and host/ (liblldb's RUNPATH loads its own libraries from them)
  <site-packages>/lldb/       LLDB's Python package, real dirs on the way, its _lldb*.so re-pointed at the
                              shadow liblldb (so Python never maps the toolchain's copy too)
  everything else             symlinks to the toolchain

lldb, lldb-dap and liblldb are hard links when the cache and the toolchain share a filesystem and the owner
allows it, else reflinks (btrfs, XFS: instant, no extra disk), else copies (~230 MB). The tree lives in
<cache>/lldb-ios/<hash of the toolchain>/; a stamp written last records what it was built from (toolchain, SDK,
lldb/liblldb/lldb-dap size and mtime) and decides reuse with a few stat calls, cheap enough for every session.
Any difference rebuilds it in a temp dir that then replaces it, under a lock, so concurrent runs wait and reuse.
Leftovers of a killed build are removed by the next build; only the newest KEEP trees stay.

Port of omarchy-apple-dev v0.4.8 tools/lldb-ios-shadow.sh. Then lldb needs
`settings set target.sdk-path <Shadow.sdk>` (lldb_driver.py does it from the `sdkPath` config key).

  ensure(usr, bundle, pythonpath) -> Shadow     raises Unavailable(plain reason)
  for_lldb(lldb, pythonpath)      -> (Shadow | None, reason)   finds the SDK bundle itself; never raises
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from iosstudio.core import appdirs, fileio

STAMP_VERSION = 1
KEEP = 2                                   # shadow trees kept (one per toolchain path)
MODES = ("hardlink", "reflink", "copy")    # how lldb, lldb-dap and liblldb get into the shadow, in order
FICLONE = 0x40049409                       # linux/fs.h: _IOW(0x94, 9, int)
RESOURCES = "Developer/Toolchains/XcodeDefault.xctoolchain/usr/lib/swift"
SDKS = "Developer/Platforms/iPhoneOS.platform/Developer/SDKs"
_KEY = re.compile(r"[0-9a-f]{12}")


class Unavailable(Exception):
    """No shadow lldb; str() is a plain sentence for the user."""


@dataclass(frozen=True)
class Shadow:
    root: Path                  # <cache>/lldb-ios/<key>
    lldb: Path                  # root/usr/bin/lldb
    lldb_dap: Path | None       # root/usr/bin/lldb-dap, when the toolchain has one
    pythonpath: str | None      # put on sys.path for `import lldb` (None: no Python package was asked for)
    sdk: Path                   # the iPhoneOS SDK, for `settings set target.sdk-path`
    link: str                   # how liblldb got there: hardlink | reflink | copy
    built: bool = False         # this call built the tree


@dataclass(frozen=True)
class _Plan:
    usr: Path                   # the toolchain's usr (resolved)
    resources: Path             # the SDK's Swift resource dir
    sdk: Path
    lib: Path                   # the real liblldb.so.X.Y.Z file
    pkg: str | None             # LLDB's Python package, relative to usr
    modes: tuple[str, ...]
    stamp: dict


def cache_root() -> Path:
    return appdirs.cache_dir() / "lldb-ios"


def ios_sdk(bundle: Path) -> Path | None:
    """The bundle's iPhoneOS SDK: iPhoneOS.sdk, else its newest versioned one (as Setup lists them)."""
    plain = bundle / SDKS / "iPhoneOS.sdk"
    if plain.is_dir():
        return plain
    from iosstudio.features.setup.facts import sdk_ios_versions
    vers = sdk_ios_versions(bundle)
    return bundle / SDKS / f"iPhoneOS{vers[-1]}.sdk" if vers else None


def _sig(path: Path) -> list[int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return [st.st_size, st.st_mtime_ns]


def _plan(usr: Path | str, bundle: Path | str, pythonpath: str | None, modes: tuple[str, ...]) -> _Plan:
    """What the shadow is built from, with stat and readlink only."""
    usr, bundle = Path(os.path.realpath(usr)), Path(bundle)
    resources = bundle / RESOURCES
    res = _sig(resources)
    if res is None:
        raise Unavailable("The iOS SDK's Swift files weren't found (Setup installs them with the SDK).")
    sdk = ios_sdk(bundle)
    if sdk is None:
        raise Unavailable("The iOS SDK (iPhoneOS.sdk) wasn't found (Setup installs it).")
    lldb, lib = _sig(usr / "bin/lldb"), Path(os.path.realpath(usr / "lib/liblldb.so"))
    lib_sig = _sig(lib)
    if lldb is None or lib_sig is None or lib.parent != usr / "lib" or not lib.name.startswith("liblldb.so"):
        raise Unavailable("This Swift toolchain's lldb isn't laid out as expected (usr/bin/lldb, usr/lib/liblldb.so).")
    pkg = None
    if pythonpath:
        try:
            pkg = str((Path(os.path.realpath(pythonpath)) / "lldb").relative_to(usr))
        except ValueError:
            raise Unavailable("lldb's Python module is outside its Swift toolchain.") from None
    stamp = {"version": STAMP_VERSION, "toolchain": str(usr), "resources": str(resources), "resourcesSig": res,
             "sdk": str(sdk), "python": pkg, "modes": list(modes), "lldb": lldb, "liblldb": [lib.name, *lib_sig],
             "lldbDap": _sig(usr / "bin/lldb-dap")}
    return _Plan(usr, resources, sdk, lib, pkg, modes, stamp)


def _shadow(tree: Path, plan: _Plan, link: str, built: bool) -> Shadow:
    u = tree / "usr"
    return Shadow(tree, u / "bin/lldb", u / "bin/lldb-dap" if plan.stamp["lldbDap"] else None,
                  str((u / plan.pkg).parent) if plan.pkg else None, plan.sdk, link, built)


def _current(tree: Path, plan: _Plan) -> Shadow | None:
    have = fileio.load_json(tree / ".stamp")
    if not isinstance(have, dict) or {k: v for k, v in have.items() if k != "link"} != plan.stamp:
        return None
    return _shadow(tree, plan, str(have.get("link") or "copy"), False)


def ensure(usr: Path | str, bundle: Path | str, pythonpath: str | None = None, modes: tuple[str, ...] = MODES) -> Shadow:
    """The shadow for the toolchain at `usr` and the darwin SDK `bundle`, built when missing or stale.
    `pythonpath` is what `lldb -P` prints; its lldb package is mirrored. Raises Unavailable."""
    plan = _plan(usr, bundle, pythonpath, modes)
    cache = cache_root()
    tree = cache / hashlib.sha256(str(plan.usr).encode()).hexdigest()[:12]
    if (s := _current(tree, plan)) is not None:
        return s
    try:
        with fileio.lock(cache / ".lock"):
            if (s := _current(tree, plan)) is not None:     # built while this run waited
                return s
            _clear_leftovers(cache)                        # holding the lock, no other build is running
            link = _build(cache, tree, plan)
            fileio.prune((p for p in cache.iterdir() if _KEY.fullmatch(p.name)), KEEP, min_keep=1, protect=[tree])
    except OSError as exc:
        raise Unavailable(f"The debugger's iOS copy couldn't be prepared in {appdirs.tilde(cache)} "
                          f"({exc.strerror or exc}).") from None
    return _shadow(tree, plan, link, True)


def for_lldb(lldb: str | Path, pythonpath: str | None = None) -> tuple[Shadow | None, str]:
    """ensure() for the toolchain of `lldb` (<usr>/bin/lldb) and the installed darwin SDK bundle; (None, reason)
    when there is no shadow. For the session driver (pythonpath from `lldb -P`) and editors (lldb_dap)."""
    bundle = appdirs.sdk_bundle()
    if bundle is None:
        return None, "The iOS SDK isn't installed (Setup installs it)."
    try:
        return ensure(Path(lldb).parent.parent, bundle, pythonpath), ""
    except Unavailable as exc:
        return None, str(exc)


# ---------------------------------------------------------------- building

def _clear_leftovers(cache: Path) -> None:
    for p in cache.glob(".build.*"):
        shutil.rmtree(p, ignore_errors=True)
    for p in cache.glob(".old.*"):
        shutil.rmtree(p, ignore_errors=True)


def _reflink(src: Path, dst: Path) -> None:
    with open(src, "rb") as s, open(dst, "xb") as d:
        fcntl.ioctl(d.fileno(), FICLONE, s.fileno())
    shutil.copystat(src, dst)


def _place(src: Path, dst: Path, modes: tuple[str, ...]) -> str:
    """A real file at dst with src's contents: the first of `modes` that works."""
    for i, mode in enumerate(modes):
        try:
            if mode == "hardlink":
                os.link(src, dst)        # EXDEV across filesystems/subvolumes, EPERM under protected_hardlinks
            elif mode == "reflink":
                _reflink(src, dst)       # EOPNOTSUPP/EXDEV/EINVAL where the filesystem can't share extents
            else:
                shutil.copy2(src, dst)
            return mode
        except OSError:
            with contextlib.suppress(OSError):
                dst.unlink()
            if i == len(modes) - 1:
                raise
    raise ValueError("no link mode")


def _link_rest(src: Path, dst: Path) -> None:
    """Symlink every entry of src (dotfiles too) that dst doesn't have yet."""
    for e in os.scandir(src):
        if not os.path.lexists(dst / e.name):
            os.symlink(e.path, dst / e.name)


def _build(cache: Path, tree: Path, plan: _Plan) -> str:
    cache.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=".build.", dir=cache))
    try:
        u = tmp / "usr"
        (u / "lib/swift").mkdir(parents=True)
        (u / "bin").mkdir()
        tc = plan.usr
        links = [_place(tc / "bin/lldb", u / "bin/lldb", plan.modes)]
        if plan.stamp["lldbDap"]:
            links.append(_place(tc / "bin/lldb-dap", u / "bin/lldb-dap", plan.modes))
        _link_rest(tc / "bin", u / "bin")
        links.append(_place(plan.lib, u / "lib" / plan.lib.name, plan.modes))
        for e in os.scandir(tc / "lib"):
            if e.name == "swift" or e.name == plan.lib.name:
                continue
            if e.name.startswith("liblldb.so") and e.is_symlink():
                os.symlink(plan.lib.name, u / "lib" / e.name)   # liblldb.so -> liblldb.so.21.0.0, inside the shadow
            else:
                os.symlink(e.path, u / "lib" / e.name)
        for e in os.scandir(plan.resources):
            if e.name not in ("host", "linux"):
                os.symlink(e.path, u / "lib/swift" / e.name)
        for name in ("linux", "host"):
            if os.path.lexists(tc / "lib/swift" / name):
                os.symlink(tc / "lib/swift" / name, u / "lib/swift" / name)
        if plan.pkg:
            _mirror_package(tc, u, plan)
        _link_rest(tc, u)
        stamp = {**plan.stamp, "link": "hardlink" if set(links) == {"hardlink"} else
                 ("copy" if "copy" in links else "reflink")}
        fileio.atomic_write(tmp / ".stamp", json.dumps(stamp, indent=1) + "\n")   # last: a complete tree
        if os.path.lexists(tree):
            old = Path(tempfile.mkdtemp(prefix=".old.", dir=cache))
            os.rename(tree, old / "tree")     # an lldb still running from it keeps its mapped files
            shutil.rmtree(old, ignore_errors=True)
        os.rename(tmp, tree)
        return stamp["link"]
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def _mirror_package(tc: Path, u: Path, plan: _Plan) -> None:
    """Real directories down to LLDB's Python package (their other entries symlink to the toolchain), and the
    package's _lldb*.so pointing at the shadow liblldb."""
    pkg = tc / plan.pkg
    sos = sorted(pkg.glob("_lldb*.so"))
    if not any(os.path.realpath(so) == str(plan.lib) for so in sos):
        raise Unavailable("lldb's Python module doesn't belong to this toolchain's liblldb.")
    rel = Path()
    for part in Path(plan.pkg).parts:
        rel /= part
        d = u / rel
        if d.is_symlink():          # linked one level up: replace it with a real directory
            d.unlink()
        d.mkdir(exist_ok=True)
        _link_rest(tc / rel, d)
    for so in sos:
        if os.path.realpath(so) == str(plan.lib):
            (u / plan.pkg / so.name).unlink()
            os.symlink(os.path.relpath(u / "lib" / plan.lib.name, u / plan.pkg), u / plan.pkg / so.name)
