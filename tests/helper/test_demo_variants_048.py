"""Demo fidelity for v0.4.8: the demo debugger parses p/po/expression as the real driver does, sends a failed
expression back as typed, and IOSSTUDIO_DEMO_DEBUG=plain shows the no-shadow fallback with the real notes;
IOSSTUDIO_DEMO_APPLE_DEV=outdated shows the iOS build scripts row the real steps.py builds for an old checkout.
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

from iosstudio.demo import debugger_demo as demo
from iosstudio.features import debugger, editor_debug, lldb_shadow
from iosstudio.features.setup import steps

DRIVER = Path(debugger.__file__).with_name("lldb_driver.py")


def from_driver(*names: str) -> dict:
    """Top-level names of lldb_driver.py, without importing it (it imports lldb and rewires stdout at import)."""
    tree = ast.parse(DRIVER.read_text())
    keep = [n for n in tree.body if (isinstance(n, ast.Assign) and any(getattr(t, "id", "") in names for t in n.targets))
            or (isinstance(n, ast.FunctionDef) and n.name in names)]
    assert len(keep) == len(names)
    ns: dict = {"re": re}
    exec(compile(ast.Module(body=keep, type_ignores=[]), str(DRIVER), "exec"), ns)
    return ns


def driver_parse_expr():
    return from_driver("_EXPR_CMD", "parse_expr")["parse_expr"]


EXPRESSIONS = ["day", "p day", "po day", "print day", "e day", "expr day", "expression day", "expression -- day",
               "expression -O -- day", "p -O day", "po  self.days[0]", "p + 1", "p - 1", "p * 2", "p == 3", "po",
               "p", "", "  p days.count  ", "p \"text\"", "p (1 + 2)", "p [1, 2]", "p .celsius", "p $R0", "p @x",
               "p #line", "p \\(x)", "expression", "po -O x", "pp x", "print(x)", "e -- x", "exprx", "p\tday"]


@pytest.mark.parametrize("text", EXPRESSIONS)
def test_demo_parses_expressions_like_the_real_driver(text):
    assert demo._parse_expr(text) == driver_parse_expr()(text)


def test_a_variable_named_p_stays_swift():
    assert demo._parse_expr("p + 1") == ("p + 1", False)
    assert demo._parse_expr("po forecast") == ("forecast", True)


# ================================================================ a demo session, fed from a script

class Script:
    """Stands in for debugger_demo.Stdin: hands out one batch of commands per poll, then detaches."""
    batches: list[list[dict]] = []

    def __init__(self):
        self.left = list(Script.batches)

    def poll(self, timeout: float) -> list[dict]:
        return self.left.pop(0) if self.left else [{"cmd": "detach"}]


def session(monkeypatch, capsys, *cmds: dict) -> list[dict]:
    monkeypatch.setattr(demo, "_daemon", lambda cmd, args: (None, None))     # no demo daemon: the built-in answers
    Script.batches = [[{"cmd": "pause"}], [dict(c, requestId=i) for i, c in enumerate(cmds)]]
    monkeypatch.setattr(demo, "Stdin", Script)
    assert demo.session({"bundle_id": "XTL-A1B2C3D4E5.com.example.weatherglance"}) == 0
    return [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.strip()]


def evaluated(ev: list[dict]) -> list[dict]:
    return [e for e in ev if e.get("event") == "evaluated"]


def test_a_failed_expression_comes_back_as_typed(monkeypatch, capsys):
    ev = session(monkeypatch, capsys, {"cmd": "evaluate", "expr": "p nope.count"}, {"cmd": "evaluate", "expr": "po day"},
                 {"cmd": "evaluate", "expr": "p + 1"}, {"cmd": "evaluate", "expr": "  "})
    bad, good, plus = evaluated(ev)
    assert (bad["expr"], bad["ok"]) == ("p nope.count", False) and "nope.count" in bad["result"]["error"]
    assert (good["expr"], good["ok"]) == ("po day", True) and good["result"]["description"].startswith("▿ Day")
    assert (plus["expr"], plus["ok"]) == ("p + 1", False) and "'p + 1'" in plus["result"]["error"]   # Swift, not `p`
    empty = next(e for e in ev if e.get("event") == "ack" and e.get("cmd") == "evaluate")
    assert (empty["ok"], empty["code"], empty["message"]) == (False, "bad-args", "Nothing to evaluate.")


def test_the_default_demo_has_swift_expressions(monkeypatch, capsys):
    monkeypatch.delenv("IOSSTUDIO_DEMO_DEBUG", raising=False)
    ev = session(monkeypatch, capsys)
    assert next(e for e in ev if e.get("event") == "started")["swiftExpressions"] is True
    assert not any(e.get("id") == "lldb-plain" for e in ev)
    assert demo.check({}) == 0
    chk = json.loads(capsys.readouterr().out.strip().splitlines()[-1])["data"]
    assert chk["swiftExpressions"] is True and chk["expressionsNote"] is None and chk["shadowLldb"]


# ================================================================ IOSSTUDIO_DEMO_DEBUG=plain

@pytest.fixture
def real_notes(monkeypatch) -> tuple[str, str]:
    """The notes real mode builds when there's no iOS SDK (so no shadow lldb)."""
    monkeypatch.setattr(lldb_shadow.appdirs, "sdk_bundle", lambda: None)
    tc = debugger.Toolchain("/usr/bin/lldb", "lldb 21", "/usr/bin/python3", "/py", {})
    return debugger.session_lldb(tc).note, editor_debug.expressions_note(lldb_shadow.for_lldb("/usr/bin/lldb")[1])


def test_plain_check_has_the_real_note(monkeypatch, capsys, real_notes):
    monkeypatch.setenv("IOSSTUDIO_DEMO_DEBUG", "plain")
    assert demo.check({}) == 0
    chk = json.loads(capsys.readouterr().out.strip().splitlines()[-1])["data"]
    assert chk["swiftExpressions"] is False and chk["shadowLldb"] is None
    assert chk["expressionsNote"] == real_notes[0] == demo.PLAIN_NOTE


def test_plain_session_carries_the_notice_and_expressions_fail(monkeypatch, capsys, real_notes):
    monkeypatch.setenv("IOSSTUDIO_DEMO_DEBUG", "plain")
    ev = session(monkeypatch, capsys, {"cmd": "evaluate", "expr": "po day"})
    kinds = [e.get("event") if e.get("event") != "notice" else e["id"] for e in ev]
    assert kinds.index("lldb-plain") < kinds.index("started")              # right after "Checking the debugger", as real
    notice = next(e for e in ev if e.get("id") == "lldb-plain")
    assert (notice["level"], notice["message"]) == ("info", real_notes[0])
    assert next(e for e in ev if e.get("event") == "started")["swiftExpressions"] is False
    (res,) = evaluated(ev)
    assert (res["expr"], res["ok"], res["code"]) == ("po day", False, "swift-modules")
    trouble = from_driver("_MODULE_TROUBLE", "_STALE_TYPES", "_NO_JIT", "_HOSTINFO_LINE", "strip_noise", "run_fix",
                          "module_trouble")["module_trouble"](res["result"]["error"], False)
    assert (res["message"], res["fix"]) == (trouble["message"], trouble["fix"])   # the driver's wording for this error


def test_plain_editor_debugging_has_the_editor_note(monkeypatch, capsys, real_notes):
    monkeypatch.setenv("IOSSTUDIO_DEMO_DEBUG", "plain")
    monkeypatch.setattr(demo, "_daemon", lambda cmd, args: (None, None))
    monkeypatch.setattr(demo, "Stdin", Script)
    Script.batches = []
    assert demo.editor({"bundle_id": "XTL-A1B2C3D4E5.com.example.weatherglance"}) == 0
    ev = [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert next(e for e in ev if e.get("id") == "lldb-plain")["message"] == real_notes[1]
    assert next(e for e in ev if e.get("event") == "ready")["swiftExpressions"] is False


# ================================================================ IOSSTUDIO_DEMO_APPLE_DEV=outdated

def test_outdated_row_matches_what_steps_builds(tmp_path, monkeypatch):
    from support.setup_upgrade import facts_for, old_machine, relocate
    m = old_machine(tmp_path)
    relocate(monkeypatch, m)
    real = next(r for r in steps.build_steps(facts_for(m))[0] if r["id"] == "apple-dev")
    done = dict(real, state="done", detail="At the tested version", canAutoInstall=False,
                action={"kind": "install", "label": "Install"})   # what the ready demo shows before the variant
    from iosstudio.demo import demo as d
    assert d.outdated_apple_dev_row(done, head=m.ad_old, pinned=m.ad_new) == real


def test_outdated_variant_in_setup_check(monkeypatch):
    from iosstudio.demo import demo as d
    from iosstudio.features.setup import pins
    row = {"id": "apple-dev", "title": "iOS build scripts", "group": "Toolchain", "state": "done", "essential": True,
           "canAutoInstall": False, "action": {"kind": "install", "label": "Install"}, "detail": ""}
    monkeypatch.setattr(d, "_real", lambda *a: {"steps": [dict(row)], "groups": []})
    monkeypatch.delenv("IOSSTUDIO_DEMO_APPLE_DEV", raising=False)
    assert d._setup_check({})["steps"][0]["state"] == "done"
    monkeypatch.setenv("IOSSTUDIO_DEMO_APPLE_DEV", "outdated")
    out = d._setup_check({})
    ad = out["steps"][0]
    assert ad["state"] == "broken" and ad["action"] == {"kind": "install", "label": "Update"} and ad["canAutoInstall"]
    assert ad["update"] is True      # an update, not a fault: Setup shows it calmly
    assert ad["detail"].startswith(f"You have {d.OLD_APPLE_DEV[:7]}; iOS Studio is tested with {pins.APPLE_DEV_COMMIT[:7]}.")
    assert out["summary"]["ready"] is False and out["summary"]["broken"] == [] and out["summary"]["updates"] == ["apple-dev"]
    monkeypatch.setattr(d, "FRESH", True)                    # a fresh machine has no checkout to be outdated
    assert d._setup_check({})["steps"][0]["action"]["label"] != "Update"
