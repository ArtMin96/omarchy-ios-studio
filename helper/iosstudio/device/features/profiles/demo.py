"""Demo: profiles.list."""
from __future__ import annotations

import time

from iosstudio.device.demo.core import APPS, TEAM


def register(reg):
    @reg.command("profiles.list")
    def profiles(ctx, a):
        now = time.time()
        return [{"name": "XTL profile " + n, "uuid": "00000000-0000-4000-8000-00000000000%d" % i, "appId": f"{TEAM}.{b}",
                 "team": TEAM, "teamName": "Alex Rivera", "created": now - (2 + i) * 86400, "expires": now + (5 - i) * 86400,
                 "expired": False, "devices": 1, "isXcodeManaged": False}
                for i, (n, b, *_) in enumerate(APPS[:2])]
