"""Setup slice pure logic: xip_version on synthetic .xip files, downloads (file:// URLs, SHA-256, dry run, .SRCINFO),
runtime helpers (formatting, dry-run file ops, the delete guard, the streaming wrapper), facts parsing and
steps (summarize, and build_steps on hand-made facts)."""
from __future__ import annotations

import hashlib
import json
import os
import signal
from pathlib import Path

import pytest

from iosstudio.core.output import HelperError
from iosstudio.features import xip_version
from iosstudio.features.setup import downloads, facts, pins, steps
from iosstudio.features.setup import runtime as rt
from support.xip import VERSION_PLIST, cpio, cpio_entry, pbzx, xar, xcode_xip


def events(capsys) -> list[dict]:
    return [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.strip()]


# ================================================================ xip_version

def write(tmp_path: Path, data: bytes, name: str = "Xcode_27.1.xip") -> Path:
    p = tmp_path / name
    p.write_bytes(data)
    return p


def test_xip_reads_version_across_compressed_and_raw_chunks(tmp_path):
    data, version, build = xip_version.read_version(str(write(tmp_path, xcode_xip())))
    assert (version, build) == ("27.1", "27A123")
    assert data == VERSION_PLIST


def test_xip_main_writes_plist_and_prints_version(tmp_path, capsys):
    out = tmp_path / "out.plist"
    assert xip_version.main(["x", str(write(tmp_path, xcode_xip())), str(out)]) == 0
    assert capsys.readouterr().out == "27.1 27A123\n"
    assert out.read_bytes() == VERSION_PLIST


def test_xip_main_usage_and_failure_exit_codes(tmp_path, capsys):
    assert xip_version.main(["x"]) == 2
    out = tmp_path / "out.plist"
    assert xip_version.main(["x", str(write(tmp_path, b"nope")), str(out)]) == 3
    assert not out.exists()
    assert "not a xar archive" in capsys.readouterr().err


@pytest.mark.parametrize("data, why", [
    (b"short", "file too short"),
    (xar(b"", magic=b"zip!"), "bad magic"),
    (xar(b"pbzx", name="Other"), "no Content member"),
    (xar(b"gzip" + b"\0" * 20), "not a pbzx stream"),
    (xar(pbzx(cpio([("Xcode.app/x", b"")]))[:-5]), "truncated"),
    (xar(pbzx(cpio_entry("a", b"", magic=b"070701"))), "not odc"),
    (xar(pbzx(cpio([("./Xcode.app/Contents/Info.plist", b"1")]))), "no Xcode.app/Contents/version.plist"),
    (xar(pbzx(cpio([("./Xcode.app/Contents/version.plist", b"not a plist")]))), "not a plist"),
    (xar(pbzx(cpio([("./Xcode.app/Contents/version.plist", b"<?xml")]))), "not a plist"),
    (xar(pbzx(cpio([("x", b"1")], trailer=False))), "truncated cpio stream"),
])
def test_xip_rejects_broken_archives(tmp_path, data, why):
    with pytest.raises((ValueError, EOFError)) as ei:
        xip_version.read_version(str(write(tmp_path, data)))
    assert why in str(ei.value)


def test_xip_member_must_be_top_level_app(tmp_path):
    # a nested .app's version.plist (e.g. a simulator inside Xcode) is not Xcode's
    data = xar(pbzx(cpio([("./Xcode.app/Contents/Dev/Sim.app/Contents/version.plist", VERSION_PLIST)])))
    with pytest.raises(ValueError, match="no Xcode.app"):
        xip_version.read_version(str(write(tmp_path, data)))


def test_xip_plist_without_version_strings(tmp_path):
    import plistlib
    bad = plistlib.dumps({"CFBundleShortVersionString": 27, "ProductBuildVersion": "x"})
    with pytest.raises(ValueError, match="no usable Xcode version"):
        xip_version.read_version(str(write(tmp_path, xcode_xip(bad))))


# ================================================================ downloads

def test_download_file_url_with_matching_checksum(tmp_path, capsys):
    src = tmp_path / "src.bin"
    src.write_bytes(b"hello" * 1000)
    sha = hashlib.sha256(src.read_bytes()).hexdigest()
    dest = tmp_path / "out" / "dest.bin"
    assert downloads.download(src.as_uri(), dest, "s", sha256=sha) == dest
    assert dest.read_bytes() == src.read_bytes()
    assert not dest.with_name("dest.bin.part").exists()
    # a second run with the same checksum doesn't download again
    downloads.download("file:///does/not/exist", dest, "s", sha256=sha)
    assert any(e.get("text") == "already downloaded: dest.bin" for e in events(capsys))


def test_download_checksum_mismatch_removes_part(tmp_path):
    src = tmp_path / "src.bin"
    src.write_bytes(b"data")
    dest = tmp_path / "dest.bin"
    with pytest.raises(HelperError) as ei:
        downloads.download(src.as_uri(), dest, "swift", sha256="0" * 64, label="Swift")
    assert ei.value.code == "checksum" and "Swift" in ei.value.message
    assert ei.value.fix == {"label": "Try again", "page": "setup", "step": "swift"}
    assert not dest.exists() and not dest.with_name("dest.bin.part").exists()


def test_download_missing_source_is_network_error(tmp_path):
    with pytest.raises(HelperError) as ei:
        downloads.download((tmp_path / "missing").as_uri(), tmp_path / "d", None)
    assert ei.value.code == "network" and ei.value.fix is None
    assert not (tmp_path / "d.part").exists()


def test_download_dry_run_only_logs(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(rt, "DRY", True)
    dest = tmp_path / "d"
    assert downloads.download("https://example.invalid/x", dest, "s", sha256="ab" * 32) == dest
    assert not dest.exists()
    ev = events(capsys)[0]
    assert ev["step"] == "s" and ev["text"].startswith("would download https://example.invalid/x")


def test_sha256_of(tmp_path):
    p = tmp_path / "f"
    p.write_bytes(b"x" * (3 << 20))
    assert downloads.sha256_of(p) == hashlib.sha256(p.read_bytes()).hexdigest()


def test_srcinfo_and_strip_ver(tmp_path):
    (tmp_path / ".SRCINFO").write_text("pkgbase = swift-bin\n\tpkgver = 6.4.2\n\tdepends = a>=1\n\tdepends = b\n"
                                       "\tsource_x86_64 = swift.tgz::https://x/s.tgz\n")
    info = downloads.srcinfo(tmp_path)
    assert info["depends"] == ["a>=1", "b"] and info["pkgver"] == ["6.4.2"]
    assert info["source_x86_64"] == ["swift.tgz::https://x/s.tgz"]
    assert downloads.srcinfo(tmp_path / "missing") == {}
    assert [downloads.strip_ver(d) for d in ("foo>=1.2", "bar", "baz=3", "q<2")] == ["foo", "bar", "baz", "q"]


def test_prefetch_swift_sources_uses_srcinfo_sums(tmp_path, monkeypatch):
    got = []
    monkeypatch.setattr(downloads, "arch", lambda: "x86_64")
    monkeypatch.setattr(downloads, "download", lambda url, dest, step, **kw: got.append((url, dest.name, kw["sha256"])))
    (tmp_path / ".SRCINFO").write_text("source_x86_64 = s.tgz::https://x/s.tgz\nsource_x86_64 = local.patch\n"
                                       "source_x86_64 = https://x/other.tgz\nsha256sums_x86_64 = abc\n"
                                       "sha256sums_x86_64 = SKIP\nsha256sums_x86_64 = SKIP\n")
    downloads.prefetch_swift_sources(tmp_path, "swift")
    assert got == [("https://x/s.tgz", "s.tgz", "abc"), ("https://x/other.tgz", "other.tgz", None)]


def test_pins_are_well_formed():
    for sha in [*pins.RCODESIGN_SHA.values(), *(v[1] for v in pins.IPSW_SHA.values())]:
        assert len(sha) == 64 and int(sha, 16) >= 0
    for c in (pins.APPLE_DEV_COMMIT, pins.DEFAULT_XTOOL_SHA, pins.XTOOL_FIX_SHA, pins.SWIFT_AUR_COMMIT):
        assert len(c) == 40
    assert "{v}" in pins.DIRECT_XIP and "{v}" in pins.DOWNLOAD_PAGE
    for rec, major, size in pins.XCODE_FOR_SWIFT.values():
        assert rec.split(".")[0] == major and size > pins.XIP_MIN_BYTES


# ================================================================ runtime

@pytest.mark.parametrize("n, text", [(0, "0 B"), (999, "999 B"), (1000, "1 KB"), (1_500_000, "2 MB"),
                                     (2_027_369_056, "2.0 GB"), (3e12, "3.0 TB"), (5e15, "5000.0 TB")])
def test_fmt_bytes(n, text):
    assert rt.fmt_bytes(n) == text


def test_fmt_dur():
    assert [rt.fmt_dur(s) for s in (-5, 0, 59, 60, 89, 91, 3600)] == ["0 s", "0 s", "59 s", "1 min", "1 min",
                                                                      "2 min", "60 min"]


def test_shell_quote():
    assert rt.shell_quote("/usr/bin/x") == "/usr/bin/x"
    assert rt.shell_quote("a b") == "'a b'"
    assert rt.shell_quote("it's") == "'it'\\''s'"


def test_progress_clamps_and_rounds(capsys):
    rt.progress("s", 1.7, "d", 3.9)
    rt.progress(None, -1)
    rt.progress(None, None)
    a, b, c = events(capsys)
    assert a == {"event": "progress", "fraction": 1.0, "detail": "d", "eta": 3, "step": "s"}
    assert b["fraction"] == 0.0 and "step" not in b
    assert c["fraction"] is None


def test_must():
    rt.must(0, "x", "fine")
    with pytest.raises(HelperError) as ei:
        rt.must(2, "code", "msg", "step", ["a", "b"])
    assert ei.value.code == "code" and ei.value.detail == "a\nb" and ei.value.fix["step"] == "step"
    with pytest.raises(HelperError) as ei:
        rt.must(1, "code", "msg")
    assert ei.value.fix is None and ei.value.detail is None


def test_do_rmtree_guard(tmp_path, monkeypatch, capsys):
    root = tmp_path / "root"
    (root / "inner").mkdir(parents=True)
    (root / "inner" / "f").write_bytes(b"x" * 10)
    with pytest.raises(HelperError) as ei:
        rt.do_rmtree(root / "inner")                  # not one of ours, not allowed by inside/exact
    assert ei.value.code == "unsafe-path"
    with pytest.raises(HelperError):
        rt.do_rmtree(root, inside=(root,))            # inside means strictly inside
    assert rt.do_rmtree(root / "missing", inside=(root,)) == 0
    monkeypatch.setattr(rt, "DRY", True)
    rt.do_rmtree(root / "inner", inside=(root,), step_id="s")
    assert (root / "inner").exists()
    assert "would delete" in events(capsys)[-1]["text"]
    monkeypatch.setattr(rt, "DRY", False)
    assert rt.do_rmtree(root / "inner", inside=(root,)) > 0
    assert not (root / "inner").exists()
    (root / "exact").mkdir()
    rt.do_rmtree(root / "exact", exact=(root / "exact",))
    assert not (root / "exact").exists()


def test_do_rmtree_removes_a_symlink_not_its_target(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    (target / "keep").write_text("k")
    link = tmp_path / "zone" / "link"
    link.parent.mkdir()
    link.symlink_to(target)
    assert rt.do_rmtree(link, inside=(tmp_path / "zone",)) == 0
    assert not os.path.lexists(link) and (target / "keep").exists()


def test_dry_run_file_ops(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(rt, "DRY", True)
    rt.do_mkdir(tmp_path / "new", "s")
    rt.do_symlink(tmp_path / "t", tmp_path / "l", "s")
    (tmp_path / "f").write_text("x")
    rt.do_unlink(tmp_path / "f", "s")
    rt.do_unlink(tmp_path / "missing", "s")
    assert not (tmp_path / "new").exists() and not (tmp_path / "l").exists() and (tmp_path / "f").exists()
    texts = [e["text"] for e in events(capsys)]
    assert texts == [f"would create {tmp_path / 'new'}", f"would link {tmp_path / 'l'} -> {tmp_path / 't'}",
                     f"would delete {tmp_path / 'f'}"]
    monkeypatch.setattr(rt, "DRY", False)
    rt.do_mkdir(tmp_path / "new")
    rt.do_symlink(tmp_path / "t", tmp_path / "l")
    rt.do_unlink(tmp_path / "f")
    assert (tmp_path / "new").is_dir() and os.readlink(tmp_path / "l") == str(tmp_path / "t")
    assert not (tmp_path / "f").exists()


def test_need_space_and_confirm(tmp_path, monkeypatch):
    rt.need_space(tmp_path, 1, "a test")
    with pytest.raises(HelperError) as ei:
        rt.need_space(tmp_path / "not" / "yet", 1 << 62, "the SDK")
    assert ei.value.code == "disk-full" and "the SDK" in ei.value.message
    rt.confirm("Sure?", "Yes", {"yes": True})          # --yes: no prompt
    monkeypatch.setattr(rt.output, "prompt", lambda *a, **k: "no")
    with pytest.raises(HelperError) as ei:
        rt.confirm("Sure?", "Yes", {})
    assert ei.value.code == "cancelled"


def test_run_root_refuses_while_pacman_is_locked(monkeypatch):
    rt.run_root([], "s", "nothing")   # no args: nothing to do
    real_exists = Path.exists
    monkeypatch.setattr(Path, "exists", lambda self: True if str(self) == "/var/lib/pacman/db.lck" else real_exists(self))
    with pytest.raises(HelperError) as ei:
        rt.run_root(["--packages", "zip"], "packages", "System packages")
    assert ei.value.code == "pacman-locked"


def test_streaming_wrapper_turns_errors_into_done_events(capsys):
    old = signal.getsignal(signal.SIGTERM)
    try:
        seen = {}

        @rt.streaming
        def ok_handler(flags):
            seen["dry"] = rt.DRY
            return 0

        @rt.streaming
        def handled(flags):
            raise HelperError("x", "X")

        @rt.streaming
        def crashed(flags):
            raise RuntimeError("boom")

        @rt.streaming
        def interrupted(flags):
            raise KeyboardInterrupt

        assert ok_handler({"dry_run": True}) == 0 and seen["dry"] is True
        assert handled({}) == 1 and rt.DRY is False
        assert crashed({}) == 1
        assert interrupted({}) == 1
        a, b, c = events(capsys)
        assert a == {"event": "done", "ok": False, "error": {"code": "x", "message": "X"}}
        assert b["error"]["code"] == "internal" and "boom" in b["error"]["detail"]
        assert c["error"]["code"] == "cancelled"
    finally:
        signal.signal(signal.SIGTERM, old)
        rt.DRY = False


# ================================================================ facts

def test_version_parsing():
    assert facts.parse_swift_version("Swift version 6.4.2 (swift-6.4.2-RELEASE)\nTarget: x") == "6.4.2"
    assert facts.parse_swift_version("Apple Swift version 6.3 (x)") == "6.3"
    assert facts.parse_swift_version("nothing") is None
    assert facts.first_version("rcodesign 0.29.0\n") == "0.29.0"
    assert facts.first_version("weird\nsecond") == "weird"
    assert facts.first_version("") is None


def test_xcode_for_and_needed_xcode(monkeypatch):
    assert facts.xcode_for("6.4.2") == pins.XCODE_FOR_SWIFT["6.4"]
    assert facts.xcode_for("6.3") == pins.XCODE_FOR_SWIFT["6.3"]
    assert facts.xcode_for("5.9") is None and facts.xcode_for(None) is None
    monkeypatch.setattr(facts.appdirs, "settings", lambda: {})
    assert facts.needed_xcode(None) == pins.XCODE_FOR_SWIFT["6.4"]
    monkeypatch.setattr(facts.appdirs, "settings", lambda: {"xcodeVersion": "26.4"})
    assert facts.needed_xcode("6.3.1") == ("26.4", "26", pins.XCODE_FOR_SWIFT["6.3"][2])


def test_apple_dev_pins_read_from_install_script(tmp_path):
    assert facts.apple_dev_pins(None)["XTOOL_SHA"] == pins.DEFAULT_XTOOL_SHA
    (tmp_path / "install-toolchain.sh").write_text('#!/bin/sh\nXTOOL_REPO="https://x/fork"\nXTOOL_SHA=abc123\n'
                                                   'XTOOL_SDK_VERSION=""\n')
    p = facts.apple_dev_pins(tmp_path)
    assert p == {"XTOOL_REPO": "https://x/fork", "XTOOL_SHA": "abc123", "XTOOL_SDK_VERSION": pins.DEFAULT_SDK_STAMP}


def make_bundle(root: Path, ios: list[str], stamp: str = "", tools: bool = True) -> Path:
    sdks = root / "Developer/Platforms/iPhoneOS.platform/Developer/SDKs"
    sdks.mkdir(parents=True)
    for v in ios:
        (sdks / f"iPhoneOS{v}.sdk").mkdir()
    (sdks / "iPhoneOS.sdk").mkdir()          # the unversioned link is not a version
    if stamp:
        (root / "darwin-sdk-version.txt").write_text(stamp + "\n")
    if tools:
        b = root / "Developer/Platforms/iPhoneOS.platform/Developer/usr/bin"
        b.mkdir(parents=True)
        for t in ("actool", "xcstringstool", "ibtool", "momc"):
            (b / t).write_text("")
            (b / t).chmod(0o755)
        (root / "OpenAppleMacrosServer").write_text("")
        (root / "OpenAppleMacrosServer").chmod(0o755)
    return root


def test_sdk_bundle_facts(tmp_path, monkeypatch):
    b = make_bundle(tmp_path / "b", ["26.4", "27.1"], "epoch=4")
    assert facts.sdk_ios_versions(b) == ["26.4", "27.1"]
    assert facts.sdk_stamp(b) == "epoch=4" and facts.sdk_stamp(tmp_path / "none") == ""
    assert facts.sdk_extras_missing(b) == []
    bare = make_bundle(tmp_path / "bare", [], tools=False)
    assert facts.sdk_extras_missing(bare) == ["actool", "xcstringstool", "ibtool", "momc", "macro server"]
    cache = tmp_path / "cache"
    make_bundle(cache / "darwin-iPhoneOS27.1.xtoolsdk", ["27.1"], "S")
    make_bundle(cache / "darwin-other.xtoolsdk", [], "T")
    monkeypatch.setattr(facts, "SDK_CACHE", cache)
    got = {c["ios"]: c for c in facts.sdk_caches("S")}
    assert got["27.1"]["current"] and got["27.1"]["complete"]
    assert not got[""]["current"] and not got[""]["complete"]


def test_binary_has_old_ua(tmp_path):
    a, b, empty = tmp_path / "a", tmp_path / "b", tmp_path / "e"
    a.write_bytes(b"\0junk" + pins.XTOOL_OLD_UA + b"more")
    b.write_bytes(b"\0junk akd/1.0 new")
    empty.write_bytes(b"")
    assert facts.binary_has_old_ua(a) is True
    assert facts.binary_has_old_ua(b) is False
    assert facts.binary_has_old_ua(empty) is None            # mmap of an empty file fails
    assert facts.binary_has_old_ua(tmp_path / "missing") is None


def test_parallel_keeps_order_and_reraises():
    assert list(facts._parallel({"b": lambda: 2, "a": lambda: 1}).items()) == [("b", 2), ("a", 1)]
    with pytest.raises(ZeroDivisionError):
        facts._parallel({"ok": lambda: 1, "bad": lambda: 1 / 0})


def test_last_device_developer_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(facts, "STATE_DIR", tmp_path)
    assert facts.last_device_developer_mode() is None
    (tmp_path / "last-device.json").write_text(json.dumps({"data": {"developer_mode": False}}))
    assert facts.last_device_developer_mode() is False
    (tmp_path / "device.json").write_text(json.dumps({"developerMode": True}))
    assert facts.last_device_developer_mode() is True


def test_swift_hold_hook_present(tmp_path, monkeypatch):
    hook = tmp_path / "hooks" / "ios-studio-hold-swift-bin"
    monkeypatch.setattr(facts, "OMARCHY_HOOK", hook)
    assert facts.swift_hold_hook_present() is False             # no folder at all
    hook.parent.mkdir()
    other = hook.parent / "mine"
    other.write_text("sed IgnorePkg swift-bin")
    assert facts.swift_hold_hook_present() is False             # not executable
    other.chmod(0o755)
    assert facts.swift_hold_hook_present() is True
    other.chmod(0o644)
    hook.write_text("")
    assert facts.swift_hold_hook_present() is True


def test_save_state_is_a_no_op_in_dry_run(tmp_path, monkeypatch):
    monkeypatch.setattr(facts, "SETUP_STATE", tmp_path / "setup.json")
    facts.save_state({"a": 1})
    facts.save_state({"b": 2})
    assert facts.load_state() == {"a": 1, "b": 2}
    monkeypatch.setattr(rt, "DRY", True)
    facts.save_state({"c": 3})
    assert "c" not in facts.load_state()


# ================================================================ steps

def row(id_, state, **kw):
    return steps._row(id_, id_.title(), "G", "desc", state, **kw)


def test_row_defaults():
    r = row("x", "broken")
    assert r["action"] == {"kind": "install", "label": "Repair"} and r["canAutoInstall"] is True
    r = row("x", "done", auto=True)
    assert r["action"]["label"] == "Install" and r["canAutoInstall"] is False
    assert row("x", "missing", auto=False, pkg="p")["canAutoInstall"] is False
    assert row("x", "missing", pkg="p")["pkg"] == "p"


def test_summarize_ready():
    s = steps.summarize([row("platform", "done", essential=True), row("a", "done", essential=True),
                         row("opt", "optional", optional=True), row("b", "missing")])
    assert s["ready"] and s["level"] == "ok" and s["badge"] == ""
    assert s["total"] == 2 and s["done"] == 1 and s["readiness"] == "1 of 2 ready"   # platform/optional not counted
    assert s["autoInstallable"] == ["b"]


def test_summarize_warn_and_error():
    s = steps.summarize([row("a", "missing", essential=True), row("b", "broken"),
                         row("c", "missing", essential=True, auto=False)])
    assert s["level"] == "warn" and s["title"] == "2 steps left" and s["badge"] == "2"
    assert s["essentialMissing"] == ["a", "c"] and s["broken"] == ["b"] and s["autoInstallable"] == ["a", "b"]
    s = steps.summarize([row("a", "broken", essential=True)])
    assert s["level"] == "error" and s["title"] == "Something needs repair"
    assert steps.summarize([row("a", "missing", essential=True)])["title"] == "1 step left"


SWIFT_QI = "Name            : swift-bin\nVersion         : 6.4.2-2\nDepends On      : python312  glibc\n"


def facts_for(**over) -> dict:
    """gather()'s shape for a machine where everything that build_steps probes is installed and fine."""
    F = {
        "pacman_T": (0, "", ""), "swift_qi": (0, SWIFT_QI, ""),
        "swift_ver": (0, "Swift version 6.4.2 (swift-6.4.2-RELEASE)", ""), "ignore": (0, "swift-bin other", ""),
        "xtool_ver": (0, "xtool f300489f3a3fddbd\n", ""), "sdk_status": (1, "", ""),
        "auth": (0, "Logged in.\n - Apple ID: a@b.c\n - Token expiry: 31/10/2027, 2:33 PM\n", ""),
        "rcodesign": (0, "rcodesign 0.29.0", ""), "ipsw": (1, "", ""),
        "pymd3": [("/v/bin/python", (0, f"{pins.PYMD3_VERSION}\n3.13.1\n", ""))],
        "ad_head": (0, pins.APPLE_DEV_COMMIT + "\n", ""), "ad_dirty": (0, " M x\n", ""),
        "xtool_old_ua": False, "xcspec": True, "idevice": (1, "", ""), "avahi": (0, "inactive", ""),
        "py312": (0, "python312 3.12.9", ""),
        "xtool": Path("/fake/xtool"), "apple_dev": Path("/fake/apple-dev"),
        "pins": {"XTOOL_SDK_VERSION": "STAMP"},
    }
    F.update(over)
    return F


@pytest.fixture
def steps_env(tmp_path, monkeypatch):
    """build_steps with no SDK, no .xip and no saved state on this machine."""
    monkeypatch.setattr(steps, "is_omarchy", lambda: False)
    monkeypatch.setattr(steps, "best_xip", lambda major: None)
    monkeypatch.setattr(steps, "sdk_caches", lambda stamp: [])
    monkeypatch.setattr(steps, "last_device_developer_mode", lambda: None)
    monkeypatch.setattr(steps.appdirs, "sdk_bundles", lambda: [tmp_path / "no-sdk"])
    monkeypatch.setattr(steps.appdirs, "settings", lambda: {})
    return tmp_path


def by_id(F) -> dict:
    rows, ctx = steps.build_steps(F)
    return {r["id"]: r for r in rows} | {"_ctx": ctx}


def test_build_steps_healthy_toolchain(steps_env):
    s = by_id(facts_for())
    assert s["packages"]["state"] == "done"
    assert s["swift"]["state"] == "done" and "matches Xcode 27.1" in s["swift"]["detail"]
    assert s["swift-hold"]["state"] == "done"
    assert s["xcspec"]["state"] == "done"
    assert s["apple-dev"]["detail"].startswith("At the tested version") and "local changes" in s["apple-dev"]["detail"]
    assert s["xtool"]["state"] == "done" and s["xtool"]["detail"].startswith("Build f300489 ")
    assert s["rcodesign"]["detail"] == "rcodesign 0.29.0"
    assert s["pymobiledevice3"]["state"] == "done"
    assert s["ipsw"]["state"] == "optional"
    assert s["debugger"]["state"] == "done" and s["debugger"]["pkg"] == "python312"
    assert s["apple-id"]["state"] == "done" and "a@b.c" in s["apple-id"]["detail"]
    assert s["sdk"]["state"] == "missing" and "needs the Xcode download first" in s["sdk"]["detail"]
    assert s["xcode"]["state"] == "missing" and s["xcode"]["xcode"]["needed"] == "27.1"
    assert s["xcode"]["action"]["url"].endswith("Xcode_27.1.xip")
    assert s["developer-mode"]["state"] == "unknown"
    assert s["_ctx"]["missing_pkgs"] == [] and s["_ctx"]["swift_ver"] == "6.4.2"


def test_build_steps_broken_toolchain(steps_env):
    s = by_id(facts_for(pacman_T=(1, "zip\nffmpeg\n", ""), swift_ver=(1, "", ""), ignore=(0, "", ""),
                        xtool_ver=(0, "xtool 1", ""), xtool_old_ua=True, xcspec=False, ad_head=(0, "deadbeef1\n", ""),
                        auth=(1, "Not logged in\n", ""), pymd3=[("/v/bin/python", (1, "", "boom"))]))
    assert s["packages"]["detail"] == "Missing: zip, ffmpeg" and s["_ctx"]["missing_pkgs"] == ["zip", "ffmpeg"]
    assert s["swift"]["state"] == "broken" and "doesn't run" in s["swift"]["detail"]
    assert s["swift-hold"]["state"] == "missing"
    assert s["xcspec"]["state"] == "broken"
    assert "not the tested" in s["apple-dev"]["detail"]
    assert s["xtool"]["state"] == "broken" and "503" in s["xtool"]["detail"]
    assert s["pymobiledevice3"]["state"] == "broken"
    assert s["apple-id"]["state"] == "missing"


def test_build_steps_without_swift_or_xtool(steps_env):
    s = by_id(facts_for(swift_qi=(1, "", ""), swift_ver=(127, "", ""), xtool=None, xtool_ver=(127, "", ""),
                        apple_dev=None, pymd3=[], xcspec=None))
    assert s["swift"]["detail"] == "Not installed" and s["swift-hold"]["detail"] == "Needs Swift first"
    assert s["xcspec"]["detail"] == "Needs Swift first"
    assert s["xtool"]["state"] == "missing" and s["apple-id"]["detail"] == "Needs xtool first"
    assert s["apple-dev"]["state"] == "missing" and s["pymobiledevice3"]["state"] == "missing"
    assert "debugger" not in s                        # no swift-bin package -> no python dependency known
    assert steps.summarize(list(v for k, v in s.items() if k != "_ctx"))["ready"] is False


def test_build_steps_unknown_swift_version_and_xip_mismatch(steps_env, monkeypatch):
    monkeypatch.setattr(steps, "best_xip", lambda major: {"name": "Xcode_26.xip", "size": 3e9, "valid": True,
                                                          "version": "26.0"})
    s = by_id(facts_for(swift_ver=(0, "Swift version 5.9", "")))
    assert s["swift"]["state"] == "broken" and "doesn't match" in s["swift"]["detail"]
    assert s["xcode"]["state"] == "broken" and "you need Xcode 27.1" in s["xcode"]["detail"]
    assert s["sdk"]["canAutoInstall"] is True                 # a valid .xip makes the SDK step automatic


def test_build_steps_installed_sdk(steps_env, monkeypatch):
    bundle = make_bundle(steps_env / "sdk", ["27.1"], "STAMP")
    s = by_id(facts_for(sdk_status=(0, f"SDK is installed\nPath: {bundle}\n", "")))
    assert s["sdk"]["state"] == "done" and s["sdk"]["detail"] == "iOS 27.1 SDK"
    assert s["xcode"]["state"] == "done" and s["xcode"]["essential"] is False
    old = by_id(facts_for(sdk_status=(0, f"is installed\nPath: {bundle}\n", ""), pins={"XTOOL_SDK_VERSION": "NEW"}))
    assert old["sdk"]["state"] == "broken" and "older xtool" in old["sdk"]["detail"]
