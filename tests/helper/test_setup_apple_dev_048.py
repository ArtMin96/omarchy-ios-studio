"""omarchy-apple-dev v0.4.8: an existing install moves to the new pin in one Update that redoes only what changed.

xtool is built as pinned (the pin carries the Apple sign-in fix), the older `-gsa406` builds count for their own pin
until the scripts move, then get rebuilt and pruned; darwin-tools whose inputs changed after they were installed
mark the SDK for a quick repair from the spare copy (no Xcode, no SDK rebuild while its stamp is unchanged).
The upgrade itself runs in process against tests/support/setup_upgrade.py: real git on local remotes, fake swift,
ldd, xtool and install-toolchain.sh.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from iosstudio.core.output import HelperError
from iosstudio.features.setup import facts, installers, pins, steps, toolchain
from iosstudio.features.setup import runtime as rt
from support.setup_upgrade import ACTOOL, STAMP, Machine, facts_for, git, make_bundle, setup_machine


def events(capsys) -> list[dict]:
    return [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.strip()]


def steps_of(evs: list[dict]) -> list[tuple[str, str]]:
    return [(e["id"], e["state"]) for e in evs if e.get("event") == "step"]


# ================================================================ pins

def test_pins_follow_apple_dev_v048():
    assert pins.APPLE_DEV_COMMIT == "9f8352fc353d9d4f4112a454a81eb5769812387a"
    assert pins.DEFAULT_XTOOL_SHA == "7cc56c391a67652e86d164eddbbd62065a26599f"
    assert pins.DEFAULT_SDK_STAMP == "epoch=4,darwinTools=1.1.0,oam=1.4.1"
    assert pins.IPSW_VERSION == "3.1.743"
    assert pins.IPSW_SHA["x86_64"][1].startswith("56e9ba75") and pins.IPSW_SHA["aarch64"][1].startswith("c7976f40")
    for gone in ("XTOOL_FIX_REPO", "XTOOL_FIX_SHA", "XTOOL_KNOWN_BASE", "XTOOL_KNOWN_TREE"):
        assert not hasattr(pins, gone)                 # the #406 overlay is gone with the pin that needed it


# ================================================================ facts

def test_xtool_built_from_both_layouts(tmp_path, monkeypatch):
    monkeypatch.setattr(facts, "APPLE_DEV_LIB", tmp_path)
    sha = "7cc56c391a67652e86d164eddbbd62065a26599f"
    assert facts.xtool_built_from(tmp_path / f"xtool-{sha}/xtool") == sha
    assert facts.xtool_built_from(tmp_path / "xtool-f300489-gsa406/xtool") == "f300489"
    for other in ("xtool-f300489", f"xtool-{sha}.partial", "elsewhere"):
        assert facts.xtool_built_from(tmp_path / other / "xtool") is None
    assert facts.xtool_built_from(Path("/usr/bin/xtool")) is None    # not Setup's build


def test_darwin_tools_stale_compares_mtimes(tmp_path):
    ad, bundle = tmp_path / "ad", make_bundle(tmp_path / "sdk")
    src = ad / "tools/darwin-tools/Sources/DarwinAssets/CatalogMerge.swift"
    src.parent.mkdir(parents=True)
    src.write_text("x")
    (ad / "tools/ibtool").write_text("x")
    past = time.time() - 100
    for f in (src, ad / "tools/ibtool"):
        os.utime(f, (past, past))
    os.utime(bundle / ACTOOL, (past + 50, past + 50))
    assert facts.darwin_tools_stale(ad, bundle) is False
    os.utime(src, None)                                  # a new commit rewrote a source file
    assert facts.darwin_tools_stale(ad, bundle) is True
    (bundle / ACTOOL).unlink()
    assert facts.darwin_tools_stale(ad, bundle) is False  # a missing actool is "missing extras", not stale


# ================================================================ steps on hand-made facts

@pytest.fixture
def machine(tmp_path, monkeypatch) -> Machine:
    return setup_machine(tmp_path, monkeypatch)


def rows(F: dict) -> dict:
    return {r["id"]: r for r in steps.build_steps(F)[0]}


def test_old_pin_shows_one_update_on_the_scripts_row(machine):
    s = rows(steps.gather())
    ad = s["apple-dev"]
    assert ad["state"] == "broken" and ad["action"]["label"] == "Update" and ad["canAutoInstall"]
    assert ad["update"] is True and f"iOS Studio is tested with {machine.ad_new[:7]}." in ad["detail"]
    assert s["xtool"]["state"] == "done"              # the -gsa406 build is what the old scripts pin
    assert s["sdk"]["state"] == "done"                # the old checkout's tools are what's installed


def test_rows_after_the_scripts_moved(machine):
    git("-C", str(machine.apple_dev), "fetch", "-q", "--depth", "1", f"file://{machine.root / 'remotes/omarchy-apple-dev'}",
        machine.ad_new, cwd=machine.root)
    git("-C", str(machine.apple_dev), "checkout", "-q", "--detach", machine.ad_new, cwd=machine.root)
    s = rows(steps.gather())
    assert s["apple-dev"]["state"] == "done"
    xt = s["xtool"]
    assert xt["state"] == "broken" and f"Built from {machine.x_old[:7]}" in xt["detail"] and machine.x_new[:7] in xt["detail"]
    sdk = s["sdk"]
    assert sdk["state"] == "broken" and "newer build tools" in sdk["detail"] and sdk["canAutoInstall"]
    assert sdk["time"] == "about 2–5 minutes"          # the spare copy: no Xcode, no SDK build


def test_a_checkout_of_your_own_is_never_moved(machine, monkeypatch, tmp_path):
    other = tmp_path / "mine"
    monkeypatch.setattr(steps, "APPLE_DEV_HOME", other)   # ours lives elsewhere: the one found is the user's
    s = rows(steps.gather())
    assert s["apple-dev"]["state"] == "done" and "your copy at" in s["apple-dev"]["detail"]


def test_an_xtool_from_elsewhere_only_has_to_work(machine):
    s = rows(facts_for(machine, xtool=Path("/usr/bin/xtool")))
    assert s["xtool"]["state"] == "done"
    s = rows(facts_for(machine, xtool=Path("/usr/bin/xtool"), xtool_old_ua=True))
    assert s["xtool"]["state"] == "broken" and "503" in s["xtool"]["detail"]


# ================================================================ the upgrade, end to end

def test_update_redoes_only_what_changed_and_prunes_the_old_builds(machine, capsys):
    assert installers.install({"step": "apple-dev", "yes": True}) == 0
    evs = events(capsys)
    assert steps_of(evs) == [("apple-dev", "running"), ("apple-dev", "done"), ("xtool", "running"), ("xtool", "done"),
                             ("sdk", "running"), ("sdk", "done")]
    done = evs[-1]
    assert done["ok"] and done["data"]["installed"] == ["apple-dev", "xtool", "sdk"]
    assert done["data"]["summary"]["ready"]

    assert git("-C", str(machine.apple_dev), "rev-parse", "HEAD", cwd=machine.root) == machine.ad_new
    new = machine.lib / f"xtool-{machine.x_new}"
    assert os.path.realpath(machine.local_bin / "xtool") == str(new / "xtool")
    assert (new / "libswiftCore.so").exists() and facts.binary_has_old_ua(new / "xtool") is False
    assert not any(p.exists() for p in machine.legacy_dirs())                 # both old layouts pruned
    assert sorted(p.name for p in machine.cache.glob("xtool-*")) == [f"xtool-{machine.x_new}"]

    builds = machine.calls("swift build -c release --product xtool")
    assert len(builds) == 1 and builds[0].endswith(machine.x_new)             # built as pinned, no overlay
    assert machine.calls("install-toolchain.sh") == ["install-toolchain.sh --mode full --repair"]
    assert not machine.calls("xtool sdk build")                               # stamp unchanged: no SDK rebuild

    s = rows(steps.gather())
    assert {k: s[k]["state"] for k in ("apple-dev", "xtool", "sdk")} == {"apple-dev": "done", "xtool": "done", "sdk": "done"}
    assert installers.install({"step": "apple-dev", "yes": True}) == 0       # a second Update has nothing to do
    assert events(capsys)[-1]["data"]["message"] == "Already done."


def test_install_all_follows_up_the_same_way(machine, capsys):
    assert installers.install({"all": True, "yes": True}) == 0
    assert events(capsys)[-1]["data"]["installed"] == ["apple-dev", "xtool", "sdk"]
    assert len(machine.calls("swift build -c release --product xtool")) == 1
    assert len(machine.calls("install-toolchain.sh")) == 1


def test_a_pin_without_the_sign_in_fix_is_refused_and_keeps_the_old_xtool(machine, capsys, monkeypatch):
    (machine.apple_dev / "install-toolchain.sh").write_text(machine.install_script(machine.x_nofix))            # scripts that pin an unfixed xtool
    monkeypatch.setattr(steps, "APPLE_DEV_HOME", machine.root / "elsewhere")  # theirs: not moved by Setup
    with pytest.raises(HelperError) as cm:
        installers.install({"step": "xtool", "yes": True})
    assert cm.value.code == "xtool-no-fix" and cm.value.fix["step"] == "apple-dev"
    assert os.path.realpath(machine.local_bin / "xtool") == str(machine.legacy_dirs()[0] / "xtool")
    assert all(p.exists() for p in machine.legacy_dirs())                     # nothing pruned
    assert not (machine.lib / f"xtool-{machine.x_nofix}").exists()
    capsys.readouterr()


def test_dry_run_update_changes_nothing(machine, capsys, monkeypatch):
    monkeypatch.setattr(rt, "DRY", True)
    assert installers.install({"step": "apple-dev"}) == 0
    logs = [e["text"] for e in events(capsys) if e.get("event") == "log"]
    assert any("would then rebuild what the update changed: xtool, sdk" in t for t in logs)
    assert git("-C", str(machine.apple_dev), "rev-parse", "HEAD", cwd=machine.root) == machine.ad_old
    assert not machine.calls("swift") and not machine.calls("install-toolchain")   # only read-only probes ran


# ================================================================ disk usage and Remove everything

def test_disk_lists_both_old_layouts_and_keeps_the_active_one(machine):
    items = {i["id"]: i for i in toolchain.disk_items()}
    active = f"xtool-{machine.x_old[:7]}-gsa406"
    assert items["xtool-old"]["paths"] == [machine.lib / f"xtool-{machine.x_old}"]   # the upstream-built leftover
    assert items["xtool"]["paths"] == [machine.lib / active]
    assert machine.cache / active / ".build" in items["build-trees"]["paths"]        # the active build's source
    orphan = machine.cache / "xtool-0123456789abcdef0123456789abcdef01234567"
    (orphan / ".build").mkdir(parents=True)                                   # a source tree whose build is gone
    items = {i["id"]: i for i in toolchain.disk_items()}
    assert orphan in items["xtool-old"]["paths"] and orphan / ".build" not in items["build-trees"]["paths"]


def test_remove_everything_takes_both_layouts(machine, capsys, monkeypatch):
    monkeypatch.setattr(toolchain, "run_root", lambda *a: None)
    monkeypatch.setattr(toolchain.appdirs, "sdk_bundles", lambda: [machine.bundle])
    monkeypatch.setattr(toolchain, "VENV_DIR", machine.root / "venv")
    monkeypatch.setattr(toolchain, "OMARCHY_HOOK", machine.root / "hook")
    monkeypatch.setattr(toolchain, "XTOOL_CONFIG", machine.root / "xtool-config")
    monkeypatch.setattr(toolchain, "run_stream", lambda *a, **k: 0)
    monkeypatch.setattr(rt, "ALLOWED_DELETE_ROOTS", rt.ALLOWED_DELETE_ROOTS + (machine.root / "venv",))
    monkeypatch.setattr(toolchain, "save_state", lambda upd: None)
    assert toolchain.remove_all({"yes": True, "keep_swift": True}) == 0
    capsys.readouterr()
    assert not machine.lib.exists() and not machine.cache.exists() and not machine.apple_dev.exists()
    assert not os.path.lexists(machine.local_bin / "xtool")


# ================================================================ demo

def test_demo_shows_the_tested_versions(monkeypatch):
    from iosstudio.demo import demo
    real_steps = [steps._row("apple-dev", "iOS build scripts", "Toolchain", "", "broken", "At 79bda71", essential=True),
                  steps._row("xtool", "xtool", "Toolchain", "", "broken", "Built from f300489", essential=True),
                  steps._row("ipsw", "ipsw", "Toolchain", "", "done", "ipsw 3.1.731", optional=True)]
    items = [{"id": "xtool", "status": "error", "version": "1.24.0", "detail": "~/x"},
             {"id": "ipsw", "status": "ok", "version": "3.1.731", "detail": "Debugger helper (optional)"}]
    monkeypatch.setattr(demo, "FRESH", False)
    monkeypatch.setattr(demo, "_real", lambda *a: {"steps": real_steps, "items": items, "sdks": {}})
    s = {r["id"]: r for r in demo._setup_check({})["steps"]}
    assert s["apple-dev"]["state"] == "done" and pins.APPLE_DEV_COMMIT[:7] in s["apple-dev"]["detail"]
    assert s["xtool"]["detail"] == f"Build {pins.DEFAULT_XTOOL_SHA[:7]} · has the Apple sign-in fix"
    assert s["ipsw"]["detail"] == "ipsw 3.1.743"
    info = {i["id"]: i for i in demo._toolchain_info({})["items"]}
    assert info["xtool"]["version"] == pins.DEFAULT_XTOOL_SHA[:7] and info["xtool"]["hasFix"]
    assert pins.DEFAULT_XTOOL_SHA in info["xtool"]["detail"] and info["ipsw"]["version"] == "3.1.743"


def test_stamp_is_the_pinned_default():
    assert STAMP == pins.DEFAULT_SDK_STAMP
