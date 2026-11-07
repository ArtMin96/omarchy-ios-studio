"""Tool tests against fakes that behave like the real backends (needs the MCP server's dependencies).

    class T(ToolCase):                      # unittest.IsolatedAsyncioTestCase
        async def test_x(self):
            self.helper.reply("projects", "list", {"projects": [...]})      # the fake iosstudio-helper
            self.daemon.replies["apps.list"] = (0, [...])                    # the fake device daemon
            res = await self.call(MODULE, "ios_tool", arg=...)               # through the real @tool wrapper
            err = await self.error(MODULE, "ios_tool", ...)                  # the one-line ToolError text

- FakeDaemon speaks the device daemon's protocol (JSON lines on a Unix socket, docs/deviced-protocol.md) to the
  real DeviceClient; replies[cmd] = (delay, data) | (delay, {"error": {code, message}}) | callable(args).
  `delay` can be an asyncio.Event instead of seconds: the reply waits until the test sets it (no timing races).
  `emit(event, data)` pushes an event to every connected client, like the broker does.
- FakeHelper is an executable run by the real helper.call()/spawn_stream(): per "group action" it prints one
  {"ok": ...} line, or a list of JSON event lines (streams), after an optional delay; it records every argv.
- Tools run through the real core.registry.tool() wrapper (time budget, to_result, one-line errors);
  `self.ctx` carries `self.state` (a real AppState whose device client is connected to the fake daemon).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mcp.server.mcpserver.exceptions import ToolError

from iosstudio_mcp import helper
from iosstudio_mcp.backends import device as DEVICE
from iosstudio_mcp.core import deadline
from iosstudio_mcp.core.registry import tool as real_tool
from iosstudio_mcp.state import AppState
from support.mcp_fakes import FakeMCP, size_of
from support.waiting import until

__all__ = ["FAKE_HELPER", "FakeDaemon", "FakeHelper", "ToolCase", "Recorder", "err", "size_of", "registered"]


class Recorder(FakeMCP):
    """FakeMCP that also records the tool annotations and accepts (ignores) resources."""

    def __init__(self):
        super().__init__()
        self.annotations = {}

    def tool(self, **kw):
        def deco(fn):
            self.tools[fn.__name__] = fn
            self.annotations[fn.__name__] = kw.get("annotations")
            return fn
        return deco

    def resource(self, *a, **k):
        return lambda fn: fn


def registered(module) -> Recorder:
    """The module's tools registered through the real @tool wrapper."""
    rec = Recorder()
    with mock.patch.object(module, "tool", lambda m, *a, **k: real_tool(rec, *a, **k)):
        module.register(rec)
    return rec


class FakeDaemon:
    """The device daemon's request/reply protocol on a Unix socket (see the module docstring)."""

    def __init__(self, path: str):
        self.path = path
        self.replies: dict = {}
        self.requests: list[tuple[str, dict]] = []
        self.server = None
        self.writers: list[asyncio.StreamWriter] = []
        self._tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        self.server = await asyncio.start_unix_server(self._serve, path=self.path)

    async def stop(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        for w in self.writers:
            w.close()
        self.server.close()
        await self.server.wait_closed()

    async def _serve(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        self.writers.append(w)
        while line := await r.readline():
            t = asyncio.create_task(self._answer(w, json.loads(line)))
            self._tasks.add(t)
            t.add_done_callback(self._tasks.discard)

    async def _answer(self, w: asyncio.StreamWriter, msg: dict) -> None:
        cmd, args = msg["cmd"], msg.get("args") or {}
        self.requests.append((cmd, args))
        if cmd in ("hello", "subscribe"):
            rep = (0, {"pid": os.getpid(), "version": 1})
        else:
            rep = self.replies.get(cmd, (0, {}))
            if callable(rep):
                rep = rep(args)
        delay, data = rep
        if isinstance(delay, asyncio.Event):
            await delay.wait()
        elif delay:
            await asyncio.sleep(delay)
        failed = isinstance(data, dict) and isinstance(data.get("error"), dict)   # real replies carry "error": null
        out = {"id": msg["id"], "ok": False, "error": data["error"]} if failed \
            else {"id": msg["id"], "ok": True, "data": data}
        self._write(w, out)

    def _write(self, w, obj) -> None:
        with contextlib.suppress(Exception):
            w.write((json.dumps(obj) + "\n").encode())

    def emit(self, event: str, data) -> None:
        for w in self.writers:
            self._write(w, {"event": event, "data": data})

    def sent(self, cmd: str) -> list[dict]:
        return [a for c, a in self.requests if c == cmd]

    def count(self, cmd: str) -> int:
        return len(self.sent(cmd))


def err(code: str, message: str = "It failed.", **extra) -> dict:
    """A daemon error reply body."""
    return {"error": {"code": code, "message": message, **extra}}


FAKE_HELPER = textwrap.dedent("""\
    #!/usr/bin/env python3
    import json, sys, time
    me = sys.argv[0]
    with open(me + ".log", "a") as f:
        f.write(json.dumps(sys.argv[1:]) + "\\n")
    key = " ".join(sys.argv[1:3])
    spec = json.load(open(me + ".json")).get(key)
    if spec is None:
        print(json.dumps({"ok": False, "error": {"code": "usage", "message": "fake helper: no reply for " + key}}))
        sys.exit(2)
    time.sleep(spec.get("delay", 0))
    for line in spec.get("lines") or []:
        print(json.dumps(line), flush=True)
    if "out" in spec:
        print(json.dumps(spec["out"]))
    sys.exit(spec.get("exit", 0))
""")


class FakeHelper:
    """A fake bin/iosstudio-helper (see the module docstring). Patches helper.HELPER while active."""

    def __init__(self, folder: Path):
        self.exe = folder / "iosstudio-helper"
        self.exe.write_text(FAKE_HELPER)
        self.exe.chmod(0o755)
        self.spec: dict = {}
        self._save()

    def _save(self) -> None:
        Path(str(self.exe) + ".json").write_text(json.dumps(self.spec))

    def reply(self, group: str, action: str, data=None, *, delay: float = 0) -> None:
        self.spec[f"{group} {action}"] = {"out": {"ok": True, "data": data}, "delay": delay}
        self._save()

    def fail(self, group: str, action: str, code: str, message: str = "It failed.", **extra) -> None:
        self.spec[f"{group} {action}"] = {"out": {"ok": False, "error": {"code": code, "message": message, **extra}},
                                          "exit": 1}
        self._save()

    def stream(self, group: str, action: str, events: list[dict], *, delay: float = 0) -> None:
        """A streaming command: JSON event lines; end with {"event": "done", "ok": ..., "data": ...}."""
        self.spec[f"{group} {action}"] = {"lines": events, "delay": delay}
        self._save()

    def calls(self, group: str | None = None, action: str | None = None) -> list[list[str]]:
        p = Path(str(self.exe) + ".log")
        rows = [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []
        return [r for r in rows if (group is None or r[0] == group) and (action is None or r[1] == action)]

    @staticmethod
    def flag(argv: list[str], name: str) -> str | None:
        """The value of --name in a recorded argv (None if absent; '' for a bare flag)."""
        key = "--" + name.replace("_", "-")
        if key not in argv:
            return None
        i = argv.index(key)
        return argv[i + 1] if i + 1 < len(argv) and not argv[i + 1].startswith("--") else ""


class ToolCase(unittest.IsolatedAsyncioTestCase):
    """A fake daemon + fake helper + real AppState for each test. Set BUDGET to shorten MAX_WAIT."""

    BUDGET: float | None = 4.0
    DAEMON = True

    async def asyncSetUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="iost-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.helper = FakeHelper(self.tmp)
        patches = [mock.patch.object(helper, "HELPER", self.helper.exe)]
        if self.BUDGET:
            patches.append(mock.patch.object(deadline, "MAX_WAIT", self.BUDGET))
        self.state = AppState()
        self.daemon = None
        self.clients = []
        if self.DAEMON:
            self.daemon = FakeDaemon(str(self.tmp / "d.sock"))
            await self.daemon.start()
            patches.append(mock.patch.object(DEVICE, "new_client", self._new_client))
            DEVICE.attach(self.state)
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=self.state),
                                   report_progress=self._progress, info=self._progress)
        self._registered: dict = {}

    async def asyncTearDown(self):
        await self.state.jobs.shutdown()
        for c in self.clients:
            with contextlib.suppress(Exception):
                await c.close()
        if self.daemon:
            await self.daemon.stop()

    async def _progress(self, *a, **k):
        pass

    def _new_client(self, name, events):
        mod = helper.device_client_module()
        c = mod.DeviceClient(name=name, events=events, spawn=False, reconnect=False, socket=self.daemon.path)
        self.clients.append(c)
        return c

    def rec(self, module) -> Recorder:
        if module not in self._registered:
            self._registered[module] = registered(module)
        return self._registered[module]

    def fn(self, module, name: str, /):
        return self.rec(module).tools[name]

    async def call(self, module, name: str, /, **kw):
        """The tool's CallToolResult (raises the ToolError)."""
        fn = self.fn(module, name)
        if "ctx" in fn.__signature__.parameters:
            kw.setdefault("ctx", self.ctx)
        return await fn(**kw)

    async def data(self, module, name: str, /, **kw) -> dict:
        """The tool's structuredContent, after checking the result fits its budget."""
        res = await self.call(module, name, **kw)
        self.assertLessEqual(size_of(res), 48_000, name)
        return res.structured_content

    async def error(self, module, name: str, /, **kw) -> str:
        """The ToolError text of a failing call; asserts it is one line."""
        with self.assertRaises(ToolError) as cm:
            await self.call(module, name, **kw)
        text = str(cm.exception)
        self.assertNotIn("\n", text, name)
        return text

    async def until(self, cond, /, timeout: float = 5.0, what: str = ""):
        """support.waiting.until: wait for something that happens on the loop instead of a fixed sleep."""
        return await until(cond, timeout=timeout, what=what)

    async def poll(self, module, name: str, job_id: str, /, tries: int = 20, **kw) -> dict:
        """Call the tool again with job_id until its state isn't 'running'."""
        out = {}
        for _ in range(tries):
            out = await self.data(module, name, job_id=job_id, **kw)
            if out.get("state") != "running":
                return out
        self.fail(f"{name} {job_id} still running: {out}")
