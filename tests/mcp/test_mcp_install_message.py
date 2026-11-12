"""install_ipa (backends/device.py) hands the daemon's installProgress `message` on as the status, and the
install's progress line shows that sentence as is ("Waiting for your iPhone. If App is open on it, close it")
instead of "installing 0%", so an agent can tell the user what the iPhone waits for.

    python -m pytest tests/mcp/test_mcp_install_message.py
"""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from iosstudio_mcp.backends import device as D

WAIT = "Waiting for your iPhone. If App is open on it, close it"


class InstallMessage(unittest.IsolatedAsyncioTestCase):
    async def test_the_message_replaces_the_status_and_the_percent(self):
        state = SimpleNamespace(extras={})
        seen: list[tuple[float, str]] = []

        async def client(_state):
            return None

        async def dcall(_state, cmd, args, **kw):
            for cb in list(D.hub(state)["listeners"]):
                cb("installProgress", {"path": "/p/App.ipa", "percent": 0, "status": "installing"})
                cb("installProgress", {"path": "/p/App.ipa", "percent": 0, "status": "installing", "message": WAIT})
                cb("installProgress", {"path": "/other.ipa", "percent": 9, "status": "installing", "message": "x"})
            return {"bundleId": "dev.app"}

        with mock.patch.object(D, "client", client), mock.patch.object(D, "dcall", dcall):
            out = await D.install_ipa(state, None, "/p/App.ipa", lambda pct, st: seen.append((pct, st)))
        await asyncio.sleep(0)
        self.assertEqual(out, {"bundleId": "dev.app"})
        self.assertEqual(seen, [(0.0, "installing"), (0.0, WAIT)])
        self.assertEqual([D.install_detail(p, s) for p, s in seen], ["installing 0%", WAIT])
        self.assertEqual(D.install_detail(40, ""), "installing 40%")


if __name__ == "__main__":
    unittest.main()
