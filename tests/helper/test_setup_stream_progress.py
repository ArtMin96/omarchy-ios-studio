"""Setup's streamed progress and log, fed with lines recorded during a real toolchain update (Swift 6.3, xtool
7cc56c3, omarchy-apple-dev 9f8352f).

- xtool's swift build prints its tasks as "[n / m]" with thin spaces; SwiftPM's "[0/100] Downloading …" and
  "[1/5669] Fetching …" counters belong to one package each. The bar used to take the download's 100/100 for the
  build and sat at 92 % with ETA 0 for the whole compile.
- install-toolchain.sh --repair ends with survive_status, whose pairing and Apple sign-in lines are wrong for iOS
  Studio users and tell them to run a terminal command; Setup drops exactly those.
"""
from __future__ import annotations

import json
import shlex
import stat
from pathlib import Path

from iosstudio.features.setup import installers, sources
from iosstudio.features.setup.runtime import StreamProgress

TS = "\u2009"   # swift-build's thin space
DASH = "\u2014"


def task(n: int, m: int, name: str = "CCryptoBoringSSL") -> str:
    return f"[{n}{TS}/{TS}{m}] {name}"


RCODESIGN = "https://github.com/xtool-org/swift-rcodesign/releases/download/source-0.2.0/RCodesignBinary.artifactbundle.zip"
FETCH = [
    "Fetching https://github.com/apple/swift-asn1.git from cache",
    "Fetching https://github.com/pointfreeco/swift-custom-dump",
    "[1/5669] Fetching swift-custom-dump",
    "Fetched https://github.com/pointfreeco/swift-custom-dump (1.75s)",
    "Computing version for https://github.com/tuist/XcodeProj.git",
    "Creating working copy for https://github.com/apple/swift-numerics.git",
    "Working copy of https://github.com/apple/swift-numerics.git resolved at 1.1.1",
    f"Downloading binary artifact {RCODESIGN}",
    f"[0/100] Downloading {RCODESIGN} (1 KB / 27.1 MB)",
    f"[42/100] Downloading {RCODESIGN} (11.4 MB / 27.1 MB)",
    f"[100/100] Downloading {RCODESIGN} (27.1 MB / 27.1 MB)",
    f"Downloaded {RCODESIGN} (123.17s)",
]
PLAN = ["Building for production...", "[Computing dependencies]", f"[Planning 540{TS}/{TS}3030]",
        f"[Constructing 1{TS}/{TS}7505]", "[Starting]"]


def xtool_bar() -> StreamProgress:
    return StreamProgress("Compiling xtool", 840, sources.xtool_build_phase, start=0.0, lead=sources.XTOOL_FETCH_SHARE)


def test_package_counters_are_drawn_not_logged_and_not_the_build():
    bar = xtool_bar()
    logged = [ln for ln in FETCH if bar.line(ln)]
    assert not any(ln.startswith(("[0/100]", "[42/100]", "[100/100]", "[1/5669]")) for ln in logged)
    assert f"Downloaded {RCODESIGN} (123.17s)" in logged and FETCH[0] in logged
    frac, detail, eta = bar.reading(120.0)
    assert frac == 120 / 840                       # still the estimate, not the download's 100/100
    assert detail.startswith("Downloading xtool's packages · 2 min so far")
    assert eta == 720


def test_a_long_fetch_leaves_most_of_the_bar_to_the_build():
    bar = xtool_bar()
    for ln in FETCH + PLAN:
        bar.line(ln)
    frac, detail, _ = bar.reading(420.0)           # the recorded run: about 7 min before the first task
    assert frac == sources.XTOOL_FETCH_SHARE
    assert detail.startswith("Planning the build")


def test_the_bar_follows_the_tasks_to_the_end():
    bar = xtool_bar()
    for ln in FETCH + PLAN:
        bar.line(ln)
    bar.reading(420.0)
    seen = []
    # the recorded counts (the total grows as SwiftPM adds tasks); about 12.7 min of compiling
    for t, (n, m) in enumerate([(1, 1841), (502, 1841), (1001, 1899), (1400, 1905), (1600, 1905),
                                (1900, 1991), (1976, 1991), (1995, 1995)]):
        assert bar.line(task(n, m))                 # build lines stay in the log
        seen.append(bar.reading(422.0 + 100 * (t + 1)))
    fracs = [f for f, _, _ in seen]
    assert fracs == sorted(fracs) and len(set(fracs)) == len(fracs)   # moves on every new count, never back
    assert fracs[0] > sources.XTOOL_FETCH_SHARE - 0.01 and fracs[-1] == 1.0
    f1400, detail, eta = seen[3]
    assert 0.75 < f1400 < 0.85                       # 1400 of 1905 of the build's share, not 92 %
    assert detail.startswith("Compiling xtool · 1400 of 1905 · ")
    assert 60 < eta < 600                            # from the task rate since compiling started, not 0
    assert seen[-1][2] == 0


def test_incremental_build_and_percent_lines():
    bar = StreamProgress("Building", None, start=0.0)
    assert bar.reading(5.0) == (None, "Building · 5 s", None)
    bar.line(task(900, 1000))                        # a rebuild that starts near the end
    assert bar.reading(10.0)[0] == 0.9
    pct = StreamProgress("Unpacking", None, start=0.0)
    for ln in ("unpacking 10%", "unpacking 40%", "unpacking 70%"):
        pct.line(ln)
    assert pct.reading(3.0)[0] == 0.7                # every percent line counts, not only the first


def test_estimate_alone_still_stops_at_95_percent():
    bar = StreamProgress("Building the Swift macro server", 300, start=0.0)
    assert bar.reading(600.0)[0] == 0.95


# ------------------------------------------------------------------ survive_status (install-toolchain.sh 9f8352f)

HOME = "/home/someone"
SURVIVE_MISSING = ["-- Survive status --",
                   f"SDK: re-registered from {HOME}/.cache/xtool/darwin-iPhoneOS27.1.xtoolsdk",
                   "pairing: no records yet (plug the iPhone once; nothing is lost here by a swap)",
                   f"auth: missing {DASH} run: {HOME}/.local/bin/xtool auth"]
SURVIVE_PRESENT = ["-- Survive status --",
                   f"SDK: re-registered from {HOME}/.cache/xtool/darwin-iPhoneOS27.1.xtoolsdk",
                   f"pairing: intact at {HOME}/.pymobiledevice3",
                   f"auth: present at {HOME}/.local/share/xtool"]


def test_survive_status_drops_only_the_pairing_and_sign_in_lines():
    for block in (SURVIVE_MISSING, SURVIVE_PRESENT):
        st: dict = {}
        kept = [ln for ln in block if not installers.survive_noise(ln, st)]
        assert kept == block[:2]


def test_the_same_words_outside_the_block_are_kept():
    st: dict = {}
    assert not installers.survive_noise(f"auth: missing {DASH} run: /x/xtool auth", st)
    assert not installers.survive_noise("pairing: no records yet (plug the iPhone once; nothing is lost here by a swap)", st)
    installers.survive_noise("-- Survive status --", st)
    assert not installers.survive_noise(f"error: auth: missing {DASH} run: /x/xtool auth failed", st)


def test_sdk_repair_log_through_the_real_script_boundary(tmp_path: Path, capsys):
    """run_sdk_repair runs a fake install-toolchain.sh that prints the recorded tail of the real one."""
    script = tmp_path / "install-toolchain.sh"
    body = ["warning: '--build-system native' has been deprecated and will be removed in a future release",
            *SURVIVE_MISSING, "error: something real went wrong"]
    script.write_text("#!/bin/sh\n" + "".join(f"printf '%s\\n' {shlex.quote(ln)}\n" for ln in body), encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    installers.run_sdk_repair(tmp_path, "sdk")
    logs = [e["text"] for e in map(json.loads, capsys.readouterr().out.splitlines()) if e.get("event") == "log"]
    assert logs == [body[0], *SURVIVE_MISSING[:2], "error: something real went wrong"]
