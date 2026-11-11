"""A minimal Debug Adapter Protocol client (what VS Code / Zed do with lldb-dap), for end-to-end tests.

    dap = DapClient([lldb_dap], env=env)
    dap.request("initialize", {...})
    seq = dap.send("attach", {"attachCommands": [...]})
    dap.wait(lambda m: m.get("type") == "response" and m.get("request_seq") == seq)
    dap.close()

`output` collects the console text lldb-dap sent (attach command output, error messages).
"""
from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from collections.abc import Callable


class DapClient:
    def __init__(self, argv: list[str], env: dict | None = None, cwd: str | None = None):
        self.p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  env=env, cwd=cwd)
        self.seq = 0
        self.output: list[str] = []
        self.events: list[dict] = []
        self._q: queue.Queue = queue.Queue()
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self) -> None:
        f = self.p.stdout
        try:
            while True:
                n = 0
                while True:
                    line = f.readline()
                    if not line:
                        self._q.put(None)
                        return
                    if not line.strip():
                        break
                    if line.lower().startswith(b"content-length:"):
                        n = int(line.split(b":", 1)[1])
                self._q.put(json.loads(f.read(n)))
        except (OSError, ValueError):
            self._q.put(None)

    def send(self, command: str, arguments: dict | None = None) -> int:
        self.seq += 1
        body = json.dumps({"seq": self.seq, "type": "request", "command": command,
                           "arguments": arguments or {}}).encode()
        self.p.stdin.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
        self.p.stdin.flush()
        return self.seq

    def _read(self, deadline: float) -> dict:
        try:
            m = self._q.get(timeout=max(0.01, deadline - time.monotonic()))
        except queue.Empty:
            raise TimeoutError("lldb-dap sent nothing in time") from None
        if m is None:
            raise EOFError("lldb-dap exited")
        return m

    def wait(self, pred: Callable[[dict], bool], timeout: float = 60) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            m = self._read(deadline)
            if m.get("type") == "event":
                self.events.append(m)
                if m.get("event") == "output":
                    self.output.append(str((m.get("body") or {}).get("output") or ""))
            if pred(m):
                return m

    def request(self, command: str, arguments: dict | None = None, timeout: float = 60) -> dict:
        seq = self.send(command, arguments)
        return self.wait(lambda m: m.get("type") == "response" and m.get("request_seq") == seq, timeout)

    def event(self, name: str, timeout: float = 60) -> dict:
        return self.wait(lambda m: m.get("type") == "event" and m.get("event") == name, timeout)

    def close(self) -> None:
        if self.p.poll() is None:
            self.p.kill()
        self.p.wait(10)
