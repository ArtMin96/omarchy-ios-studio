"""Crash report dates (helper/device_crash.py): file-name dates are the iPhone's local time. Stdlib only:
    python3 -m unittest discover -s tests
"""
from __future__ import annotations

import json
import os
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "helper"))

import device_crash  # noqa: E402

NAME = "/MyApp-2026-11-01-142233.ips"


class CrashDates(unittest.TestCase):
    def test_phone_zone_gives_exact_time(self):
        item = device_crash.classify(NAME, "America/New_York")    # DST ended that morning: -05:00
        self.assertEqual(item["process"], "MyApp")
        self.assertEqual(item["date"], "2026-11-01T14:22:33-05:00")
        self.assertEqual(item["ts"], 1793560953.0)
        self.assertFalse(item["dateLocal"])

    def test_unknown_zone_stays_naive_and_flagged(self):
        for tz in (None, "", "Not/AZone"):
            item = device_crash.classify(NAME, tz)
            self.assertEqual(item["date"], "2026-11-01T14:22:33")
            self.assertTrue(item["dateLocal"])
            self.assertGreater(item["ts"], 0)

    def test_computer_zone_does_not_change_phone_zone_result(self):
        old = os.environ.get("TZ")
        try:
            for zone in ("UTC", "Asia/Tokyo"):
                os.environ["TZ"] = zone
                time.tzset()
                self.assertEqual(device_crash.classify(NAME, "Europe/Lisbon")["ts"], 1793542953.0)
        finally:
            if old is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old
            time.tzset()

    def test_header_timestamp_carries_offset(self):
        head = {"app_name": "MyApp", "timestamp": "2026-11-01 14:22:33.00 +0100", "bug_type": "309"}
        out = device_crash.parse_ips(json.dumps(head) + "\n{}")
        self.assertEqual(out["date"], head["timestamp"])
        self.assertEqual(out["dateIso"], "2026-11-01T14:22:33+01:00")
        self.assertEqual(out["ts"], 1793539353.0)
        self.assertIsNone(device_crash.header_time("yesterday"))


if __name__ == "__main__":
    unittest.main()
