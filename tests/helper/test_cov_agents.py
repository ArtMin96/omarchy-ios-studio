"""AI agents slice: the entry shapes per client, reading registrations from fixture configs on a temp home, the
writers' pure helpers (marked blocks, TOML tables, CLI steps), and `agents test` / rpc against a fake MCP server
over stdio (tests/support/fake_mcp.py)."""
from __future__ import annotations

import json
import os
import shlex
import sys
import tomllib
from pathlib import Path

import pytest

from iosstudio.core.output import HelperError
from iosstudio.features.agents import clients as cl
from iosstudio.features.agents import commands, rpc, writers
from support import write_exe as _write_exe
from support.fake_mcp import FAKE_MCP

FAKE_MCP_HERE = FAKE_MCP.replace("#!/usr/bin/env python3", f"#!{sys.executable}", 1)   # PATH is emptied below


def write_exe(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    return _write_exe(path, text)

LAUNCH = "/opt/ios studio/bin/iosstudio-mcp"


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A temp home with no AI tool on PATH; the launcher is a fake path with a space in it."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setattr(cl, "HOME", h)
    monkeypatch.setattr(writers, "HOME", h)
    monkeypatch.setattr(cl, "LAUNCHER", Path(LAUNCH))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(h / ".config"))
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr(writers.appdirs, "STATE_DIR", tmp_path / "state")
    return h


def put(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


# ================================================================ entries

def test_entry_shapes_per_client():
    assert cl.entry_json("claude", "/x", False) == {"command": "/x", "args": []}
    assert cl.entry_json("cursor", "/x", True) == {"command": "/x", "args": [], "env": {"IOSSTUDIO_DEMO": "1"}}
    assert cl.entry_json("opencode", "/x", True) == {"type": "local", "command": ["/x"], "enabled": True,
                                                     "timeout": 120000, "environment": {"IOSSTUDIO_DEMO": "1"}}
    assert cl.entry_json("zed", "/x", False) == {"command": "/x", "args": [], "env": {}}
    assert cl.entry_json("gemini", "/x", False)["timeout"] == 600000
    assert cl.entry_json("crush", "/x", False)["type"] == "stdio"


def test_toml_block_parses_with_any_path():
    cmd = 'C:\\odd "path"/ünï'
    d = tomllib.loads(cl.toml_block(cmd, True))
    assert d["mcp_servers"]["iosstudio"] == {"command": cmd, "args": [], "startup_timeout_sec": 30,
                                             "tool_timeout_sec": 120, "env": {"IOSSTUDIO_DEMO": "1"}}


def test_crush_line_round_trips_through_crushrc_entries():
    text = "# my rc\nset x 1\n" + cl.crush_line(LAUNCH, True) + "\nmcp add other --command /y\nmcp add 'unclosed\n"
    found = cl.crushrc_entries(text)
    assert found == [{"line": 2, "command": LAUNCH, "env": {"IOSSTUDIO_DEMO": "1"}}]


def test_client_lookup():
    assert cl.client("zed")["format"] == "json"
    with pytest.raises(HelperError) as ei:
        cl.client("emacs")
    assert ei.value.code == "usage" and "claude" in ei.value.message


def test_which_checks_extra_bin_dirs(home, tmp_path):
    assert cl.which(("claude",)) is None
    exe = write_exe(home / ".local/share/mise/shims/claude", "#!/bin/sh\n")
    assert cl.which(("nope", "claude")) == str(exe)
    (home / ".bun/bin").mkdir(parents=True)
    (home / ".bun/bin/gemini").write_text("")                         # not executable: not found
    assert cl.which(("gemini",)) is None


# ================================================================ registrations

def test_registration_json_with_comments(home):
    c = cl.client("claude")
    assert cl.registration(c) == {"registered": False, "command": None, "demo": False, "where": str(home / ".claude.json")}
    put(home / ".claude.json", '// mine\n{"mcpServers": {"iosstudio": {"command": "/old/path", '
                               '"env": {"IOSSTUDIO_DEMO": "0"}},},}')
    r = cl.registration(c)
    assert r["registered"] and r["command"] == "/old/path" and r["demo"] is False


def test_registration_opencode_argv_and_environment(home):
    put(home / ".config/opencode/opencode.jsonc",
        '{"mcp": {"iosstudio": {"command": ["/x/mcp"], "environment": {"IOSSTUDIO_DEMO": "1"}}}}')
    c = cl.client("opencode")
    assert c["file"].name == "opencode.jsonc"
    r = cl.registration(c)
    assert r["command"] == "/x/mcp" and r["demo"] is True


def test_registration_toml_and_crush_json_fallback(home):
    put(home / ".codex/config.toml", '[mcp_servers.iosstudio]\ncommand = "/c"\nenv = { IOSSTUDIO_DEMO = "1" }\n')
    assert cl.registration(cl.client("codex"))["demo"] is True
    put(home / ".config/crush/crush.json", '{"mcp": {"iosstudio": {"command": "/k"}}}')
    r = cl.registration(cl.client("crush"))
    assert r["command"] == "/k" and r["where"].endswith("crush.json")


def test_codex_home_override(home, monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    assert cl.client("codex")["file"] == tmp_path / "codex" / "config.toml"


def test_describe_current_stale_and_unreadable(home):
    put(home / ".cursor/mcp.json", json.dumps({"mcpServers": {"iosstudio": {"command": LAUNCH}}}))
    put(home / ".gemini/settings.json", json.dumps({"mcpServers": {"iosstudio": {"command": "/moved/away"}}}))
    put(home / ".claude.json", "{broken")
    rows = {c["id"]: cl.describe(c) for c in cl.clients()}
    assert rows["cursor"]["current"] and not rows["cursor"]["stale"] and rows["cursor"]["installed"]
    assert rows["gemini"]["stale"] and rows["gemini"]["method"] == "file"        # no gemini CLI on PATH
    assert rows["claude"]["error"]["code"] == "config-unreadable" and not rows["claude"]["registered"]
    assert not rows["zed"]["installed"]
    assert rows["claude"]["snippet"] == {"kind": "shell",
                                         "text": f"claude mcp add --scope user iosstudio -- {shlex.quote(LAUNCH)}"}
    zed = rows["zed"]["snippet"]
    assert zed["kind"] == "json" and json.loads(zed["text"]) == {"context_servers": {"iosstudio": {
        "command": LAUNCH, "args": [], "env": {}}}}
    assert tomllib.loads(rows["codex"]["snippet"]["text"])["mcp_servers"]["iosstudio"]["command"] == LAUNCH


# ================================================================ writers: pure parts

def test_marked_block_round_trip():
    for text in ("", "a = 1\n", "a = 1", "a = 1\n\n"):
        added = writers._append_marked(text, "body\n")
        assert f"# {writers.MARK_START}\nbody\n# {writers.MARK_END}\n" in added
        back = "".join(writers._strip_marked(added.splitlines(True)))
        assert back == (text if text.endswith("\n") or not text else text + "\n")


def test_toml_header_path():
    assert writers._toml_header_path("[mcp_servers.iosstudio.env]") == ["mcp_servers", "iosstudio", "env"]
    assert writers._toml_header_path('[ "mcp_servers" . iosstudio ]') == ["mcp_servers", "iosstudio"]
    assert writers._toml_header_path("[[profiles]]") == ["profiles"]
    assert writers._toml_header_path("key = 1") is None and writers._toml_header_path("[broken") is None


def test_toml_remove_hand_written_table_and_subtables():
    text = ('model = "x"\n[mcp_servers.iosstudio]\ncommand = "/a"\n[mcp_servers.iosstudio.env]\nA = "1"\n'
            '[mcp_servers.other]\ncommand = "/b"\n')
    out = writers._toml_remove(text)
    assert tomllib.loads(out) == {"model": "x", "mcp_servers": {"other": {"command": "/b"}}}


def test_toml_apply_connect_keeps_other_servers_and_disconnect_cleans_up(home):
    c = cl.client("codex")
    put(c["file"], 'model = "o3"\n[mcp_servers.other]\ncommand = "/b"\n')
    plan = writers.change(c, True, demo=True, dry=True, via="file")
    assert plan["changed"] and plan["dryRun"] and "+command = " in plan["diff"]
    writers.change(c, True, demo=True, via="file")
    d = tomllib.loads(c["file"].read_text())
    assert d["mcp_servers"]["iosstudio"]["env"] == {"IOSSTUDIO_DEMO": "1"} and d["mcp_servers"]["other"]
    writers.change(c, False, via="file")
    assert tomllib.loads(c["file"].read_text()) == {"model": "o3", "mcp_servers": {"other": {"command": "/b"}}}
    assert writers.change(c, False)["method"] == "none"


def test_toml_apply_refuses_a_broken_file(home):
    c = cl.client("codex")
    put(c["file"], "this is = = not toml\n")
    with pytest.raises(HelperError) as ei:
        writers.change(c, True, via="file")
    assert ei.value.code == "config-unreadable"


def test_json_apply_refuses_a_non_object(home):
    c = cl.client("cursor")
    put(c["file"], "[1, 2]")
    with pytest.raises(HelperError) as ei:
        writers.change(c, True)
    assert ei.value.code == "config-unreadable"


def test_json_disconnect_drops_an_emptied_parent(home):
    c = cl.client("zed")
    put(c["file"], '{\n  "theme": "x",\n  "context_servers": {"iosstudio": {"command": "/a"}}\n}\n')
    writers.change(c, False)
    assert json.loads(c["file"].read_text()) == {"theme": "x"}


def test_crush_apply_replaces_a_hand_written_line(home):
    c = cl.client("crush")
    put(c["file"], "set a 1\nmcp add iosstudio --command /old\nmcp add other --command /o\n")
    writers.change(c, True)
    found = cl.crushrc_entries(c["file"].read_text())
    assert [e["command"] for e in found] == [LAUNCH]
    assert "mcp add other --command /o" in c["file"].read_text()


def test_cli_steps_and_apply_with_a_fake_cli(home, tmp_path):
    log = tmp_path / "cli.log"
    write_exe(tmp_path / "bin" / "claude", f'#!/bin/sh\necho "$@" >> {log}\n'
              f'echo \'{{"mcpServers": {{"iosstudio": {{"command": "x"}}}}}}\' > {home}/.claude.json\n')
    c = cl.client("claude")
    assert cl.method(c) == "cli"
    put(home / ".claude.json", json.dumps({"mcpServers": {"iosstudio": {"command": "/old"}}}))
    steps = writers._cli_steps(c, True, True)
    assert [s[1:3] for s in steps] == [["mcp", "remove"], ["mcp", "add"]]
    assert steps[1][-4:] == ["user", "iosstudio", "--", LAUNCH]
    assert "-e" in steps[1] and "IOSSTUDIO_DEMO=1" in steps[1]
    plan = writers.change(c, True, demo=True, dry=True)
    assert plan["method"] == "cli" and plan["commands"][1].endswith(shlex.quote(LAUNCH))
    assert not log.exists()
    res = writers.change(c, True)
    assert res["method"] == "cli" and len(log.read_text().splitlines()) == 2


def test_cli_apply_reports_a_refusal(home, tmp_path):
    write_exe(tmp_path / "bin" / "gemini", "#!/bin/sh\necho 'nope: bad flag' >&2\nexit 2\n")
    with pytest.raises(HelperError) as ei:
        writers.change(cl.client("gemini"), True)
    assert ei.value.code == "agent-cli" and "nope: bad flag" in ei.value.detail


def test_cli_apply_notices_a_change_that_did_not_land(home, tmp_path):
    write_exe(tmp_path / "bin" / "gemini", "#!/bin/sh\nexit 0\n")
    with pytest.raises(HelperError) as ei:
        writers.change(cl.client("gemini"), True)
    assert ei.value.code == "agent-cli" and "can't see the change" in ei.value.message


# ================================================================ rpc / agents test

@pytest.fixture
def server(tmp_path, monkeypatch):
    exe = write_exe(tmp_path / "fake-mcp", FAKE_MCP_HERE)

    def run(mode: str = "ok", timeout: float = 10, **env) -> dict:
        full = {**os.environ, "FAKE_MCP_MODE": mode, **env}
        return rpc.list_tools(str(exe), full, tmp_path, timeout)
    return run


def test_list_tools_pages_and_skips_noise(server):
    res = server(FAKE_MCP_TOOLS="5")
    assert res["names"] == ["tool0", "tool1", "tool2", "tool3", "tool4"]
    assert res["init"]["serverInfo"]["name"] == "fake"


def test_list_tools_stops_after_max_pages(server, monkeypatch):
    monkeypatch.setattr(rpc, "MAX_PAGES", 3)
    assert len(server("endless", FAKE_MCP_TOOLS="100")["names"]) == 6


@pytest.mark.parametrize("mode, code, detail", [("exit", "server-exited", "fatal: no config"),
                                                ("garbage", "server-protocol", "Welcome"),
                                                ("error", "server-protocol", "no tools")])
def test_list_tools_failures(server, mode, code, detail):
    with pytest.raises(HelperError) as ei:
        server(mode)
    assert ei.value.code == code and detail in ei.value.detail


def test_list_tools_timeout_reports_stderr(server):
    with pytest.raises(HelperError) as ei:
        server("silent", timeout=0.3)
    assert ei.value.code == "timeout" and "got initialize" in ei.value.detail


def test_agents_test_command_against_the_fake(home, tmp_path, monkeypatch, capsys):
    exe = write_exe(tmp_path / "srv", FAKE_MCP_HERE)
    monkeypatch.setattr(cl, "LAUNCHER", exe)
    monkeypatch.setattr(commands, "HOME", tmp_path)
    monkeypatch.setenv("FAKE_MCP_MODE", "ok")
    assert commands.test({"demo": True, "timeout": "10"}) == 0
    d = json.loads(capsys.readouterr().out.splitlines()[-1])["data"]
    assert d["tools"] == 5 and d["demo"] is True and d["server"] == "fake" and d["protocol"] == "2025-11-25"


def test_agents_test_for_a_client(home, tmp_path, monkeypatch):
    with pytest.raises(HelperError) as ei:
        commands.test({"client": "cursor"})
    assert ei.value.code == "not-connected"
    put(home / ".cursor/mcp.json", json.dumps({"mcpServers": {"iosstudio": {"command": str(tmp_path / "gone")}}}))
    with pytest.raises(HelperError) as ei:
        commands.test({"client": "cursor"})
    assert ei.value.code == "stale"


def test_connect_needs_client_and_launcher(home, monkeypatch, tmp_path):
    with pytest.raises(HelperError) as ei:
        commands.connect({})
    assert ei.value.code == "usage"
    monkeypatch.setattr(commands, "LAUNCHER", tmp_path / "missing-launcher")
    with pytest.raises(HelperError) as ei:
        commands.connect({"client": "zed"})
    assert ei.value.code == "no-launcher"
