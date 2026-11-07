"""Installers: one function per Setup step, the root (pkexec) bundle, and `setup install` itself."""
from __future__ import annotations

import os
import re
import shutil
import tarfile
import tempfile
import time
from pathlib import Path

from iosstudio.core import appdirs, fileio, output, subproc
from iosstudio.core.appdirs import HOME, LOCAL_BIN, REPO, VENV_DIR
from iosstudio.core.output import HelperError, fix

from . import runtime as rt
from .downloads import aur_fetch, download, prefetch_swift_sources, srcinfo, strip_ver
from .facts import arch, is_omarchy, sdk_ios_versions, swift_hold_hook_present
from .pins import (
    HOOK_SRC,
    IPSW_SHA,
    IPSW_VERSION,
    LEGACY_VENV,
    OMARCHY_HOOK,
    PACKAGES,
    PYMD3_VERSION,
    RCODESIGN_SHA,
    RCODESIGN_VERSION,
    SDK_CACHE,
    SWIFT_AUR_COMMIT,
    UXPLAY_RUNTIME,
    XCSPEC_PY,
)
from .runtime import (
    confirm,
    do_mkdir,
    do_rmtree,
    do_symlink,
    do_unlink,
    log,
    must,
    need_space,
    probe,
    progress,
    run_root,
    run_stream,
)
from .sources import i_apple_dev, i_xtool
from .steps import Ctx, summarize
from .xip import xip_quick, xip_read_version


def i_root_bundle(ctx: Ctx, ids: list[str], step_id: str, label: str) -> None:
    """Collect the root work of several steps and run it with one password prompt."""
    args: list[str] = []
    a = arch()
    if "packages" in ids:
        miss = ctx.c.get("missing_pkgs") or []
        if miss or ctx.flags.get("force"):
            args += ["--packages", " ".join(miss or PACKAGES)]
    if "swift" in ids and (ctx.state("swift") != "done" or ctx.flags.get("force")):
        d = aur_fetch("swift-bin", SWIFT_AUR_COMMIT, step_id)
        info = srcinfo(d) if not rt.DRY else {}
        mk = sorted({strip_ver(x) for x in info.get("makedepends", []) + info.get(f"makedepends_{a}", [])} | {"base-devel"})
        if rt.DRY:
            mk = ["base-devel", "patchelf"]
        need_space(HOME, 5_500_000_000, "Swift")
        prefetch_swift_sources(d, step_id) if not rt.DRY else log(step_id, "would download the Swift toolchain (about 1 GB) from swift.org")
        args += ["--deps", " ".join(mk), "--aur-build", str(d)]
        ids = list(ids) + ["swift-hold", "xcspec"]
    if "swift-hold" in ids and ("swift-bin" not in ctx.F["ignore"][1].split() or ctx.flags.get("force")):
        args += ["--hold-swift"]
    if "xcspec" in ids and (ctx.state("xcspec") != "done" or ctx.flags.get("force") or "--aur-build" in args):
        args += ["--xcspec", str(XCSPEC_PY)]
    if "usbmuxd" in ids and ctx.state("usbmuxd") != "done":
        args += ["--usbmuxd-dropin", "--udev-trigger"]
    if "uxplay" in ids:
        if not shutil.which("uxplay") or ctx.flags.get("force"):
            d = aur_fetch("uxplay", None, step_id)
            info = srcinfo(d) if not rt.DRY else {}
            deps = sorted({strip_ver(x) for x in info.get("depends", []) + info.get("makedepends", []) +
                           info.get(f"depends_{a}", []) + info.get(f"makedepends_{a}", [])} | {"base-devel"})
            args += ["--deps", " ".join(deps), "--aur-build", str(d)]
        ux_miss = UXPLAY_RUNTIME if rt.DRY else [x for x in UXPLAY_RUNTIME if probe(["pacman", "-T", x])[0] != 0]
        if ux_miss:
            if "--packages" in args:
                i = args.index("--packages") + 1
                args[i] = " ".join(dict.fromkeys(args[i].split() + ux_miss))
            else:
                args += ["--packages", " ".join(ux_miss)]
        args += ["--avahi", "--ufw-uxplay"]
    if "wireshark" in ids and (not shutil.which("wireshark") or ctx.flags.get("force")):
        if "--packages" in args:   # one --packages flag: root-tasks.sh keeps only the last one
            i = args.index("--packages")
            args[i + 1] += " wireshark-qt"
        else:
            args += ["--packages", "wireshark-qt"]
    if "debugger" in ids:
        pkg = ctx.by_id.get("debugger", {}).get("pkg")
        if pkg and (ctx.state("debugger") != "done" or ctx.flags.get("force")):
            d = aur_fetch(pkg, None, step_id)
            info = srcinfo(d) if not rt.DRY else {}
            deps = sorted({strip_ver(x) for x in info.get("depends", []) + info.get("makedepends", [])} | {"base-devel"})
            args += ["--deps", " ".join(deps), "--aur-build", str(d)]
    if not args:
        log(step_id, "Nothing needs your password here.")
        return
    run_root(args, step_id, label)


def i_swift_hook(ctx: Ctx, step_id: str) -> None:
    if not is_omarchy() or swift_hold_hook_present():
        return
    if rt.DRY:
        log(step_id, f"would install the Omarchy hook {OMARCHY_HOOK}")
        return
    OMARCHY_HOOK.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(HOOK_SRC, OMARCHY_HOOK)
    os.chmod(OMARCHY_HOOK, 0o755)
    log(step_id, f"installed the Omarchy hook {appdirs.tilde(OMARCHY_HOOK)} (keeps the hold through “omarchy refresh pacman”)")


def i_rcodesign(ctx: Ctx, step_id: str) -> None:
    a = arch()
    sha = RCODESIGN_SHA.get(a)
    if not sha:
        raise HelperError("unsupported", f"rcodesign has no build for {a}.")
    name = f"apple-codesign-{RCODESIGN_VERSION}-{a}-unknown-linux-musl"
    url = f"https://github.com/indygreg/apple-platform-rs/releases/download/apple-codesign%2F{RCODESIGN_VERSION}/{name}.tar.gz"
    _install_tarball_binary(url, sha, f"{name}/rcodesign", LOCAL_BIN / "rcodesign", step_id, "rcodesign")


def i_ipsw(ctx: Ctx, step_id: str) -> None:
    a = arch()
    if a not in IPSW_SHA:
        raise HelperError("unsupported", f"ipsw has no build for {a}.")
    ia, sha = IPSW_SHA[a]
    url = f"https://github.com/blacktop/ipsw/releases/download/v{IPSW_VERSION}/ipsw_{IPSW_VERSION}_linux_{ia}.tar.gz"
    _install_tarball_binary(url, sha, "ipsw", LOCAL_BIN / "ipsw", step_id, "ipsw")


def _install_tarball_binary(url: str, sha: str, member: str, dest: Path, step_id: str, label: str) -> None:
    if rt.DRY:
        download(url, Path("/nonexistent"), step_id, sha256=sha, label=label)
        log(step_id, f"would install {member} -> {dest}")
        return
    with tempfile.TemporaryDirectory(prefix="ios-studio-") as td:
        tgz = download(url, Path(td) / "pkg.tar.gz", step_id, sha256=sha, label=label, frac_range=(0, 0.9))
        with tarfile.open(tgz) as tf:
            try:
                m = tf.getmember(member)
            except KeyError:
                raise HelperError("bad-download", f"The {label} download doesn't contain the program.")
            f = tf.extractfile(m)
            with fileio.replacing(dest, 0o755) as tmp, open(tmp, "xb") as out:
                shutil.copyfileobj(f, out)
    log(step_id, f"installed {appdirs.tilde(dest)}")


def i_pymd3(ctx: Ctx, step_id: str) -> None:
    py = "/usr/bin/python3"
    if not Path(py).exists():
        raise HelperError("needs-step", "Install “System packages” first.", fix("Install System packages", "setup", step="packages"))
    venv = VENV_DIR
    if venv.exists():
        do_rmtree(venv, step_id)
    do_mkdir(venv.parent, step_id)
    progress(step_id, 0.05, "Creating a private Python environment")
    tail: list = []
    must(run_stream([py, "-m", "venv", str(venv)], step_id, tail=tail), "venv", "Couldn't create the Python environment.", step_id, tail)
    progress(step_id, 0.15, f"Downloading pymobiledevice3 {PYMD3_VERSION}")

    def parse(line, st):
        if line.startswith(("Collecting", "Downloading")):
            st["detail"] = line.split(" (")[0][:80]
        if line.startswith("Installing collected"):
            st["detail"] = "Installing"
        return None
    rc = run_stream([venv / "bin/pip", "install", "--disable-pip-version-check", "--no-input", "--progress-bar", "off",
                     f"pymobiledevice3=={PYMD3_VERSION}"], step_id, label="Installing pymobiledevice3", estimate=90,
                    parse=parse, frac_range=(0.15, 0.97), tail=tail)
    must(rc, "network", "Couldn't install pymobiledevice3. Check your internet connection, then try again.", step_id, tail)
    # omarchy-apple-dev's scripts (device-run.sh, install-toolchain.sh --repair) look for ~/pymobile3-venv.
    if not os.path.lexists(LEGACY_VENV):
        do_symlink(venv, LEGACY_VENV, step_id)
    if not rt.DRY:
        rc, out, err = probe([venv / "bin/python", "-I", "-c", "import pymobiledevice3, cryptography"], timeout=30)
        must(rc, "venv", "pymobiledevice3 was installed but doesn't load.", step_id, [err])


def ensure_legacy_venv(ctx: Ctx, step_id: str) -> None:
    """install-toolchain.sh --repair runs venv_ensure on ~/pymobile3-venv; make sure it is healthy so it
    never pip-installs a second, unpinned copy."""
    best = ctx.c.get("pymd3")
    if os.path.lexists(LEGACY_VENV):
        ok_ = probe([LEGACY_VENV / "bin/python", "-I", "-c", "import cryptography, pymobiledevice3"], timeout=20)[0] == 0
        if ok_:
            return
        if LEGACY_VENV.is_symlink():
            do_unlink(LEGACY_VENV, step_id)
        else:
            raise HelperError("venv", "The older Python environment at ~/pymobile3-venv is broken. Repair "
                              "“iPhone connection tools” first.", fix("Repair", "setup", step="pymobiledevice3"))
    if not (best and best[0].startswith(str(VENV_DIR))):
        i_pymd3(ctx, step_id)
    else:
        do_symlink(VENV_DIR, LEGACY_VENV, step_id)


def i_sdk(ctx: Ctx, step_id: str) -> None:
    for dep in ("xtool", "pymobiledevice3"):
        if ctx.state(dep) != "done" and not rt.DRY:
            t = ctx.by_id[dep]["title"]
            raise HelperError("needs-step", f"Install “{t}” first.", fix(f"Install {t}", "setup", step=dep))
    ad = ctx.c.get("apple_dev") or i_apple_dev(ctx, step_id)
    major = ctx.c["xcode_major"]
    cache = ctx.c.get("good_cache")
    xip_path = ctx.flags.get("xip")
    xt = str(LOCAL_BIN / "xtool")
    benv = subproc.tool_env("setup", {"PATH": f"{os.path.dirname(os.path.realpath('/usr/bin/swift'))}:/usr/bin:{LOCAL_BIN}:/bin"})
    tail: list = []
    if not cache or ctx.flags.get("rebuild"):
        xip = Path(xip_path) if xip_path else (Path(ctx.c["xip"]["path"]) if ctx.c.get("xip") and ctx.c["xip"].get("valid") else None)
        if not xip:
            raise HelperError("needs-xcode", f"Download Xcode {ctx.c['xcode']} first; iOS Studio builds the SDK from it.",
                              fix("Get Xcode", "setup", step="xcode"))
        info = xip_quick(xip)
        if not info.get("valid"):
            raise HelperError("bad-xip", info.get("message", "That Xcode file can't be used."), fix("Get Xcode", "setup", step="xcode"))
        if not info.get("version") and not rt.DRY:
            v, b = xip_read_version(xip, step_id)
            info["version"] = v
        if info.get("version") and info["version"].split(".")[0] != major:
            raise HelperError("xcode-mismatch", f"That file is Xcode {info['version']}, but your Swift needs Xcode {ctx.c['xcode']}.",
                              fix("Get Xcode", "setup", step="xcode"))
        need_space(SDK_CACHE, 15_000_000_000, "building the iOS SDK")
        confirm(f"Build the iOS SDK from {xip.name}? It takes about 20–30 minutes and needs about 15 GB of free space "
                f"while it works (about 7 GB stays). You can keep using your computer meanwhile.", "Build SDK", ctx.flags)
        do_mkdir(SDK_CACHE, step_id)
        tmpd = Path(tempfile.mkdtemp(prefix=".build.", dir=SDK_CACHE)) if not rt.DRY else SDK_CACHE / ".build.XXXX"

        def parse(line, st):
            m = re.match(r"^\[([^\]]+)\]\s*(\d+(?:\.\d+)?)%", line)
            if m:
                st["detail"] = f"Unpacking Xcode: {m.group(1)}"
                st["frac"] = float(m.group(2)) / 100
                return "quiet"
            return None
        rc = run_stream([xt, "sdk", "build", str(xip), str(tmpd)], step_id, label="Unpacking Xcode and building the SDK",
                        estimate=900, env=benv, parse=parse, frac_range=(0.03, 0.6), tail=tail)
        must(rc, "sdk-build", "Building the SDK from Xcode failed. The log has the details.", step_id, tail)
        if not rt.DRY:
            built = tmpd / "darwin.xtoolsdk"
            vers = sdk_ios_versions(built)
            ver = f"iPhoneOS{vers[-1]}" if vers else "unknown"
            cached = SDK_CACHE / f"darwin-{ver}.xtoolsdk"
            if cached.exists():
                shutil.rmtree(cached)
            os.replace(built, cached)
            shutil.rmtree(tmpd, ignore_errors=True)
            plist = Path(str(cached) + ".version.plist")
            try:
                from iosstudio.features import xip_version
                data, _v, _b = xip_version.read_version(str(xip))
                plist.write_bytes(data)
            except Exception:  # noqa: BLE001
                log(step_id, "couldn't record the Xcode version next to the SDK (only matters for App Store uploads)")
            log(step_id, f"SDK cache kept at {appdirs.tilde(cached)}")
            cache = {"path": str(cached)}
        else:
            cache = {"path": str(SDK_CACHE / f"darwin-iPhoneOS{ctx.c['xcode']}.xtoolsdk")}
    # --repair re-registers the newest current cache and (re)installs the darwin tools and the macro server.
    if not rt.DRY:
        os.utime(cache["path"])  # make ours the newest so --repair picks it
    ensure_legacy_venv(ctx, step_id)
    progress(step_id, 0.62, "Registering the SDK and building its extras")

    def parse2(line, st):
        if "Building OpenAppleMacrosServer" in line:
            st["detail"], st["frac"] = "Building the Swift macro server (about 5 min, quiet)", None
        elif line.startswith("== ") or "sdk install" in line:
            st["detail"] = line.strip("= ")[:80]
        return None
    rc = run_stream([str(Path(ad) / "install-toolchain.sh"), "--mode", "full", "--repair"], step_id,
                    label="Registering the SDK and building its extras", estimate=420, env=benv, cwd=str(ad),
                    parse=parse2, frac_range=(0.62, 0.99), tail=tail)
    must(rc, "sdk-install", "Installing the SDK failed. The log has the details.", step_id, tail)


def i_app_integration(ctx: Ctx, step_id: str) -> None:
    inst = REPO / "install.sh"
    if not inst.exists():
        raise HelperError("missing", "The app's installer (install.sh) is missing from this copy of iOS Studio.")
    must(run_stream(["bash", str(inst)], step_id), "install", "Adding iOS Studio to the launcher failed.", step_id)


INSTALLERS = {
    "packages": lambda c, s: i_root_bundle(c, ["packages"], s, "Installing system packages"),
    "usbmuxd": lambda c, s: i_root_bundle(c, ["usbmuxd"], s, "Configuring the USB service"),
    "swift": lambda c, s: (i_root_bundle(c, ["packages", "swift", "swift-hold", "xcspec"], s, "Installing Swift"), i_swift_hook(c, s)),
    "swift-hold": lambda c, s: (i_root_bundle(c, ["swift-hold"], s, "Holding Swift"), i_swift_hook(c, s)),
    "xcspec": lambda c, s: i_root_bundle(c, ["xcspec"], s, "Fixing Swift's build settings"),
    "apple-dev": lambda c, s: i_apple_dev(c, s),
    "xtool": i_xtool,
    "rcodesign": i_rcodesign,
    "ipsw": i_ipsw,
    "pymobiledevice3": i_pymd3,
    "sdk": i_sdk,
    "uxplay": lambda c, s: i_root_bundle(c, ["uxplay"], s, "Installing UxPlay"),
    "debugger": lambda c, s: i_root_bundle(c, ["debugger"], s, "Installing debugger support"),
    "wireshark": lambda c, s: i_root_bundle(c, ["wireshark"], s, "Installing Wireshark"),
    "app-integration": i_app_integration,
}

ORDER = ["packages", "usbmuxd", "swift", "swift-hold", "xcspec", "apple-dev", "xtool", "rcodesign", "pymobiledevice3",
         "ipsw", "debugger", "sdk", "uxplay", "wireshark", "app-integration"]
ROOT_STEPS = {"packages", "usbmuxd", "swift", "swift-hold", "xcspec", "uxplay", "wireshark", "debugger"}
OPTIONAL_IN_ALL = {"ipsw", "debugger", "uxplay", "wireshark"}

def _run_one(ctx: Ctx, sid: str) -> None:
    s = ctx.by_id.get(sid)
    title = s["title"] if s else sid
    output.step(sid, title, "running")
    t0 = time.time()
    try:
        INSTALLERS[sid](ctx, sid)
    except HelperError as exc:
        output.step(sid, title, "failed", error=exc.to_json())
        raise
    progress(sid, 1.0, "Done")
    output.step(sid, title, "done", elapsed=round(time.time() - t0))


def install(flags: dict) -> int:
    """`setup install --step ID | --all`: run the installers, streaming step/log/progress events."""
    ctx = Ctx(flags)
    if rt.DRY:
        log(None, "Dry run: nothing will be changed; every command is printed instead.")
    if flags.get("all"):
        todo = [sid for sid in ORDER if sid in ctx.by_id and
                (ctx.by_id[sid]["canAutoInstall"] or (flags.get("force") and sid in INSTALLERS)) and
                (sid not in OPTIONAL_IN_ALL or flags.get("include_optional"))]
        if not todo:
            return output.done(True, {"installed": [], "summary": summarize(ctx.steps), "message": "Nothing to install."})
        big = [ctx.by_id[s]["title"] for s in todo if s in ("swift", "xtool", "sdk")]
        if big:
            confirm("Install " + ", ".join(ctx.by_id[s]["title"] for s in todo) + "? This downloads up to about "
                    f"{'2' if 'swift' in todo else '1'} GB and takes {'30–45' if 'sdk' in todo else '15–25'} minutes. "
                    "You'll be asked for your password once.", "Install everything", flags)
        # All root work first, with ONE password prompt.
        root_ids = [s for s in todo if s in ROOT_STEPS]
        if root_ids:
            for sid in root_ids:
                output.step(sid, ctx.by_id[sid]["title"], "running")
            try:
                i_root_bundle(ctx, root_ids, root_ids[0], "Installing system pieces")
            except HelperError as exc:
                for sid in root_ids:
                    output.step(sid, ctx.by_id[sid]["title"], "failed", error=exc.to_json())
                raise
            if "swift" in root_ids or "swift-hold" in root_ids:
                i_swift_hook(ctx, "swift-hold")
            for sid in root_ids:
                output.step(sid, ctx.by_id[sid]["title"], "done")
            if not rt.DRY:
                ctx.refresh()
        installed = list(root_ids)
        for sid in [s for s in todo if s not in ROOT_STEPS]:
            if sid == "sdk" and not rt.DRY and not ctx.by_id["sdk"]["canAutoInstall"]:
                output.step(sid, ctx.by_id[sid]["title"], "skipped", reason="needs the Xcode download")
                continue
            _run_one(ctx, sid)
            installed.append(sid)
            if not rt.DRY:
                ctx.refresh()
        return output.done(True, {"installed": installed, "summary": summarize(ctx.steps), "dryRun": rt.DRY})
    sid = flags.get("step")
    if not sid or sid is True:
        raise HelperError("usage", "Which step? Use --step <id> or --all.")
    if sid not in INSTALLERS:
        s = ctx.by_id.get(sid)
        if s and s["action"].get("kind") == "page":
            raise HelperError("interactive", f"“{s['title']}” is done by hand.", fix(s["action"].get("label", "Open"),
                              s["action"]["page"], action=s["action"].get("action")))
        raise HelperError("usage", f"There's no installer for '{sid}'.")
    if ctx.state(sid) == "done" and not flags.get("force"):
        output.step(sid, ctx.by_id[sid]["title"], "done", already=True)
        return output.done(True, {"installed": [], "step": ctx.by_id[sid], "message": "Already done."})
    if sid in ("swift", "xtool"):
        s = ctx.by_id[sid]
        confirm(f"Install {s['title']}? It needs {s['size']} and takes {s['time']}.", "Install", flags)
    _run_one(ctx, sid)
    if not rt.DRY:
        ctx.refresh()
    return output.done(True, {"installed": [sid], "step": ctx.by_id.get(sid), "summary": summarize(ctx.steps), "dryRun": rt.DRY})
