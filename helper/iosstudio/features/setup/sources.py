"""Source checkouts Setup builds from: omarchy-apple-dev at its pin, and xtool with the Apple sign-in fix."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from iosstudio.core import appdirs, subproc
from iosstudio.core.appdirs import LOCAL_BIN
from iosstudio.core.output import HelperError, fix

from . import runtime as rt
from .facts import apple_dev_pins, binary_has_old_ua, toolchain_root
from .pins import (
    APPLE_DEV_CACHE,
    APPLE_DEV_COMMIT,
    APPLE_DEV_HOME,
    APPLE_DEV_LIB,
    APPLE_DEV_REPO,
    XTOOL_FIX_REPO,
    XTOOL_FIX_SHA,
    XTOOL_KNOWN_BASE,
    XTOOL_KNOWN_TREE,
)
from .runtime import do_mkdir, do_symlink, log, must, need_space, probe, progress, run_stream
from .steps import Ctx

GIT_TIMEOUT = 60


def git(args: list, step_id: str, cwd: str | Path | None = None, mutating: bool = True, tail: list | None = None) -> int:
    """A streamed git command (never prompts for credentials)."""
    return run_stream(["git", *args], step_id, mutating=mutating, cwd=cwd, tail=tail,
                      env=subproc.tool_env("setup", {"GIT_TERMINAL_PROMPT": "0"}))


def i_apple_dev(ctx: Ctx, step_id: str) -> Path:
    """Check out omarchy-apple-dev at the pinned commit (kept as is unless --force)."""
    ad = ctx.c.get("apple_dev")
    if ad and not ctx.flags.get("force"):
        return ad
    d = APPLE_DEV_HOME
    if d.exists() and not (d / ".git").is_dir():
        raise HelperError("apple-dev-path", f"{appdirs.tilde(d)} exists but isn't a download of the iOS build scripts. "
                          "Move it away, then try again.")
    do_mkdir(d.parent, step_id)
    tail: list = []
    if not (d / ".git").is_dir():
        must(git(["init", "-q", str(d)], step_id), "git", "Couldn't prepare the iOS build scripts folder.", step_id)
        must(git(["-C", str(d), "remote", "add", "origin", APPLE_DEV_REPO], step_id), "git", "Couldn't prepare the iOS build scripts folder.", step_id)
    progress(step_id, 0.3, "Downloading the iOS build scripts")
    must(git(["-C", str(d), "fetch", "-q", "--depth", "1", APPLE_DEV_REPO, APPLE_DEV_COMMIT], step_id, tail=tail),
         "network", "Couldn't download the iOS build scripts from GitHub. Check your internet connection, then try again.", step_id, tail)
    must(git(["-C", str(d), "checkout", "-q", "--detach", APPLE_DEV_COMMIT], step_id, tail=tail),
         "git", "Couldn't check out the iOS build scripts.", step_id, tail)
    if not rt.DRY:
        rc, out, _ = probe(["git", "-C", d, "rev-parse", "HEAD"])
        if out.strip() != APPLE_DEV_COMMIT:
            raise HelperError("git", "The iOS build scripts aren't at the expected version after download.", detail=out)
    return d


def i_xtool(ctx: Ctx, step_id: str) -> None:
    """Build the pinned xtool with the Apple sign-in fix (PR #406) and install it with its Swift runtime."""
    for dep in ("packages", "swift"):
        if ctx.state(dep) not in ("done",) and not rt.DRY:
            t = ctx.by_id[dep]["title"]
            raise HelperError("needs-step", f"Install “{t}” first.", fix(f"Install {t}", "setup", step=dep))
    ad = ctx.c.get("apple_dev") or i_apple_dev(ctx, step_id)
    pins = apple_dev_pins(ad if ad and ad.exists() else None)
    sha, repo = pins["XTOOL_SHA"], pins["XTOOL_REPO"]
    fix_dir = APPLE_DEV_LIB / f"xtool-{sha[:7]}-gsa406"
    pin_dir = APPLE_DEV_LIB / f"xtool-{sha}"
    src = APPLE_DEV_CACHE / f"xtool-{sha[:7]}-gsa406"
    link = LOCAL_BIN / "xtool"

    def complete(d: Path) -> bool:
        return os.access(d / "xtool", os.X_OK) and (d / "libswiftCore.so").exists()

    for cand in (pin_dir, fix_dir):
        if complete(cand) and binary_has_old_ua(cand / "xtool") is False and not ctx.flags.get("force"):
            rc, _, _ = probe([cand / "xtool", "--version"])
            if rc == 0:
                log(step_id, f"A working xtool with the sign-in fix is already built: {appdirs.tilde(cand)}")
                do_symlink(cand / "xtool", link, step_id)
                return
    need_space(APPLE_DEV_CACHE, 3_000_000_000, "building xtool")
    progress(step_id, 0.02, "Downloading xtool's source code")
    do_mkdir(src, step_id)
    tail: list = []
    if not (src / ".git").is_dir():
        must(git(["init", "-q", str(src)], step_id), "git", "Couldn't prepare xtool's source folder.", step_id)

    def have(c: str) -> bool:
        return not rt.DRY and probe(["git", "-C", src, "cat-file", "-e", f"{c}^{{commit}}"])[0] == 0
    if not have(sha):
        must(git(["-C", str(src), "fetch", "-q", "--depth", "1", repo, sha], step_id, tail=tail), "network",
             "Couldn't download xtool's source code. Check your internet connection, then try again.", step_id, tail)
    if not have(XTOOL_FIX_SHA + "^"):
        must(git(["-C", str(src), "fetch", "-q", "--depth", "2", XTOOL_FIX_REPO, XTOOL_FIX_SHA], step_id, tail=tail), "network",
             "Couldn't download the xtool sign-in fix. Check your internet connection, then try again.", step_id, tail)
    progress(step_id, 0.05, "Applying the Apple sign-in fix")
    build_commit = sha
    dest = fix_dir
    if not rt.DRY:
        if base_has_fix(src, sha):
            log(step_id, f"xtool {sha[:7]} already contains the sign-in fix; building it as is")
            dest = pin_dir
            must(git(["-C", str(src), "checkout", "-q", "-f", "--detach", sha], step_id), "git", "Couldn't prepare xtool's source.", step_id)
        elif not fix_source_ok(src, sha):
            must(git(["-C", str(src), "checkout", "-q", "-f", "--detach", sha], step_id), "git", "Couldn't prepare xtool's source.", step_id)
            rc, date, _ = probe(["git", "-C", src, "log", "-1", "--format=%aI", XTOOL_FIX_SHA])
            env = subproc.tool_env("setup", {"GIT_COMMITTER_NAME": "iOS Studio", "GIT_COMMITTER_EMAIL": "setup@ios-studio.invalid",
                            "GIT_COMMITTER_DATE": date.strip()})
            rc = run_stream(["git", "-C", str(src), "-c", "commit.gpgsign=false", "cherry-pick", XTOOL_FIX_SHA], step_id, env=env, tail=tail)
            must(rc, "git", "Couldn't apply the xtool sign-in fix.", step_id, tail)
            if not fix_source_ok(src, sha):
                raise HelperError("git", "xtool's source doesn't match the expected fix after applying it.")
        build_commit = probe(["git", "-C", src, "rev-parse", "HEAD"])[1].strip()
    else:
        log(step_id, f"would cherry-pick xtool-org/xtool {XTOOL_FIX_SHA[:7]} onto {sha[:7]} in {src}")
    ncpu = os.cpu_count() or 4
    est = max(300, min(1500, 7 * 60 * 16 / ncpu))
    swift_real = os.path.realpath("/usr/bin/swift") if Path("/usr/bin/swift").exists() else "/usr/bin/swift"
    benv = subproc.tool_env("setup", {"PATH": f"{os.path.dirname(swift_real)}:/usr/bin:{LOCAL_BIN}:/bin"})
    tail = []
    rc = run_stream(["swift", "build", "-c", "release", "--product", "xtool", "-Xswiftc", "-no-toolchain-stdlib-rpath"],
                    step_id, label="Compiling xtool", estimate=est, env=benv, cwd=src, frac_range=(0.08, 0.92), tail=tail)
    must(rc, "build-failed", "xtool didn't compile. This usually means Swift and xtool versions don't match; "
         "repairing Swift first can help.", step_id, tail)
    if rt.DRY:
        log(step_id, f"would install the built xtool and its Swift runtime into {dest}, then link {link}")
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
    rc, ldd, _ = probe(["ldd", stage / "xtool"], env=subproc.tool_env("setup", {"LD_LIBRARY_PATH": str(runtime)}))
    for line in ldd.splitlines():
        m = re.search(r"=>\s*(\S+)", line)
        if m and m.group(1).startswith(str(runtime) + "/"):
            shutil.copy2(m.group(1), stage / Path(m.group(1)).name)
            os.chmod(stage / Path(m.group(1)).name, 0o644)
    if not complete(stage) or binary_has_old_ua(stage / "xtool") is not False:
        raise HelperError("build-failed", "The new xtool build is incomplete or still lacks the sign-in fix.")
    shutil.rmtree(dest, ignore_errors=True)
    os.replace(stage, dest)
    do_symlink(dest / "xtool", link, step_id)
    rc, out, _ = probe([link, "--version"])
    must(rc, "build-failed", "The new xtool doesn't start.", step_id, [out])
    log(step_id, f"Installed {appdirs.tilde(dest)} ({out.strip()}, built from {build_commit[:7]})")


def git_capture(src: Path, *args: str, input: bytes | None = None,
                env: dict | None = None) -> subprocess.CompletedProcess:
    """A local git command on the xtool checkout, bounded (a locked or huge repo can't hang setup)."""
    try:
        return subprocess.run(["git", "-C", str(src), *args], input=input, env=env, capture_output=True,
                              timeout=GIT_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise HelperError("timeout", f"git {args[0]} took longer than {GIT_TIMEOUT} s in {appdirs.tilde(src)}.",
                          fix("Try again", "setup", step="xtool"))


def base_has_fix(src: Path, sha: str) -> bool:
    """Does xtool at `sha` already contain the sign-in fix (its diff reverse-applies cleanly)?"""
    fd, index = tempfile.mkstemp()
    os.close(fd)
    try:
        env = subproc.tool_env("setup", {"GIT_INDEX_FILE": index})
        if git_capture(src, "read-tree", sha, env=env).returncode != 0:
            return False
        diff = git_capture(src, "diff", f"{XTOOL_FIX_SHA}^", XTOOL_FIX_SHA).stdout
        r = git_capture(src, "apply", "--cached", "--reverse", "--check", input=diff, env=env)
        return r.returncode == 0
    finally:
        os.unlink(index)


def git_patch_id(src: Path, a: str, b: str) -> str:
    diff = git_capture(src, "diff", a, b).stdout
    r = git_capture(src, "patch-id", "--stable", input=diff)
    return (r.stdout.decode().split() or [""])[0]


def fix_source_ok(src: Path, sha: str) -> bool:
    """Is the checkout exactly `sha` plus the sign-in fix, with nothing else changed?"""
    if probe(["git", "-C", src, "rev-parse", "-q", "--verify", "HEAD^"])[1].strip() != sha:
        return False
    if probe(["git", "-C", src, "diff", "--quiet", "HEAD"])[0] != 0:
        return False
    if sha == XTOOL_KNOWN_BASE and probe(["git", "-C", src, "rev-parse", "HEAD^{tree}"])[1].strip() != XTOOL_KNOWN_TREE:
        return False
    return git_patch_id(src, sha, "HEAD") == git_patch_id(src, f"{XTOOL_FIX_SHA}^", XTOOL_FIX_SHA)
