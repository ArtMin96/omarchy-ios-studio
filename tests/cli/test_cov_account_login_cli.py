"""`account login` end to end (helper subprocess, real Login state machine) against a fake xtool that asks for a
password, a team and a 6-digit code like the real one (tests/support/xtool_account.py)."""
from __future__ import annotations

import json
import queue
import subprocess
import threading

import pytest

from support import HELPER_PY, isolated_env, write_exe
from support.xtool_account import FAKE_XTOOL


class Session:
    """The helper's login stream: read events with a deadline, answer prompts on stdin."""

    def __init__(self, env: dict, *args: str):
        self.p = subprocess.Popen([*HELPER_PY, "account", "login", *args], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True, bufsize=1)
        self.events: list[dict] = []
        self.q: queue.Queue = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        for line in self.p.stdout:
            if line.strip():
                self.q.put(json.loads(line))
        self.q.put(None)

    def next(self, timeout: float = 10) -> dict:
        try:
            ev = self.q.get(timeout=timeout)
        except queue.Empty:
            self.close()
            raise AssertionError(f"no event in {timeout}s; got {self.events}") from None
        if ev is None:
            raise AssertionError(f"stream ended; got {self.events}; stderr {self.p.stderr.read()}")
        self.events.append(ev)
        return ev

    def until(self, name: str) -> dict:
        while True:
            ev = self.next()
            if ev.get("event") == name:
                return ev

    def answer(self, prompt: dict, value: str | None = None, cancel: bool = False) -> None:
        msg = {"prompt": prompt["id"], **({"cancel": True} if cancel else {"value": value})}
        self.p.stdin.write(json.dumps(msg) + "\n")
        self.p.stdin.flush()

    def close(self) -> int:
        try:
            self.p.stdin.close()
        except OSError:
            pass
        try:
            return self.p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.p.kill()
            return self.p.wait()


@pytest.fixture
def env(tmp_path):
    d = tmp_path / "scenario"
    d.mkdir()
    exe = write_exe(tmp_path / "xtool", FAKE_XTOOL)
    return isolated_env(tmp_path, IOSSTUDIO_XTOOL=str(exe), FAKE_XTOOL_DIR=str(d)), d


def test_password_team_code_sign_in(env):
    e, d = env
    (d / "teams").write_text("")
    s = Session(e, "--email", "jane@example.com")
    pw = s.until("prompt")
    assert pw["kind"] == "password" and pw["email"] == "jane@example.com"
    s.answer(pw, "hunter2\nextra line")                      # only the first line is sent
    team = s.until("prompt")
    assert team["kind"] == "team"
    assert team["options"] == [{"value": "1", "label": "Jane Doe", "teamId": "TEAM123456"},
                               {"value": "2", "label": "Acme Inc", "teamId": "ACME000001"}]
    s.answer({"id": "p999"}, "2")                             # an answer to another prompt is ignored
    s.answer(team, "2")
    code = s.until("prompt")
    assert code["kind"] == "code" and code["digits"] == 6 and code["attempt"] == 1
    s.answer(code, "123 456")                                 # non-digits are dropped
    done = s.until("done")
    assert s.close() == 0
    assert done["ok"] is True and done["data"]["status"]["appleId"] == "jane@example.com"
    steps = [(ev["id"], ev["state"]) for ev in s.events if ev.get("event") == "step"]
    assert ("login", "done") in steps
    answers = [ln for ln in (d / "calls").read_text().splitlines() if ln.startswith("answer:")]
    assert answers == ["answer:hunter2", "answer:2", "answer:123456"]
    logs = " ".join(ev.get("text", "") for ev in s.events if ev.get("event") == "log")
    assert "hunter2" not in logs                              # secrets never reach the log


def test_wrong_code_fails_with_a_plain_message(env):
    e, _ = env
    s = Session(e, "--email", "jane@example.com")
    s.answer(s.until("prompt"), "pw")
    s.answer(s.until("prompt"), "000000")
    done = s.until("done")
    assert s.close() == 1
    assert done["ok"] is False and done["error"]["code"] == "code-wrong"
    assert any(ev.get("event") == "step" and ev["state"] == "failed" for ev in s.events)


def test_cancel_at_a_prompt_kills_xtool(env):
    e, d = env
    s = Session(e, "--email", "jane@example.com")
    s.answer(s.until("prompt"), cancel=True)
    done = s.until("done")
    assert s.close() == 1 and done["error"]["code"] == "cancelled"
    assert "answer:" not in (d / "calls").read_text()


def test_email_is_asked_for_when_missing(env):
    e, _ = env
    s = Session(e)
    ask = s.until("prompt")
    assert ask["id"] == "p0" and ask["field"] == "email"
    s.answer(ask, "  ")
    done = s.until("done")
    assert s.close() == 1 and done["error"]["code"] == "empty"


def test_missing_xtool_is_a_done_event(tmp_path):
    e = isolated_env(tmp_path, IOSSTUDIO_XTOOL=str(tmp_path / "nope"))
    s = Session(e, "--email", "a@b.c")
    done = s.until("done")
    assert s.close() == 1 and done["error"]["code"] == "missing-tool"
