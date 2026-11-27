"""A machine Setup installed at an older omarchy-apple-dev pin, behind fakes at the process boundary.

Git is real: local file:// repositories stand in for GitHub (omarchy-apple-dev with an old and a new commit, xtool
with an old pin, a pin without the sign-in fix and a new pin). swift, ldd, xtool and install-toolchain.sh are small
shell fakes that append what they were asked to one log file, so a test can tell exactly what an update rebuilt.
The installed state is the older layout: xtool built the `-gsa406` way (the pin plus the sign-in fix on top), an
upstream-built leftover next to it, and an SDK whose actool is older than the new checkout's darwin-tools.
"""
from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

STAMP = "epoch=4,darwinTools=1.1.0,oam=1.4.1"
OLD_UA = "akd/1.0 CFNetwork/808.1.4"
ACTOOL = "Developer/Platforms/iPhoneOS.platform/Developer/usr/bin/actool"

# xtool: --version, sdk status (the bundle) and auth status. `exit 0` keeps bytes appended after it inert.
XTOOL = """#!/bin/sh
echo "xtool $*" >> "@LOG@"
case "$1" in
  --version) echo "xtool 1.24.0" ;;
  sdk) [ "$2" = status ] && printf 'SDK is installed\nPath: %s\n' "@BUNDLE@" ;;
  auth) printf 'Logged in.\n - Apple ID: a@b.c\n' ;;
esac
exit 0
"""

# swift build in an xtool checkout: logs the commit it builds; a checkout with an OLD_UA file builds an xtool that
# still has the old user agent (a pin without the sign-in fix).
SWIFT = """#!/bin/sh
echo "swift $* @ $(git rev-parse HEAD 2>/dev/null)" >> "@LOG@"
case "$*" in
  *--show-bin-path*) echo "$PWD/.build/release" ;;
  build*) mkdir -p .build/release && cp "@XTOOL@" .build/release/xtool && chmod 755 .build/release/xtool
          if [ -f OLD_UA ]; then printf '%s' "@OLD_UA@" >> .build/release/xtool; fi ;;
esac
exit 0
"""

LDD = """#!/bin/sh
printf '\tlibswiftCore.so => %s/libswiftCore.so (0x0)\n' "$LD_LIBRARY_PATH"
"""

# install-toolchain.sh --mode full --repair: re-registers the SDK and installs the darwin tools (a fresh actool).
INSTALL_TOOLCHAIN = """#!/bin/sh
XTOOL_REPO=@XTOOL_REPO@
XTOOL_SHA=@XTOOL_SHA@
XTOOL_SDK_VERSION="@STAMP@"
echo "install-toolchain.sh $*" >> "@LOG@"
[ "$*" = "--mode full --repair" ] || exit 2
touch "@BUNDLE@/@ACTOOL@"
"""

def _exe(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)
    return path


def git(*args: str, cwd: Path) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True,
                          timeout=30).stdout.strip()


def _commit(repo: Path, files: dict[str, str | None], msg: str) -> str:
    """Write (or, for None, delete) files and commit them; returns the commit."""
    for rel, text in files.items():
        p = repo / rel
        if text is None:
            p.unlink()
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        if rel.endswith(".sh"):
            p.chmod(0o755)
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", msg, cwd=repo)
    return git("rev-parse", "HEAD", cwd=repo)


def make_bundle(root: Path, ios: str = "27.1", stamp: str = STAMP) -> Path:
    (root / f"Developer/Platforms/iPhoneOS.platform/Developer/SDKs/iPhoneOS{ios}.sdk").mkdir(parents=True)
    (root / "darwin-sdk-version.txt").write_text(stamp + "\n")
    for t in ("actool", "xcstringstool", "ibtool", "momc"):
        _exe(root / "Developer/Platforms/iPhoneOS.platform/Developer/usr/bin" / t, "")
    _exe(root / "OpenAppleMacrosServer", "")
    return root


@dataclass
class Machine:
    root: Path
    home: Path
    swift: Path          # its folder also has the fake ldd (Setup runs both with the toolchain's PATH)
    toolchain: Path      # a fake swift toolchain root (usr/lib/swift/linux/libswiftCore.so)
    log: Path
    apple_dev: Path      # ~/.local/share/omarchy-apple-dev
    lib: Path            # ~/.local/lib/omarchy-apple-dev
    cache: Path          # ~/.cache/omarchy-apple-dev
    local_bin: Path
    sdk_cache: Path
    bundle: Path
    legacy_venv: Path
    xtool_remote: Path
    ad_old: str = ""
    ad_new: str = ""
    x_old: str = ""
    x_nofix: str = ""    # an xtool commit whose build still has the old user agent
    x_new: str = ""

    def calls(self, prefix: str = "") -> list[str]:
        lines = self.log.read_text().splitlines() if self.log.exists() else []
        return [ln for ln in lines if ln.startswith(prefix)]

    def legacy_dirs(self) -> list[Path]:
        return [self.lib / f"xtool-{self.x_old[:7]}-gsa406", self.lib / f"xtool-{self.x_old}",
                self.cache / f"xtool-{self.x_old[:7]}-gsa406"]

    def install_script(self, xtool_sha: str) -> str:
        return self.fill(INSTALL_TOOLCHAIN, **{"@XTOOL_REPO@": f"file://{self.xtool_remote}", "@XTOOL_SHA@": xtool_sha})

    def fill(self, text: str, **more: str) -> str:
        sub = {"@LOG@": str(self.log), "@BUNDLE@": str(self.bundle), "@STAMP@": STAMP, "@OLD_UA@": OLD_UA,
               "@ACTOOL@": ACTOOL, **more}
        for k, v in sub.items():
            text = text.replace(k, v)
        return text


def old_machine(root: Path) -> Machine:
    """omarchy-apple-dev checked out at its old commit, xtool built the -gsa406 way, an SDK and a spare copy."""
    home = root / "home"
    m = Machine(root=root, home=home, swift=root / "toolchain/usr/bin/swift",
                toolchain=root / "toolchain", log=root / "calls.log", apple_dev=home / ".local/share/omarchy-apple-dev",
                lib=home / ".local/lib/omarchy-apple-dev", cache=home / ".cache/omarchy-apple-dev",
                local_bin=home / ".local/bin", sdk_cache=home / ".cache/xtool",
                bundle=home / ".config/swiftpm/swift-sdks/darwin.artifactbundle", legacy_venv=home / "pymobile3-venv",
                xtool_remote=root / "remotes/xtool")
    xtool_tpl = _exe(root / "xtool-template", m.fill(XTOOL))
    _exe(m.swift, m.fill(SWIFT, **{"@XTOOL@": str(xtool_tpl)}))
    _exe(m.swift.parent / "ldd", LDD)
    (m.toolchain / "usr/lib/swift/linux").mkdir(parents=True)
    (m.toolchain / "usr/lib/swift/linux/libswiftCore.so").write_text("runtime")
    _exe(m.legacy_venv / "bin/python", "#!/bin/sh\nexit 0\n")

    xr = m.xtool_remote
    xr.mkdir(parents=True)
    git("init", "-q", cwd=xr)
    m.x_old = _commit(xr, {"Package.swift": "// 1.23\n"}, "xtool old pin")
    m.x_nofix = _commit(xr, {"OLD_UA": "1\n"}, "a pin without the sign-in fix")
    m.x_new = _commit(xr, {"OLD_UA": None, "Package.swift": "// main, with #406 and #432\n"}, "xtool new pin")

    ar = root / "remotes/omarchy-apple-dev"
    ar.mkdir(parents=True)
    git("init", "-q", cwd=ar)
    m.ad_old = _commit(ar, {"install-toolchain.sh": m.install_script(m.x_old), "tools/sdk-state.sh": "sdk_state() { :; }\n",
                            "tools/ibtool": "ibtool\n", "tools/darwin-tools/Package.resolved": '{"revision": "09875cc"}\n',
                            "tools/darwin-tools/Sources/DarwinAssets/CatalogMerge.swift": "// merge\n"}, "v0.4.3")
    m.ad_new = _commit(ar, {"install-toolchain.sh": m.install_script(m.x_new),
                            "tools/darwin-tools/Package.resolved": '{"revision": "d797d6e"}\n',
                            "tools/darwin-tools/Sources/DarwinAssets/CatalogMerge.swift": "// merge shared folders\n"},
                       "v0.4.8")

    # Our checkout at the old commit, its files two minutes old; the SDK's actool installed after them.
    m.apple_dev.parent.mkdir(parents=True)
    git("init", "-q", str(m.apple_dev), cwd=root)
    git("-C", str(m.apple_dev), "fetch", "-q", "--depth", "1", f"file://{ar}", m.ad_old, cwd=root)
    git("-C", str(m.apple_dev), "checkout", "-q", "--detach", m.ad_old, cwd=root)
    past = time.time() - 120
    for d, _, fs in os.walk(m.apple_dev):
        for f in fs:
            os.utime(Path(d, f), (past, past))
    make_bundle(m.bundle)
    os.utime(m.bundle / ACTOOL, (past + 60, past + 60))
    make_bundle(m.sdk_cache / "darwin-iPhoneOS27.1.xtoolsdk")

    # xtool as the older Setup built it, plus an upstream-built leftover and the -gsa406 source tree.
    for d in (m.lib / f"xtool-{m.x_old[:7]}-gsa406", m.lib / f"xtool-{m.x_old}"):
        _exe(d / "xtool", xtool_tpl.read_text())
        (d / "libswiftCore.so").write_text("runtime")
    (m.cache / f"xtool-{m.x_old[:7]}-gsa406/.build").mkdir(parents=True)
    m.local_bin.mkdir(parents=True)
    (m.local_bin / "xtool").symlink_to(m.lib / f"xtool-{m.x_old[:7]}-gsa406/xtool")
    return m
