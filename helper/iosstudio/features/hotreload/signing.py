"""Sign a hot-reload patch with xtool's development identity (stdlib only).

xtool keeps the identity of each team in `<xtool data>/<TEAMID>`: a binary plist
`{privateKey: {data: <key bytes>}, certificate: <DER bytes>}`. The key bytes may be PEM or DER, PKCS#1 or PKCS#8;
rcodesign wants a PEM `PRIVATE KEY` (PKCS#8), so other forms are converted in memory.

    sign_dylib(path, team=None, data_dir=None) -> float      seconds it took

runs `rcodesign sign --pem-file /dev/stdin --team-name TEAM --timestamp-url none <dylib>` with the certificate and
the key as one PEM stream on rcodesign's stdin (a pipe). THE PRIVATE KEY NEVER TOUCHES THE DISK: no file, no temp
file, no log line, no error detail; `Identity` doesn't show it in its repr.

The xtool data folder is $IOSSTUDIO_XTOOL_DATA when set (tests point it at a throwaway identity), else
$XDG_CONFIG_HOME/xtool/data, else ~/.config/xtool/data.
Errors: not-signed-in (no identity for the team: fix → Account page), missing-tool (no rcodesign), sign-failed.
"""
from __future__ import annotations

import base64
import os
import plistlib
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from iosstudio.core import appdirs, subproc
from iosstudio.core.output import HelperError, fix

SIGN_TIMEOUT = 30
TEAM_RE = re.compile(r"^[A-Z0-9]{10}$")
MAX_IDENTITY = 256 * 1024
RSA_OID = bytes.fromhex("06092a864886f70d0101010500")   # rsaEncryption OID + NULL
EC_OID = bytes.fromhex("06072a8648ce3d0201")             # id-ecPublicKey
PEM_BLOCK = re.compile(rb"-----BEGIN ([A-Z0-9 ]+)-----(.*?)-----END \1-----", re.S)


def not_signed_in(message: str = "Sign in with your Apple ID so iOS Studio can sign the reload.") -> HelperError:
    return HelperError("not-signed-in", message, fix("Open Account", "account"))


def sign_failed(message: str, detail: str | None = None) -> HelperError:
    return HelperError("sign-failed", message, fix("Open Account", "account"), _scrub(detail) if detail else None)


@dataclass(frozen=True)
class Identity:
    team: str
    certificate: bytes = field(repr=False)          # DER
    key_pem: bytes = field(repr=False)              # PKCS#8 PEM, in memory only

    def pem(self) -> bytes:
        """The certificate and the key as one PEM stream (certificate first, so rcodesign pairs them)."""
        return pem_block("CERTIFICATE", self.certificate) + self.key_pem


def xtool_data_dir() -> Path:
    env = os.environ.get("IOSSTUDIO_XTOOL_DATA")
    if env:
        return Path(env)
    cand = appdirs.config_home() / "xtool/data"
    if cand.is_dir():
        return cand
    return Path.home() / ".config/xtool/data"


def teams(data_dir: Path | None = None) -> list[str]:
    """The team ids that have an identity in the xtool data folder, newest first."""
    base = data_dir or xtool_data_dir()
    try:
        found = [p for p in base.iterdir() if TEAM_RE.match(p.name) and p.is_file()]
    except OSError:
        return []
    return [p.name for p in sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)]


def load_identity(team: str | None = None, data_dir: Path | None = None) -> Identity:
    """The identity of `team` (None: the only/newest one xtool has)."""
    base = data_dir or xtool_data_dir()
    if team is None:
        known = teams(base)
        if not known:
            raise not_signed_in()
        team = known[0]
    if not TEAM_RE.match(team):
        raise not_signed_in()
    path = base / team
    try:
        if path.stat().st_size > MAX_IDENTITY:
            raise not_signed_in("The saved signing identity can't be read: sign in again.")
        raw = path.read_bytes()
    except OSError:
        raise not_signed_in() from None
    try:
        pl = plistlib.loads(raw)
    except Exception:
        raise not_signed_in("The saved signing identity can't be read: sign in again.") from None
    del raw
    if not isinstance(pl, dict):
        raise not_signed_in("The saved signing identity can't be read: sign in again.")
    key = pl.get("privateKey")
    if isinstance(key, dict):
        key = key.get("data")
    cert = pl.get("certificate")
    if isinstance(cert, dict):
        cert = cert.get("data")
    if not isinstance(key, bytes) or not isinstance(cert, bytes):
        raise not_signed_in("The saved signing identity is incomplete: sign in again.")
    try:
        return Identity(team, certificate_der(cert), private_key_pem(key))
    except ValueError:
        raise not_signed_in("The saved signing identity has a key or certificate iOS Studio can't use: "
                            "sign in again.") from None


# ---------------------------------------------------------------- key and certificate formats

def pem_block(label: str, der: bytes) -> bytes:
    b64 = base64.b64encode(der)
    lines = b"\n".join(b64[i:i + 64] for i in range(0, len(b64), 64))
    return b"-----BEGIN " + label.encode() + b"-----\n" + lines + b"\n-----END " + label.encode() + b"-----\n"


def _pem_blocks(data: bytes) -> list[tuple[str, bytes]]:
    out = []
    for m in PEM_BLOCK.finditer(data):
        body = b"".join(ln for ln in m.group(2).split() if b":" not in ln)    # skip Proc-Type style headers
        try:
            out.append((m.group(1).decode(), base64.b64decode(body, validate=True)))
        except ValueError:
            raise ValueError("bad PEM") from None
    return out


def _der_header(b: bytes, i: int) -> tuple[int, int, int]:
    """(tag, content start, content end) of the DER element at b[i]."""
    if i + 2 > len(b):
        raise ValueError("truncated DER")
    tag, n = b[i], b[i + 1]
    i += 2
    if n & 0x80:
        k = n & 0x7F
        if k == 0 or k > 4 or i + k > len(b):
            raise ValueError("bad DER length")
        n = int.from_bytes(b[i:i + k], "big")
        i += k
    if i + n > len(b):
        raise ValueError("truncated DER")
    return tag, i, i + n


def _der(tag: int, content: bytes) -> bytes:
    n = len(content)
    if n < 0x80:
        return bytes([tag, n]) + content
    ln = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([tag, 0x80 | len(ln)]) + ln + content


def key_kind(der: bytes) -> str:
    """'pkcs8', 'pkcs1' (RSA) or 'sec1' (EC) for a DER private key; ValueError otherwise."""
    tag, start, end = _der_header(der, 0)
    if tag != 0x30:
        raise ValueError("not a DER sequence")
    t1, s1, e1 = _der_header(der, start)          # version
    if t1 != 0x02:
        raise ValueError("no version")
    t2, _s2, _e2 = _der_header(der, e1)
    if t2 == 0x30:
        return "pkcs8"
    if t2 == 0x02:
        return "pkcs1"
    if t2 == 0x04:
        return "sec1"
    raise ValueError("unknown key structure")


def pkcs1_to_pkcs8(der: bytes) -> bytes:
    return _der(0x30, _der(0x02, b"\x00") + _der(0x30, RSA_OID) + _der(0x04, der))


def sec1_to_pkcs8(der: bytes) -> bytes:
    """An EC key (SEC1, with its [0] curve parameters) as PKCS#8."""
    _tag, start, end = _der_header(der, 0)
    i, curve = start, None
    while i < end:
        tag, s, e = _der_header(der, i)
        if tag == 0xA0:
            curve = der[s:e]
        i = e
    if not curve:
        raise ValueError("EC key without its curve")
    alg = _der(0x30, EC_OID + curve)
    return _der(0x30, _der(0x02, b"\x00") + alg + _der(0x04, der))


def private_key_pem(data: bytes) -> bytes:
    """Any of PEM/DER × PKCS#1/PKCS#8/SEC1 -> a PKCS#8 'PRIVATE KEY' PEM (in memory)."""
    if b"-----BEGIN" in data:
        blocks = [(label, der) for label, der in _pem_blocks(data) if "PRIVATE KEY" in label]
        if not blocks:
            raise ValueError("no private key")
        label, der = blocks[0]
        if label == "ENCRYPTED PRIVATE KEY":
            raise ValueError("encrypted key")
    else:
        der = data
    kind = key_kind(der)
    if kind == "pkcs1":
        der = pkcs1_to_pkcs8(der)
    elif kind == "sec1":
        der = sec1_to_pkcs8(der)
    return pem_block("PRIVATE KEY", der)


def certificate_der(data: bytes) -> bytes:
    if b"-----BEGIN" in data:
        certs = [der for label, der in _pem_blocks(data) if label == "CERTIFICATE"]
        if not certs:
            raise ValueError("no certificate")
        data = certs[0]
    tag, _s, end = _der_header(data, 0)
    if tag != 0x30:
        raise ValueError("not a certificate")
    return data[:end]


def _scrub(text: str) -> str:
    """Belt and braces: never pass a PEM block (a key) on in an error detail."""
    return re.sub(r"-----BEGIN [A-Z ]+-----.*?(-----END [A-Z ]+-----|$)", "[pem]", text, flags=re.S)[-2000:]


# ---------------------------------------------------------------- signing

def rcodesign_path() -> str | None:
    return os.environ.get("IOSSTUDIO_RCODESIGN") or subproc.which("rcodesign")


def sign_dylib(path: str | Path, team: str | None = None, data_dir: Path | None = None,
               rcodesign: str | None = None, timeout: float = SIGN_TIMEOUT) -> float:
    """Sign `path` in place with the team's development identity; returns the seconds rcodesign took."""
    tool = rcodesign or rcodesign_path()
    if not tool:
        raise HelperError("missing-tool", "rcodesign isn't installed yet, so the reload can't be signed.",
                          fix("Open Setup", "setup"))
    ident = load_identity(team, data_dir)
    argv = [tool, "sign", "--pem-file", "/dev/stdin", "--team-name", ident.team, "--timestamp-url", "none",
            str(path)]
    started = time.monotonic()
    try:
        r = subproc.run_group(argv, timeout=timeout, input=ident.pem(), text=False,
                              env=subproc.tool_env("plain"))
    except subprocess.TimeoutExpired:
        raise sign_failed("Signing the reload took too long.") from None
    except FileNotFoundError:
        raise HelperError("missing-tool", "rcodesign isn't installed yet, so the reload can't be signed.",
                          fix("Open Setup", "setup")) from None
    finally:
        del ident
    if r.returncode != 0:
        out = (r.stderr or b"").decode("utf-8", "replace") + (r.stdout or b"").decode("utf-8", "replace")
        raise sign_failed("The reload couldn't be signed with your development certificate.", out)
    return time.monotonic() - started
