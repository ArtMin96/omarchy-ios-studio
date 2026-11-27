"""`updates apply` through the helper as a subprocess (/usr/bin/python3, a private HOME and XDG folders), against the
fake upstream of tests/support/updates_fakes.py: real downloads with checksums, a real venv built by pip from a
fake index, real rollbacks.

The helper runs as the app runs it (/usr/bin/python3 -I) with the fake's base URLs (IOSSTUDIO_GITHUB_DL for
release downloads and the rest from FakeUpstream.env()); everything else (urllib, tar, venv, pip, git) is real.

    .venv/bin/pytest -q tests/cli/test_updates_apply_cli.py
"""
from __future__ import annotations

import fcntl
import json
import os
import select
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from support import ROOT, isolated_env
from support.updates_fakes import FakeDaemon, FakeUpstream, Upstream, tool_script

PY = "/usr/bin/python3"


class UpdatesApply(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp()))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "home"
        self.bin = self.home / ".local/bin"
        self.bin.mkdir(parents=True)
        self.up = FakeUpstream(self.tmp / "up")
        self.up.__enter__()
        self.addCleanup(self.up.__exit__, None, None, None)
        self.run_dir = Path(tempfile.mkdtemp(prefix="iosu-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.run_dir, ignore_errors=True)
        self.env = isolated_env(self.tmp, HOME=str(self.home), IOSSTUDIO_RUNTIME_DIR=str(self.run_dir), **self.up.env())
        self.state = self.tmp / "xdg_state_home/ios-studio"
        self.cache = self.tmp / "xdg_cache_home/ios-studio"
        self.venv = self.tmp / "xdg_data_home/ios-studio/venv"
        for name, ver in (("ipsw", "3.1.743"), ("rcodesign", "0.29.0")):
            (self.bin / name).write_text(tool_script(name, ver))
            (self.bin / name).chmod(0o755)
        self.fake_venv("11.49.0")

    def fake_venv(self, version: str) -> None:
        """An installed pymobiledevice3 venv as Setup's probes see it (its python answers the version probe)."""
        (self.venv / "bin").mkdir(parents=True)
        py = self.venv / "bin/python"
        py.write_text(f"#!/bin/sh\nprintf '{version}\\n3.14.0\\n'\n")
        py.chmod(0o755)
        (self.venv / f"lib/python3.14/site-packages/pymobiledevice3-{version}.dist-info").mkdir(parents=True)

    # ---- running the helper
    def argv(self, *args: str) -> list[str]:
        return [PY, "-I", "-X", f"pycache_prefix={self.tmp / 'pyc'}", str(ROOT / "helper/run.py"), "iosstudio", "updates",
                *args]

    def helper(self, *args: str, timeout: float = 240) -> list[dict]:
        r = subprocess.run(self.argv(*args), capture_output=True, text=True, env=self.env, timeout=timeout,
                           input="")
        lines = [json.loads(ln) for ln in r.stdout.splitlines() if ln.strip()]
        self.assertTrue(lines, r.stdout + r.stderr)
        return lines

    def done(self, evs: list[dict]) -> dict:
        d = evs[-1]
        self.assertEqual(d.get("event"), "done", evs[-5:])
        return d

    def status(self) -> dict:
        return self.helper("status")[0]["data"]

    def comp(self, st: dict, cid: str) -> dict:
        return next(c for c in st["components"] if c["id"] == cid)

    def check(self) -> dict:
        d = self.done(self.helper("check", "--force"))
        self.assertTrue(d["ok"], d)
        return d["data"]

    def start(self, *args: str) -> subprocess.Popen:
        p = subprocess.Popen(self.argv(*args), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, env=self.env)
        self.addCleanup(lambda: p.poll() is None and p.kill())
        return p

    def read_until(self, p: subprocess.Popen, pred, timeout: float = 60) -> list[dict]:
        out, end = [], time.monotonic() + timeout
        while time.monotonic() < end:
            ready, _, _ = select.select([p.stdout], [], [], 0.5)
            if not ready:
                continue
            line = p.stdout.readline()
            if not line:
                break
            ev = json.loads(line)
            out.append(ev)
            if pred(ev):
                return out
        self.fail(f"no matching event: {out[-5:]}")

    def settings(self) -> dict:
        p = self.tmp / "xdg_config_home/ios-studio/settings.json"
        return json.loads(p.read_text()) if p.exists() else {}

    def channel_pins(self) -> dict:
        p = self.state / "setup.json"
        return json.loads(p.read_text()).get("channelPins", {}) if p.exists() else {}

    def leftovers(self) -> list[str]:
        prev = self.cache / "updates/prev"
        return sorted(p.name for p in prev.iterdir()) if prev.is_dir() else []

    # ================================================================ tests

    def test_pymobiledevice3_latest_then_back_to_tested(self):
        st = self.check()
        self.assertEqual(self.comp(st, "pymobiledevice3")["state"], "current")
        with FakeDaemon(self.run_dir) as daemon:
            evs = self.helper("apply", "--component", "pymobiledevice3", "--channel", "latest", "--yes")
            self.assertEqual(daemon.shutdowns, 1)              # stopped before the venv moved; nothing restarted it
        d = self.done(evs)
        self.assertTrue(d["ok"], d)
        self.assertEqual((d["data"]["applied"], d["data"]["rolledBack"]), (["pymobiledevice3"], []))
        self.assertIn({"event": "verified", "id": "pymobiledevice3", "source": "pypi"}, evs)
        self.assertTrue(any(e.get("event") == "log" and "Checked: pymobiledevice3 99.1.0" in e["text"] for e in evs))
        self.assertTrue((self.venv / "lib").glob("python3*/site-packages/pymobiledevice3-99.1.0.dist-info"))
        out = subprocess.run([self.venv / "bin/python", "-I", "-c", "import pymobiledevice3; print(pymobiledevice3.__version__)"],
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(out.stdout.strip(), "99.1.0")
        self.assertFalse(self.venv.with_name("venv.prev").exists())
        self.assertFalse((self.state / "updates-inprogress.json").exists())
        self.assertEqual(self.settings()["toolchainChannel"], "latest")
        self.assertEqual(self.channel_pins()["pymobiledevice3"]["version"], "99.1.0")
        self.assertEqual(len(self.channel_pins()["pymobiledevice3"]["sha256"]), 64)
        st = d["data"]["status"]
        self.assertEqual(self.comp(st, "pymobiledevice3")["state"], "newer-than-tested")

        # Switching back to Tested lists the reinstall, then apply --all --channel tested does it.
        res = self.helper("channel", "--set", "tested")[0]["data"]
        self.assertEqual(res, {"channel": "tested", "reinstall": ["pymobiledevice3"]})
        self.helper("channel", "--set", "latest")
        d = self.done(self.helper("apply", "--all", "--channel", "tested", "--yes"))
        self.assertTrue(d["ok"], d)
        self.assertEqual(d["data"]["applied"], ["pymobiledevice3"])
        out = subprocess.run([self.venv / "bin/python", "-I", "-c", "import pymobiledevice3; print(pymobiledevice3.__version__)"],
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(out.stdout.strip(), "11.49.0")
        self.assertEqual(self.settings()["toolchainChannel"], "tested")
        st = self.status()
        self.assertEqual((st["channel"], st["count"]), ("tested", 0))
        self.assertEqual(self.comp(st, "pymobiledevice3")["state"], "current")

    def test_pymobiledevice3_waits_for_debug_and_mirror_sessions(self):
        self.check()
        with FakeDaemon(self.run_dir, [{"key": "debug:com.example.App"}, {"key": "mirror"}]) as daemon:
            d = self.done(self.helper("apply", "--component", "pymobiledevice3", "--channel", "latest", "--yes"))
            self.assertEqual(daemon.shutdowns, 0)
        self.assertFalse(d["ok"])
        self.assertEqual(d["error"]["code"], "iphone-busy")
        self.assertIn("debugging, screen mirroring", d["error"]["message"])
        self.assertEqual(d["error"]["fix"]["page"], "device")
        self.assertTrue((self.venv / "lib/python3.14/site-packages/pymobiledevice3-11.49.0.dist-info").is_dir())

    def test_ipsw_verification_failure_rolls_back_then_a_good_release_installs(self):
        old = (self.bin / "ipsw").read_bytes()
        self.up.up = Upstream(ipsw_reports="3.1.750")      # the download checks out, the program says otherwise
        self.up.publish()
        self.check()
        evs = self.helper("apply", "--component", "ipsw", "--channel", "latest")
        d = self.done(evs)
        self.assertFalse(d["ok"])
        self.assertEqual(d["error"]["code"], "verify-failed")
        self.assertEqual(d["error"]["fix"]["page"], "toolchain")
        self.assertTrue(d["error"]["message"].endswith("The previous version is back."), d["error"])
        self.assertEqual((d["data"]["applied"], d["data"]["rolledBack"]), ([], ["ipsw"]))
        rb = next(e for e in evs if e.get("event") == "rollback")
        self.assertEqual(rb["id"], "ipsw")
        self.assertIn("3.1.750", rb["reason"])
        self.assertTrue(any(e.get("event") == "log" and e["text"].startswith("Put the previous ipsw back") for e in evs))
        self.assertEqual((self.bin / "ipsw").read_bytes(), old)
        self.assertEqual(self.leftovers(), [])
        self.assertNotIn("toolchainChannel", self.settings())
        self.assertNotIn("ipsw", self.channel_pins())

        self.up.up = Upstream(ipsw="3.1.753")
        self.up.publish()
        self.check()
        evs = self.helper("apply", "--component", "ipsw", "--channel", "latest")
        d = self.done(evs)
        self.assertTrue(d["ok"], d)
        self.assertIn({"event": "verified", "id": "ipsw", "source": "checksums.txt"}, evs)
        self.assertIn("3.1.753", subprocess.run([self.bin / "ipsw", "version"], capture_output=True, text=True).stdout)
        self.assertEqual(self.channel_pins()["ipsw"]["version"], "3.1.753")
        self.assertEqual(self.leftovers(), [])
        self.assertEqual(self.comp(d["data"]["status"], "ipsw")["state"], "newer-than-tested")

    def test_cancel_rolls_back(self):
        old = (self.bin / "rcodesign").read_bytes()
        self.check()
        self.up.delay["apple-codesign-0.30.0-x86_64-unknown-linux-musl.tar.gz"] = 8
        self.up.delay["apple-codesign-0.30.0-aarch64-unknown-linux-musl.tar.gz"] = 8
        p = self.start("apply", "--component", "rcodesign", "--channel", "latest")
        self.read_until(p, lambda e: e.get("event") == "log" and e["text"].startswith("downloading"))
        t0 = time.monotonic()
        p.stdin.write(json.dumps({"cancel": True}) + "\n")
        p.stdin.flush()
        evs = self.read_until(p, lambda e: e.get("event") == "done")
        self.assertLess(time.monotonic() - t0, 6)
        d = evs[-1]
        self.assertEqual((d["ok"], d["error"]["code"]), (False, "cancelled"))
        self.assertIn({"event": "rollback", "id": "rcodesign", "reason": "cancelled"}, evs)
        self.assertEqual((self.bin / "rcodesign").read_bytes(), old)
        self.assertEqual(self.leftovers(), [])
        self.assertFalse((self.state / "updates-inprogress.json").exists())
        p.wait(10)

    def test_killed_update_is_put_back_by_the_next_apply(self):
        old = (self.bin / "ipsw").read_bytes()
        self.check()
        self.up.delay["ipsw_3.1.752_linux_x86_64.tar.gz"] = 8
        self.up.delay["ipsw_3.1.752_linux_arm64.tar.gz"] = 8
        p = self.start("apply", "--component", "ipsw", "--channel", "latest")
        self.read_until(p, lambda e: e.get("event") == "log" and e["text"].startswith("downloading"))
        p.kill()                                             # SIGKILL: no rollback ran
        p.wait(10)
        (self.bin / "ipsw").write_text("half-written")        # as if it died while installing
        st = self.status()
        self.assertEqual(st["interrupted"]["component"], "ipsw")
        self.assertIsInstance(st["interrupted"]["since"], float)
        self.assertEqual(self.leftovers(), ["ipsw"])
        evs = self.helper("apply")
        d = self.done(evs)
        self.assertTrue(d["ok"], d)
        self.assertEqual(d["data"]["rolledBack"], ["ipsw"])
        self.assertIn({"event": "rollback", "id": "ipsw", "reason": "interrupted"}, evs)
        self.assertTrue(any(e.get("event") == "log" and "was interrupted" in e["text"] for e in evs))
        self.assertEqual((self.bin / "ipsw").read_bytes(), old)
        self.assertIsNone(self.status()["interrupted"])
        self.assertEqual(self.leftovers(), [])
        d = self.done(self.helper("apply"))                  # nothing left to put back
        self.assertEqual(d["error"]["code"], "usage")

    def test_dry_run_changes_nothing(self):
        self.check()
        before = {p.name: p.read_bytes() for p in self.bin.iterdir()}
        evs = self.helper("apply", "--all", "--channel", "latest", "--dry-run")
        d = self.done(evs)
        self.assertTrue(d["ok"], d)
        self.assertTrue(d["data"]["dryRun"])
        self.assertEqual([w["id"] for w in d["data"]["would"]], ["rcodesign", "pymobiledevice3", "ipsw"])
        self.assertEqual(d["data"]["would"][2], {"id": "ipsw", "installed": "3.1.743", "target": "3.1.752"})
        self.assertEqual(d["data"]["applied"], [])
        self.assertTrue(any("would download" in e.get("text", "") for e in evs))
        self.assertEqual({p.name: p.read_bytes() for p in self.bin.iterdir()}, before)
        self.assertFalse(self.venv.with_name("venv.prev").exists())
        self.assertEqual(self.settings(), {})
        self.assertFalse((self.state / "updates-inprogress.json").exists())

    def test_one_update_at_a_time(self):
        self.check()
        self.state.mkdir(parents=True, exist_ok=True)
        with open(self.state / "updates.lock", "a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            d = self.done(self.helper("apply", "--component", "ipsw", "--channel", "latest"))
        self.assertEqual((d["ok"], d["error"]["code"]), (False, "busy"))
        self.assertEqual(d["error"]["fix"]["page"], "toolchain")

    def test_nothing_to_do_and_blocked(self):
        self.check()
        d = self.done(self.helper("apply", "--component", "rcodesign", "--channel", "tested"))
        self.assertTrue(d["ok"], d)
        self.assertEqual(d["data"]["applied"], [])
        self.assertEqual(self.settings()["toolchainChannel"], "tested")   # nothing to do: the channel is saved
        d = self.done(self.helper("apply", "--component", "apple-dev"))
        self.assertEqual(d["error"]["code"], "blocked")
        self.assertEqual(d["error"]["fix"], {"label": "Open Setup", "page": "setup", "step": "apple-dev"})


# ================================================================ omarchy-apple-dev + xtool, in process
# setup_upgrade's machine: real git on local remotes, fake swift/ldd/xtool/install-toolchain.sh. Tested = the old
# commit, latest (from a cached check) = the new one, whose XTOOL_SHA moved: the update rebuilds xtool and the
# SDK's build tools, and a failed check puts both back (xtool before the scripts).

import pytest  # noqa: E402

from iosstudio.core import appdirs  # noqa: E402
from iosstudio.features.setup import channel, facts  # noqa: E402
from iosstudio.features.updates import apply as upd_apply  # noqa: E402
from iosstudio.features.updates import model  # noqa: E402
from iosstudio.features.updates.commands import cmd_apply  # noqa: E402
from support.setup_upgrade import STAMP, setup_machine  # noqa: E402


@pytest.fixture
def machine(tmp_path, monkeypatch):
    m = setup_machine(tmp_path / "m", monkeypatch)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache-home"))
    monkeypatch.setattr(appdirs, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(appdirs, "SETTINGS_FILE", tmp_path / "config/settings.json")
    monkeypatch.setattr(facts, "SETUP_STATE", tmp_path / "state/setup.json")
    monkeypatch.setattr(channel, "VENV_DIR", tmp_path / "data/venv")
    monkeypatch.setattr(facts, "VENV_DIR", tmp_path / "data/venv")
    monkeypatch.setattr(channel, "APPLE_DEV_COMMIT", m.ad_old)
    cache = model.empty_cache()
    cache["components"] = [{"id": "apple-dev", "latest": m.ad_new, "commit": m.ad_new, "tag": "v0.4.8",
                            "sdkStamp": STAMP, "xtoolSha": m.x_new},
                           {"id": "xtool", "latest": m.x_new, "commit": m.x_new}]
    model.save_cache(cache)
    return m


def run_apply(capsys, **flags) -> tuple[list[dict], dict]:
    cmd_apply(flags)
    evs = [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    return evs, evs[-1]


def test_apple_dev_update_rebuilds_xtool_and_records_both(machine, capsys):
    st = model.status("latest")
    assert [c["state"] for c in st["components"][:2]] == ["update", "update"]
    evs, d = run_apply(capsys, component="xtool", channel="latest")      # xtool comes with the scripts
    assert d["ok"], d
    assert d["data"]["applied"] == ["apple-dev"]
    assert [e["id"] for e in evs if e.get("event") == "verified"] == ["apple-dev", "xtool"]
    assert model.git_head(machine.apple_dev) == machine.ad_new
    assert Path(os.path.realpath(machine.local_bin / "xtool")).parent.name == f"xtool-{machine.x_new}"
    assert not (machine.lib / f"xtool-{machine.x_old[:7]}-gsa406").exists()   # pruned once it checked out
    pins = channel.channel_pins()
    assert (pins["apple-dev"]["commit"], pins["xtool"]["commit"]) == (machine.ad_new, machine.x_new)
    assert appdirs.settings()["toolchainChannel"] == "latest"
    st = d["data"]["status"]
    assert [c["state"] for c in st["components"][:2]] == ["newer-than-tested", "newer-than-tested"]


def test_apple_dev_failed_check_puts_xtool_then_the_scripts_back(machine, capsys, monkeypatch):
    old_link = os.path.realpath(machine.local_bin / "xtool")

    def broken():
        raise upd_apply.HelperError("verify-failed", "The new xtool doesn't start.")
    monkeypatch.setattr(upd_apply, "verify_xtool", broken)
    evs, d = run_apply(capsys, component="apple-dev", channel="latest")
    assert not d["ok"] and d["error"]["code"] == "verify-failed"
    assert d["data"]["rolledBack"] == ["apple-dev"]
    logs = [e["text"] for e in evs if e.get("event") == "log"]
    xt = next(i for i, t in enumerate(logs) if t == "put back the previous xtool")
    ad = next(i for i, t in enumerate(logs) if t.startswith("put back the iOS build scripts"))
    assert xt < ad
    assert model.git_head(machine.apple_dev) == machine.ad_old
    assert os.path.realpath(machine.local_bin / "xtool") == old_link
    assert not (machine.lib / f"xtool-{machine.x_new}").exists()       # the new build is pruned again
    assert machine.calls("install-toolchain.sh")[-1] == "install-toolchain.sh --mode full --repair"
    assert "apple-dev" not in channel.channel_pins() and "toolchainChannel" not in appdirs.settings()
    assert not model.marker_path().exists()
