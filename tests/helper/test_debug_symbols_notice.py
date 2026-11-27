"""One notice about the iPhone's debug symbols, with one fix (v0.4.8): a session without them used to show both
"no-symbols" and "slow-variables", each with a button to the same download. Now the helper says it once at the start
(what's affected: system frames, some Swift values, slow variables over Wi-Fi), in sessions, the editor bridge and
demo mode alike, and the fix carries action "symbols" so the iPhone page starts the download from any link."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from iosstudio.demo import debugger_demo as demo
from iosstudio.features import debugger as dbg
from iosstudio.features import editor_debug_bridge as bridge
from iosstudio.features import lldb_shadow
from support.fake_deviced import FakeDeviced


def notices(capsys) -> list[dict]:
    return [e for e in (json.loads(x) for x in capsys.readouterr().out.splitlines() if x.strip())
            if e.get("event") == "notice"]


def test_the_fix_starts_the_download_on_the_iphone_page():
    assert dbg.SYMBOLS_FIX == {"label": "Download symbols", "page": "device", "tab": "debug", "action": "symbols"}
    assert dbg.SYMBOLS_UPDATE_FIX == {**dbg.SYMBOLS_FIX, "label": "Update symbols"}
    for words in ("system frames", "Swift values", "slow over Wi-Fi", "iPhone › Debug › Debug symbols"):
        assert words in dbg.SYMBOLS_NOTE


@pytest.mark.parametrize("emit", [dbg.symbols_notice, demo.symbols_notice])
def test_one_notice_for_each_state(emit, capsys):
    emit({"symbolsReady": False})
    assert notices(capsys) == [{"event": "notice", "id": "no-symbols", "level": "info", "message": dbg.SYMBOLS_NOTE,
                                "fix": dbg.SYMBOLS_FIX}]
    emit({"symbolsReady": True, "symbolsUpdate": {"needed": True}})
    assert notices(capsys) == [{"event": "notice", "id": "symbols-update", "level": "warn",
                                "message": dbg.SYMBOLS_UPDATE_NOTE, "fix": dbg.SYMBOLS_UPDATE_FIX}]
    emit({"symbolsReady": True})
    assert notices(capsys) == []


def test_the_editor_bridge_says_the_same(tmp_path, monkeypatch, capsys):
    tc = tmp_path / "tc/usr/bin"
    tc.mkdir(parents=True)
    for name in ("lldb", "lldb-dap"):
        (tc / name).write_text("#!/bin/sh\n")
        (tc / name).chmod(0o755)
    monkeypatch.setattr(dbg, "toolchain", lambda: dbg.Toolchain(str(tc / "lldb"), "lldb-21", sys.executable, "", {}))
    monkeypatch.setattr(lldb_shadow, "for_lldb", lambda *a: (None, "No SDK."))
    monkeypatch.setenv("IOSSTUDIO_EDITOR_DEBUG_LIFETIME", "0.2")
    start = {"bundleId": "com.example.n", "port": 50012, "host": "127.0.0.1", "pid": 0, "launched": False,
             "symbolsReady": False}
    with FakeDeviced(Path(os.environ["IOSSTUDIO_RUNTIME_DIR"]), {"debug.start": start, "debug.stop": {}}):
        assert bridge.cmd_editor({"bundle_id": "com.example.n"}) == 0
    symbols = [n for n in notices(capsys) if "symbols" in n["id"]]
    assert symbols == [{"event": "notice", "id": "no-symbols", "level": "info", "message": dbg.SYMBOLS_NOTE,
                        "fix": dbg.SYMBOLS_FIX}]
