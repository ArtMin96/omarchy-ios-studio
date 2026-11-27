"""Source checkouts Setup builds from: omarchy-apple-dev at its pin, and the xtool that checkout pins."""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from iosstudio.core import appdirs, subproc
from iosstudio.core.appdirs import LOCAL_BIN
from iosstudio.core.output import HelperError, fix

from . import runtime as rt
from .facts import apple_dev_pins, binary_has_old_ua, toolchain_root
from .pins import (
    APPLE_DEV_CACHE,
    APPLE_DEV_HOME,
    APPLE_DEV_LIB,
    APPLE_DEV_REPO,
)
from .runtime import (
    BUILD_FRAC,
    SUBTASK,
    do_mkdir,
    do_rmtree,
    do_symlink,
    log,
    must,
    need_space,
    probe,
    progress,
    run_stream,
)
from .steps import Ctx

SWIFT = Path("/usr/bin/swift")   # swift-bin's driver; xtool is built with the toolchain it points to


def git(args: list, step_id: str, cwd: str | Path | None = None, mutating: bool = True, tail: list | None = None) -> int:
    """A streamed git command (never prompts for credentials)."""
    return run_stream(["git", *args], step_id, mutating=mutating, cwd=cwd, tail=tail,
                      env=subproc.tool_env("setup", {"GIT_TERMINAL_PROMPT": "0"}))


def i_apple_dev(ctx: Ctx, step_id: str) -> Path:
    """Check out omarchy-apple-dev at the pinned commit (ctx.pins). A checkout found elsewhere (appleDevDir) or already at the
    pin is kept as is unless --force; our own checkout at an older commit is moved to the pin."""
    ad = ctx.c.get("apple_dev")
    if ad and not ctx.c.get("apple_dev_outdated") and not ctx.flags.get("force"):
        return ad
    d = APPLE_DEV_HOME
    if d.exists() and not (d / ".git").is_dir():
        raise HelperError("apple-dev-path", f"{appdirs.tilde(d)} exists but isn't a download of the iOS build scripts. "
                          "Move it away, then try again.")
    do_mkdir(d.parent, step_id)
    commit = ctx.pins.apple_dev_commit
    tail: list = []
    if not (d / ".git").is_dir():
        must(git(["init", "-q", str(d)], step_id), "git", "Couldn't prepare the iOS build scripts folder.", step_id)
        must(git(["-C", str(d), "remote", "add", "origin", APPLE_DEV_REPO], step_id), "git", "Couldn't prepare the iOS build scripts folder.", step_id)
    progress(step_id, 0.3, "Downloading the iOS build scripts")
    must(git(["-C", str(d), "fetch", "-q", "--depth", "1", APPLE_DEV_REPO, commit], step_id, tail=tail),
         "network", "Couldn't download the iOS build scripts from GitHub. Check your internet connection, then try again.", step_id, tail)
    must(git(["-C", str(d), "checkout", "-q", "--detach", commit], step_id, tail=tail),
         "git", "Couldn't check out the iOS build scripts.", step_id, tail)
    if not rt.DRY:
        rc, out, _ = probe(["git", "-C", d, "rev-parse", "HEAD"])
        if out.strip() != commit:
            raise HelperError("git", "The iOS build scripts aren't at the expected version after download.", detail=out)
        ctx.c["apple_dev"], ctx.c["apple_dev_outdated"] = d, False
    return d


def apple_dev_checkout(ctx: Ctx, step_id: str) -> Path:
    """The checkout to build from: downloaded, or moved to the pin, first when it needs that (never re-fetched)."""
    ad = ctx.c.get("apple_dev")
    return ad if ad and not ctx.c.get("apple_dev_outdated") else i_apple_dev(ctx, step_id)


def i_xtool(ctx: Ctx, step_id: str) -> None:
    """Build the xtool omarchy-apple-dev pins, as its install-toolchain.sh does (same folders), and install it
    with its Swift runtime. Every pin since v0.4.8 has the Apple sign-in fix; the binary is checked for it."""
    for dep in ("packages", "swift"):
        if ctx.state(dep) not in ("done",) and not rt.DRY:
            t = ctx.by_id[dep]["title"]
            raise HelperError("needs-step", f"Install “{t}” first.", fix(f"Install {t}", "setup", step=dep))
    ad = apple_dev_checkout(ctx, step_id)
    pins = apple_dev_pins(ad if ad and ad.exists() else None)
    sha, repo = pins["XTOOL_SHA"], pins["XTOOL_REPO"]
    name = f"xtool-{sha}"
    dest, src, link = APPLE_DEV_LIB / name, APPLE_DEV_CACHE / name, LOCAL_BIN / "xtool"

    def complete(d: Path) -> bool:
        return os.access(d / "xtool", os.X_OK) and (d / "libswiftCore.so").exists()

    if complete(dest) and binary_has_old_ua(dest / "xtool") is False and not ctx.flags.get("force"):
        rc, _, _ = probe([dest / "xtool", "--version"])
        if rc == 0:
            log(step_id, f"xtool {sha[:7]} is already built: {appdirs.tilde(dest)}")
            do_symlink(dest / "xtool", link, step_id)
            if not ctx.keep_previous:          # an update prunes once it checks out (channel.backup)
                prune_old_xtool(name, step_id)
            return
    need_space(APPLE_DEV_CACHE, 3_000_000_000, "building xtool")
    progress(step_id, 0.02, "Downloading xtool's source code")
    do_mkdir(src, step_id)
    tail: list = []
    if not (src / ".git").is_dir():
        must(git(["init", "-q", str(src)], step_id), "git", "Couldn't prepare xtool's source folder.", step_id)
    if rt.DRY or probe(["git", "-C", src, "cat-file", "-e", f"{sha}^{{commit}}"])[0] != 0:
        must(git(["-C", str(src), "fetch", "-q", "--depth", "1", repo, sha], step_id, tail=tail), "network",
             "Couldn't download xtool's source code. Check your internet connection, then try again.", step_id, tail)
    must(git(["-C", str(src), "checkout", "-q", "-f", "--detach", sha], step_id, tail=tail), "git",
         "Couldn't prepare xtool's source.", step_id, tail)
    ncpu = os.cpu_count() or 4
    est = max(300, min(1500, 7 * 60 * 16 / ncpu))
    swift_real = os.path.realpath(SWIFT) if SWIFT.exists() else str(SWIFT)
    benv = subproc.tool_env("setup", {"PATH": f"{os.path.dirname(swift_real)}:/usr/bin:{LOCAL_BIN}:/bin"})
    tail = []
    rc = run_stream(["swift", "build", "-c", "release", "--product", "xtool", "-Xswiftc", "-no-toolchain-stdlib-rpath"],
                    step_id, label="Compiling xtool", estimate=est, env=benv, cwd=src, parse=xtool_build_phase,
                    frac_range=(0.05, 0.92), lead=XTOOL_FETCH_SHARE, tail=tail)
    must(rc, "build-failed", "xtool didn't compile. This usually means Swift and xtool versions don't match; "
         "repairing Swift first can help.", step_id, tail)
    if rt.DRY:
        log(step_id, f"would install the built xtool and its Swift runtime into {dest}, link {link}, "
                     "then delete older xtool builds")
        return
    progress(step_id, 0.94, "Installing xtool")
    rc, binp, _ = probe(["swift", "build", "-c", "release", "--show-bin-path"], env=benv, cwd=src, timeout=120)
    binp = Path(binp.strip())
    root = toolchain_root() or Path("/usr/lib/swift")
    runtime = root / "usr/lib/swift/linux"
    stage = dest.with_name(dest.name + ".partial")
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)
    shutil.copy2(binp / "xtool", stage / "xtool")
    os.chmod(stage / "xtool", 0o755)
    rc, ldd, _ = probe(["ldd", stage / "xtool"], env={**benv, "LD_LIBRARY_PATH": str(runtime)})
    for line in ldd.splitlines():
        m = re.search(r"=>\s*(\S+)", line)
        if m and m.group(1).startswith(str(runtime) + "/"):
            shutil.copy2(m.group(1), stage / Path(m.group(1)).name)
            os.chmod(stage / Path(m.group(1)).name, 0o644)
    if not complete(stage):
        shutil.rmtree(stage, ignore_errors=True)
        raise HelperError("build-failed", "The new xtool build is incomplete (its Swift runtime is missing).")
    if binary_has_old_ua(stage / "xtool") is not False:
        shutil.rmtree(stage, ignore_errors=True)
        raise HelperError("xtool-no-fix", f"xtool {sha[:7]}, the version the iOS build scripts ask for, can't sign in "
                          "to Apple (it predates the fix for HTTP 503). Update the iOS build scripts, then try again.",
                          fix("Update the iOS build scripts", "setup", step="apple-dev"))
    shutil.rmtree(dest, ignore_errors=True)
    os.replace(stage, dest)
    do_symlink(dest / "xtool", link, step_id)
    rc, out, _ = probe([link, "--version"])
    must(rc, "build-failed", "The new xtool doesn't start.", step_id, [out])
    log(step_id, f"Installed {appdirs.tilde(dest)} ({out.strip()}, built from {sha[:7]})")
    if not ctx.keep_previous:
        prune_old_xtool(name, step_id)


XTOOL_FETCH_SHARE = 0.25   # of the build's bar: fetching packages and planning, before swift build counts tasks
FETCH_LINES = ("Fetching ", "Fetched ", "Computing version for ", "Creating working copy ", "Working copy of ",
               "Downloading ", "Downloaded ")


def xtool_build_phase(line: str, st: dict) -> None:
    """Name the phase of xtool's swift build in the progress detail; run_stream reads the numbers ([n / m] tasks)."""
    if line.startswith(FETCH_LINES) or SUBTASK.match(line):
        st["detail"] = "Downloading xtool's packages"
    elif line.startswith(("Building for production", "[Computing dependencies", "[Planning", "[Constructing")):
        st["detail"] = "Planning the build"
    elif BUILD_FRAC.match(line):
        st["detail"] = "Compiling xtool"


def prune_old_xtool(keep: str, step_id: str) -> None:
    """Once the xtool in `keep` works: delete every other build and source tree, either layout (xtool-<sha> and
    the older xtool-<sha7>-gsa406), so an update doesn't leave 2 GB behind."""
    for root in (APPLE_DEV_LIB, APPLE_DEV_CACHE):
        for p in sorted(root.glob("xtool-*")):
            if p.name != keep and p.is_dir() and not p.is_symlink():
                do_rmtree(p, step_id)
