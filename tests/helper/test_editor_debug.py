"""Debug in editor (features/editor_debug.py): the VS Code and Zed attach configurations (new file, merged into an
existing one with comments and other configurations kept byte for byte, re-run is a skip, an outdated entry is
updated alone), the attach file (sysroot or not, the stub after a bridge ends), pruning, running-bridge state and
the editor-connection check on /proc/net/tcp."""
from __future__ import annotations

import ast
import json
import os
import signal
import socket
import threading
import time
from pathlib import Path
from unittest import mock

import pytest

from iosstudio.core import appdirs, jsonc
from iosstudio.features import editor_debug as ed
from iosstudio.features import editor_debug_bridge as bridge
from support.fake_deviced import FakeDeviced


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("XDG_STATE_HOME", str(h / ".local/state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(h / ".config"))
    with mock.patch.object(appdirs, "HOME", h):
        yield h


def project(tmp_path: Path, name: str = "Hello", bundle: str = "com.example.hello") -> Path:
    p = tmp_path / name
    p.mkdir()
    (p / "Package.swift").write_text(f'// swift-tools-version: 6.0\nimport PackageDescription\nlet package = Package(name: "{name}")\n')
    (p / "xtool.yml").write_text(f"version: 1\nbundleID: {bundle}\nproduct: {name}\n")
    return p


# ================================================================ the attach command

def test_attach_command_uses_a_home_relative_path(home):
    script = ed.script_path("com.example.hello")
    cmd = ed.attach_command(script)
    assert str(home) not in cmd
    assert "os.path.expanduser('~/.local/state/ios-studio/editor-debug/com.example.hello.lldb')" in cmd
    assert cmd.startswith("script import os; lldb.debugger.HandleCommand('command source \"%s\"'")


def test_attach_command_outside_home_is_absolute(home, tmp_path):
    assert ed.attach_command(tmp_path / "elsewhere/x.lldb") == f'command source "{tmp_path}/elsewhere/x.lldb"'


def test_xtl_ids_share_the_project_file(home):
    assert ed.script_path("XTL-ABC123.com.example.hello") == ed.script_path("com.example.hello")
    assert ed.file_key("a b/c") == "a_b_c"


# ================================================================ VS Code launch.json

EXISTING = """{
  // My own launch configs
  "version": "0.2.0",
  "configurations": [
    {
      "name": "Run tests", // keep this
      "type": "lldb",
      "request": "launch",
      "program": "${workspaceFolder}/.build/debug/HelloPackageTests.xctest",
    },
    /* a block comment */
    {"name": "Other", "type": "node", "request": "attach"}
  ],
  "compounds": []
}
"""


def config_of(p: Path, editor: str = "vscode") -> dict:
    return ed.editor_config(p, editor)


def test_new_launch_json(home, tmp_path):
    p = project(tmp_path)
    out = config_of(p)
    e = out["editors"][0]
    assert (e["id"], e["state"], e["action"]["kind"], out["applied"]) == ("vscode", "missing", "write", True)
    doc = json.loads((p / ".vscode/launch.json").read_text())
    assert doc["version"] == "0.2.0"
    assert doc["configurations"] == [ed.vscode_config("Hello", ed.script_path("com.example.hello"))]
    assert doc["configurations"][0]["name"] == out["configName"] == "Attach to Hello on iPhone (iOS Studio)"
    assert out["scriptPath"] == str(ed.script_path("com.example.hello"))
    assert "iosstudio-fail" in Path(out["scriptPath"]).read_text()   # F5 before a bridge runs: a readable refusal
    assert [x.name for x in p.rglob("*") if x.is_file() and x.parent.name == ".vscode"] == ["launch.json"]


def test_merge_keeps_everything_else_byte_for_byte(home, tmp_path):
    p = project(tmp_path)
    (p / ".vscode").mkdir()
    (p / ".vscode/launch.json").write_text(EXISTING)
    out = config_of(p)
    assert out["editors"][0]["action"]["kind"] == "merge"
    assert "Adds" in out["editors"][0]["action"]["detail"]
    text = (p / ".vscode/launch.json").read_text()
    for line in EXISTING.splitlines():
        assert line in text.splitlines() or line.strip() == '{"name": "Other", "type": "node", "request": "attach"}'
    doc = jsonc.load(text)
    assert [c["name"] for c in doc["configurations"]] == ["Run tests", "Other", out["configName"]]
    assert doc["compounds"] == []
    # running again: nothing to do
    again = config_of(p)
    assert (again["editors"][0]["state"], again["editors"][0]["action"]["kind"], again["applied"]) == \
        ("present", "skip", False)
    assert (p / ".vscode/launch.json").read_text() == text


def test_outdated_entry_is_updated_alone(home, tmp_path):
    p = project(tmp_path)
    (p / ".vscode").mkdir()
    old = dict(ed.vscode_config("Hello", ed.script_path("com.example.hello")), timeout=30,
               attachCommands=["command source \"/old/place/editor-debug/com.example.hello.lldb\""])
    text = EXISTING.replace('"compounds": []', '"compounds": []').replace(
        '{"name": "Other", "type": "node", "request": "attach"}',
        '{"name": "Other", "type": "node", "request": "attach"},\n    ' + json.dumps(old))
    (p / ".vscode/launch.json").write_text(text)
    plan = ed.editor_config(p, "vscode", dry_run=True)
    assert (plan["editors"][0]["state"], plan["editors"][0]["action"]["kind"]) == ("outdated", "merge")
    assert "Updates" in plan["editors"][0]["action"]["detail"]
    assert "-    " in plan["editors"][0]["action"]["preview"]
    assert (p / ".vscode/launch.json").read_text() == text           # dry run
    config_of(p)
    new = (p / ".vscode/launch.json").read_text()
    doc = jsonc.load(new)
    assert [c["name"] for c in doc["configurations"]] == ["Run tests", "Other", "Attach to Hello on iPhone (iOS Studio)"]
    assert doc["configurations"][2] == ed.vscode_config("Hello", ed.script_path("com.example.hello"))
    assert new.split('"Other"')[0] == text.split('"Other"')[0]       # everything before our entry unchanged


def test_renamed_entry_is_found_by_its_attach_file(home, tmp_path):
    p = project(tmp_path)
    (p / ".vscode").mkdir()
    mine = dict(ed.vscode_config("OldName", ed.script_path("com.example.hello")))
    (p / ".vscode/launch.json").write_text(json.dumps({"version": "0.2.0", "configurations": [mine]}, indent=2))
    config_of(p)
    doc = jsonc.load((p / ".vscode/launch.json").read_text())
    assert [c["name"] for c in doc["configurations"]] == ["Attach to Hello on iPhone (iOS Studio)"]


def test_launch_json_without_configurations_gets_the_list(home, tmp_path):
    p = project(tmp_path)
    (p / ".vscode").mkdir()
    (p / ".vscode/launch.json").write_text('{\n  // nothing yet\n  "version": "0.2.0"\n}\n')
    config_of(p)
    text = (p / ".vscode/launch.json").read_text()
    assert "// nothing yet" in text
    assert len(jsonc.load(text)["configurations"]) == 1


@pytest.mark.parametrize("bad", ["{ not json", "[1, 2]", '{"configurations": {"a": 1}}'])
def test_unusable_files_are_left_alone(home, tmp_path, bad):
    p = project(tmp_path)
    (p / ".vscode").mkdir()
    (p / ".vscode/launch.json").write_text(bad)
    out = config_of(p)
    assert out["editors"][0]["action"]["kind"] == "skip"
    assert "left alone" in out["editors"][0]["action"]["detail"]
    assert (p / ".vscode/launch.json").read_text() == bad
    assert out["applied"] is False


# ================================================================ Zed debug.json

def test_new_zed_file(home, tmp_path):
    p = project(tmp_path)
    out = config_of(p, "zed")
    assert out["editors"][0]["action"]["kind"] == "write"
    doc = json.loads((p / ".zed/debug.json").read_text())
    assert doc == [ed.zed_config("Hello", ed.script_path("com.example.hello"))]
    assert doc[0]["adapter"] == "Swift" and doc[0]["label"] == out["configName"]


def test_zed_merge_keeps_comments_and_entries(home, tmp_path):
    p = project(tmp_path)
    (p / ".zed").mkdir()
    existing = '[\n  // tests\n  {\n    "label": "Debug tests",\n    "adapter": "CodeLLDB",\n    "request": "launch",\n' \
               '    "program": "$ZED_WORKTREE_ROOT/.build/debug/x"\n  }\n]\n'
    (p / ".zed/debug.json").write_text(existing)
    config_of(p, "zed")
    text = (p / ".zed/debug.json").read_text()
    assert text.startswith(existing.rstrip("]\n").rstrip())
    assert [c["label"] for c in jsonc.load(text)] == ["Debug tests", "Attach to Hello on iPhone (iOS Studio)"]
    assert config_of(p, "zed")["editors"][0]["state"] == "present"


def test_upsert_on_one_line_lists():
    script = Path("/s/editor-debug/x.lldb")
    entry = ed.zed_config("X", script)
    r = ed.upsert_entry('[{"label": "A", "adapter": "B"}]', [], entry, script)
    assert r["status"] == "added"
    assert [x["label"] for x in jsonc.load(r["text"])] == ["A", entry["label"]]
    r2 = ed.upsert_entry(r["text"], [], entry, script)
    assert r2["status"] == "current"


# ================================================================ which editors

def test_editor_choice(home, tmp_path, monkeypatch):
    with mock.patch("iosstudio.features.editor_setup.preferred", return_value="zed"):
        assert ed.pick_editors(None) == (["zed"], [])
    with mock.patch("iosstudio.features.editor_setup.preferred", return_value="neovim"):
        ids, notes = ed.pick_editors(None)
        assert ids == [] and "nvim-dap" in notes[0]
    with mock.patch("iosstudio.features.editor_setup.preferred", return_value=None), \
         mock.patch("iosstudio.features.editor_setup.editors.vscode_installed", return_value="/usr/bin/code"), \
         mock.patch("iosstudio.features.editor_setup.editors.zed_installed", return_value=None):
        assert ed.pick_editors(None) == (["vscode"], [])
    assert ed.pick_editors("vscode") == (["vscode"], [])
    with pytest.raises(ed.HelperError):
        ed.pick_editors("emacs")


def test_zed_note_when_lldb_dap_is_not_on_path(home, tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    out = config_of(project(tmp_path), "zed")
    assert any("Settings › Code editor" in n for n in out["notes"])


def test_no_zed_note_once_zed_settings_name_lldb_dap(home, tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    cfg = tmp_path / "xdg-config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg))
    (cfg / "zed").mkdir(parents=True)
    (cfg / "zed/settings.json").write_text('// mine\n{"dap": {"Swift": {"binary": "/opt/swift/usr/bin/lldb-dap"}},}\n')
    out = config_of(project(tmp_path), "zed")
    assert not any("lldb-dap" in n for n in out["notes"])


# ================================================================ the attach file

def test_attach_file_with_symbols(home):
    text = ed.attach_text("com.example.hello", "Hello", port=52917, host="127.0.0.1", pid=913, helper_pid=4242,
                          helper_start="12345", local_exe="/p/Hello/.build/debug/Hello-App",
                          remote_path="/private/var/containers/Bundle/Application/X/Hello.app/Hello",
                          sysroot="/c/DeviceSupport/27.2 (24B115)")
    lines = [x for x in text.splitlines() if not x.startswith("#")]
    module = ed.folder() / "iosstudio_editor_attach.py"
    assert lines == [
        f'command script import "{module}"',
        "iosstudio-check 4242 12345",
        "settings set target.memory-module-load-level minimal",
        'platform select remote-ios --sysroot "/c/DeviceSupport/27.2 (24B115)"',
        'target create "/p/Hello/.build/debug/Hello-App"',
        'iosstudio-remote-file "/private/var/containers/Bundle/Application/X/Hello.app/Hello"',
        "iosstudio-connect connect://127.0.0.1:52917",
        "process attach --pid 913",
    ]


def test_attach_file_without_symbols_or_build(home):
    text = ed.attach_text("b", "A", port=1, host="", pid=2, helper_pid=3, helper_start="4", local_exe=None,
                          remote_path="/r", sysroot=None, platform="remote-linux")
    assert "platform select remote-linux\n" in text
    assert "target create" not in text and "iosstudio-remote-file" not in text
    assert "connect://127.0.0.1:1" in text


def test_quoting():
    assert ed._q('a "b" \\c') == '"a \\"b\\" \\\\c"'


def test_write_and_stub(home):
    p = ed.write_attach("XTL-T.com.example.hello", "Hello", port=1, host="127.0.0.1", pid=2, helper_pid=3,
                        helper_start="4", local_exe=None, remote_path=None, sysroot=None)
    assert p == ed.script_path("com.example.hello")
    module = ed.folder() / "iosstudio_editor_attach.py"
    assert module.read_text() == ed.LLDB_MODULE.read_text()
    assert "process attach --pid 2" in p.read_text()
    ed.write_stub("com.example.hello", "Hello")
    stub = p.read_text()
    assert "connect" not in stub.replace("iosstudio_editor_attach", "")
    assert "Start 'Debug in editor' in iOS Studio first" in stub


def test_lldb_module_defines_the_commands():
    tree = ast.parse(ed.LLDB_MODULE.read_text())
    funcs = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert {"check", "remote_file", "connect", "fail", "__lldb_init_module"} <= funcs


def test_prune_keeps_the_newest_and_running_ones(home):
    d = ed.folder()
    d.mkdir(parents=True)
    for i in range(ed.KEEP_FILES + 5):
        f = d / f"com.example.app{i}.lldb"
        f.write_text("x")
        os.utime(f, (1000 + i, 1000 + i))
    pid, start = ed.helper_identity()
    (d / "com.example.app0.json").write_text(json.dumps({"bundleId": "com.example.app0", "helperPid": pid,
                                                         "helperStart": start}))
    os.utime(d / "com.example.app0.json", (1, 1))
    ed.prune()
    left = sorted(p.stem for p in d.glob("*.lldb"))
    assert "com.example.app0" in left                                  # a running bridge's file stays
    assert "com.example.app1" not in left and f"com.example.app{ed.KEEP_FILES + 4}" in left
    assert len(left) == ed.KEEP_FILES + 1


# ================================================================ running bridges

def test_bridges_only_lists_live_processes(home):
    pid, start = ed.helper_identity()
    ed.save_state("com.example.live", {"bundleId": "com.example.live", "helperPid": pid, "helperStart": start, "port": 5})
    ed.save_state("com.example.dead", {"bundleId": "com.example.dead", "helperPid": pid, "helperStart": "0"})
    assert [b["bundleId"] for b in ed.bridges()] == ["com.example.live"]
    assert ed.running_bridge("XTL-T.com.example.live")["port"] == 5
    assert ed.bridges("com.example.dead") == []


def test_proc_start_names_this_process():
    assert ed.proc_start(os.getpid()) == ed.helper_identity()[1]
    assert ed.proc_start(2 ** 22 + 7) is None


def test_tcp_peers(tmp_path):
    hdr = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
    (tmp_path / "tcp").write_text(hdr +
        "   0: 0100007F:CE55 0100007F:A1B2 01 00000000:00000000 00:00000000 00000000  1000        0 1\n"   # editor -> forward
        "   1: 0100007F:A1B2 0100007F:CE55 01 00000000:00000000 00:00000000 00000000  1000        0 2\n"   # forward's side
        "   2: 0100007F:CE56 0100007F:A1B2 06 00000000:00000000 00:00000000 00000000  1000        0 3\n"   # TIME_WAIT
        "   3: 0A00000F:CE57 0A000001:A1B2 01 00000000:00000000 00:00000000 00000000  1000        0 4\n")  # not loopback
    (tmp_path / "tcp6").write_text(hdr +
        "   0: 00000000000000000000000001000000:CE58 00000000000000000000000001000000:A1B2 01 0:0 0:0 0 1000 0 5\n")
    assert ed.tcp_peers(0xA1B2, tmp_path) == 2
    assert ed.tcp_peers(0x1234, tmp_path) == 0
    assert ed.tcp_peers(1, tmp_path / "missing") == 0


def test_tcp_peers_sees_a_real_connection():
    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        assert ed.tcp_peers(port) == 0
        with socket.create_connection(("127.0.0.1", port)):
            conn, _ = srv.accept()
            assert ed.tcp_peers(port) == 1
            conn.close()


def test_match_installed():
    apps = [{"bundleId": "com.other"}, {"bundleId": "XTL-AB12.com.example.hello"}]
    assert bridge.match_installed(apps, "com.example.hello") == "XTL-AB12.com.example.hello"
    assert bridge.match_installed(apps, "com.other") == "com.other"
    assert bridge.match_installed(apps, "com.example.nope") is None


# ================================================================ the bridge stream (stand-in daemon and toolchain)

@pytest.fixture
def fake_tc(tmp_path, monkeypatch):
    from iosstudio.features import debugger
    bin_ = tmp_path / "tc/usr/bin"
    bin_.mkdir(parents=True)
    for name in ("lldb", "lldb-dap"):
        (bin_ / name).write_text("")
    tc = debugger.Toolchain(str(bin_ / "lldb"), "lldb version 21 (test)", "/usr/bin/python3", "", {})
    monkeypatch.setattr(debugger, "toolchain", lambda: tc)
    monkeypatch.setattr(bridge, "POLL", 0.02)
    monkeypatch.setattr(bridge, "APP_POLL", 0.05)
    return bin_


def stream(capsys) -> list[dict]:
    return [json.loads(x) for x in capsys.readouterr().out.splitlines() if x.strip()]


def test_bridge_ends_when_the_app_exits_and_kills_an_unattached_launch(home, fake_tc, capsys, monkeypatch):
    calls = {"ps": 0, "start": 0}

    def ps(_a):
        calls["ps"] += 1
        return [{"pid": 913}] if calls["ps"] < 4 else []

    def start(a):
        calls["start"] += 1
        return {"bundleId": a["bundleId"], "port": 50001 if calls["start"] == 1 else 50002, "host": "127.0.0.1",
                "pid": 913 if a["launch"] != "none" else None, "launched": True, "symbolsReady": False}
    monkeypatch.setattr(bridge, "KEEPALIVE", 0.03)
    with FakeDeviced(Path(os.environ["IOSSTUDIO_RUNTIME_DIR"]),
                     {"debug.start": start, "processes.list": ps,
                      "debug.stop": lambda a: {"bundleId": a["bundleId"], "killed": a.get("pid") if a["kill"] else None}}) as d:
        rc = bridge.cmd_editor({"bundle_id": "XTL-T.com.example.gone"})
        assert rc == 0
        ev = stream(capsys)
        names = [e.get("event") for e in ev]
        assert names.index("ready") < names.index("done")
        ready = next(e for e in ev if e.get("event") == "ready")
        assert (ready["port"], ready["pid"], ready["launched"], ready["editors"]) == (50001, 913, True, [])
        assert any(e.get("id") == "waiting" for e in ev)              # the app waits for the editor
        assert any(e.get("event") == "forward" and e["port"] == 50002 for e in ev)
        done = ev[-1]
        assert done["ok"] and done["data"]["ended"] == "app-exited"
        assert d.sent("debug.start")[1]["launch"] == "none"           # keepalive
        stop = d.sent("debug.stop")[-1]
        assert stop["kill"] is False                                  # it exited by itself: nothing to kill
    assert "iosstudio-fail" in ed.script_path("com.example.gone").read_text()
    assert not ed.state_path("com.example.gone").exists()


def test_bridge_timeout_kills_a_launch_nobody_attached_to(home, fake_tc, capsys, monkeypatch):
    monkeypatch.setenv("IOSSTUDIO_EDITOR_DEBUG_LIFETIME", "0.3")
    with FakeDeviced(Path(os.environ["IOSSTUDIO_RUNTIME_DIR"]),
                     {"debug.start": {"bundleId": "com.example.a", "port": 50003, "host": "127.0.0.1", "pid": 7,
                                      "launched": True, "symbolsReady": True, "sysroot": "/s"},
                      "processes.list": [{"pid": 7}],
                      "debug.stop": lambda a: {"bundleId": a["bundleId"], "killed": a["pid"] if a["kill"] else None}}) as d:
        assert bridge.cmd_editor({"bundle_id": "com.example.a", "launch": "suspended"}) == 0
        ev = stream(capsys)
        assert d.sent("debug.start")[0]["launch"] == "suspended"
        assert ev[-1]["data"]["ended"] == "timeout"
        assert ev[-1]["data"]["killed"] == 7 and d.sent("debug.stop")[0]["kill"] is True


def test_bridge_sees_the_editor_attach_and_writes_state(home, fake_tc, capsys, monkeypatch):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(2)
    port = srv.getsockname()[1]
    monkeypatch.setenv("IOSSTUDIO_EDITOR_DEBUG_LIFETIME", "20")
    seen: dict = {}

    def editor():   # an editor connecting to the forward, then going away
        deadline = time.monotonic() + 10
        while not ed.state_path("com.example.b").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        seen["running"] = ed.bridges()
        c = socket.create_connection(("127.0.0.1", port))
        while (json.loads(ed.state_path("com.example.b").read_text()).get("editor") != "attached"
               and time.monotonic() < deadline):
            time.sleep(0.02)
        c.close()
        while (json.loads(ed.state_path("com.example.b").read_text()).get("editor") != "detached"
               and time.monotonic() < deadline):
            time.sleep(0.02)
        os.kill(os.getpid(), signal.SIGTERM)       # the UI cancelling

    t = threading.Thread(target=editor)
    with FakeDeviced(Path(os.environ["IOSSTUDIO_RUNTIME_DIR"]),
                     {"debug.start": {"bundleId": "com.example.b", "port": port, "host": "127.0.0.1", "pid": 8,
                                      "launched": False, "symbolsReady": True},
                      "processes.list": [{"pid": 8}], "debug.stop": {"bundleId": "com.example.b"}}):
        t.start()
        assert bridge.cmd_editor({"bundle_id": "com.example.b"}) == 0
        t.join(10)
    srv.close()
    ev = stream(capsys)
    states = [e["state"] for e in ev if e.get("event") == "editor"]
    assert states == ["attached", "detached"]
    assert ev[-1]["data"]["ended"] == "cancelled" and ev[-1]["data"]["attached"] is True
    assert seen["running"][0]["bundleId"] == "com.example.b" and seen["running"][0]["port"] == port


def test_busy_is_passed_on(home, fake_tc, capsys):
    busy = {"error": {"code": "busy", "message": "A debugger is already attached to this app."}}
    with FakeDeviced(Path(os.environ["IOSSTUDIO_RUNTIME_DIR"]), {"debug.start": busy}):
        assert bridge.cmd_editor({"bundle_id": "com.example.c"}) == 1
    ev = stream(capsys)
    assert ev[-1]["event"] == "done" and ev[-1]["error"]["code"] == "busy"


def test_not_installed(home, fake_tc, capsys, tmp_path):
    p = project(tmp_path)
    with FakeDeviced(Path(os.environ["IOSSTUDIO_RUNTIME_DIR"]), {"apps.list": {"apps": [{"bundleId": "com.x"}]}}):
        assert bridge.cmd_editor({"project": str(p), "no_config": True}) == 1
    ev = stream(capsys)
    assert ev[-1]["error"]["code"] == "not-installed" and ev[-1]["error"]["fix"]["page"] == "projects"
    assert not (p / ".vscode").exists()                                 # --no-config


def test_missing_lldb_dap(home, fake_tc, capsys):
    (fake_tc / "lldb-dap").unlink()
    assert bridge.cmd_editor({"bundle_id": "com.example.d"}) == 1
    assert stream(capsys)[-1]["error"]["code"] == "needs-lldb"
