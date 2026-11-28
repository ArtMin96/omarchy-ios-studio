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
from .facts import arch, is_omarchy, load_state, save_state, sdk_ios_versions, swift_hold_hook_present
from .pins import (
    AUR_COMMITS,
    HOOK_SRC,
    IPSW_SHA,
    LEGACY_VENV,
    OMARCHY_HOOK,
    PACKAGES,
    SDK_CACHE,
    UXPLAY_RUNTIME,
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
from .sources import apple_dev_checkout, i_apple_dev, i_xtool
from .steps import Ctx, summarize
from .xip import xip_quick, xip_read_version


def avahi_enabled() -> bool:
    """Is avahi (the service or its socket) enabled to start at boot? systemctl answers 0 if either is."""
    return probe(["systemctl", "is-enabled", "--quiet", "avahi-daemon.service", "avahi-daemon.socket"], timeout=3)[0] == 0


def i_root_bundle(ctx: Ctx, ids: list[str], step_id: str, label: str) -> None:
    """Collect the root work of several steps and run it with one password prompt. --packages and --deps may
    repeat: root-tasks.sh adds the lists up."""
    args: list[str] = []
    a = arch()
    turns_avahi_on = False
    if "packages" in ids:
        miss = ctx.c.get("missing_pkgs") or []
        if miss or ctx.flags.get("force"):
            args += ["--packages", " ".join(miss or PACKAGES)]
    if "swift" in ids and (ctx.state("swift") != "done" or ctx.flags.get("force")):
        d = aur_fetch("swift-bin", AUR_COMMITS["swift-bin"], step_id)
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
        args += ["--xcspec"]     # root-tasks.sh runs the xcspec.py next to itself
    if "usbmuxd" in ids and ctx.state("usbmuxd") != "done":
        args += ["--usbmuxd-dropin", "--udev-trigger"]
    if "uxplay" in ids:
        if not shutil.which("uxplay") or ctx.flags.get("force"):
            d = aur_fetch("uxplay", AUR_COMMITS["uxplay"], step_id)
            info = srcinfo(d) if not rt.DRY else {}
            deps = sorted({strip_ver(x) for x in info.get("depends", []) + info.get("makedepends", []) +
                           info.get(f"depends_{a}", []) + info.get(f"makedepends_{a}", [])} | {"base-devel"})
            args += ["--deps", " ".join(deps), "--aur-build", str(d)]
        ux_miss = UXPLAY_RUNTIME if rt.DRY else [x for x in UXPLAY_RUNTIME if probe(["pacman", "-T", x])[0] != 0]
        if ux_miss:
            args += ["--packages", " ".join(ux_miss)]
        args += ["--avahi", "--ufw-uxplay"]
        # Remember that Setup turned avahi on, so removing the toolchain turns it off again (and only then).
        turns_avahi_on = not rt.DRY and not avahi_enabled()
    if "wireshark" in ids and (not shutil.which("wireshark") or ctx.flags.get("force")):
        args += ["--packages", "wireshark-qt"]
    if "debugger" in ids:
        pkg = ctx.by_id.get("debugger", {}).get("pkg")
        if pkg and (ctx.state("debugger") != "done" or ctx.flags.get("force")):
            if pkg not in AUR_COMMITS:
                raise HelperError("unsupported", f"Your Swift needs {pkg} for the debugger, and this version of iOS "
                                  "Studio has no tested build recipe for it yet. Update iOS Studio, then try again.",
                                  fix("Open Setup", "setup", step="debugger"))
            d = aur_fetch(pkg, AUR_COMMITS[pkg], step_id)
            info = srcinfo(d) if not rt.DRY else {}
            deps = sorted({strip_ver(x) for x in info.get("depends", []) + info.get("makedepends", [])} | {"base-devel"})
            args += ["--deps", " ".join(deps), "--aur-build", str(d)]
    if not args:
        log(step_id, "Nothing needs your password here.")
        return
    run_root(args, step_id, label)
    if turns_avahi_on and not load_state().get("avahiEnabledBySetup"):
        save_state({"avahiEnabledBySetup": True})


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


def github_dl() -> str:
    """Where GitHub release downloads come from: https://github.com, or IOSSTUDIO_GITHUB_DL (tests point it at a
    local server, as updates/upstream.py's IOSSTUDIO_GITHUB_API). The checksum is checked either way."""
    return (os.environ.get("IOSSTUDIO_GITHUB_DL") or "https://github.com").rstrip("/")


def i_rcodesign(ctx: Ctx, step_id: str) -> None:
    a, ver, sha = arch(), ctx.pins.rcodesign_version, ctx.pins.rcodesign_sha
    if not sha:
        raise HelperError("unsupported", f"rcodesign has no build for {a}.")
    name = f"apple-codesign-{ver}-{a}-unknown-linux-musl"
    url = f"{github_dl()}/indygreg/apple-platform-rs/releases/download/apple-codesign%2F{ver}/{name}.tar.gz"
    _install_tarball_binary(url, sha, f"{name}/rcodesign", LOCAL_BIN / "rcodesign", step_id, "rcodesign")


def i_ipsw(ctx: Ctx, step_id: str) -> None:
    a, ver, sha = arch(), ctx.pins.ipsw_version, ctx.pins.ipsw_sha
    if a not in IPSW_SHA or not sha:
        raise HelperError("unsupported", f"ipsw has no build for {a}.")
    ia = IPSW_SHA[a][0]           # the release's name for this architecture
    url = f"{github_dl()}/blacktop/ipsw/releases/download/v{ver}/ipsw_{ver}_linux_{ia}.tar.gz"
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
    venv, ver = VENV_DIR, ctx.pins.pymd3_version
    if venv.exists() and not ctx.keep_previous:   # keep_previous: the update's backup moved it to venv.prev
        do_rmtree(venv, step_id)
    do_mkdir(venv.parent, step_id)
    progress(step_id, 0.05, "Creating a private Python environment")
    tail: list = []
    must(run_stream([py, "-m", "venv", str(venv)], step_id, tail=tail), "venv", "Couldn't create the Python environment.", step_id, tail)
    progress(step_id, 0.15, f"Downloading pymobiledevice3 {ver}")

    def parse(line, st):
        if line.startswith(("Collecting", "Downloading")):
            st["detail"] = line.split(" (")[0][:80]
        if line.startswith("Installing collected"):
            st["detail"] = "Installing"
        return None
    rc = run_stream([venv / "bin/pip", "install", "--disable-pip-version-check", "--no-input", "--progress-bar", "off",
                     f"pymobiledevice3=={ver}"], step_id, label="Installing pymobiledevice3", estimate=90,
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
    ad = apple_dev_checkout(ctx, step_id)
    major = ctx.c["xcode_major"]
    cache = ctx.c.get("good_cache")
    xip_path = ctx.flags.get("xip")
    xt = str(LOCAL_BIN / "xtool")
    benv = _build_env()
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
    run_sdk_repair(ad, step_id)


def _build_env() -> dict:
    """The environment Swift builds run in: the toolchain swift-bin points to first, then ~/.local/bin."""
    return subproc.tool_env("setup", {"PATH": f"{os.path.dirname(os.path.realpath('/usr/bin/swift'))}:/usr/bin:{LOCAL_BIN}:/bin"})


# install-toolchain.sh's survive_status lines that are wrong for iOS Studio: it looks for the Apple sign-in in
# ~/.local/share/xtool (xtool keeps it in ~/.config/xtool/data), for pairing records in one folder only, and tells
# you to run a terminal command. Setup's own Apple ID and iPhone rows say how those stand.
SURVIVE_HEADER = "-- Survive status --"
SURVIVE_WRONG = re.compile(r"pairing: intact at .+/\.pymobiledevice3"
                           r"|pairing: no records yet \(plug the iPhone once; nothing is lost here by a swap\)"
                           r"|auth: present at .+/\.local/share/xtool"
                           r"|auth: missing — run: .+/xtool auth")


def survive_noise(line: str, st: dict) -> bool:
    """True for a survive_status pairing/auth line (only inside its block, which starts at its header)."""
    if line == SURVIVE_HEADER:
        st["survive"] = True
        return False
    return bool(st.get("survive")) and SURVIVE_WRONG.fullmatch(line) is not None


def run_sdk_repair(ad: Path, step_id: str) -> None:
    """install-toolchain.sh --mode full --repair in the checkout `ad`: re-registers the newest current SDK copy and
    (re)builds the darwin tools and the macro server. Also how an update's rollback rebuilds the old tools."""
    progress(step_id, 0.62, "Registering the SDK and building its extras")

    def parse(line, st):
        if survive_noise(line, st):
            return "quiet"
        if "Building OpenAppleMacrosServer" in line:
            st["detail"], st["frac"] = "Building the Swift macro server (about 5 min, quiet)", None
        elif line.startswith("== ") or "sdk install" in line:
            st["detail"] = line.strip("= ")[:80]
        return None
    tail: list = []
    rc = run_stream([str(Path(ad) / "install-toolchain.sh"), "--mode", "full", "--repair"], step_id,
                    label="Registering the SDK and building its extras", estimate=420, env=_build_env(), cwd=str(ad),
                    parse=parse, frac_range=(0.62, 0.99), tail=tail)
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
# What a new omarchy-apple-dev commit can leave outdated: they're rebuilt right after it, only if it changed them
# (plus whatever they depend on that isn't done), in ORDER.
FOLLOW_UPS = {"apple-dev": ("xtool", "sdk")}
ROOT_STEPS = {"packages", "usbmuxd", "swift", "swift-hold", "xcspec", "uxplay", "wireshark", "debugger"}
OPTIONAL_IN_ALL = {"ipsw", "debugger", "uxplay", "wireshark"}

def run_one(ctx: Ctx, sid: str) -> None:
    """Run one step's installer between its step running/done (or failed) events."""
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


def follow_ups(ctx: Ctx, sid: str, was: dict[str, str]) -> list[str]:
    """The FOLLOW_UPS of `sid` that its run turned from done to broken, plus the steps they depend on (the rows'
    `depends`, transitively) that aren't done and that a follow-up may run (not root work, not `sid`), in ORDER:
    xtool before the SDK repair that needs it, also when xtool was already outdated before (a fresh download
    of the scripts next to an older xtool build)."""
    todo: set[str] = set()

    def need(d: str) -> None:
        if d in todo or d == sid or d not in INSTALLERS or d in ROOT_STEPS:
            return
        todo.add(d)
        for dep in ctx.by_id.get(d, {}).get("depends", []):
            if ctx.state(dep) != "done":
                need(dep)

    for d in FOLLOW_UPS.get(sid, ()):
        if was[d] == "done" and ctx.state(d) == "broken":
            need(d)
    return [d for d in ORDER if d in todo]


def run_chain(ctx: Ctx, sid: str) -> list[str]:
    """Run one step, then its follow_ups() (an omarchy-apple-dev update that moved the xtool pin or the SDK's
    build tools), so one Update redoes exactly what changed, in dependency order. Returns the ids run."""
    deps = FOLLOW_UPS.get(sid, ())
    was = {d: ctx.state(d) for d in deps}
    run_one(ctx, sid)
    ran = [sid]
    if rt.DRY:
        if deps:
            log(sid, "would then rebuild what the update changed: " + ", ".join(d for d in ORDER if d in deps))
        return ran
    ctx.refresh()
    for d in follow_ups(ctx, sid, was):
        s = ctx.by_id[d]
        if s["state"] not in ("missing", "broken"):
            continue
        waiting = [ctx.by_id.get(x, {}).get("title", x) for x in s.get("depends", []) if ctx.state(x) != "done"]
        if waiting or not s["canAutoInstall"]:
            why = ("needs " + ", ".join(f"“{t}”" for t in waiting) + " first") if waiting else s["detail"]
            log(sid, f"“{s['title']}” needs a repair too: {why}")
            continue
        run_one(ctx, d)
        ctx.refresh()
        ran.append(d)
    return ran


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
        big = [ctx.by_id[s]["title"] for s in todo if s in ("swift", "xtool", "sdk")
               or (s == "apple-dev" and ctx.c.get("apple_dev_outdated"))]
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
            if sid in installed:          # already run as another step's follow-up
                continue
            if sid == "sdk" and not rt.DRY and not ctx.by_id["sdk"]["canAutoInstall"]:
                output.step(sid, ctx.by_id[sid]["title"], "skipped", reason="needs the Xcode download")
                continue
            installed += run_chain(ctx, sid)
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
    if sid == "apple-dev" and ctx.c.get("apple_dev_outdated"):
        confirm(f"Update the iOS build scripts to the {ctx.pins.word('apple-dev')} version "
                f"{ctx.pins.apple_dev_commit[:7]}? iOS Studio then rebuilds "
                f"only what changed: xtool if its version moved ({ctx.by_id['xtool']['time']}) and the iOS SDK's "
                "build tools (a few minutes). The SDK is rebuilt from Xcode only if the new scripts need that.",
                "Update", flags)
    ran = run_chain(ctx, sid)
    if not rt.DRY:
        ctx.refresh()
    return output.done(True, {"installed": ran, "step": ctx.by_id.get(sid), "summary": summarize(ctx.steps), "dryRun": rt.DRY})
