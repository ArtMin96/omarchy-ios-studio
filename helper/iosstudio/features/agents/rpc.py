"""`agents test`: start an MCP server over stdio, initialize, page through tools/list and count the tools."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

from iosstudio.core.output import HelperError

STDERR_KEEP = 40         # stderr lines kept (the last 20 go into an error's detail)
MAX_PAGES = 49           # tools/list requests at most (ids 2..50)


class StdioRpc:
    """A JSON-RPC client over a child's stdin/stdout (one JSON message per line). Two daemon threads read
    stdout and stderr; close() ends the child's whole process group."""

    def __init__(self, argv: list[str], env: dict, cwd: Path):
        self.p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  env=env, cwd=str(cwd), start_new_session=True)
        self.err: list[str] = []
        self.lines: list[bytes] = []
        self.cv = threading.Condition()
        threading.Thread(target=self._drain_err, daemon=True).start()
        threading.Thread(target=self._read_out, daemon=True).start()

    def _drain_err(self) -> None:
        assert self.p.stderr
        for raw in self.p.stderr:
            self.err.append(raw.decode(errors="replace").rstrip())
            del self.err[:-STDERR_KEEP]

    def _read_out(self) -> None:
        assert self.p.stdout
        for raw in self.p.stdout:
            with self.cv:
                self.lines.append(raw)
                self.cv.notify_all()
        with self.cv:
            self.cv.notify_all()

    def send(self, obj: dict) -> None:
        assert self.p.stdin
        self.p.stdin.write((json.dumps(obj) + "\n").encode())
        self.p.stdin.flush()

    def wait_for(self, rid: int, deadline: float) -> dict:
        """The result of request `rid` (other messages are skipped), by time.monotonic() deadline."""
        while True:
            with self.cv:
                while not self.lines:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        raise HelperError("timeout", "The server didn't answer in time.", detail=self.stderr())
                    if self.p.poll() is not None:
                        raise HelperError("server-exited", "The server stopped before it answered.",
                                          detail=self.stderr())
                    self.cv.wait(min(left, 0.5))
                raw = self.lines.pop(0)
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                raise HelperError("server-protocol", "The server printed something that isn't MCP.",
                                  detail=raw.decode(errors="replace")[:500]) from None
            if msg.get("id") == rid and ("result" in msg or "error" in msg):
                if "error" in msg:
                    raise HelperError("server-protocol", "The server rejected the request.",
                                      detail=json.dumps(msg["error"]))
                return msg["result"]

    def stderr(self) -> str:
        return "\n".join(self.err[-20:])

    def close(self) -> None:
        try:
            if self.p.stdin:
                self.p.stdin.close()
            self.p.wait(timeout=5)
        except Exception:
            try:
                os.killpg(self.p.pid, signal.SIGTERM)
                self.p.wait(timeout=3)
            except Exception:
                try:
                    os.killpg(self.p.pid, signal.SIGKILL)
                except Exception:
                    pass


def list_tools(cmd: str, env: dict, cwd: Path, timeout: float) -> dict:
    """Start the server, initialize, list its tools. -> {init: initialize result, names: [tool names]}"""
    deadline = time.monotonic() + timeout
    rpc = StdioRpc([cmd], env, cwd)
    try:
        rpc.send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                             "clientInfo": {"name": "ios-studio-test", "version": "1"}}})
        init = rpc.wait_for(1, deadline)
        rpc.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        names: list[str] = []
        cursor = None
        for rid in range(2, 2 + MAX_PAGES):
            rpc.send({"jsonrpc": "2.0", "id": rid, "method": "tools/list",
                      "params": {"cursor": cursor} if cursor else {}})
            res = rpc.wait_for(rid, deadline)
            names += [t.get("name", "") for t in res.get("tools", [])]
            cursor = res.get("nextCursor")
            if not cursor:
                break
    finally:
        rpc.close()
    return {"init": init, "names": names}
