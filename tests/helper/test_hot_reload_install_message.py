"""Hot reload's Wi-Fi install (session.Session.install): the device daemon's installProgress `message` (the
installer waits for the phone, or for the app to close) replaces the bar's "Installing on your iPhone over
Wi-Fi" while it lasts, for this .ipa only, and nothing is published from a late event once the install returned.

    .venv/bin/pytest -q tests/helper/test_hot_reload_install_message.py
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from iosstudio.features.hotreload import session

IPA = "/p/App/xtool/App.ipa"
WAIT = "Waiting for your iPhone. If App is open on it, close it"


class EventingDevice:
    """app.install sends `during` as installProgress events (name, data) before it answers."""

    def __init__(self, during: list[dict]):
        self.during = during
        self.subs: list = []
        self.calls: list[str] = []

    def on_event(self, cb, names):
        sub = (cb, names)
        self.subs.append(sub)
        return lambda: self.subs.remove(sub)

    def request(self, cmd, args, timeout, cancel=None):
        self.calls.append(cmd)
        if cmd == "app.install":
            for ev in self.during:
                for cb, names in list(self.subs):
                    if "installProgress" in names:
                        cb("installProgress", ev)
        return {}

    def close(self):
        pass


class InstallMessage(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp()))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        (self.tmp / "App").mkdir()
        self.events: list[dict] = []

    def session(self, dev) -> session.Session:
        s = session.Session(self.tmp / "App", udid="U1", device=dev, write=self.events.append, check_parent=False)
        s.snap = {"event": "hotreload", "state": "building", "message": ""}
        self.addCleanup(s.shutdown)
        return s

    def messages(self) -> list[str]:
        return [e["message"] for e in self.events if e.get("state") == "installing"]

    def test_the_waiting_message_shows_then_the_plain_text_comes_back(self):
        dev = EventingDevice([
            {"path": IPA, "percent": 0, "status": "installing"},
            {"path": IPA, "percent": 0, "status": "installing", "message": WAIT},
            {"path": IPA, "percent": 0, "status": "installing", "message": WAIT},      # said once
            {"path": "/other/App.ipa", "percent": 0, "status": "installing", "message": "not ours"},
            {"path": IPA, "percent": 60, "status": "installing"},
            {"path": IPA, "percent": 100, "status": "done"},
        ])
        s = self.session(dev)
        s.install({"ipa": IPA, "bundleId": "dev.app"})
        self.assertEqual(self.messages(), ["Installing on your iPhone over Wi-Fi", WAIT,
                                           "Installing on your iPhone over Wi-Fi"])
        self.assertEqual(dev.subs, [], "unsubscribed when the install returns")

    def test_a_device_without_events_still_installs(self):
        class Plain:
            calls: list[str] = []

            def request(self, cmd, args, timeout, cancel=None):
                self.calls.append(cmd)
                return {}

            def close(self):
                pass
        s = self.session(Plain())
        s.install({"ipa": IPA})
        self.assertEqual(self.messages(), ["Installing on your iPhone over Wi-Fi"])

    def test_the_link_subscribes_to_install_progress(self):
        src = Path(session.__file__).read_text()
        self.assertIn('DeviceClient(name=name, events=["installProgress"])', src)


if __name__ == "__main__":
    unittest.main()
