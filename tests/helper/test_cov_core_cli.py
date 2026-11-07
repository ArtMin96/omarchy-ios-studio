"""core/flags.py, core/output.py (JSON shapes, prompts) and cli.py dispatch (unknown group/action, aliases,
HelperError -> fail JSON, the internal-error path), all in-process with a fake feature module."""
from __future__ import annotations

import io
import json
import sys
import types

import pytest

from iosstudio import cli
from iosstudio.core import output
from iosstudio.core.flags import parse_flags
from iosstudio.core.output import HelperError


def lines(capsys) -> list[dict]:
    return [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.strip()]


# ---------------------------------------------------------------- flags

def test_flags_values_switches_and_positionals():
    f = parse_flags(["build", "--project-path", "/p", "--dry-run", "--udid", "U", "--yes"])
    assert f == {"_": ["build"], "project_path": "/p", "dry_run": True, "udid": "U", "yes": True}


def test_a_switch_swallows_the_next_positional():
    # no flag schema: `--dry-run x` reads as dry_run="x" (callers put positionals first)
    assert parse_flags(["--dry-run", "x"]) == {"_": [], "dry_run": "x"}


def test_flag_followed_by_flag_is_a_switch_and_empty():
    assert parse_flags([]) == {"_": []}
    assert parse_flags(["--a", "--b", "v"]) == {"_": [], "a": True, "b": "v"}


def test_repeated_flag_last_wins_and_values_keep_dashes_inside():
    assert parse_flags(["--x", "1", "--x", "-2"]) == {"_": [], "x": "-2"}


# ---------------------------------------------------------------- output shapes

def test_ok_and_fail_shapes(capsys):
    assert output.ok() == 0
    assert output.ok({"a": 1}) == 0
    assert output.fail("bad", "Nope.", output.fix("Open", "setup", step="s", action="a"), "d" * 5000) == 1
    a, b, c = lines(capsys)
    assert a == {"ok": True, "data": {}}
    assert b == {"ok": True, "data": {"a": 1}}
    assert c["ok"] is False
    err = c["error"]
    assert err["code"] == "bad" and err["message"] == "Nope."
    assert err["fix"] == {"label": "Open", "page": "setup", "step": "s", "action": "a"}
    assert len(err["detail"]) == 4000            # detail is capped to its tail


def test_fix_omits_empty_parts_and_error_omits_empty_fields():
    assert output.fix("L", "p") == {"label": "L", "page": "p"}
    assert HelperError("c", "m").to_json() == {"code": "c", "message": "m"}


def test_write_is_one_compact_line_and_stringifies_unknown_types(capsys):
    from pathlib import Path
    output.event("x", path=Path("/a b"), n=1)
    out = capsys.readouterr().out
    assert out == '{"event":"x","path":"/a b","n":1}\n'


def test_stream_events(capsys):
    output.step("s1", "Title", "running", detail="d")
    output.progress(0.5, "half", 3.0)
    output.log("line\n\n")
    output.log("y" * 5000)
    assert output.done(True) == 0
    assert output.done(False) == 1
    assert output.done(False, error=HelperError("x", "X")) == 1
    st, pr, lg, lg2, d1, d2, d3 = lines(capsys)
    assert st == {"event": "step", "id": "s1", "title": "Title", "state": "running", "detail": "d"}
    assert pr == {"event": "progress", "fraction": 0.5, "detail": "half", "eta": 3.0}
    assert lg == {"event": "log", "text": "line"}
    assert len(lg2["text"]) == 4000
    assert d1 == {"event": "done", "ok": True, "data": {}}
    assert d2["error"]["code"] == "failed"
    assert d3["error"] == {"code": "x", "message": "X"}


def test_prompt_skips_noise_and_other_ids(monkeypatch, capsys):
    before = output._prompt_counter
    pid = f"p{before + 1}"
    stdin = io.StringIO("\nnot json\n" + json.dumps({"prompt": "other", "value": "no"}) + "\n"
                        + json.dumps({"prompt": pid, "value": 42}) + "\n")
    monkeypatch.setattr(sys, "stdin", stdin)
    assert output.prompt("confirm", "Sure?", [{"value": "yes"}], extra=1) == "42"
    ev = lines(capsys)[0]
    assert ev == {"event": "prompt", "id": pid, "kind": "confirm", "message": "Sure?",
                  "options": [{"value": "yes"}], "extra": 1}


@pytest.mark.parametrize("stdin_text", ["", "CANCEL"])
def test_prompt_cancel_and_eof(monkeypatch, capsys, stdin_text):
    pid = f"p{output._prompt_counter + 1}"
    text = json.dumps({"prompt": pid, "cancel": True}) + "\n" if stdin_text else ""
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))
    with pytest.raises(HelperError) as ei:
        output.prompt("text", "Name?")
    assert ei.value.code == "cancelled"


def test_read_stdin_json(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO('  {"a": 1} '))
    assert output.read_stdin_json() == {"a": 1}
    monkeypatch.setattr(sys, "stdin", io.StringIO("   \n"))
    assert output.read_stdin_json() == {}
    monkeypatch.setattr(sys, "stdin", io.StringIO("{oops"))
    with pytest.raises(HelperError) as ei:
        output.read_stdin_json()
    assert ei.value.code == "bad-input"


# ---------------------------------------------------------------- cli dispatch

@pytest.fixture
def fake_group(monkeypatch):
    """A feature module `iosstudio.features._covfake` serving group `cov` (and the `format` alias target)."""
    calls: list[dict] = []

    def good(flags):
        calls.append(flags)
        return output.ok({"flags": flags})

    def helper_error(flags):
        raise HelperError("nope", "Not today.", output.fix("Fix", "setup"), "why")

    def crash(flags):
        raise RuntimeError("boom")

    def interrupted(flags):
        raise KeyboardInterrupt

    def returns_none(flags):
        output.ok()

    mod = types.ModuleType("iosstudio.features._covfake")
    mod.GROUPS = {"cov": {"good": good, "err": helper_error, "crash": crash, "int": interrupted,
                          "none": returns_none},
                  "projects": {"format": good}}
    monkeypatch.setitem(sys.modules, "iosstudio.features._covfake", mod)
    monkeypatch.setitem(cli.MODULES, "cov", "_covfake")
    monkeypatch.setitem(cli.MODULES, "projects", "_covfake")
    monkeypatch.delenv("IOSSTUDIO_DEMO", raising=False)
    return calls


def test_usage_when_too_few_args(capsys):
    assert cli.main([]) == 1                        # fail() returns 1, so `or 2` never applies
    assert cli.main(["setup"]) == 1
    for ev in lines(capsys):
        assert ev["error"]["code"] == "usage" and "groups:" in ev["error"]["message"]


def test_unknown_group_and_action(fake_group, capsys):
    assert cli.main(["nosuch", "x"]) == 2
    assert cli.main(["cov", "nosuch"]) == 2
    a, b = lines(capsys)
    assert a["error"] == {"code": "usage", "message": "Unknown command group 'nosuch'."}
    assert b["error"] == {"code": "usage", "message": "Unknown command 'cov nosuch'."}


def test_handler_gets_parsed_flags(fake_group, capsys):
    assert cli.main(["cov", "good", "--some-flag", "v", "pos"]) == 0
    assert fake_group == [{"_": ["pos"], "some_flag": "v"}]
    assert lines(capsys)[0]["data"]["flags"]["some_flag"] == "v"


def test_alias_rewrites_to_group_action(fake_group, capsys):
    assert cli.main(["format", "--project", "P"]) == 0
    assert fake_group == [{"_": [], "project": "P"}]


def test_helper_error_becomes_fail_json(fake_group, capsys):
    assert cli.main(["cov", "err"]) == 1
    ev = lines(capsys)[0]
    assert ev == {"ok": False, "error": {"code": "nope", "message": "Not today.",
                                         "fix": {"label": "Fix", "page": "setup"}, "detail": "why"}}


def test_unexpected_exception_is_internal_with_traceback(fake_group, capsys):
    assert cli.main(["cov", "crash"]) == 1
    err = lines(capsys)[0]["error"]
    assert err["code"] == "internal" and "RuntimeError: boom" in err["detail"]


def test_keyboard_interrupt_is_cancelled(fake_group, capsys):
    assert cli.main(["cov", "int"]) == 1
    assert lines(capsys)[0]["error"]["code"] == "cancelled"


def test_handler_returning_none_is_exit_0(fake_group, capsys):
    assert cli.main(["cov", "none"]) == 0


def test_broken_module_is_reported_not_raised(monkeypatch, capsys):
    monkeypatch.setitem(cli.MODULES, "broken", "_cov_does_not_exist")
    assert cli.main(["broken", "x"]) == 1
    err = lines(capsys)[0]["error"]
    assert err["code"] == "internal" and "ModuleNotFoundError" in err["detail"]


def test_every_registered_group_is_served_by_its_module():
    import importlib
    for group, mod_name in cli.MODULES.items():
        mod = importlib.import_module(f"iosstudio.features.{mod_name}")
        assert group in mod.GROUPS, f"{mod_name} doesn't serve group {group}"
        assert all(callable(h) for h in mod.GROUPS[group].values())
    for alias, (group, action) in cli.ALIASES.items():
        mod = importlib.import_module(f"iosstudio.features.{cli.MODULES[group]}")
        assert action in mod.GROUPS[group], alias


def test_demo_mode_failure_is_internal(monkeypatch, capsys):
    from iosstudio.demo import demo
    monkeypatch.setenv("IOSSTUDIO_DEMO", "1")
    monkeypatch.setattr(demo, "run", lambda g, a, f: (_ for _ in ()).throw(ValueError("x")))
    assert cli.main(["setup", "check"]) == 1
    assert lines(capsys)[0]["error"]["message"] == "Demo mode failed."
