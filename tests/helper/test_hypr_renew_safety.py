"""Security fixes for the app's Hyprland edits and the auto-renew systemd unit.

appconfig (helper/iosstudio/features/appconfig.py), against a fake `hyprctl` on PATH: an error Hyprland
already reported before the edit doesn't undo it, a new one does; a missing hyprland.lua / bindings.lua
is never created. renew (helper/iosstudio/features/projects/renew.py): the ExecStart= and
ConditionFileIsExecutable= lines for an app folder with spaces, $, % and quotes."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from iosstudio.core.output import HelperError
from iosstudio.features import appconfig as ac
from iosstudio.features.projects import renew

# `hyprctl reload` recomputes the errors: the pre-existing one (if any) names the file's current last
# line, so it moves when our block is added; a "reject" file makes Hyprland refuse our block.
FAKE_HYPRCTL = r"""#!/bin/sh
d="$FAKE_HYPR"
echo "$*" >> "$d/calls"
case "$1" in
  reload)
    : > "$d/errors"
    if [ -f "$d/preexisting" ]; then
      echo "Config error in file $d/hyprland.lua at line $(wc -l < "$d/hyprland.lua"): $(cat "$d/preexisting")" >> "$d/errors"
    fi
    if [ -f "$d/reject" ] && grep -q "ios-studio:" "$d/hyprland.lua"; then
      echo "Config error in file $d/hyprland.lua at line 99: bad window rule" >> "$d/errors"
    fi ;;
  configerrors) cat "$d/errors" 2>/dev/null ;;
esac
"""


@pytest.fixture
def hypr(tmp_path, monkeypatch):
    """Temp Hyprland files and backups, a fake hyprctl first on PATH, and no omarchy (no key conflicts)."""
    h = tmp_path / "hypr"
    h.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "hyprctl"
    fake.write_text(FAKE_HYPRCTL)
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setenv("FAKE_HYPR", str(h))
    monkeypatch.setattr(ac, "BINDINGS", h / "bindings.lua")
    monkeypatch.setattr(ac, "HYPRLAND", h / "hyprland.lua")
    monkeypatch.setattr(ac, "BACKUPS", tmp_path / "backups")
    real_which = ac.subproc.which
    monkeypatch.setattr(ac.subproc, "which", lambda name: None if name == "omarchy" else real_which(name))
    return h


def calls(h: Path) -> list[str]:
    f = h / "calls"
    return f.read_text().splitlines() if f.exists() else []


def test_fake_hyprctl_is_the_one_used(hypr):
    assert ac.subproc.which("hyprctl") == str(hypr.parent / "bin/hyprctl")


def test_an_old_error_does_not_undo_the_change(hypr):
    ac.HYPRLAND.write_text("-- mine\no.broken()\n")
    (hypr / "preexisting").write_text("unknown function broken")
    subprocess.run(["hyprctl", "reload"], check=True)            # Hyprland already shows the old error
    (hypr / "calls").unlink()
    ac.window_rule({"mode": "float"})
    text = ac.HYPRLAND.read_text()
    assert text.startswith("-- mine\no.broken()\n") and ac.WIN_START in text   # kept, although the error moved
    assert calls(hypr) == ["configerrors", "reload", "configerrors"]
    assert "unknown function broken" in (hypr / "errors").read_text()


def test_a_new_error_undoes_the_change(hypr):
    ac.HYPRLAND.write_text("-- mine\no.broken()\n")
    (hypr / "preexisting").write_text("unknown function broken")
    subprocess.run(["hyprctl", "reload"], check=True)
    (hypr / "reject").touch()
    with pytest.raises(HelperError) as ei:
        ac.opacity({"opaque": "on"})
    err = ei.value
    assert err.code == "hypr-config" and "bad window rule" in err.detail
    assert "unknown function broken" not in err.detail              # only what our change broke
    assert ac.HYPRLAND.read_text() == "-- mine\no.broken()\n"     # restored from the backup
    assert calls(hypr)[-3:] == ["reload", "configerrors", "reload"]


def test_a_new_error_on_a_clean_config_undoes_the_change(hypr):
    ac.HYPRLAND.write_text("-- mine\n")
    (hypr / "reject").touch()
    with pytest.raises(HelperError):
        ac.window_rule({"mode": "float"})
    assert ac.HYPRLAND.read_text() == "-- mine\n"


def test_removing_a_block_keeps_an_old_error_that_moved(hypr):
    ac.HYPRLAND.write_text(f"-- mine\n\n{ac.WIN_START}\no.window(\"x\", {{}})\n{ac.WIN_END}\n")
    (hypr / "preexisting").write_text("old problem")
    subprocess.run(["hyprctl", "reload"], check=True)
    ac.window_rule({"mode": "tile"})
    assert ac.WIN_START not in ac.HYPRLAND.read_text()


def test_new_errors_ignores_line_numbers_only():
    before = ["Config error in file a.lua at line 3: old", "a.lua:7: other"]
    after = ["Config error in file a.lua at line 9: old", "a.lua:12: other", "a.lua:12: fresh"]
    assert ac._new_errors(before, after) == ["a.lua:12: fresh"]
    assert ac._new_errors([], ["x"]) == ["x"]


@pytest.mark.parametrize("fn, flags, path", [
    (ac.keybinding, {"enable": True}, "BINDINGS"),
    (ac.window_rule, {"mode": "float"}, "HYPRLAND"),
    (ac.opacity, {"opaque": "on"}, "HYPRLAND"),
])
def test_adding_to_a_missing_file_refuses_and_creates_nothing(hypr, fn, flags, path):
    target = getattr(ac, path)
    with pytest.raises(HelperError) as ei:
        fn(flags)
    err = ei.value
    assert err.code == "hypr-config" and target.name in err.message
    assert err.fix == {"label": "Try again", "page": "settings"}
    assert "terminal" not in err.message.lower() and "run " not in err.message.lower()
    assert not target.exists() and not ac.BACKUPS.exists() and calls(hypr) == []


def test_removing_from_a_missing_file_is_a_no_op(hypr):
    ac.keybinding({"enabled": "off"})
    ac.window_rule({"mode": "tile"})
    ac.opacity({"opaque": "off"})
    assert not ac.BINDINGS.exists() and not ac.HYPRLAND.exists() and calls(hypr) == []


# ---------------------------------------------------------------- renew unit text

def unit_line(service: str, key: str) -> str:
    return next(ln for ln in service.splitlines() if ln.startswith(key + "="))[len(key) + 1:]


@pytest.fixture
def repo(tmp_path, monkeypatch):
    def at(name: str) -> Path:
        r = tmp_path / name
        monkeypatch.setattr(renew.appdirs, "REPO", r)
        return r
    return at


def test_exec_start_and_condition_for_a_path_with_space_dollar_percent(repo):
    r = repo("my apps $HOME 100%")
    service, _ = renew.unit_texts()
    helper = f"{r}/bin/iosstudio-helper"
    assert unit_line(service, "ExecStart") == \
        '"' + helper.replace("%", "%%") + '" projects renew-due --background'
    # $ is literal in the executable path (only arguments get $VAR substitution), % is a specifier
    assert "$HOME" in unit_line(service, "ExecStart") and "$$" not in service
    cond = unit_line(service, "ConditionFileIsExecutable")
    assert cond == helper.replace("%", "%%")                    # no quotes, no \x20: the line is the path
    unit_section = service.split("[Service]")[0]
    assert "[Unit]" in unit_section and "ConditionFileIsExecutable=" in unit_section
    assert "Restart=on-failure" in service                     # a skipped condition never triggers it


@pytest.mark.parametrize("name", ['say "hi"', "back\\slash", "new\nline"])
def test_paths_systemd_cannot_run_are_refused(repo, name):
    repo(name)
    with pytest.raises(HelperError) as ei:
        renew.unit_texts()
    assert ei.value.code == "unsafe-path" and ei.value.fix["page"] == "settings"


def test_enable_dry_run_refuses_an_unsafe_path_before_writing(repo, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    repo('a"b')
    monkeypatch.setattr(renew, "systemctl", lambda *a: pytest.fail("ran systemctl"))
    with pytest.raises(HelperError):
        renew.enable_timer(dry=False)
    assert not (tmp_path / "cfg/systemd/user").exists()


@pytest.mark.skipif(not shutil.which("systemd-analyze"), reason="needs systemd-analyze")
def test_systemd_parses_the_generated_unit(repo, tmp_path):
    """systemd itself reads the escaped lines back as the real path (read-only systemd-analyze, no units started)."""
    r = repo("odd dir $x 5%")
    helper = r / "bin/iosstudio-helper"
    helper.parent.mkdir(parents=True)
    helper.write_text("#!/bin/sh\n")
    helper.chmod(0o755)
    service, _ = renew.unit_texts()
    unit = tmp_path / "units/ios-studio-renew.service"
    unit.parent.mkdir()
    unit.write_text(service)
    v = subprocess.run(["systemd-analyze", "verify", "--user", str(unit)], capture_output=True, text=True, timeout=30)
    assert v.returncode == 0, v.stdout + v.stderr
    cond = "ConditionFileIsExecutable=" + unit_line(service, "ConditionFileIsExecutable")
    c = subprocess.run(["systemd-analyze", "condition", cond], capture_output=True, text=True, timeout=30)
    assert c.returncode == 0 and str(helper) in c.stdout + c.stderr
    helper.unlink()                                             # the app folder was deleted: skipped, not failed
    c = subprocess.run(["systemd-analyze", "condition", cond], capture_output=True, text=True, timeout=30)
    assert c.returncode != 0 and "failed" in c.stdout + c.stderr
