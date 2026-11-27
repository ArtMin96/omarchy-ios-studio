"""`updates status`: the cached upstream facts merged with what's installed, read from files only.

No subprocess and no network: versions come from files (the omarchy-apple-dev checkout's .git/HEAD, the xtool
build's folder name, pymobiledevice3's dist-info, pacman's local database for swift-bin) and, for the ipsw and
rcodesign binaries, from the version `updates check` probed (kept with the binary's mtime and size, so a binary
Setup replaced since reads as unknown until the next check).

The cache is appdirs.cache_put("toolchain-updates", ...):
  {schema: 1, checkedAt, nextCheckAt, error: {code, message} | null, rate: {remaining, resetAt},
   etags: {<url>: {etag, body}}, components: [<upstream fact>], notices: [<unverifiable version>], probes: {}}
An apply in progress leaves a marker (state dir, updates-inprogress.json) naming the component and its backups;
one whose process is gone is reported as "interrupted" (the next apply puts the backups back first).
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from iosstudio.core import appdirs, fileio
from iosstudio.core.output import fix

from ..setup import channel as ch
from ..setup import facts
from ..setup.pins import (
    APPLE_DEV_REPO,
    DEFAULT_XTOOL_REPO,
    DEFAULT_XTOOL_SHA,
    XCODE_FOR_SWIFT,
)

SCHEMA = 1
CACHE_NAME = "toolchain-updates"
DAY = 24 * 3600
RETRY_AFTER_ERROR = 3 * 3600          # a failed or partial check tries again after this
PACMAN_LOCAL = Path("/var/lib/pacman/local")
NAMES = {"apple-dev": "iOS build scripts", "xtool": "xtool", "pymobiledevice3": "pymobiledevice3",
         "ipsw": "ipsw", "rcodesign": "rcodesign"}
CHECKSUM = {"apple-dev": "git-commit", "xtool": "git-commit", "pymobiledevice3": "pypi", "ipsw": "checksums.txt",
            "rcodesign": "SHA256SUMS"}
GIT_IDS = ("apple-dev", "xtool")
_COMMIT = re.compile(r"[0-9a-f]{40}")
NOT_INSTALLED = "Not installed yet: Setup installs it."
SDK_BLOCK = "This version needs the iOS SDK built again from Xcode. Rebuild the SDK in Setup first."


# ------------------------------------------------------------------ cache

def empty_cache() -> dict:
    return {"schema": SCHEMA, "checkedAt": None, "nextCheckAt": None, "error": None,
            "rate": {"remaining": None, "resetAt": None}, "etags": {}, "components": [], "notices": [], "probes": {}}


def load_cache() -> dict:
    entry = appdirs.cache_get(CACHE_NAME)
    data = entry.get("data") if isinstance(entry, dict) else None
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        return empty_cache()
    return {**empty_cache(), **data}


def save_cache(data: dict) -> None:
    appdirs.cache_put(CACHE_NAME, data)


def cache_lock():
    """Held while a check (or an apply's probe refresh) rewrites the cache."""
    return fileio.lock(appdirs.CACHE_DIR / ".toolchain-updates.lock")


def is_due(cache: dict, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    reset = (cache.get("rate") or {}).get("resetAt")
    if reset and now < float(reset):
        return False
    nxt = cache.get("nextCheckAt")
    return not nxt or now >= float(nxt)


# ------------------------------------------------------------------ the in-progress marker

def marker_path() -> Path:
    return appdirs.state_dir() / "updates-inprogress.json"


def proc_start(pid: int) -> str | None:
    """A process's start time (/proc/<pid>/stat field 22): tells a live pid from a reused one."""
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def read_marker() -> dict | None:
    m = fileio.load_json(marker_path())
    return m if isinstance(m, dict) else None


def marker_alive(m: dict) -> bool:
    try:
        pid = int(m.get("pid") or 0)
    except (TypeError, ValueError):
        return False
    return pid > 0 and proc_start(pid) is not None and proc_start(pid) == m.get("pidStart")


def interrupted() -> dict | None:
    """{component, since} of an apply whose process is gone without putting its backups back or away."""
    m = read_marker()
    if not m or marker_alive(m):
        return None
    return {"component": m.get("component"), "since": m.get("since")}


# ------------------------------------------------------------------ installed versions (files only)

def git_head(repo: Path) -> str | None:
    """HEAD's commit from the files in .git (a detached HEAD, a branch ref, or packed-refs)."""
    g = repo / ".git"
    try:
        if g.is_file():                            # a worktree: "gitdir: <path>"
            g = Path(g.read_text().split(":", 1)[1].strip())
        head = (g / "HEAD").read_text().strip()
        if head.startswith("ref: "):
            ref = head[5:]
            p = g / ref
            if p.is_file():
                head = p.read_text().strip()
            else:
                packed = (g / "packed-refs").read_text() if (g / "packed-refs").is_file() else ""
                head = next((ln.split()[0] for ln in packed.splitlines() if ln.endswith(" " + ref)), "")
    except (OSError, IndexError):
        return None
    return head if _COMMIT.fullmatch(head) else None


def pymd3_installed() -> str | None:
    """The version in the venv in use: the first of Setup's venvs (ours, then ~/pymobile3-venv) that has one."""
    for v in facts.pymd3_venvs():
        for d in v.glob("lib/python3*/site-packages/pymobiledevice3-*.dist-info"):
            return d.name[len("pymobiledevice3-"):-len(".dist-info")]
    return None


def swift_installed() -> str | None:
    """swift-bin's package version (e.g. 6.4.2-2) from pacman's local database."""
    try:
        for d in PACMAN_LOCAL.glob("swift-bin-*"):
            m = re.fullmatch(r"swift-bin-(\d[^-]*-\d+(?:\.\d+)?)", d.name)
            if m:
                return m.group(1)
    except OSError:
        pass
    return None


def stat_key(p: Path) -> list | None:
    try:
        st = p.stat()
    except OSError:
        return None
    return [st.st_mtime, st.st_size]


PROBED = {"ipsw": ["version"], "rcodesign": ["--version"], "xtool": ["--version"]}


def probed(probes: dict, name: str) -> str | None:
    """The version `updates check` read from ~/.local/bin/<name>, if that binary hasn't changed since."""
    e = probes.get(name)
    key = stat_key(ch.LOCAL_BIN / name)
    return e.get("version") if isinstance(e, dict) and key and e.get("key") == key else None


def refresh_probes(probes: dict) -> dict:
    """Run `<tool> --version` for each binary that changed since its last probe (check and apply only)."""
    from iosstudio.core import subproc
    for name, args in PROBED.items():
        p = ch.LOCAL_BIN / name
        key = stat_key(p)
        if key is None:
            probes.pop(name, None)
            continue
        if isinstance(probes.get(name), dict) and probes[name].get("key") == key:
            continue
        r = subproc.run_cmd([p, *args], timeout=5, check=False)
        probes[name] = {"key": key, "version": facts.first_version(r.stdout) if r.returncode == 0 else None}
    return probes


# ------------------------------------------------------------------ rows

def vtuple(v: str | None) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v or ""))


def short(cid: str, v: str | None) -> str | None:
    return v[:7] if v and cid in GIT_IDS else v


def same(cid: str, a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    if cid in GIT_IDS and min(len(a), len(b)) >= 7:   # an older -gsa406 xtool build names a short commit
        return a.startswith(b) or b.startswith(a)
    return a == b


@dataclass
class Row:
    """One component as status reports it, plus the pin an apply would install (pin: the override entry)."""
    id: str
    installed: str | None
    tested: str
    latest: str | None
    target: str | None
    pin: dict = field(default_factory=dict)
    notes_url: str | None = None
    latest_at: str | None = None
    blocked: str | None = None
    extra: dict = field(default_factory=dict)

    @property
    def state(self) -> str:
        if self.installed is None:
            return "unknown"
        if same(self.id, self.installed, self.target):
            return "current" if same(self.id, self.target, self.tested) else "newer-than-tested"
        return "blocked" if self.blocked else "update"

    def public(self) -> dict:
        return {"id": self.id, "name": NAMES[self.id], "installed": short(self.id, self.installed),
                "tested": short(self.id, self.tested), "latest": short(self.id, self.latest),
                "target": short(self.id, self.target), "state": self.state,
                "untested": bool(self.target) and not same(self.id, self.target, self.tested),
                "notesUrl": self.notes_url, "latestAt": self.latest_at, "applicable": self.blocked is None,
                "blockedReason": self.blocked, "checksum": {"source": CHECKSUM[self.id]},
                "restarts": ["iphone-connection"] if self.id == "pymobiledevice3" else [], **self.extra}


def _pick(channel: str, tested, latest, applied):
    """The target: the tested pin, or on latest the newest verified version (else the one an update applied)."""
    return tested if channel == "tested" else (latest or applied)


def _sdk_blocks(fact: dict | None) -> bool:
    """Would the latest apple-dev need an SDK rebuild (its stamp differs and no spare copy has the new one)?"""
    want = (fact or {}).get("sdkStamp")
    bundle = appdirs.sdk_bundle()
    have = facts.sdk_stamp(bundle) if bundle else ""
    if not want or not have or want == have:
        return False
    return not any(c["current"] and c["complete"] for c in facts.sdk_caches(want))


def rows(channel: str, cache: dict) -> list[Row]:
    fx = {c.get("id"): c for c in cache.get("components") or [] if isinstance(c, dict)}
    tested, applied, cp = ch.tested_pins(), ch.effective_pins("latest"), ch.channel_pins()
    probes = cache.get("probes") or {}
    out: list[Row] = []

    # omarchy-apple-dev: our own checkout follows the pin; a copy the user keeps elsewhere is theirs.
    ad = facts.find_apple_dev()
    f = fx.get("apple-dev") or {}
    latest = f.get("commit")
    target = _pick(channel, tested.apple_dev_commit, latest, applied.apple_dev_commit)
    blocked = None
    if ad and os.path.realpath(ad) != os.path.realpath(facts.APPLE_DEV_HOME):
        blocked = f"Your own copy at {appdirs.tilde(ad)}: iOS Studio doesn't move it."
    elif not ad:
        blocked = NOT_INSTALLED
    elif target == latest and target != tested.apple_dev_commit and _sdk_blocks(f):
        blocked = SDK_BLOCK
    notes = f.get("notesUrl") if target == latest else f"{APPLE_DEV_REPO}/commit/{target}"
    ad_row = Row("apple-dev", git_head(ad) if ad else None, tested.apple_dev_commit, latest, target,
                 {"commit": target}, notes, f.get("latestAt"), blocked, {"latestTag": f.get("tag")})
    out.append(ad_row)

    # xtool: no pin of its own; it follows the apple-dev commit's XTOOL_SHA.
    xf = fx.get("xtool") or {}
    link = ch.LOCAL_BIN / "xtool"
    built = facts.xtool_built_from(Path(os.path.realpath(link))) if link.exists() else None
    head = ad_row.installed

    def xtool_for(commit: str | None) -> str | None:
        """The xtool commit an apple-dev commit pins: the checkout's own when it's there, else what we know."""
        if commit and commit == head:
            return facts.apple_dev_pins(ad)["XTOOL_SHA"]
        if commit == tested.apple_dev_commit:
            return DEFAULT_XTOOL_SHA
        if commit == (fx.get("apple-dev") or {}).get("commit"):
            return xf.get("commit")
        return (cp.get("xtool") or {}).get("commit")
    x_latest = xf.get("commit")
    x_tested = xtool_for(tested.apple_dev_commit) or DEFAULT_XTOOL_SHA
    x_target = xtool_for(target)
    x_blocked = None
    if ad_row.state == "unknown":
        x_blocked = "Needs the iOS build scripts first: Setup installs them."
    elif ad_row.state in ("update", "blocked"):
        x_blocked = ad_row.blocked          # it comes with the scripts' update (or waits with it)
    if link.exists() and not built:
        x_blocked = "This xtool wasn't built by iOS Studio, so it isn't updated here."
    elif not link.exists():
        x_blocked = NOT_INSTALLED
    x_notes = xf.get("notesUrl") if x_target == x_latest else \
        f"{DEFAULT_XTOOL_REPO}/commit/{x_target}" if x_target else None
    out.append(Row("xtool", built, x_tested, x_latest, x_target, {"commit": x_target}, x_notes,
                   xf.get("latestAt"), x_blocked))

    # pymobiledevice3: the venv in use, ours or ~/pymobile3-venv. An update always installs ours (it comes first);
    # a ~/pymobile3-venv of omarchy-apple-dev's own stays as it is, so a rollback that removes ours goes back to it.
    f = fx.get("pymobiledevice3") or {}
    latest = f.get("latest")
    target = _pick(channel, tested.pymd3_version, latest, applied.pymd3_version)
    inst = pymd3_installed()
    out.append(Row("pymobiledevice3", inst, tested.pymd3_version, latest, target,
                   {"version": target, **({"sha256": f["sha256"]} if target == latest and f.get("sha256") else {})},
                   f.get("notesUrl") if target == latest else f"https://pypi.org/project/pymobiledevice3/{target}/",
                   f.get("latestAt"), None if inst else NOT_INSTALLED))

    # ipsw and rcodesign: single binaries in ~/.local/bin, verified by the release's checksum file.
    for cid, tv, tsha, av, asha, tag in (
            ("ipsw", tested.ipsw_version, tested.ipsw_sha, applied.ipsw_version, applied.ipsw_sha,
             "https://github.com/blacktop/ipsw/releases/tag/v{v}"),
            ("rcodesign", tested.rcodesign_version, tested.rcodesign_sha, applied.rcodesign_version,
             applied.rcodesign_sha, "https://github.com/indygreg/apple-platform-rs/releases/tag/apple-codesign%2F{v}")):
        f = fx.get(cid) or {}
        latest = f.get("latest")
        target = _pick(channel, tv, latest, av)
        sha = f.get("sha256") if target == latest else (tsha if target == tv else asha)
        present = (ch.LOCAL_BIN / cid).is_file()
        blocked = None if sha else f"{cid} has no build for this computer."
        if not present:
            blocked = NOT_INSTALLED
        out.append(Row(cid, probed(probes, cid) if present else None, tv, latest, target,
                       {"version": target, "sha256": sha}, f.get("notesUrl") if target == latest else tag.format(v=target),
                       f.get("latestAt"), blocked))
    return out


# ------------------------------------------------------------------ notices

def notices(channel: str, cache: dict, rws: list[Row]) -> list[dict]:
    fx = {c.get("id"): c for c in cache.get("components") or [] if isinstance(c, dict)}
    out: list[dict] = []
    xcode_fix = fix("Get Xcode", "setup", step="xcode")
    ad = next(r for r in rws if r.id == "apple-dev")
    if channel == "latest" and ad.blocked == SDK_BLOCK:
        tag = (fx.get("apple-dev") or {}).get("tag") or short("apple-dev", ad.latest)
        out.append({"id": "sdk", "level": "warn", "fix": xcode_fix,
                    "message": f"The newest iOS build scripts ({tag}) need the iOS SDK built again from Xcode, so "
                               "that update waits until the SDK is rebuilt."})
    swift = swift_installed()
    rec = XCODE_FOR_SWIFT.get(".".join((swift or "").split(".")[:2]))
    bundle = appdirs.sdk_bundle()
    have = facts.sdk_ios_versions(bundle) if bundle else []
    if rec and have and vtuple(have[-1]) < vtuple(rec[0]):
        out.append({"id": "xcode", "level": "info", "fix": xcode_fix,
                    "message": f"Xcode {rec[0]} is the one for your Swift. Your iOS SDK comes from Xcode {have[-1]}; "
                               f"building it from Xcode {rec[0]} adds the newer iOS APIs."})
    sv = (fx.get("swift") or {}).get("latest")
    if sv and swift and vtuple(sv) > vtuple(swift):
        out.append({"id": "swift", "level": "info", "fix": None,
                    "message": f"Swift {sv.split('-')[0]} is out. iOS Studio stays on Swift {swift.split('-')[0]}: a "
                               "new Swift needs a matching Xcode and a rebuilt iOS SDK, so it comes with an iOS "
                               "Studio release."})
    xu = (fx.get("xtool-upstream") or {}).get("latest")
    xi = probed(cache.get("probes") or {}, "xtool")
    if xu and xi and vtuple(xu) > vtuple(xi):
        out.append({"id": "xtool-upstream", "level": "info", "fix": None,
                    "message": f"xtool {xu} is out upstream. iOS Studio uses the xtool the iOS build scripts pin, "
                               "so it arrives with an update of the scripts."})
    for n in cache.get("notices") or []:          # newer versions that couldn't be verified
        if isinstance(n, dict) and n.get("id"):
            out.append({"id": f"{n['id']}-unverified", "level": "info", "fix": None,
                        "message": f"{n.get('reason', '')} iOS Studio doesn't offer it.".strip()})
    return out


# ------------------------------------------------------------------ status

def status(channel: str | None = None, cache: dict | None = None) -> dict:
    channel = channel or ch.current_channel()
    cache = cache if cache is not None else load_cache()
    rws = rows(channel, cache)
    comps = [r.public() for r in rws]
    return {"channel": channel, "checkedAt": cache.get("checkedAt"), "nextCheckAt": cache.get("nextCheckAt"),
            "due": is_due(cache), "checking": False, "error": cache.get("error"),
            "count": sum(1 for c in comps if c["state"] == "update"), "components": comps,
            "notices": notices(channel, cache, rws), "interrupted": interrupted()}
