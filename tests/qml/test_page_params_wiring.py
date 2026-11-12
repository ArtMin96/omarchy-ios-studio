"""shell.qml hands pages their nav() params only through app.takePageParams (one delivery per nav, see
tst_page_params.qml), never app.pageParams directly: a page that loads again mustn't repeat an action such as
{action: "renew"}."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SHELL = (ROOT / "shell.qml").read_text()
APPSTATE = (ROOT / "AppState.qml").read_text()


def test_pages_get_params_once() -> None:
    calls = re.findall(r"item\.activated\(([^)]*\)?)\)", SHELL)
    assert len(calls) == 2, calls                      # when a loaded page is shown, and when its loader finishes
    for arg in calls:
        assert arg.startswith("app.takePageParams("), arg
    assert "pendingParams = { page: key, params: root.pageParams }" in APPSTATE
    assert "Model.takePageParams(root.pendingParams, key" in APPSTATE
