"""`app` group (helper/iosstudio/features/appconfig.py): marker blocks in temp Hyprland Lua files, backups,
validation with a fake hyprctl (restore on errors), the key-conflict check and migration of the old app id."""
from __future__ import annotations

import json
import subprocess

import pytest

from iosstudio.core.output import HelperError
from iosstudio.features import appconfig as ac


def data(capsys) -> dict:
    return json.loads(capsys.readouterr().out.splitlines()[-1])["data"]


@pytest.fixture
def hypr(tmp_path, monkeypatch):
    """Temp bindings.lua / hyprland.lua / backups; no hyprctl or omarchy unless a test installs fakes."""
    h = tmp_path / "hypr"
    h.mkdir()
    monkeypatch.setattr(ac, "BINDINGS", h / "bindings.lua")
    monkeypatch.setattr(ac, "HYPRLAND", h / "hyprland.lua")
    monkeypatch.setattr(ac, "BACKUPS", tmp_path / "backups")
    monkeypatch.setattr(ac.subproc, "which", lambda name: None)
    return h


class FakeTools:
    """subproc.run_cmd stand-in: records argv, answers `hyprctl configerrors` and the keybinding listing."""

    def __init__(self, errors: str = "", keys: str = ""):
        self.calls: list[list[str]] = []
        self.errors, self.keys = errors, keys

    def __call__(self, argv, timeout=60, **kw):
        self.calls.append(list(argv))
        out = self.errors if argv[:2] == ["hyprctl", "configerrors"] else (self.keys if argv[0] == "omarchy" else "")
        return subprocess.CompletedProcess(argv, 0, out, "")


def install(monkeypatch, tools: FakeTools) -> FakeTools:
    monkeypatch.setattr(ac.subproc, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(ac.subproc, "run_cmd", tools)
    return tools


def test_status_on_empty_config(hypr, capsys):
    assert ac.status({}) == 0
    d = data(capsys)
    assert d["keybinding"] == {"enabled": False, "key": ac.KEY, "label": "Super + Shift + I", "conflict": ""}
    assert d["windowRule"] == {"mode": "tile"} and d["opaque"] is False


def test_keybinding_on_off_keeps_the_rest(hypr, capsys):
    ac.BINDINGS.write_text("-- mine\no.bind(\"SUPER + A\", \"x\", \"y\")\n")
    ac.keybinding({"enable": True})
    text = ac.BINDINGS.read_text()
    assert text.startswith("-- mine\no.bind(\"SUPER + A\"")
    assert f"{ac.BIND_START}\no.bind(\"{ac.KEY}\", \"iOS Studio\", \"{ac.LAUNCHER}\")\n{ac.BIND_END}\n" in text
    ac.keybinding({"enabled": "yes"})                    # again: still exactly one block
    assert ac.BINDINGS.read_text().count(ac.BIND_START) == 1
    assert len(list(ac.BACKUPS.glob("bindings.lua.*"))) >= 1
    ac.keybinding({"enabled": "off"})
    assert ac.BINDINGS.read_text() == "-- mine\no.bind(\"SUPER + A\", \"x\", \"y\")\n"
    assert data(capsys) == {"enabled": False, "key": ac.KEY}


def test_window_rule_and_opacity(hypr, capsys):
    ac.window_rule({"mode": "float", "width": "900", "height": "600"})
    ac.opacity({"opaque": "on"})
    text = ac.HYPRLAND.read_text()
    assert "size = { 900, 600 }" in text and ac.OPAQUE_RULE in text
    assert ac.status({}) == 0
    d = data(capsys)
    assert d["windowRule"]["mode"] == "float" and d["opaque"] is True
    ac.window_rule({"mode": "TILE"})
    ac.opacity({"opaque": "off"})
    assert ac.WIN_START not in ac.HYPRLAND.read_text() and ac.OPAQUE_START not in ac.HYPRLAND.read_text()


@pytest.mark.parametrize("fn, flags", [(ac.window_rule, {"mode": "maximize"}), (ac.opacity, {"opaque": "maybe"}),
                                       (ac.opacity, {})])
def test_usage_errors(hypr, fn, flags):
    with pytest.raises(HelperError) as ei:
        fn(flags)
    assert ei.value.code == "usage"


def test_symlinked_config_is_refused(hypr, tmp_path):
    real = tmp_path / "real.lua"
    real.write_text("x\n")
    ac.BINDINGS.symlink_to(real)
    with pytest.raises(HelperError) as ei:
        ac.keybinding({"enable": True})
    assert ei.value.code == "unsafe-file" and real.read_text() == "x\n"


def test_no_change_writes_nothing(hypr):
    ac.BINDINGS.write_text("-- mine\n")
    before = ac.BINDINGS.stat().st_mtime_ns
    ac.keybinding({"enabled": "off"})
    assert ac.BINDINGS.stat().st_mtime_ns == before and not ac.BACKUPS.exists()


def test_turning_off_on_a_missing_file_creates_nothing(hypr, monkeypatch):
    tools = install(monkeypatch, FakeTools())
    ac.keybinding({"enabled": "off"})
    ac.window_rule({"mode": "tile"})
    ac.opacity({"opaque": "off"})
    assert not ac.BINDINGS.exists() and not ac.HYPRLAND.exists() and tools.calls == []


def test_hyprland_errors_restore_the_backup(hypr, monkeypatch):
    ac.HYPRLAND.write_text("-- original\n")
    tools = install(monkeypatch, FakeTools(errors="hyprland.lua:3: bad rule\n"))
    with pytest.raises(HelperError) as ei:
        ac.window_rule({"mode": "float"})
    assert ei.value.code == "hypr-config" and "bad rule" in ei.value.detail
    assert ac.HYPRLAND.read_text() == "-- original\n"
    assert tools.calls == [["hyprctl", "reload"], ["hyprctl", "configerrors"], ["hyprctl", "reload"]]


def test_hyprland_errors_remove_a_new_file(hypr, monkeypatch):
    install(monkeypatch, FakeTools(errors="error\n"))
    with pytest.raises(HelperError):
        ac.opacity({"opaque": "on"})
    assert not ac.HYPRLAND.exists()


def test_clean_validation_keeps_the_change(hypr, monkeypatch):
    tools = install(monkeypatch, FakeTools(errors="\n  \n"))
    ac.opacity({"opaque": "on"})
    assert ac.OPAQUE_RULE in ac.HYPRLAND.read_text()
    assert tools.calls == [["hyprctl", "reload"], ["hyprctl", "configerrors"]]


def test_key_conflict(hypr, monkeypatch, capsys):
    keys = ("SUPER + RETURN  →  Terminal\nSUPER   SHIFT + I  →  Inspector\n")
    install(monkeypatch, FakeTools(keys=keys))
    assert ac._key_conflict() == "Inspector"
    with pytest.raises(HelperError) as ei:
        ac.keybinding({"enable": True})
    assert ei.value.code == "key-taken" and "Inspector" in ei.value.message
    ac.status({})
    assert data(capsys)["keybinding"]["conflict"] == "Inspector"
    install(monkeypatch, FakeTools(keys="SUPER SHIFT + I → iOS Studio\n"))
    assert ac._key_conflict() == ""                                    # that's us


def test_backups_keep_ten(hypr, monkeypatch):
    ac.BACKUPS.mkdir()
    for i in range(12):
        (ac.BACKUPS / f"bindings.lua.{1000 + i}").write_text(str(i))
    ac.BINDINGS.write_text("x\n")
    ac._backup(ac.BINDINGS)
    assert len(list(ac.BACKUPS.glob("bindings.lua.*"))) == 10
    assert ac._backup(hypr / "missing.lua") is None


def test_migrate_rewrites_the_legacy_class_only_inside_our_blocks(hypr, capsys):
    legacy = '"^dev.jane.iosstudio$"'
    ac.HYPRLAND.write_text(f'o.window({legacy}, {{ float = true }})\n\n{ac.WIN_START}\n'
                           f'o.window({legacy}, {{ float = true }})\n{ac.WIN_END}\n')
    ac.BINDINGS.write_text("unrelated\n")
    assert ac.migrate({}) == 0
    assert data(capsys) == {"migrated": [str(ac.HYPRLAND)]}
    text = ac.HYPRLAND.read_text()
    assert text.startswith(f"o.window({legacy}")                       # a hand-written rule is not ours
    assert f'{ac.WIN_START}\no.window("{ac.CLASS_MATCH}"' in text
    ac.migrate({})
    assert data(capsys) == {"migrated": []}


def test_about_without_tools(hypr, monkeypatch, capsys):
    def run(argv, timeout=60, **kw):
        if argv[0] == "git":
            return subprocess.CompletedProcess(argv, 0, "abc1234 2026-11-01\n", "")
        if argv[0] == "qs":
            return subprocess.CompletedProcess(argv, 0, "", "quickshell 0.3\n")
        raise HelperError("missing-tool", "nope")
    monkeypatch.setattr(ac.subproc, "run_cmd", run)
    ac.about({})
    d = data(capsys)
    assert d["commit"] == "abc1234 2026-11-01" and d["quickshell"] == "quickshell 0.3" and d["omarchy"] == ""
