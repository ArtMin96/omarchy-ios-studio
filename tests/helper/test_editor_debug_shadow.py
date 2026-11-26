"""Debug in editor with the shadow lldb (Swift expressions): the editors' stable lldb-dap launcher
(lldb_shadow.launcher_path / write_launcher), the VS Code and Zed configurations that point at it, migrating ones
written before (VS Code's "type": "swift" entry, Zed's dap.Swift.binary naming the toolchain's own lldb-dap), the
attach file's target.sdk-path step (iosstudio-sdk-path, inside lldb-dap), and the plain fallback note when the
shadow can't be built (bridge notice lldb-plain, ready.swiftExpressions, Settings › Code editor setup).

Everything runs on temp folders (pytest's XDG dirs, a fake home and toolchain); the shadow itself is faked at
lldb_shadow.for_lldb (tests/helper/test_lldb_shadow.py covers building it).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import types
from pathlib import Path
from unittest import mock

import pytest

from iosstudio.core import appdirs, jsonc
from iosstudio.features import debugger, lldb_shadow
from iosstudio.features import editor_debug as ed
from iosstudio.features import editor_debug_bridge as bridge
from iosstudio.features import editor_setup as E
from iosstudio.features.editor_setup import editors as E_editors
from support.fake_deviced import FakeDeviced


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    for var, sub in (("XDG_STATE_HOME", ".local/state"), ("XDG_CONFIG_HOME", ".config"), ("XDG_CACHE_HOME", ".cache")):
        monkeypatch.setenv(var, str(h / sub))
    with mock.patch.object(appdirs, "HOME", h):
        yield h


def exe(path: Path, body: str = "exit 0") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)
    return path


def fake_shadow(root: Path, sdk: Path, dap_body: str = "echo shadow") -> lldb_shadow.Shadow:
    u = root / "usr"
    exe(u / "bin/lldb")
    return lldb_shadow.Shadow(root, u / "bin/lldb", exe(u / "bin/lldb-dap", dap_body), str(u / "lib/python3/site"),
                              sdk, "hardlink")


def project(tmp_path: Path, name: str = "Hello", bundle: str = "com.example.hello") -> Path:
    p = tmp_path / name
    p.mkdir()
    (p / "Package.swift").write_text(f'// swift-tools-version: 6.0\nimport PackageDescription\nlet package = Package(name: "{name}")\n')
    (p / "xtool.yml").write_text(f"version: 1\nbundleID: {bundle}\nproduct: {name}\n")
    return p


# ================================================================ the launcher (a stable path)

def test_launcher_runs_the_shadow_and_falls_back_to_the_toolchain(home, tmp_path):
    plain = exe(tmp_path / "tc/usr/bin/lldb-dap", 'echo "plain $*"')
    sh = fake_shadow(tmp_path / "shadow", tmp_path / "sdk", 'echo "shadow $*"')
    path = lldb_shadow.write_launcher(plain, sh)
    assert path == home / ".cache/ios-studio/lldb-ios/lldb-dap" == lldb_shadow.launcher_path()
    assert os.access(path, os.X_OK)
    assert subprocess.run([str(path), "--x", "a b"], capture_output=True, text=True).stdout == "shadow --x a b\n"
    sh.lldb_dap.unlink()                   # a shadow pruned or being rebuilt: the toolchain's own still starts
    assert subprocess.run([str(path)], capture_output=True, text=True).stdout == "plain \n"
    lldb_shadow.write_launcher(plain, None)
    assert "shadow=" not in path.read_text()


def test_launcher_is_rewritten_only_when_it_changes(home, tmp_path):
    plain = exe(tmp_path / "tc it's/usr/bin/lldb-dap", "echo plain")     # quoting survives odd paths
    path = lldb_shadow.write_launcher(plain, None)
    ino = path.stat().st_ino
    assert lldb_shadow.write_launcher(plain, None).stat().st_ino == ino
    assert subprocess.run([str(path)], capture_output=True, text=True).stdout == "plain\n"
    lldb_shadow.write_launcher(plain, fake_shadow(tmp_path / "s", tmp_path / "sdk"))
    assert path.stat().st_ino != ino
    assert [p.name for p in path.parent.iterdir()] == ["lldb-dap"]   # no temp files left behind


def test_launcher_survives_shadow_pruning(home, tmp_path):
    lldb_shadow.write_launcher(exe(tmp_path / "lldb-dap"), None)
    cache = lldb_shadow.cache_root()
    from iosstudio.core import fileio
    fileio.prune((p for p in cache.iterdir() if lldb_shadow._KEY.fullmatch(p.name)), 0, min_keep=0)
    lldb_shadow._clear_leftovers(cache)
    assert lldb_shadow.launcher_path().is_file()


# ================================================================ the editor configurations

def test_vscode_config_names_the_launcher_without_a_machine_path(home):
    cfg = ed.vscode_config("Hello", ed.script_path("com.example.hello"))
    assert (cfg["type"], cfg["request"]) == ("lldb-dap", "attach")
    assert cfg["debugAdapterExecutable"] == "${userHome}/.cache/ios-studio/lldb-ios/lldb-dap"
    assert str(home) not in json.dumps(cfg)


def test_vscode_config_with_a_cache_outside_home_is_absolute(home, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "elsewhere"))
    cfg = ed.vscode_config("Hello", ed.script_path("com.example.hello"))
    assert cfg["debugAdapterExecutable"] == str(tmp_path / "elsewhere/ios-studio/lldb-ios/lldb-dap")


def test_zed_config_is_unchanged(home):
    cfg = ed.zed_config("Hello", ed.script_path("com.example.hello"))
    assert cfg["adapter"] == "Swift" and "debugAdapterExecutable" not in cfg   # Zed takes dap.Swift.binary


OLD_LAUNCH = """{
  // my configs
  "version": "0.2.0",
  "configurations": [
    {"name": "Other", "type": "node", "request": "attach"},
    {
      "name": "Attach to Hello on iPhone (iOS Studio)",
      "type": "swift",
      "request": "attach",
      "attachCommands": ["script import os; lldb.debugger.HandleCommand('command source \\"%s\\"' % os.path.expanduser('~/.local/state/ios-studio/editor-debug/com.example.hello.lldb'))"],
      "timeout": 300
    }, // ours, from before
  ],
}
"""


def test_old_vscode_entry_is_migrated_by_editor_config(home, tmp_path):
    p = project(tmp_path)
    (p / ".vscode").mkdir()
    (p / ".vscode/launch.json").write_text(OLD_LAUNCH)
    dry = ed.editor_config(p, "vscode", dry_run=True)
    e = dry["editors"][0]
    assert (e["state"], e["action"]["kind"]) == ("outdated", "merge")
    assert '"type": "lldb-dap"' in e["action"]["preview"] and '-      "type": "swift",' in e["action"]["preview"]
    out = ed.editor_config(p, "vscode")
    assert out["applied"]
    text = (p / ".vscode/launch.json").read_text()
    assert text.startswith('{\n  // my configs\n')
    assert '{"name": "Other", "type": "node", "request": "attach"},' in text and "}, // ours, from before" in text
    mine = jsonc.load(text)["configurations"][1]
    assert mine == ed.vscode_config("Hello", ed.script_path("com.example.hello"))
    assert ed.editor_config(p, "vscode")["editors"][0]["state"] == "present"     # then current


def test_editor_config_puts_a_launcher_in_place(home, tmp_path, monkeypatch):
    tc = tmp_path / "toolchain"
    exe(tc / "usr/bin/lldb-dap", "echo plain")
    monkeypatch.setattr("iosstudio.features.setup.toolchain_root", lambda: tc)
    ed.editor_config(project(tmp_path), "vscode")
    launcher = lldb_shadow.launcher_path()
    assert subprocess.run([str(launcher)], capture_output=True, text=True).stdout == "plain\n"
    launcher.write_text("#!/bin/sh\necho kept\n")                   # one the bridge wrote stays as it is
    ed.editor_config(project(tmp_path, "Two", "com.example.two"), "vscode")
    assert "kept" in launcher.read_text()


def test_vscode_note_when_the_lldb_dap_extension_is_missing(home, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(home))
    exts = home / ".vscode/extensions"
    (exts / "swiftlang.swift-vscode-2.4.0").mkdir(parents=True)
    out = ed.editor_config(project(tmp_path), "vscode", dry_run=True)
    assert any("llvm-vs-code-extensions.lldb-dap" in n for n in out["notes"])
    (exts / "llvm-vs-code-extensions.lldb-dap-0.4.1").mkdir()
    assert ed.editor_config(project(tmp_path, "B", "com.b"), "vscode", dry_run=True)["notes"] == []


# ================================================================ Zed: the note and the migration (Settings)

def zed_env(tmp_path: Path, home: Path) -> tuple[E.Env, Path]:
    bin_, tc = tmp_path / "bin", tmp_path / "toolchain/usr/bin"
    for n in ("zeditor", "sourcekit-lsp", "xtool"):
        exe(bin_ / n)
    for n in ("swift", "lldb", "lldb-dap"):
        exe(tc / n)
    (bin_ / "swift").symlink_to(tc / "swift")
    (home / ".local/share/zed/extensions/installed/swift").mkdir(parents=True)
    return E.Env(home=home, config=home / ".config", data=home / ".local/share", path=str(bin_),
                 state=home / ".local/state/ios-studio", swift_version=(6, 4), probe_swift=False), tc


def write_zed(home: Path, binary: str) -> Path:
    zs = home / ".config/zed/settings.json"
    zs.parent.mkdir(parents=True, exist_ok=True)
    zs.write_text('// Zed settings\n{\n  "theme": "Omazed", // mine\n  "dap": {\n    "Swift": {\n'
                  f'      "binary": "{binary}"\n    }}\n  }},\n}}\n')
    return zs


def test_zed_setup_replaces_the_plain_lldb_dap_it_wrote_before(home, tmp_path):
    env, tc = zed_env(tmp_path, home)
    zs = write_zed(home, str(tc / "lldb-dap"))
    assert E.editor_info("zed", env)["state"] == "needs-setup"          # Settings shows Set up for existing users
    acts = E.zed_machine_actions(env)
    assert [a["kind"] for a in acts] == ["backup", "merge"]
    assert acts[1]["keys"] == ["dap › Swift › binary"] and "set before" in acts[1]["detail"]
    E.apply_actions(acts, env)
    text = zs.read_text()
    assert text.startswith("// Zed settings\n") and '"theme": "Omazed", // mine' in text
    assert jsonc.load(text)["dap"]["Swift"]["binary"] == str(lldb_shadow.launcher_path())
    assert Path(acts[0]["path"]).read_text().count(str(tc / "lldb-dap")) == 1      # the backup has the old value
    assert E.zed_machine_actions(env)[0]["configured"]
    assert E.editor_info("zed", env)["state"] == "ready"


@pytest.mark.parametrize("old", ["gone", "other-cache"])
def test_zed_setup_replaces_a_missing_path_or_an_old_launcher(home, tmp_path, old):
    env, _ = zed_env(tmp_path, home)
    value = str(tmp_path / "gone/usr/bin/lldb-dap") if old == "gone" else str(exe(tmp_path / "c/lldb-ios/lldb-dap"))
    write_zed(home, value)
    acts = E.zed_machine_actions(env)
    assert acts[-1]["kind"] == "merge" and acts[-1]["keys"] == ["dap › Swift › binary"]


def test_zed_setup_keeps_an_lldb_dap_of_the_users_own(home, tmp_path):
    env, _ = zed_env(tmp_path, home)
    write_zed(home, str(exe(tmp_path / "swiftly/bin/lldb-dap")))
    acts = E.zed_machine_actions(env)
    assert [a["kind"] for a in acts] == ["skip"] and acts[0]["kept"][0]["key"] == "dap › Swift › binary"


def test_zed_note_names_the_launcher_or_says_why_expressions_may_fail(home, tmp_path, monkeypatch):
    env, tc = zed_env(tmp_path, home)
    monkeypatch.setattr(E.Env, "from_os", classmethod(lambda cls: env))
    p = project(tmp_path)
    assert any("can't find" in n for n in ed.editor_config(p, "zed", dry_run=True)["notes"])
    write_zed(home, str(tc / "lldb-dap"))
    assert any("Swift expressions" in n for n in ed.editor_config(p, "zed", dry_run=True)["notes"])
    write_zed(home, str(lldb_shadow.launcher_path()))
    assert ed.editor_config(p, "zed", dry_run=True)["notes"] == []


def test_machine_setup_prepares_the_debugger_and_passes_its_note_on(home, tmp_path, monkeypatch):
    env, _ = zed_env(tmp_path, home)
    calls = []

    def prepare():
        calls.append(1)
        return ed.expressions_note("The iOS SDK isn't installed (Setup installs it).")
    monkeypatch.setattr(E_editors, "prepare_debugger", prepare)
    dry = E.machine_setup("zed", False, env, prefer=False)
    assert calls == [] and dry["notes"] == []                 # a dry run builds nothing
    out = E.machine_setup("zed", True, env, prefer=False)
    assert calls == [1]
    assert out["notes"][-1].startswith("Swift expressions (p, po) may not work")
    assert "the iOS SDK isn't installed" in out["notes"][-1]
    assert out["state"] == "ready"


def test_prepare_debugger_never_raises(monkeypatch):
    def broken():
        raise debugger.HelperError("needs-python", "The debugger needs Python 3.12.")
    monkeypatch.setattr(debugger, "toolchain", broken)
    assert E_editors.prepare_debugger() == ""


# ================================================================ prepare_dap and the attach file

def tc_at(tmp_path: Path) -> debugger.Toolchain:
    b = tmp_path / "tc/usr/bin"
    exe(b / "lldb")
    exe(b / "lldb-dap", "echo plain")
    return debugger.Toolchain(str(b / "lldb"), "lldb version 21 (test)", "/usr/bin/python3",
                              str(tmp_path / "tc/usr/lib/python3/site"), {})


def test_prepare_dap_with_the_shadow(home, tmp_path, monkeypatch):
    tc = tc_at(tmp_path)
    sh = fake_shadow(tmp_path / "shadow", tmp_path / "sdk")
    seen = []
    monkeypatch.setattr(lldb_shadow, "for_lldb", lambda lldb, py: (seen.append((lldb, py)), (sh, ""))[1])
    d = ed.prepare_dap(tc)
    assert seen == [(tc.lldb, tc.pythonpath)]           # the same stamp as the session driver's: one shadow tree
    assert (d.shadow, d.note) == (sh, "")
    assert subprocess.run([str(d.launcher)], capture_output=True, text=True).stdout == "shadow\n"


def test_prepare_dap_without_the_shadow_runs_the_toolchains_lldb_dap(home, tmp_path, monkeypatch):
    monkeypatch.setattr(lldb_shadow, "for_lldb", lambda *a: (None, "The iOS SDK isn't installed (Setup installs it)."))
    d = ed.prepare_dap(tc_at(tmp_path))
    assert d.shadow is None
    assert d.note == ("Swift expressions (p, po) may not work when debugging from your editor: the iOS SDK isn't "
                      "installed (Setup installs it). Variables and breakpoints still work.")
    assert subprocess.run([str(d.launcher)], capture_output=True, text=True).stdout == "plain\n"


def test_prepare_dap_needs_the_shadows_lldb_dap_and_python(home, tmp_path, monkeypatch):
    sh = fake_shadow(tmp_path / "shadow", tmp_path / "sdk")
    for broken in (lldb_shadow.Shadow(sh.root, sh.lldb, None, sh.pythonpath, sh.sdk, "copy"),
                   lldb_shadow.Shadow(sh.root, sh.lldb, sh.lldb_dap, None, sh.sdk, "copy")):
        monkeypatch.setattr(lldb_shadow, "for_lldb", lambda *a, b=broken: (b, ""))
        d = ed.prepare_dap(tc_at(tmp_path))
        assert d.shadow is None and "may not work" in d.note


def kw(**extra) -> dict:
    return {"port": 52917, "host": "127.0.0.1", "pid": 913, "helper_pid": 4242, "helper_start": "1",
            "local_exe": "/p/Hello-App", "remote_path": "/var/Hello.app/Hello", "sysroot": "/s", **extra}


def test_attach_file_sets_the_sdk_before_the_target(home):
    lines = ed.attach_text("com.example.hello", "Hello", **kw(sdk_path="/sdk/iPhoneOS.sdk",
                                                              lldb_lib="/c/lldb-ios/abc/usr/lib")).splitlines()
    i = lines.index('iosstudio-sdk-path "/sdk/iPhoneOS.sdk" "/c/lldb-ios/abc/usr/lib"')
    assert lines.index("settings set target.memory-module-load-level minimal") < i
    assert i < next(n for n, x in enumerate(lines) if x.startswith("platform select"))
    assert i < next(n for n, x in enumerate(lines) if x.startswith("target create"))
    plain = ed.attach_text("com.example.hello", "Hello", **kw())
    assert "sdk-path" not in plain


# ================================================================ iosstudio-sdk-path (runs inside lldb-dap)

class _Ret:
    def __init__(self, ok: bool = True, err: str = ""):
        self.ok, self.err, self.warnings, self.error = ok, err, [], None

    def AppendWarning(self, s):
        self.warnings.append(s)

    def SetError(self, s):
        self.error = s

    def Succeeded(self):
        return self.ok

    def GetError(self):
        return self.err


@pytest.fixture
def lldb_module(monkeypatch):
    """editor_debug_lldb with a stand-in `lldb` module: SBHostOS says where liblldb is, commands are recorded."""
    fake = types.SimpleNamespace(shlib="/x", ran=[], ePathTypeLLDBShlibDir=1)
    fake.SBHostOS = types.SimpleNamespace(GetLLDBPath=lambda _t: types.SimpleNamespace(fullpath=fake.shlib))
    fake.SBCommandReturnObject = _Ret
    monkeypatch.setitem(sys.modules, "lldb", fake)
    monkeypatch.delitem(sys.modules, "iosstudio.features.editor_debug_lldb", raising=False)
    import iosstudio.features.editor_debug_lldb as mod
    interp = types.SimpleNamespace(HandleCommand=lambda cmd, r: fake.ran.append(cmd))
    fake.debugger = types.SimpleNamespace(GetCommandInterpreter=lambda: interp)
    yield mod, fake
    sys.modules.pop("iosstudio.features.editor_debug_lldb", None)


def test_sdk_path_is_set_only_in_the_shadow(lldb_module, tmp_path):
    mod, fake = lldb_module
    lib = tmp_path / "lldb-ios/abc/usr/lib"
    lib.mkdir(parents=True)
    (tmp_path / "link").symlink_to(lib)
    fake.shlib = str(tmp_path / "link")                  # symlinks resolved on both sides
    r = _Ret()
    mod.sdk_path(fake.debugger, f'"/S D K/iPhone\\"OS.sdk" "{lib}"', r, None)
    assert fake.ran == ['settings set target.sdk-path "/S D K/iPhone\\"OS.sdk"'] and r.warnings == []
    fake.ran.clear()
    fake.shlib = "/usr/lib/swift/usr/lib"                # the editor runs the toolchain's own lldb-dap
    r = _Ret()
    mod.sdk_path(fake.debugger, f'"/sdk" "{lib}"', r, None)
    assert fake.ran == [] and r.warnings == [mod.PLAIN_LLDB] and r.error is None   # a warning, the attach goes on
    r = _Ret()
    mod.sdk_path(fake.debugger, '"/sdk"', r, None)
    assert r.error


# ================================================================ demo mode keeps the real shapes

def test_demo_po_has_a_description_like_the_real_driver():
    from iosstudio.demo import debugger_demo as demo
    assert demo._parse_expr("po day") == ("day", True)
    assert demo._parse_expr("expression -O -- day") == ("day", True)
    assert demo._parse_expr("p days.count") == ("days.count", False)
    assert demo._parse_expr("day") == ("day", False)
    v = {"name": "day", "type": "Day", "value": "Mon", "children": [{"name": "high", "type": "Double", "value": "24.5"}]}
    assert demo._description(v) == "▿ Day\n  - high : 24.5"
    assert demo._description({"name": "$R0", "type": "Int", "value": "7"}) == "7"


# ================================================================ the bridge: notice, ready, attach file

@pytest.fixture
def fake_tc(tmp_path, monkeypatch):
    tc = tc_at(tmp_path)
    monkeypatch.setattr(debugger, "toolchain", lambda: tc)
    monkeypatch.setattr(bridge, "POLL", 0.02)
    monkeypatch.setattr(bridge, "APP_POLL", 0.05)
    monkeypatch.setenv("IOSSTUDIO_EDITOR_DEBUG_LIFETIME", "0.3")
    return tc


def run_bridge(capsys, bid: str) -> tuple[list[dict], str]:
    seen = {}

    def start(a):
        return {"bundleId": a["bundleId"], "port": 50011, "host": "127.0.0.1", "pid": 9, "launched": False,
                "symbolsReady": True, "sysroot": "/s"}

    def stop(a):
        seen["attach"] = ed.script_path(bid).read_text()   # the attach file while the bridge ran
        return {"bundleId": a["bundleId"]}
    with FakeDeviced(Path(os.environ["IOSSTUDIO_RUNTIME_DIR"]),
                     {"debug.start": start, "processes.list": [{"pid": 9}], "debug.stop": stop}):
        assert bridge.cmd_editor({"bundle_id": bid}) == 0
    return [json.loads(x) for x in capsys.readouterr().out.splitlines() if x.strip()], seen["attach"]


def test_bridge_with_the_shadow(home, fake_tc, capsys, tmp_path, monkeypatch):
    sh = fake_shadow(tmp_path / "shadow", tmp_path / "sdk")
    monkeypatch.setattr(lldb_shadow, "for_lldb", lambda *a: (sh, ""))
    ev, attach = run_bridge(capsys, "com.example.s")
    assert not any(e.get("id") == "lldb-plain" for e in ev)
    assert next(e for e in ev if e.get("event") == "ready")["swiftExpressions"] is True
    assert f'iosstudio-sdk-path "{sh.sdk}" "{sh.root / "usr/lib"}"' in attach
    assert f"shadow={sh.lldb_dap}" in lldb_shadow.launcher_path().read_text()


def test_bridge_without_the_shadow_says_so(home, fake_tc, capsys, monkeypatch):
    monkeypatch.setattr(lldb_shadow, "for_lldb", lambda *a: (None, "The iOS SDK isn't installed (Setup installs it)."))
    ev, attach = run_bridge(capsys, "com.example.p")
    notice = next(e for e in ev if e.get("id") == "lldb-plain")
    assert notice["level"] == "info" and "Swift expressions (p, po) may not work" in notice["message"]
    names = [e.get("event") for e in ev]
    assert names.index("notice") < names.index("ready")
    assert next(e for e in ev if e.get("event") == "ready")["swiftExpressions"] is False
    assert "sdk-path" not in attach
    assert "shadow=" not in lldb_shadow.launcher_path().read_text()
    assert ev[-1]["ok"]


def test_bridge_fails_plainly_when_the_launcher_cant_be_written(home, fake_tc, capsys, monkeypatch):
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(lldb_shadow, "for_lldb", lambda *a: (None, "x"))

    def nope(*a):
        raise PermissionError(13, "Permission denied", str(lldb_shadow.launcher_path()))
    monkeypatch.setattr(lldb_shadow, "write_launcher", nope)
    assert bridge.cmd_editor({"bundle_id": "com.example.w"}) == 1
    done = [json.loads(x) for x in capsys.readouterr().out.splitlines() if x.strip()][-1]
    assert done["error"]["code"] == "write-failed" and "~/.cache/ios-studio/lldb-ios/lldb-dap" in done["error"]["message"]
