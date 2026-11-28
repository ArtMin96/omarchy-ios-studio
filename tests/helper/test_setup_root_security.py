"""Setup's root work after the security review: the root script's arguments, where it runs from, its pinned AUR
recipes and what removing the toolchain undoes (helper/root/root-tasks.sh, setup/installers.py, runtime.py,
toolchain.py). The root script runs here only with --dry-run, which prints each command instead of running it."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from iosstudio.core.output import HelperError
from iosstudio.features.setup import downloads, facts, installers, pins, toolchain
from iosstudio.features.setup import runtime as rt

REPO = Path(__file__).resolve().parents[2]
ROOT_TASKS = REPO / "helper/root/root-tasks.sh"


def dry(*args: str, script: Path = ROOT_TASKS, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(script), *args, "--dry-run"], capture_output=True, text=True, timeout=30,
                          env={**os.environ, **(env or {})})


def commands(out: str) -> list[str]:
    return [line[2:] for line in out.splitlines() if line.startswith("+ ")]


# ------------------------------------------------------------------ 1. --packages / --deps add up

SRCINFO = {
    "swift-bin": "pkgbase = swift-bin\n\tmakedepends = patchelf\n",
    "uxplay": "pkgbase = uxplay\n\tmakedepends = cmake\n\tdepends = openssl\n\tdepends = avahi\n",
    "python312": "pkgbase = python312\n\tmakedepends = gdb\n\tmakedepends = tk\n\tdepends = libffi\n",
}


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    """i_root_bundle with the AUR, the disk, pacman and pkexec faked; returns the root-tasks.sh arguments."""
    fetched: list[tuple[str, str]] = []
    calls: list[list[str]] = []

    def fake_fetch(pkg, ref, step_id):
        fetched.append((pkg, ref))
        d = tmp_path / "aur" / pkg
        d.mkdir(parents=True, exist_ok=True)
        (d / ".SRCINFO").write_text(SRCINFO[pkg])
        return d

    monkeypatch.setattr(rt, "DRY", False)
    monkeypatch.setattr(installers, "aur_fetch", fake_fetch)
    monkeypatch.setattr(installers, "need_space", lambda *a: None)
    monkeypatch.setattr(installers, "prefetch_swift_sources", lambda *a: None)
    monkeypatch.setattr(installers, "probe", lambda argv, **kw: (1, "", ""))   # gst pieces missing, avahi off
    monkeypatch.setattr(installers.shutil, "which", lambda name, *a, **k: None)
    monkeypatch.setattr(installers, "run_root", lambda args, sid, label: calls.append(list(args)))
    monkeypatch.setattr(facts, "SETUP_STATE", tmp_path / "setup.json")

    def run(ids: list[str], **ctx_over) -> list[str]:
        states = {"swift": "missing", "xcspec": "missing", "usbmuxd": "done", "debugger": "optional"}
        ctx = SimpleNamespace(c={"missing_pkgs": ["zip", "git"]}, flags={}, F={"ignore": (0, "", "")},
                              by_id={"debugger": {"pkg": "python312"}}, state=lambda s: states.get(s, "missing"))
        for k, v in ctx_over.items():
            setattr(ctx, k, v)
        installers.i_root_bundle(ctx, ids, ids[0], "Installing system pieces")
        assert len(calls) == 1
        return calls.pop()

    run.fetched = fetched
    return run


def test_install_everything_keeps_every_steps_packages_and_build_deps(bundle):
    args = bundle(["packages", "swift", "uxplay", "debugger", "wireshark"])
    # Several --packages / --deps flags: no merging in Python any more.
    assert args.count("--deps") == 3 and args.count("--packages") == 3
    r = dry(*args)
    assert r.returncode == 0, r.stdout + r.stderr
    cmds = commands(r.stdout)
    pkgs = next(c for c in cmds if c.startswith("pacman -S --needed --noconfirm -- "))
    deps = next(c for c in cmds if c.startswith("pacman -S --needed --noconfirm --asdeps -- "))
    assert pkgs.split(" -- ")[1].split() == ["zip", "git", "gst-plugins-bad", "gst-plugin-va", "wireshark-qt"]
    dep_words = deps.split(" -- ")[1].split()
    for want in ("patchelf", "cmake", "openssl", "avahi", "gdb", "tk", "libffi", "base-devel"):
        assert want in dep_words, (want, deps)       # swift's makedepends survive uxplay's and the debugger's
    assert dep_words.count("base-devel") == 1          # duplicates dropped
    assert [c for c in cmds if "makepkg" in c] and all("--skippgpcheck" not in c for c in cmds)
    assert "systemctl enable --now avahi-daemon.service" in cmds
    # Every recipe comes from its pinned AUR commit.
    assert bundle.fetched == [("swift-bin", pins.SWIFT_AUR_COMMIT), ("uxplay", pins.UXPLAY_AUR_COMMIT),
                              ("python312", pins.PYTHON312_AUR_COMMIT)]


def test_installers_no_longer_merge_package_flags():
    src = (REPO / "helper/iosstudio/features/setup/installers.py").read_text()
    assert 'args.index("--packages")' not in src


# ------------------------------------------------------------------ 4. no option injection

@pytest.mark.parametrize("flag", ["--packages", "--deps"])
@pytest.mark.parametrize("words", ["zip --config=/tmp/evil.conf", "-x", "zip --dbpath /tmp", "--"])
def test_package_words_starting_with_a_dash_are_refused(flag, words):
    r = dry(flag, words)
    assert r.returncode == 2 and "bad" in r.stderr
    assert "pacman" not in r.stdout


def test_package_lists_follow_a_double_dash():
    r = dry("--packages", "zip", "--deps", "cmake")
    cmds = commands(r.stdout)
    assert "pacman -S --needed --noconfirm -- zip" in cmds
    assert "pacman -S --needed --noconfirm --asdeps -- cmake" in cmds


# ------------------------------------------------------------------ 2. xcspec.py is never a caller-given path

def test_xcspec_takes_no_path():
    r = dry("--xcspec", "/tmp/evil.py")
    assert r.returncode == 2 and "unknown argument '/tmp/evil.py'" in r.stderr


def test_xcspec_runs_the_copy_next_to_the_real_script(tmp_path):
    link = tmp_path / "elsewhere" / "root-tasks.sh"
    link.parent.mkdir()
    (link.parent / "xcspec.py").write_text("raise SystemExit('the wrong one')\n")
    link.symlink_to(ROOT_TASKS)
    for script in (ROOT_TASKS, link):
        r = dry("--xcspec", script=script)
        assert r.returncode == 0, r.stderr
        run = next(c for c in commands(r.stdout) if "xcspec.py" in c)
        assert run.startswith(f"/usr/bin/python3 -I {ROOT_TASKS.parent / 'xcspec.py'} apply ")


def test_install_passes_xcspec_as_a_bare_flag(bundle):
    args = bundle(["xcspec"])
    assert args == ["--xcspec"]


# ------------------------------------------------------------------ 3. the script pkexec runs

def app_tree(root: Path) -> Path:
    (root / "helper/root").mkdir(parents=True)
    for name in ("root-tasks.sh", "xcspec.py"):
        (root / "helper/root" / name).write_text("#\n")
    for p in [root, root / "helper", root / "helper/root"]:
        p.chmod(0o755)
    for name in ("root-tasks.sh", "xcspec.py"):
        (root / "helper/root" / name).chmod(0o755 if name.endswith(".sh") else 0o644)
    return root


@pytest.fixture
def app(tmp_path, monkeypatch):
    root = app_tree(tmp_path / "app")
    monkeypatch.setattr(rt, "REPO", root)
    monkeypatch.setattr(rt, "ROOT_SCRIPT", root / "helper/root/root-tasks.sh")
    return root


def test_trusted_root_script_returns_the_real_path(app, tmp_path, monkeypatch):
    assert rt.trusted_root_script() == app / "helper/root/root-tasks.sh"
    # A link inside the app folder is fine; pkexec gets where it points.
    (app / "bin").mkdir(mode=0o755)
    (app / "bin/root").symlink_to(app / "helper/root/root-tasks.sh")
    monkeypatch.setattr(rt, "ROOT_SCRIPT", app / "bin/root")
    assert rt.trusted_root_script() == app / "helper/root/root-tasks.sh"


@pytest.mark.parametrize("target", ["helper/root/root-tasks.sh", "helper/root/xcspec.py", "helper/root", "helper", "."])
@pytest.mark.parametrize("bits", [0o020, 0o002])
def test_group_or_world_writable_files_and_folders_are_refused(app, target, bits):
    p = app / target
    p.chmod(p.stat().st_mode | bits)
    with pytest.raises(HelperError) as e:
        rt.trusted_root_script()
    assert e.value.code == "untrusted-root-script"
    assert "can be changed by other users" in e.value.message and e.value.fix["page"] == "setup"


def test_a_link_out_of_the_app_folder_is_refused(app, tmp_path):
    outside = app_tree(tmp_path / "outside")
    script = app / "helper/root/root-tasks.sh"
    script.unlink()
    script.symlink_to(outside / "helper/root/root-tasks.sh")
    with pytest.raises(HelperError, match="outside the iOS Studio folder"):
        rt.trusted_root_script()
    script.unlink()
    script.write_text("#\n")
    script.chmod(0o755)
    xc = app / "helper/root/xcspec.py"
    xc.unlink()
    xc.symlink_to(outside / "helper/root/xcspec.py")
    with pytest.raises(HelperError, match="xcspec.py is a link to a file outside"):
        rt.trusted_root_script()


def test_files_of_another_user_are_refused(app, monkeypatch):
    monkeypatch.setattr(rt.os, "getuid", lambda: os.stat(app).st_uid + 12345)
    if os.stat(app).st_uid == 0:
        pytest.skip("root owns the test folder: root is always trusted")
    with pytest.raises(HelperError, match="belongs to another user"):
        rt.trusted_root_script()


def test_run_root_checks_before_pkexec_and_passes_the_real_path(app, monkeypatch):
    ran: list[list[str]] = []
    monkeypatch.setattr(rt, "DRY", False)
    monkeypatch.setattr(rt.shutil, "which", lambda name, *a, **k: "/usr/bin/pkexec")
    monkeypatch.setattr(rt, "run_stream", lambda argv, *a, **kw: ran.append(argv) or 0)
    monkeypatch.setattr(rt.Path, "exists", lambda self: False if str(self).endswith("db.lck") else os.path.exists(self))
    link = app / "root-link.sh"
    link.symlink_to(app / "helper/root/root-tasks.sh")
    monkeypatch.setattr(rt, "ROOT_SCRIPT", link)
    rt.run_root(["--avahi"], "uxplay", "Installing UxPlay")
    assert ran == [["pkexec", str(app / "helper/root/root-tasks.sh"), "--avahi"]]
    (app / "helper/root/root-tasks.sh").chmod(0o777)
    with pytest.raises(HelperError):
        rt.run_root(["--avahi"], "uxplay", "Installing UxPlay")
    assert len(ran) == 1                                # pkexec never ran for the writable script


def test_the_checkout_itself_passes():
    """The real checkout (as cloned with the usual umask) is what pkexec runs."""
    if os.stat(REPO).st_mode & 0o022:
        pytest.skip("this checkout was cloned group-writable")
    assert rt.trusted_root_script() == ROOT_TASKS


# ------------------------------------------------------------------ 5. pinned AUR recipes

def test_every_aur_recipe_is_pinned():
    assert set(pins.AUR_COMMITS) == {"swift-bin", "uxplay", "python312"}
    for c in pins.AUR_COMMITS.values():
        assert re.fullmatch(r"[0-9a-f]{40}", c)
    assert not hasattr(pins, "AUR_SNAPSHOT_HEAD")
    assert "--skippgpcheck" not in ROOT_TASKS.read_text().split("No --skippgpcheck")[1]


def test_aur_fetch_needs_a_pinned_commit(monkeypatch, capsys):
    with pytest.raises(HelperError):
        downloads.aur_fetch("uxplay", None, "uxplay")
    monkeypatch.setattr(rt, "DRY", True)
    downloads.aur_fetch("uxplay", pins.UXPLAY_AUR_COMMIT, "uxplay")
    assert f"snapshot/aur-{pins.UXPLAY_AUR_COMMIT}.tar.gz" in capsys.readouterr().out


def test_aur_fetch_unpacks_the_pinned_snapshot(tmp_path, monkeypatch):
    import io
    import tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in (("PKGBUILD", b"pkgname=uxplay\n"), (".SRCINFO", b"pkgbase = uxplay\n")):
            info = tarfile.TarInfo(f"aur-{pins.UXPLAY_AUR_COMMIT}/{name}")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    snap = tmp_path / "snap.tar.gz"
    snap.write_bytes(buf.getvalue())
    monkeypatch.setattr(downloads, "AUR_DIR", tmp_path / "aur")
    urls = []
    real_download = downloads.download
    monkeypatch.setattr(downloads, "download", lambda url, dest, *a, **k: urls.append(url) or real_download(snap.as_uri(), dest, *a, **k))
    d = downloads.aur_fetch("uxplay", pins.UXPLAY_AUR_COMMIT, "uxplay")
    assert (d / "PKGBUILD").read_text() == "pkgname=uxplay\n"
    assert (d / ".ios-studio-ref").read_text().strip() == pins.UXPLAY_AUR_COMMIT
    assert pins.UXPLAY_AUR_COMMIT in urls[0]
    downloads.aur_fetch("uxplay", pins.UXPLAY_AUR_COMMIT, "uxplay")    # the same pin: no second download
    assert len(urls) == 1


def test_debugger_for_an_unpinned_python_is_refused(bundle):
    with pytest.raises(HelperError) as e:
        bundle(["debugger"], by_id={"debugger": {"pkg": "python399"}})
    assert e.value.code == "unsupported" and "python399" in e.value.message
    assert bundle.fetched == []


# ------------------------------------------------------------------ 6. uninstall undoes what Setup changed

def test_uxplay_remembers_that_setup_turned_avahi_on(bundle, tmp_path):
    args = bundle(["uxplay"])
    assert "--avahi" in args
    assert json.loads((tmp_path / "setup.json").read_text())["avahiEnabledBySetup"] is True


def test_avahi_that_was_already_on_is_not_claimed(bundle, tmp_path, monkeypatch):
    monkeypatch.setattr(installers, "probe", lambda argv, **kw: (0 if "is-enabled" in argv else 1, "", ""))
    bundle(["uxplay"])
    assert "avahiEnabledBySetup" not in facts.load_state()


@pytest.fixture
def remove_all(tmp_path, monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(facts, "SETUP_STATE", tmp_path / "setup.json")
    monkeypatch.setattr(toolchain, "run_root", lambda args, sid, label: calls.append(list(args)))
    monkeypatch.setattr(toolchain, "run_stream", lambda *a, **k: 0)
    monkeypatch.setattr(toolchain, "do_rmtree", lambda *a, **k: 0)
    monkeypatch.setattr(toolchain, "do_unlink", lambda *a, **k: None)
    monkeypatch.setattr(toolchain.appdirs, "sdk_bundles", lambda: [])

    def run(state: dict, dry_run: bool = False) -> list[str]:
        facts.fileio.save_json(tmp_path / "setup.json", state)
        monkeypatch.setattr(rt, "DRY", dry_run)
        toolchain.remove_all({"yes": True, "dry_run": dry_run})
        return calls.pop()
    return run


def test_remove_all_closes_the_firewall_and_turns_off_avahi_only_if_setup_turned_it_on(remove_all, tmp_path):
    args = remove_all({"avahiEnabledBySetup": True})
    assert "--remove-ufw-uxplay" in args and "--disable-avahi" in args
    assert facts.load_state().get("avahiEnabledBySetup") is None    # forgotten once undone
    args = remove_all({})
    assert "--remove-ufw-uxplay" in args and "--disable-avahi" not in args


def test_remove_all_args_are_accepted_by_the_root_script(remove_all):
    args = remove_all({"avahiEnabledBySetup": True}, dry_run=True)
    r = dry(*args)
    assert r.returncode == 0, r.stderr
    cmds = commands(r.stdout)
    assert "systemctl disable --now avahi-daemon.service" in cmds
    deletes = [c for c in cmds if c.startswith("ufw delete allow from ")]
    assert len(deletes) == 9 and all(" comment iOS Studio " in c for c in deletes)
    # The exact rules --ufw-uxplay adds, comments included, so ufw deletes only ours.
    text = ROOT_TASKS.read_text()
    added = re.findall(r"run ufw allow (.+)$", text, re.M)
    removed = [x.removesuffix(" || true") for x in re.findall(r"run ufw delete allow (.+)$", text, re.M)]
    assert added and removed == added


def test_hold_and_unhold_keep_only_the_newest_three_backups(tmp_path):
    conf = tmp_path / "pacman.conf"
    conf.write_text("[options]\nHoldPkg = pacman glibc\n")
    old = [tmp_path / f"pacman.conf.bak-ios-studio-2026010{i}-120000" for i in range(1, 5)]
    for p in old:
        p.write_text("old\n")
    r = dry("--hold-swift", env={"IOSSTUDIO_TEST_PACMAN_CONF": str(conf)})
    assert r.returncode == 0, r.stderr
    cmds = commands(r.stdout)
    assert any(c.startswith(f"cp -a {conf} {conf}.bak-ios-studio-") for c in cmds)
    removed = [c.split(" -- ", 1)[1] for c in cmds if c.startswith("rm -f -- ")]
    assert removed == [str(old[0]), str(old[1])]          # 4 old + the new one: the 2 oldest go


def test_a_real_run_ignores_the_test_pacman_conf():
    text = ROOT_TASKS.read_text()
    assert '[[ "$dry" == 1 && -n "${IOSSTUDIO_TEST_PACMAN_CONF:-}" ]]' in text


# ------------------------------------------------------------------ 7. one AUR folder for the helper and the root script

def test_aur_dir_is_home_cache_whatever_xdg_cache_home_says(tmp_path):
    code = "from iosstudio.features.setup import pins; from pathlib import Path; print(pins.AUR_DIR)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30, check=True,
                         env={**os.environ, "XDG_CACHE_HOME": str(tmp_path / "xdg-cache"),
                              "PYTHONPATH": str(REPO / "helper")}).stdout.strip()
    assert out == str(Path.home() / ".cache/ios-studio/aur")
    assert '"$home_real/.cache/ios-studio/aur/"*' in ROOT_TASKS.read_text()
