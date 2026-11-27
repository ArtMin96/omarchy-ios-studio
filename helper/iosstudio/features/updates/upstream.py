"""What upstream released: one fetch per source, each version verified before it is offered.

Sources (base URLs overridable for tests: IOSSTUDIO_GITHUB_API, IOSSTUDIO_GITHUB_RAW, IOSSTUDIO_PYPI, IOSSTUDIO_AUR):
  apple-dev        newest non-prerelease GitHub release -> its commit (git ls-remote) -> install-toolchain.sh at that
                   commit (raw), parsed for XTOOL_SHA (= "latest" xtool) and the SDK stamp. Verified by the commit.
  ipsw             releases/latest + checksums.txt (this machine's tarball must be listed).
  rcodesign        the newest apple-codesign/* release of apple-platform-rs + SHA256SUMS.
  pymobiledevice3  PyPI JSON: version + its wheel's sha256 (+ requires_python).
  swift            AUR RPC swift-bin version (a notice only).
  xtool-upstream   newest xtool-org/xtool release (a notice only: AppImages without checksums).

stdlib urllib, 8 s per request, 40 s for the whole run (Fetcher.deadline). Conditional requests (If-None-Match)
reuse the cached body on 304; immutable URLs (a file at a commit, a release's checksum file) are read once.
403/429 with X-RateLimit-Remaining 0 or Retry-After raise RateLimited(resetAt).
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from iosstudio.core import subproc

from ..setup.facts import arch, parse_apple_dev_pins
from ..setup.pins import APPLE_DEV_REPO, IPSW_SHA

REQUEST_TIMEOUT = 8.0
RUN_CAP = 40.0
READ_MAX = 8_000_000        # bytes read from one response (PyPI's JSON for a busy package is a few MB)
BODY_MAX = 200_000          # bytes of one trimmed body kept in the cache
UA = "iOS-Studio-toolchain-updates"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_COMMIT = re.compile(r"[0-9a-f]{40}")


def github_api() -> str:
    return (os.environ.get("IOSSTUDIO_GITHUB_API") or "https://api.github.com").rstrip("/")


def github_raw() -> str:
    return (os.environ.get("IOSSTUDIO_GITHUB_RAW") or "https://raw.githubusercontent.com").rstrip("/")


def pypi() -> str:
    return (os.environ.get("IOSSTUDIO_PYPI") or "https://pypi.org").rstrip("/")


def aur() -> str:
    return (os.environ.get("IOSSTUDIO_AUR") or "https://aur.archlinux.org").rstrip("/")


class FetchError(Exception):
    """A source couldn't be read (offline, a 5xx, a malformed answer). The message is for people."""


class RateLimited(FetchError):
    def __init__(self, reset_at: float):
        super().__init__("GitHub asked iOS Studio to wait before checking again.")
        self.reset_at = reset_at


class Unverifiable(Exception):
    """The newest version exists but can't be verified (no checksum for this computer): never offered."""

    def __init__(self, version: str, reason: str):
        super().__init__(reason)
        self.version, self.reason = version, reason


@dataclass
class Fetcher:
    """HTTP GETs sharing the ETag cache ({url: {etag, body}}) and one deadline. Thread-safe."""
    etags: dict = field(default_factory=dict)
    deadline: float = 0.0
    rate: dict = field(default_factory=dict)          # {remaining, resetAt} from GitHub's API headers
    used: set = field(default_factory=set)            # the URLs this run read (the rest is dropped from the cache)
    requests: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        if not self.deadline:
            self.deadline = time.monotonic() + RUN_CAP

    def remaining(self) -> float:
        return self.deadline - time.monotonic()

    def get(self, url: str, *, as_json: bool = True, immutable: bool = False,
            trim: Callable[[Any], Any] | None = None) -> Any:
        import urllib.error
        import urllib.request
        with self._lock:
            self.used.add(url)
            cached = self.etags.get(url)
        if immutable and isinstance(cached, dict) and "body" in cached:
            return cached["body"]
        left = self.remaining()
        if left < 0.5:
            raise FetchError("The check took too long and was stopped.")
        api = url.startswith(github_api())
        headers = {"User-Agent": UA, "Accept": "application/vnd.github+json" if api else "*/*"}
        if isinstance(cached, dict) and cached.get("etag"):
            headers["If-None-Match"] = cached["etag"]
        req = urllib.request.Request(url, headers=headers)
        with self._lock:
            self.requests += 1
        try:
            with urllib.request.urlopen(req, timeout=min(REQUEST_TIMEOUT, left)) as resp:
                self._note_rate(resp.headers, api)
                raw = resp.read(READ_MAX + 1)
                etag = resp.headers.get("ETag")
        except urllib.error.HTTPError as exc:
            self._note_rate(exc.headers, api)
            if exc.code == 304 and isinstance(cached, dict) and "body" in cached:
                return cached["body"]
            if exc.code in (403, 429):
                reset = self._reset_at(exc.headers)
                if reset is not None:
                    raise RateLimited(reset) from None
            raise FetchError(f"{_host(url)} answered HTTP {exc.code}.") from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise FetchError(f"Couldn't reach {_host(url)}. Check your internet connection.") from exc
        if len(raw) > READ_MAX:
            raise FetchError(f"{_host(url)} sent more than expected.")
        try:
            body: Any = json.loads(raw) if as_json else raw.decode("utf-8", "replace")
            if trim:
                body = trim(body)
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise FetchError(f"{_host(url)} sent an answer iOS Studio doesn't understand.") from exc
        if (etag or immutable) and len(json.dumps(body, default=str)) <= BODY_MAX:
            with self._lock:
                self.etags[url] = {"etag": etag, "body": body}
        return body

    def _note_rate(self, headers, api: bool) -> None:
        if not (api and headers):
            return
        rem, reset = headers.get("X-RateLimit-Remaining"), headers.get("X-RateLimit-Reset")
        with self._lock:
            if rem is not None and rem.isdigit():
                self.rate["remaining"] = int(rem)
            if reset is not None and reset.isdigit():
                self.rate["resetAt"] = float(reset)

    @staticmethod
    def _reset_at(headers) -> float | None:
        if not headers:
            return None
        ra = headers.get("Retry-After")
        if ra and ra.strip().isdigit():
            return time.time() + int(ra.strip())
        if headers.get("X-RateLimit-Remaining") == "0":
            reset = headers.get("X-RateLimit-Reset")
            return float(reset) if reset and reset.isdigit() else time.time() + 3600
        return None


def _host(url: str) -> str:
    m = re.match(r"^\w+://([^/]+)", url)
    return m.group(1) if m else url


# ------------------------------------------------------------------ trimming what's cached

def _trim_release(r: dict) -> dict:
    return {"tag_name": str(r["tag_name"]), "prerelease": bool(r.get("prerelease")), "draft": bool(r.get("draft")),
            "html_url": r.get("html_url"), "published_at": r.get("published_at"),
            "assets": [{"name": a.get("name"), "url": a.get("browser_download_url")} for a in r.get("assets") or []
                       if re.search(r"(checksums\.txt|SHA256SUMS)$", str(a.get("name")))]}


def _trim_releases(rs: list) -> list:
    return [_trim_release(r) for r in rs if isinstance(r, dict)]


def _trim_pypi(d: dict) -> dict:
    info = d["info"]
    return {"version": str(info["version"]), "requires_python": info.get("requires_python"),
            "project_url": info.get("project_url") or info.get("package_url"),
            "wheels": [{"filename": u.get("filename"), "sha256": (u.get("digests") or {}).get("sha256"),
                        "yanked": bool(u.get("yanked")), "upload_time": u.get("upload_time_iso_8601")}
                       for u in d.get("urls") or [] if u.get("packagetype") == "bdist_wheel"]}


def _stable(releases: list, prefix: str = "") -> dict | None:
    return next((r for r in releases if not r["draft"] and not r["prerelease"] and r["tag_name"].startswith(prefix)),
                None)


def _sum_for(text: str, name: str) -> str | None:
    """The sha256 for `name` in a checksums file ("<sha>  <name>" or "<sha> *<name>" lines)."""
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == name and _SHA256.fullmatch(parts[0].lower()):
            return parts[0].lower()
    return None


def _asset(rel: dict, pattern: str) -> str | None:
    return next((a["url"] for a in rel["assets"] if a.get("url") and re.search(pattern, str(a.get("name")))), None)


# ------------------------------------------------------------------ the sources

def ls_remote(repo: str, tag: str, fetcher: Fetcher) -> str:
    """The commit a tag points to (the peeled ^{} line for an annotated tag)."""
    left = fetcher.remaining()
    if left < 0.5:
        raise FetchError("The check took too long and was stopped.")
    r = subproc.run_cmd(["git", "ls-remote", repo, f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}"],
                        timeout=min(REQUEST_TIMEOUT, left), check=False,
                        env={"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/bin/true"})
    if r.returncode != 0:
        raise FetchError(f"Couldn't read {_host(repo)}'s tags. Check your internet connection.")
    refs = dict(reversed(ln.split("\t", 1)) for ln in r.stdout.splitlines() if "\t" in ln)
    commit = refs.get(f"refs/tags/{tag}^{{}}") or refs.get(f"refs/tags/{tag}") or ""
    if not _COMMIT.fullmatch(commit):
        raise FetchError(f"The release {tag} has no commit iOS Studio can verify.")
    return commit


def _repo_path(url: str) -> str:
    """owner/name of a GitHub repository URL."""
    return re.sub(r"^https://github\.com/|\.git$", "", url).strip("/")


def apple_dev(f: Fetcher) -> list[dict]:
    """The newest omarchy-apple-dev release and the xtool commit it pins."""
    repo = _repo_path(APPLE_DEV_REPO)
    rel = _stable(f.get(f"{github_api()}/repos/{repo}/releases?per_page=10", trim=_trim_releases))
    if not rel:
        raise FetchError("omarchy-apple-dev has no release yet.")
    tag = rel["tag_name"]
    commit = ls_remote(APPLE_DEV_REPO, tag, f)
    text = f.get(f"{github_raw()}/{repo}/{commit}/install-toolchain.sh", as_json=False, immutable=True,
                 trim=lambda t: "\n".join(ln for ln in t.splitlines() if ln.startswith("XTOOL_")))
    pins = parse_apple_dev_pins(text, defaults=False)
    xsha = pins.get("XTOOL_SHA", "")
    if not _COMMIT.fullmatch(xsha):
        raise Unverifiable(tag, f"omarchy-apple-dev {tag} doesn't pin an xtool commit iOS Studio can verify.")
    xrepo = pins.get("XTOOL_REPO", "")
    at = rel.get("published_at")
    return [{"id": "apple-dev", "latest": commit, "tag": tag, "commit": commit, "latestAt": at,
             "notesUrl": rel.get("html_url"), "sdkStamp": pins.get("XTOOL_SDK_VERSION"), "xtoolSha": xsha},
            {"id": "xtool", "latest": xsha, "commit": xsha, "latestAt": at, "repo": xrepo,
             "notesUrl": f"{xrepo.removesuffix('.git')}/commit/{xsha}" if xrepo.startswith("https://github.com/") else None}]


def ipsw(f: Fetcher) -> list[dict]:
    rel = f.get(f"{github_api()}/repos/blacktop/ipsw/releases/latest", trim=_trim_release)
    ver = rel["tag_name"].removeprefix("v")
    if arch() not in IPSW_SHA:
        raise Unverifiable(ver, f"ipsw has no build for {arch()}.")
    name = f"ipsw_{ver}_linux_{IPSW_SHA[arch()][0]}.tar.gz"
    url = _asset(rel, r"checksums\.txt$")
    sha = _sum_for(f.get(url, as_json=False, immutable=True), name) if url else None
    if not sha:
        raise Unverifiable(ver, f"ipsw {ver} lists no checksum for this computer's download.")
    return [{"id": "ipsw", "latest": ver, "sha256": sha, "latestAt": rel.get("published_at"),
             "notesUrl": rel.get("html_url")}]


def rcodesign(f: Fetcher) -> list[dict]:
    rels = f.get(f"{github_api()}/repos/indygreg/apple-platform-rs/releases?per_page=30", trim=_trim_releases)
    rel = _stable(rels, "apple-codesign/")
    if not rel:
        raise FetchError("No rcodesign release found.")
    ver = rel["tag_name"].split("/", 1)[1]
    name = f"apple-codesign-{ver}-{arch()}-unknown-linux-musl.tar.gz"
    url = _asset(rel, r"SHA256SUMS$")
    sha = _sum_for(f.get(url, as_json=False, immutable=True), name) if url else None
    if not sha:
        raise Unverifiable(ver, f"rcodesign {ver} lists no checksum for this computer's download.")
    return [{"id": "rcodesign", "latest": ver, "sha256": sha, "latestAt": rel.get("published_at"),
             "notesUrl": rel.get("html_url")}]


def python_ok(spec: str | None, version: tuple[int, ...]) -> bool:
    """Does `version` satisfy a requires_python spec (">=3.9", ">=3.9,<4")? Unknown clauses pass."""
    import operator
    ops = {">=": operator.ge, ">": operator.gt, "<=": operator.le, "<": operator.lt, "==": operator.eq,
           "!=": operator.ne}
    for clause in (spec or "").split(","):
        m = re.fullmatch(r"\s*(>=|<=|==|!=|>|<)\s*(\d+(?:\.\d+)*)(?:\.\*)?\s*", clause)
        if m:
            want = tuple(int(x) for x in m.group(2).split("."))
            if not ops[m.group(1)](version[:len(want)], want):
                return False
    return True


def system_python() -> tuple[int, ...]:
    """The version of /usr/bin/python3 the venv is built with (this process runs under it)."""
    import sys
    return tuple(sys.version_info[:3])


def pymobiledevice3(f: Fetcher) -> list[dict]:
    d = f.get(f"{pypi()}/pypi/pymobiledevice3/json", trim=_trim_pypi)
    ver = d["version"]
    wheel = next((w for w in d["wheels"] if not w["yanked"] and _SHA256.fullmatch(str(w.get("sha256") or ""))), None)
    if not wheel:
        raise Unverifiable(ver, f"pymobiledevice3 {ver} has no wheel with a checksum on PyPI.")
    if not python_ok(d.get("requires_python"), system_python()):
        raise Unverifiable(ver, f"pymobiledevice3 {ver} needs Python {d['requires_python']}.")
    return [{"id": "pymobiledevice3", "latest": ver, "sha256": wheel["sha256"], "latestAt": wheel.get("upload_time"),
             "notesUrl": f"https://pypi.org/project/pymobiledevice3/{ver}/"}]


def swift(f: Fetcher) -> list[dict]:
    d = f.get(f"{aur()}/rpc/v5/info?arg[]=swift-bin")
    res = (d.get("results") or [{}])[0] if isinstance(d, dict) else {}
    ver = str(res.get("Version") or "")
    if not ver:
        raise FetchError("The AUR didn't list swift-bin.")
    return [{"id": "swift", "latest": ver, "notesUrl": "https://aur.archlinux.org/packages/swift-bin"}]


def xtool_upstream(f: Fetcher) -> list[dict]:
    rel = f.get(f"{github_api()}/repos/xtool-org/xtool/releases/latest", trim=_trim_release)
    return [{"id": "xtool-upstream", "latest": rel["tag_name"], "latestAt": rel.get("published_at"),
             "notesUrl": rel.get("html_url")}]


SOURCES: dict[str, Callable[[Fetcher], list[dict]]] = {
    "apple-dev": apple_dev, "ipsw": ipsw, "rcodesign": rcodesign, "pymobiledevice3": pymobiledevice3,
    "swift": swift, "xtool-upstream": xtool_upstream,
}


def check_all(fetcher: Fetcher, on_result: Callable[[str, list[dict] | None, Exception | None], None]) -> None:
    """Run every source on its own thread; on_result(source, facts, error) as each one finishes (from that
    thread). Returns when all are done or the run's deadline passed (a straggler's result is then dropped)."""
    done: set[str] = set()
    closed = False
    lock = threading.Lock()

    def run(name: str, fn: Callable[[Fetcher], list[dict]]) -> None:
        try:
            facts, err = fn(fetcher), None
        except Exception as exc:  # noqa: BLE001 - reported per source
            facts, err = None, exc
        with lock:
            if not closed:
                done.add(name)
                on_result(name, facts, err)

    threads = [threading.Thread(target=run, args=item, daemon=True) for item in SOURCES.items()]
    for t in threads:
        t.start()
    for t in threads:
        t.join(max(0.0, fetcher.remaining() + 0.5))
    with lock:
        for name in SOURCES:
            if name not in done:
                on_result(name, None, FetchError("The check took too long and was stopped."))
        closed = True        # a late thread's result is ignored from here on
        fetcher.deadline = time.monotonic()
