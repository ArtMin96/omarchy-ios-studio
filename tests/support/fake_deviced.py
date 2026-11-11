"""A stand-in device daemon in a thread, for helper subprocesses that talk to the daemon (tests/cli).

It speaks the daemon's JSON-lines protocol on <IOSSTUDIO_RUNTIME_DIR>/ios-studio/deviced.sock: `hello` and
`subscribe` are answered, every other command from `replies` (a dict value, a callable(args) -> value, or an
`{"error": {...}}` dict for an error reply; unknown commands reply {}). `requests` records (cmd, args).
demo=True listens where demo-mode clients look (ios-studio-demo/), so a demo helper doesn't spawn a daemon.

    with FakeDeviced(runtime_dir, {"debug.start": {...}}) as d:
        ... run the helper with IOSSTUDIO_RUNTIME_DIR=runtime_dir ...
        d.sent("debug.stop")
"""
from __future__ import annotations

import json
import os
import socket
import threading
from pathlib import Path
from typing import Any


class FakeDeviced:
    def __init__(self, runtime_dir: Path, replies: dict[str, Any] | None = None, demo: bool = False):
        self.path = Path(runtime_dir) / ("ios-studio-demo" if demo else "ios-studio") / "deviced.sock"
        self.replies = dict(replies or {})
        self.requests: list[tuple[str, dict]] = []
        self._lock = threading.Lock()
        self._sock: socket.socket | None = None
        self._stop = threading.Event()

    def __enter__(self) -> FakeDeviced:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.unlink(missing_ok=True)
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(str(self.path))
        self._sock.listen(8)
        self._sock.settimeout(0.2)
        threading.Thread(target=self._accept, daemon=True).start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self._sock:
            self._sock.close()
        self.path.unlink(missing_ok=True)

    def sent(self, cmd: str) -> list[dict]:
        with self._lock:
            return [a for c, a in self.requests if c == cmd]

    def _accept(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except (TimeoutError, OSError):
                continue
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        f = conn.makefile("rwb")
        try:
            for line in f:
                msg = json.loads(line)
                cmd, args = msg.get("cmd"), msg.get("args") or {}
                with self._lock:
                    self.requests.append((cmd, args))
                if cmd in ("hello", "subscribe"):
                    data: Any = {"pid": os.getpid(), "version": 1}
                else:
                    data = self.replies.get(cmd, {})
                    if callable(data):
                        data = data(args)
                failed = isinstance(data, dict) and isinstance(data.get("error"), dict)
                out = {"id": msg.get("id"), "ok": not failed}
                out.update({"error": data["error"]} if failed else {"data": data})
                f.write((json.dumps(out) + "\n").encode())
                f.flush()
        except (OSError, ValueError):
            pass
        finally:
            conn.close()
