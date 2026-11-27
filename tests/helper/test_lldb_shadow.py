"""helper/iosstudio/features/lldb_shadow.py against a stub toolchain and a stub darwin SDK bundle (no Swift, no
device): the shadow layout (the SDK's Swift resource dir in lib/swift, the toolchain's linux/ and host/ kept, its
CoreFoundation left out), LLDB's Python package re-pointed at the shadow liblldb (a decoy package refused),
stat-only reuse, rebuilds after a toolchain update, another SDK or a lost stamp, reflink and copy fallbacks,
leftovers of killed builds, concurrent runs and pruning. The same checks as omarchy-apple-dev v0.4.8's
tests/lldb-ios-shadow/check.sh, plus the ones only the port has."""
from __future__ import annotations

import errno
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from iosstudio.core import appdirs
from iosstudio.features import lldb_shadow as ls
from support import ROOT, write_exe

PY = "local/lib64/python3.12/site-packages"
SO = "_lldb.cpython-312-x86_64-linux-gnu.so"


def make_toolchain(base: Path) -> Path:
    """A swift.org-shaped toolchain usr: lldb, lldb-dap and liblldb with its .so links, the Python package whose
    _lldb*.so is a relative link to usr/lib/liblldb.so, and a decoy package that belongs to another liblldb."""
    tc = base / "toolchain/usr"
    for d in ("bin", "lib/swift/linux", "lib/swift/host/compiler", "lib/swift/CoreFoundation", "lib/clang/21",
              f"{PY}/lldb/formatters", "share/doc", "lib/python3/dist-packages/lldb"):
        (tc / d).mkdir(parents=True, exist_ok=True)
    write_exe(tc / "bin/lldb", "#!/bin/sh\necho stub lldb\n")
    write_exe(tc / "bin/lldb-dap", "#!/bin/sh\necho stub dap\n")
    write_exe(tc / "bin/lldb-server", "#!/bin/sh\n")
    (tc / "lib/liblldb.so.21.0.0").write_text("lldb-v1")
    os.symlink("liblldb.so.21.0.0", tc / "lib/liblldb.so.21.0")
    os.symlink("liblldb.so.21.0", tc / "lib/liblldb.so")
    (tc / "lib/libclang.so.21.0.0").write_text("clang")
    (tc / "lib/swift/CoreFoundation/module.modulemap").touch()
    (tc / f"{PY}/lldb/__init__.py").touch()
    (tc / f"{PY}/six.py").touch()
    os.symlink("../../../../../lib/liblldb.so", tc / f"{PY}/lldb/{SO}")
    (tc / "lib/other-liblldb.so").write_text("other")
    (tc / "lib/python3/dist-packages/lldb/__init__.py").touch()
    os.symlink("../../../other-liblldb.so", tc / "lib/python3/dist-packages/lldb/_lldb.so")
    return tc


def make_bundle(config: Path, sdk: str = "iPhoneOS.sdk") -> Path:
    b = config / "swiftpm/swift-sdks/darwin.artifactbundle"
    for d in ("iphoneos", "clang", "host", "apinotes"):
        (b / ls.RESOURCES / d).mkdir(parents=True, exist_ok=True)
    (b / ls.SDKS / sdk / "usr/lib/swift/shims").mkdir(parents=True, exist_ok=True)
    return b


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    tc = make_toolchain(tmp_path)
    bundle = make_bundle(tmp_path / "config")
    return {"tc": tc, "bundle": bundle, "py": str(tc / PY), "cache": tmp_path / "cache/ios-studio/lldb-ios",
            "tmp": tmp_path}


def build(e, **kw) -> ls.Shadow:
    return ls.ensure(e["tc"], kw.pop("bundle", e["bundle"]), kw.pop("py", e["py"]), **kw)


def leftovers(cache: Path) -> list[str]:
    return sorted(p.name for p in cache.iterdir() if p.name.startswith((".build.", ".old.")))


def test_layout(env):
    tc, b = env["tc"], env["bundle"]
    sh = build(env)
    u = sh.root / "usr"
    rd = b / ls.RESOURCES
    assert sh.root.parent == env["cache"] and sh.built and sh.link == "hardlink"
    assert sh.lldb == u / "bin/lldb" and sh.lldb.samefile(tc / "bin/lldb")
    assert sh.lldb_dap == u / "bin/lldb-dap" and sh.lldb_dap.samefile(tc / "bin/lldb-dap")
    assert os.readlink(u / "bin/lldb-server") == str(tc / "bin/lldb-server")   # lldb's support executables
    assert sh.sdk == b / ls.SDKS / "iPhoneOS.sdk"
    assert (u / "lib/liblldb.so.21.0.0").samefile(tc / "lib/liblldb.so.21.0.0")
    assert not (u / "lib/liblldb.so.21.0.0").is_symlink()                    # a real file: LLDB resolves links
    assert os.readlink(u / "lib/liblldb.so.21.0") == "liblldb.so.21.0.0"
    assert os.readlink(u / "lib/liblldb.so") == "liblldb.so.21.0.0"
    for name in ("iphoneos", "clang", "apinotes"):
        assert os.readlink(u / "lib/swift" / name) == str(rd / name)
    assert os.readlink(u / "lib/swift/linux") == str(tc / "lib/swift/linux")
    assert os.readlink(u / "lib/swift/host") == str(tc / "lib/swift/host")  # not the SDK's host/
    assert not os.path.lexists(u / "lib/swift/CoreFoundation")              # the module that shadows the SDK's
    assert os.readlink(u / "lib/libclang.so.21.0.0") == str(tc / "lib/libclang.so.21.0.0")
    assert os.readlink(u / "lib/clang") == str(tc / "lib/clang")
    assert os.readlink(u / "share") == str(tc / "share")
    # the Python package: real dirs on the way, other entries link to the toolchain, _lldb*.so to the shadow
    assert sh.pythonpath == str(u / PY)
    for d in ("local", "local/lib64", PY, f"{PY}/lldb"):
        assert (u / d).is_dir() and not (u / d).is_symlink(), d
    assert os.readlink(u / f"{PY}/lldb/__init__.py") == str(tc / f"{PY}/lldb/__init__.py")
    assert os.readlink(u / f"{PY}/six.py") == str(tc / f"{PY}/six.py")
    assert os.readlink(u / f"{PY}/lldb/formatters") == str(tc / f"{PY}/lldb/formatters")
    assert os.path.realpath(u / f"{PY}/lldb/{SO}") == str(u / "lib/liblldb.so.21.0.0")
    assert subprocess.run([str(sh.lldb)], capture_output=True, text=True, timeout=30).stdout.strip() == "stub lldb"
    stamp = json.loads((sh.root / ".stamp").read_text())
    assert stamp["toolchain"] == str(tc) and stamp["python"] == f"{PY}/lldb" and stamp["link"] == "hardlink"
    assert leftovers(env["cache"]) == []


def test_python_package_must_belong_to_this_liblldb(env):
    with pytest.raises(ls.Unavailable, match="doesn't belong"):
        build(env, py=str(env["tc"] / "lib/python3/dist-packages"))     # the decoy: _lldb.so is another library
    with pytest.raises(ls.Unavailable, match="outside"):
        build(env, py=str(env["tmp"]))
    assert not any(p.name != ".lock" for p in env["cache"].iterdir() if not p.name.startswith(".build."))
    assert leftovers(env["cache"]) == []                                # the failed build cleaned up after itself


def test_without_python_there_is_no_mirror(env):
    sh = ls.ensure(env["tc"], env["bundle"])
    assert sh.pythonpath is None and sh.lldb.is_file()
    assert os.readlink(sh.root / "usr/local") == str(env["tc"] / "local")


def test_reuse_is_stat_only(env, monkeypatch):
    first = build(env)
    (first.root / "marker").touch()
    monkeypatch.setattr(ls, "_build", lambda *a: pytest.fail("rebuilt a current tree"))
    monkeypatch.setattr(ls.fileio, "lock", lambda *a, **k: pytest.fail("took the lock for a current tree"))
    again = build(env)
    assert not again.built and again.root == first.root and again.link == "hardlink"
    assert (again.root / "marker").exists()
    assert {k: v for k, v in again.__dict__.items() if k != "built"} == \
        {k: v for k, v in first.__dict__.items() if k != "built"}


def test_rebuilt_without_a_stamp(env):
    sh = build(env)
    (sh.root / "marker").touch()
    (sh.root / ".stamp").unlink()           # a build that stopped before writing it
    again = build(env)
    assert again.built and not (again.root / "marker").exists() and again.root == sh.root


def test_rebuilt_after_a_toolchain_update(env):
    tc = env["tc"]
    sh = build(env)
    (sh.root / "marker").touch()
    (tc / "lib/liblldb.so.21.0.0").unlink()
    (tc / "lib/liblldb.so.21.0.0").write_text("lldb-v2-longer")      # a new file, as a package update writes it
    again = build(env)
    assert again.built and not (again.root / "marker").exists()
    assert (again.root / "usr/lib/liblldb.so.21.0.0").samefile(tc / "lib/liblldb.so.21.0.0")
    assert again.lldb == sh.lldb


def test_rebuilt_for_another_sdk(env):
    sh = build(env)
    (sh.root / "marker").touch()
    other = make_bundle(env["tmp"] / "config2", sdk="iPhoneOS27.1.sdk")   # only a versioned SDK, as Setup lists it
    again = build(env, bundle=other)
    assert again.built and not (again.root / "marker").exists()
    assert os.readlink(again.root / "usr/lib/swift/iphoneos") == str(other / ls.RESOURCES / "iphoneos")
    assert again.sdk == other / ls.SDKS / "iPhoneOS27.1.sdk"


def test_copy_mode(env):
    tc = env["tc"]
    build(env)
    sh = build(env, modes=("copy",))       # the modes are in the stamp: a hard-linked tree is rebuilt
    lib = sh.root / "usr/lib/liblldb.so.21.0.0"
    assert sh.built and sh.link == "copy"
    assert lib.read_text() == "lldb-v1" and not lib.samefile(tc / "lib/liblldb.so.21.0.0") and lib.stat().st_nlink == 1
    assert os.access(sh.lldb, os.X_OK) and not sh.lldb.samefile(tc / "bin/lldb")
    # Python must reach the copy, not map the toolchain's liblldb a second time
    assert os.path.realpath(Path(sh.pythonpath) / f"lldb/{SO}") == str(lib)


def test_falls_back_from_hard_links_to_reflinks_then_copies(env, monkeypatch):
    def no_link(*a):
        raise OSError(errno.EXDEV, "Invalid cross-device link")      # another filesystem or btrfs subvolume
    clones = []

    def clone(fd, req, src):
        assert req == ls.FICLONE
        clones.append(fd)
        os.sendfile(fd, src, 0, os.fstat(src).st_size)
    monkeypatch.setattr(ls.os, "link", no_link)
    monkeypatch.setattr(ls.fcntl, "ioctl", clone)
    sh = build(env)
    assert sh.link == "reflink" and len(clones) == 3                   # lldb, lldb-dap, liblldb
    assert os.access(sh.lldb, os.X_OK) and (sh.root / "usr/lib/liblldb.so.21.0.0").read_text() == "lldb-v1"

    def no_clone(*a):
        raise OSError(errno.EOPNOTSUPP, "Operation not supported")    # tmpfs, ext4
    monkeypatch.setattr(ls.fcntl, "ioctl", no_clone)
    (sh.root / ".stamp").unlink()
    sh = build(env)
    assert sh.link == "copy" and (sh.root / "usr/lib/liblldb.so.21.0.0").read_text() == "lldb-v1"


def test_leftovers_of_killed_builds_are_cleared(env, monkeypatch):
    env["cache"].mkdir(parents=True)
    (env["cache"] / ".build.dead1/usr").mkdir(parents=True)             # a build killed halfway
    (env["cache"] / ".old.dead2/tree").mkdir(parents=True)             # a replace killed halfway
    sh = build(env)
    assert leftovers(env["cache"]) == [] and sh.built
    # a build failing halfway removes its own temp dir and keeps the tree that was there
    (sh.root / ".stamp").unlink()
    (sh.root / "marker").touch()
    monkeypatch.setattr(ls, "_link_rest", lambda *a: (_ for _ in ()).throw(OSError(errno.ENOSPC, "No space left")))
    with pytest.raises(ls.Unavailable, match="No space left"):
        build(env)
    assert leftovers(env["cache"]) == [] and (sh.root / "marker").exists()


RACE = """
import json, sys
sys.path.insert(0, sys.argv[1])
from iosstudio.features import lldb_shadow as ls
sh = ls.ensure(sys.argv[2], sys.argv[3], sys.argv[4])
print(json.dumps({"built": sh.built, "lldb": str(sh.lldb)}))
"""


def test_concurrent_runs_build_once_and_reuse(env):
    script = env["tmp"] / "race.py"
    script.write_text(RACE)
    procs = [subprocess.Popen([sys.executable, "-I", str(script), str(ROOT / "helper"), str(env["tc"]),
                               str(env["bundle"]), env["py"]], stdout=subprocess.PIPE, text=True,
                              env={**os.environ, "XDG_CACHE_HOME": str(env["tmp"] / "cache")}) for _ in range(8)]
    outs = [json.loads(p.communicate(timeout=60)[0]) for p in procs]
    assert all(p.returncode == 0 for p in procs)
    assert sum(o["built"] for o in outs) == 1 and len({o["lldb"] for o in outs}) == 1
    assert leftovers(env["cache"]) == []
    assert [p.name for p in env["cache"].iterdir() if ls._KEY.fullmatch(p.name)] == [Path(outs[0]["lldb"]).parents[2].name]


def test_old_trees_are_pruned(env):
    cache = env["cache"]
    cache.mkdir(parents=True)
    for i, key in enumerate(("aaaaaaaaaaa1", "aaaaaaaaaaa2", "aaaaaaaaaaa3")):
        (cache / key / "usr").mkdir(parents=True)
        os.utime(cache / key, (1000 + i, 1000 + i))
    (cache / "not-a-tree").mkdir()
    sh = build(env)
    left = sorted(p.name for p in cache.iterdir() if not p.name.startswith("."))
    assert left == sorted(["aaaaaaaaaaa3", sh.root.name, "not-a-tree"])   # the newest KEEP trees, this one first


def test_missing_sdk_parts_are_plain_reasons(env, tmp_path):
    shutil.rmtree(env["bundle"] / ls.SDKS)
    with pytest.raises(ls.Unavailable, match="iPhoneOS.sdk"):
        build(env)
    shutil.rmtree(env["bundle"] / ls.RESOURCES)
    with pytest.raises(ls.Unavailable, match="Swift files"):
        build(env)
    (env["tc"] / "lib/liblldb.so").unlink()
    with pytest.raises(ls.Unavailable, match="lldb"):
        ls.ensure(env["tc"], make_bundle(tmp_path / "c3"))


def test_an_unwritable_cache_is_a_reason(env, monkeypatch):
    (env["tmp"] / "file").write_text("")
    monkeypatch.setenv("XDG_CACHE_HOME", str(env["tmp"] / "file"))
    with pytest.raises(ls.Unavailable, match="couldn't be prepared"):
        build(env)


def test_for_lldb_finds_the_bundle_and_never_raises(env, monkeypatch):
    monkeypatch.setattr(appdirs, "sdk_bundle", lambda *a: env["bundle"])
    sh, why = ls.for_lldb(env["tc"] / "bin/lldb", env["py"])
    assert sh is not None and why == "" and sh.pythonpath
    monkeypatch.setattr(appdirs, "sdk_bundle", lambda *a: None)
    assert ls.for_lldb(env["tc"] / "bin/lldb", env["py"]) == (None, "The iOS SDK isn't installed (Setup installs it).")
    monkeypatch.setattr(appdirs, "sdk_bundle", lambda *a: env["tmp"] / "nothing")
    sh, why = ls.for_lldb(env["tc"] / "bin/lldb", env["py"])
    assert sh is None and "Swift files" in why
