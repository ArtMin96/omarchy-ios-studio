"""ios_screenshot, ios_screen_record and ios_screenshot_compare end to end over a real MCP stdio session: the
server is spawned (python -m iosstudio_mcp) in demo mode with its own XDG and runtime folders, and spawns the
demo device daemon itself. These tools build their own CallToolResult (an image block plus structuredContent);
a real client must get a result, not "Error executing tool" (to_result once re-dumped such a result and failed
validation).

Needs the MCP server's dependencies (the dev venv; skipped otherwise):
    python -m pytest tests/mcp/test_mcp_stdio_images.py
"""
from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from mcp.client.stdio import stdio_client

from mcp import ClientSession, StdioServerParameters
from support import ROOT, isolated_env


class ImageToolsOverStdio(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mcp-img-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        rt = self.tmp / "rt"
        rt.mkdir(mode=0o700)
        self.env = isolated_env(self.tmp, IOSSTUDIO_DEMO="1", XDG_RUNTIME_DIR=str(rt), IOSSTUDIO_RUNTIME_DIR=str(rt),
                                IOSSTUDIO_DEVICED_IDLE="2", PYTHONPATH=str(ROOT / "mcp"))

    def test_image_tools(self):
        asyncio.run(asyncio.wait_for(self._flow(), 120))

    async def _flow(self):
        params = StdioServerParameters(command=sys.executable, args=["-m", "iosstudio_mcp"], env=self.env,
                                       cwd=str(ROOT / "mcp"))
        async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
            await s.initialize()

            async def call(tool, **args):
                res = await s.call_tool(tool, args)
                text = "".join(getattr(c, "text", "") for c in res.content)
                self.assertFalse(res.is_error, f"{tool}: {text}")
                self.assertNotIn("Error executing tool", text)
                return res

            shot = await call("ios_screenshot")
            self.assertIn("image", [c.type for c in shot.content])
            self.assertTrue(shot.structured_content["path"])
            self.assertGreater(shot.structured_content["width"], 0)

            rec = await call("ios_screen_record", seconds=3, wait_seconds=20)
            for _ in range(10):
                if rec.structured_content.get("state") != "running":
                    break
                rec = await call("ios_screen_record", job_id=rec.structured_content["job_id"], wait_seconds=20)
            self.assertEqual(rec.structured_content["state"], "done", rec.structured_content)
            self.assertTrue(rec.structured_content["path"].endswith(".mp4"))

            base = await call("ios_screenshot_compare", project_path="WeatherGlance", name="home", save_baseline=True)
            self.assertTrue(base.structured_content["baseline_path"])
            cmp = await call("ios_screenshot_compare", project_path="WeatherGlance", name="home", include_image=True)
            self.assertIn(cmp.structured_content["verdict"], ("same", "changed"))
            self.assertIn("image", [c.type for c in cmp.content])


if __name__ == "__main__":
    unittest.main()
