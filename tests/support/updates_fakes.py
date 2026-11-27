"""Upstream for toolchain updates, without the network: one local HTTP server standing in for GitHub's API, raw
files and release downloads, PyPI (JSON API and a simple index pip installs from) and the AUR RPC, plus a file://
git remote for omarchy-apple-dev and fake release binaries.

    up = FakeUpstream(tmp)                  # serves the default "latest" world (see Upstream below)
    with up:
        env = up.env()                      # IOSSTUDIO_GITHUB_API/RAW, IOSSTUDIO_PYPI/AUR, git insteadOf, pip index
        ...
    up.hits["/api/repos/blacktop/ipsw/releases/latest"]  -> [200, 304, ...] (the statuses it answered)

JSON routes send an ETag and answer 304 to a matching If-None-Match. `up.rate_limited = reset_ts` makes the API
answer 403 with X-RateLimit-Remaining 0; `up.retry_after = s` 429 with Retry-After; `up.fail[path] = code` fails one
route; `up.delay[basename] = s` slows one download (cancel tests).

Release downloads: Setup's installers download from IOSSTUDIO_GITHUB_DL (default https://github.com), which env()
points at this server's /gh; it answers any /gh/... path by its file name.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import signal
import socket
import subprocess
import tarfile
import threading
import time
import zipfile
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from support.setup_upgrade import git

ARCHES = {"x86_64": "x86_64", "aarch64": "arm64"}       # uname -m -> the name in ipsw's release files

def tool_script(name: str, version: str) -> str:
    """A fake ipsw / rcodesign binary that reports `version` the way the real one does."""
    line = f"Version: {version}, BuildCommit: test" if name == "ipsw" else f"rcodesign {version}"
    return f"#!/bin/sh\necho '{line}'\n"


def tarball(members: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, text in members.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o755
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def wheel(version: str) -> tuple[str, bytes]:
    """A pure-Python pymobiledevice3 wheel (with a stand-in `cryptography`, which Setup imports to check the venv)."""
    name = f"pymobiledevice3-{version}-py3-none-any.whl"
    di = f"pymobiledevice3-{version}.dist-info"
    files = {"pymobiledevice3/__init__.py": f"__version__ = {version!r}\n", "cryptography/__init__.py": "",
             f"{di}/METADATA": f"Metadata-Version: 2.1\nName: pymobiledevice3\nVersion: {version}\n",
             f"{di}/WHEEL": "Wheel-Version: 1.0\nGenerator: tests\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
             f"{di}/top_level.txt": "pymobiledevice3\ncryptography\n"}
    record = []
    for path, text in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(text.encode()).digest()).rstrip(b"=").decode()
        record.append(f"{path},sha256={digest},{len(text.encode())}")
    record.append(f"{di}/RECORD,,")
    files[f"{di}/RECORD"] = "\n".join(record) + "\n"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for path, text in files.items():
            zf.writestr(path, text)
    return name, buf.getvalue()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass
class Upstream:
    """What the fake upstream has released (change it, then call FakeUpstream.publish())."""
    apple_dev_tag: str = "v0.5.0"
    xtool_sha: str = "c" * 40
    sdk_stamp: str = "epoch=4,darwinTools=1.1.0,oam=1.4.1"
    ipsw: str = "3.1.752"
    ipsw_reports: str = ""             # the version the fake binary prints ("" = its own)
    ipsw_sums: bool = True             # checksums.txt lists this machine's tarball
    rcodesign: str = "0.30.0"
    pymd3: str = "99.1.0"
    pymd3_wheel_sha: bool = True
    pymd3_versions: tuple = ("11.49.0",)  # older versions the simple index also has (Tested)
    swift: str = "6.5.0-1"
    xtool_upstream: str = "1.25.0"


class FakeUpstream:
    def __init__(self, root: Path, upstream: Upstream | None = None):
        self.root = Path(root)
        self.up = upstream or Upstream()
        self.routes: dict[str, Any] = {}
        self.files: dict[str, bytes] = {}
        self.hits: dict[str, list[int]] = {}
        self.fail: dict[str, int] = {}
        self.delay: dict[str, float] = {}
        self.rate_limited: float | None = None
        self.retry_after: int | None = None
        self.server: ThreadingHTTPServer | None = None
        self.remote = self.root / "remotes/omarchy-apple-dev"
        self.commit = ""
        self._lock = threading.Lock()

    # ---- lifecycle
    def __enter__(self) -> FakeUpstream:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                fake._answer(self)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.publish()
        return self

    def __exit__(self, *exc) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()

    @property
    def base(self) -> str:
        assert self.server
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def env(self) -> dict[str, str]:
        """The helper's base-URL overrides (API, raw files, release downloads, PyPI, AUR), git's redirect of the omarchy-apple-dev URL to the local remote, and
        pip's index (the fake one; Setup's pip runs inherit the environment)."""
        return {"IOSSTUDIO_GITHUB_API": f"{self.base}/api", "IOSSTUDIO_GITHUB_RAW": f"{self.base}/raw",
                "IOSSTUDIO_PYPI": f"{self.base}/pypi", "IOSSTUDIO_AUR": f"{self.base}/aur",
                "IOSSTUDIO_GITHUB_DL": f"{self.base}/gh",
                "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": f"url.file://{self.remote}.insteadOf",
                "GIT_CONFIG_VALUE_0": "https://github.com/joshuaswarren/omarchy-apple-dev",
                "PIP_INDEX_URL": f"{self.base}/pypi/simple/", "PIP_TRUSTED_HOST": "127.0.0.1",
                "PIP_NO_CACHE_DIR": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1"}

    # ---- the released world
    def _git_remote(self) -> str:
        up = self.up
        script = (f'XTOOL_REPO=https://github.com/joshuaswarren/xtool\nXTOOL_SHA={up.xtool_sha}\n'
                  f'XTOOL_SDK_VERSION="{up.sdk_stamp}"\n')
        if not (self.remote / ".git").is_dir():
            self.remote.mkdir(parents=True)
            git("init", "-q", cwd=self.remote)
        (self.remote / "install-toolchain.sh").write_text(script)
        git("add", "-A", cwd=self.remote)
        git("commit", "-q", "--allow-empty", "-m", up.apple_dev_tag, cwd=self.remote)
        git("tag", "-f", "-a", "-m", up.apple_dev_tag, up.apple_dev_tag, cwd=self.remote)
        return git("rev-parse", "HEAD", cwd=self.remote)

    def publish(self) -> None:
        """(Re)build every route from self.up."""
        up, b = self.up, self.base
        self.commit = self._git_remote()
        script = (self.remote / "install-toolchain.sh").read_text()
        routes: dict[str, Any] = {}
        files: dict[str, bytes] = {}

        def rel(tag: str, assets: dict[str, bytes], **kw) -> dict:
            files.update(assets)
            return {"tag_name": tag, "prerelease": False, "draft": False, "published_at": "2026-11-25T08:52:44Z",
                    "html_url": f"https://github.com/x/releases/tag/{tag}",
                    "assets": [{"name": n, "browser_download_url": f"{b}/gh/dl/{tag.replace('/', '%2F')}/{n}"}
                               for n in assets], **kw}

        routes["/api/repos/joshuaswarren/omarchy-apple-dev/releases?per_page=10"] = [
            rel("v9.9.9-beta", {}, prerelease=True), rel(up.apple_dev_tag, {})]
        routes[f"/raw/joshuaswarren/omarchy-apple-dev/{self.commit}/install-toolchain.sh"] = script

        tars, lines = {}, []
        for _m, ia in ARCHES.items():
            name = f"ipsw_{up.ipsw}_linux_{ia}.tar.gz"
            tars[name] = tarball({"ipsw": tool_script("ipsw", up.ipsw_reports or up.ipsw)})
            if up.ipsw_sums:
                lines.append(f"{sha(tars[name])}  {name}")
        lines.append(f"{'0' * 64}  ipsw_{up.ipsw}_darwin_all.tar.gz")
        routes["/api/repos/blacktop/ipsw/releases/latest"] = rel(
            f"v{up.ipsw}", {**tars, "checksums.txt": ("\n".join(lines) + "\n").encode()})

        tars, lines = {}, []
        for m in ARCHES:
            name = f"apple-codesign-{up.rcodesign}-{m}-unknown-linux-musl.tar.gz"
            top = name.removesuffix(".tar.gz")
            tars[name] = tarball({f"{top}/rcodesign": tool_script("rcodesign", up.rcodesign)})
            lines.append(f"{sha(tars[name])}  {name}")
        routes["/api/repos/indygreg/apple-platform-rs/releases?per_page=30"] = [
            rel("apple-dmg/0.9.0", {}), rel(f"apple-codesign/{up.rcodesign}", {**tars, "SHA256SUMS": (
                "\n".join(lines) + "\n").encode()}), rel("apple-codesign/0.29.0", {})]
        routes["/api/repos/xtool-org/xtool/releases/latest"] = rel(up.xtool_upstream, {})

        links = []
        for v in (*up.pymd3_versions, up.pymd3):
            wname, wdata = wheel(v)
            files[wname] = wdata
            links.append(f'<a href="{b}/pypi/files/{wname}#sha256={sha(wdata)}">{wname}</a>')
        wname = wheel(up.pymd3)[0]
        routes["/pypi/pypi/pymobiledevice3/json"] = {
            "info": {"version": up.pymd3, "requires_python": ">=3.9", "project_url": "https://pypi.org/project/x"},
            "releases": {v: [] for v in up.pymd3_versions},
            "urls": [{"packagetype": "sdist", "filename": "x.tar.gz", "digests": {"sha256": "1" * 64}},
                     {"packagetype": "bdist_wheel", "filename": wname, "yanked": False,
                      "upload_time_iso_8601": "2026-11-20T10:00:00Z",
                      "digests": {"sha256": sha(files[wname]) if up.pymd3_wheel_sha else ""}}]}
        routes["/pypi/simple/pymobiledevice3/"] = ("<html><body>" + "".join(links) + "</body></html>", "text/html")
        routes["/aur/rpc/v5/info?arg[]=swift-bin"] = {"resultcount": 1, "results": [{"Name": "swift-bin",
                                                                                  "Version": up.swift}]}
        with self._lock:
            self.routes, self.files = routes, files

    # ---- serving
    def _hit(self, path: str, code: int) -> None:
        with self._lock:
            self.hits.setdefault(path, []).append(code)

    def _answer(self, h: BaseHTTPRequestHandler) -> None:
        path = h.path
        if path in self.fail:
            return self._send(h, path, self.fail[path], b"{}")
        if path.startswith("/api/") and self.rate_limited:
            return self._send(h, path, 403, b'{"message": "API rate limit exceeded"}',
                              {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(int(self.rate_limited))})
        if path.startswith("/api/") and self.retry_after:
            return self._send(h, path, 429, b"{}", {"Retry-After": str(self.retry_after)})
        if path.startswith(("/gh/", "/pypi/files/")):
            name = path.rsplit("/", 1)[-1].replace("%2F", "/").rsplit("/", 1)[-1]
            data = self.files.get(name)
            if data is None:
                return self._send(h, path, 404, b"not found")
            if self.delay.get(name):
                time.sleep(self.delay[name])
            return self._send(h, path, 200, data, {"Content-Type": "application/octet-stream"})
        body = self.routes.get(path)
        if body is None:
            return self._send(h, path, 404, b"{}")
        ctype = "application/json"
        if isinstance(body, tuple):
            body, ctype = body
        data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        if isinstance(body, str) and ctype == "application/json":
            ctype = "text/plain"
        etag = f'"{sha(data)[:16]}"'
        if h.headers.get("If-None-Match") == etag:
            return self._send(h, path, 304, b"", {"ETag": etag})
        extra = {"ETag": etag, "Content-Type": ctype}
        if path.startswith("/api/"):
            extra.update({"X-RateLimit-Remaining": "57", "X-RateLimit-Reset": str(int(time.time()) + 3600)})
        self._send(h, path, 200, data, extra)

    def _send(self, h, path: str, code: int, data: bytes, headers: dict | None = None) -> None:
        self._hit(path, code)
        try:
            h.send_response(code)
            for k, v in (headers or {}).items():
                h.send_header(k, v)
            h.send_header("Content-Length", str(len(data)))
            h.end_headers()
            if code != 304:
                h.wfile.write(data)
        except OSError:
            pass


# ------------------------------------------------------------------ the device daemon, for pymobiledevice3 updates

class FakeDaemon:
    """A device daemon on <runtime>/ios-studio/deviced.sock that answers `hello` with the pid of a stand-in
    process, `daemon.features` with `resources`, and on `daemon.shutdown {all}` stops that process and removes its
    socket (as the real one does). `shutdowns` counts them."""

    def __init__(self, runtime_dir: Path, resources: list[dict] | None = None):
        self.path = Path(runtime_dir) / "ios-studio" / "deviced.sock"
        self.resources = resources or []
        self.shutdowns = 0
        self.proc: subprocess.Popen | None = None
        self._sock: socket.socket | None = None
        self._stop = threading.Event()

    def __enter__(self) -> FakeDaemon:
        self._listen()
        threading.Thread(target=self._accept, daemon=True).start()
        return self

    def _listen(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.unlink(missing_ok=True)
        self.proc = subprocess.Popen(["sleep", "60"])
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(str(self.path))
        self._sock.listen(8)
        self._sock.settimeout(0.2)

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self._sock:
            self._sock.close()
        self.path.unlink(missing_ok=True)
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()

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
                cmd = msg.get("cmd")
                data: Any = {}
                if cmd in ("hello", "subscribe"):
                    data = {"pid": self.proc.pid if self.proc else os.getpid(), "version": "1"}
                elif cmd == "daemon.features":
                    data = {"loaded": [], "failed": {}, "commands": {}, "resources": self.resources}
                f.write((json.dumps({"id": msg.get("id"), "ok": True, "data": data}) + "\n").encode())
                f.flush()
                if cmd == "daemon.shutdown":
                    self.shutdowns += 1
                    self.path.unlink(missing_ok=True)
                    if self.proc:
                        self.proc.send_signal(signal.SIGTERM)
                        self.proc.wait()
                    return
        except (OSError, ValueError):
            pass
        finally:
            conn.close()

    def restart(self) -> None:
        """Listen again (as the app's bridge would start a new daemon)."""
        if self._sock:
            self._sock.close()
        self._listen()
