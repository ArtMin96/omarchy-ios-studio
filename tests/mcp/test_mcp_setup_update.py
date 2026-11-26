"""An older iOS build scripts checkout (setup step state "broken" + update) reaches agents as an update, not a
fault: ios_status names it with its fix and keeps toolchain_ready, ios_toolchain_check reports state "update".
A fake iosstudio-helper behind the real helper.call().

    python -m pytest tests/mcp/test_mcp_setup_update.py
"""
from __future__ import annotations

from iosstudio_mcp.tools import status as STATUS
from iosstudio_mcp.tools import toolchain as TOOLCHAIN
from support.mcp_harness import ToolCase

DETAIL = "You have 79bda71; iOS Studio is tested with 9f8352f. Updating downloads it and rebuilds only what changed."
SETUP = {"summary": {"ready": False, "title": "1 update ready", "level": "info", "done": 13, "total": 14,
                     "essentialMissing": ["apple-dev"], "autoInstallable": ["apple-dev"], "broken": [],
                     "updates": ["apple-dev"]},
         "steps": [{"id": "swift", "title": "Swift", "group": "Toolchain", "state": "done", "essential": True},
                   {"id": "apple-dev", "title": "iOS build scripts", "group": "Toolchain", "state": "broken",
                    "update": True, "essential": True, "canAutoInstall": True, "detail": DETAIL,
                    "action": {"kind": "install", "label": "Update"}}]}


class Status(ToolCase):
    async def test_an_update_is_named_but_not_a_missing_or_broken_piece(self):
        self.helper.reply("setup", "check", SETUP)
        self.helper.reply("account", "status", {"signedIn": True})
        self.helper.reply("projects", "list", {"projects": []})
        self.helper.reply("build", "history", {"builds": []})
        d = await self.data(STATUS, "ios_status")
        self.assertTrue(d["toolchain_ready"])                    # it works as it is
        self.assertEqual(d["toolchain"], "1 update ready")
        tc = [p for p in d["problems"] if p["area"] == "toolchain"]
        self.assertEqual(len(tc), 1)
        self.assertTrue(tc[0]["message"].startswith("Update ready (it works meanwhile): iOS build scripts."))
        self.assertIn(DETAIL, tc[0]["message"])
        self.assertNotIn("Missing", tc[0]["message"])
        self.assertEqual(tc[0]["fix"], "ios_toolchain_install(step='apple-dev')")
        self.assertIn("ios_toolchain_install(step='apple-dev')", d["next_steps"])


class Check(ToolCase):
    DAEMON = False

    async def test_the_step_reads_update(self):
        self.helper.reply("setup", "check", SETUP)
        d = await self.data(TOOLCHAIN, "ios_toolchain_check")
        (ad,) = d["steps"]
        self.assertEqual((ad["id"], ad["state"], ad["can_auto_install"]), ("apple-dev", "update", True))
        self.assertIn("ios_toolchain_install(all=true) can install: apple-dev.", d["next_steps"])
