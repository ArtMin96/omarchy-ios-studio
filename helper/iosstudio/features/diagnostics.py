"""Build output -> structured diagnostics (stdlib only; used by projects.py's BuildRunner).

Compiler lines (`/abs/File.swift:12:5: error: msg`) become one diagnostic each, as before. Everything
else that commonly breaks a build is folded into the same shape, so an agent never has to read a raw
log to learn what failed:

  linker     one diagnostic per block: undefined or duplicate symbols (lld's `>>> referenced by` lines
             and ld64's "Undefined symbols for architecture" blocks), missing libraries and frameworks
  package    SwiftPM: unresolvable versions, clone/fetch failures, invalid manifests, unknown
             packages or products, Package.swift compile errors
  signing    Apple ID sign-in, app id limit, 3-app limit, certificates, provisioning, code signing
  device     not paired or trusted, locked, Developer Mode off, not found
  toolchain  missing or broken iOS SDK, a crashing compiler (stack dump folded into one diagnostic)
  config     xtool.yml or Info.plist problems
  other      any other location-less `error:` line

A diagnostic is {severity, message, category, file?, line?, col?, group?, hint?, details?}. `hint` is one
actionable sentence (naming the iOS Studio MCP tool that fixes it, where there is one) and `details` a few
short lines such as the symbols of a linker block. Everything is capped: messages at MAX_MESSAGE
characters, details at MAX_DETAILS lines, a block at MAX_BLOCK_LINES lines, location-less diagnostics at
MAX_LOOSE per build.

    p = Parser()
    for line in output: emit(p.feed(line))     # the diagnostics this line completed
    emit(p.finish(failed=rc != 0))             # flushes an open block (and a last-resort one on failure)
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable

MAX_MESSAGE = 300
MAX_DETAILS = 8
MAX_DETAIL_CHARS = 200
MAX_BLOCK_LINES = 200
MAX_LOOSE = 50
MAX_NAMES = 5          # symbol names spelled out in a linker message

# /abs/File.swift:12:5: error: message      (col optional; `fatal error` counts as error)
DIAG = re.compile(r"^(?P<file>/[^:\n]+?):(?P<line>\d+):(?:(?P<col>\d+):)?\s*"
                  r"(?P<sev>error|warning|note|fatal error):\s*(?P<msg>.+)$")
# swift-build's own form: error: /abs/File.swift:1:8 unable to resolve module dependency: 'X'
DIAG_SB = re.compile(r"^(?P<sev>error|warning): (?P<file>/[^:\s]+):(?P<line>\d+):(?P<col>\d+):? (?P<msg>.+)$")
# A tool's own error, possibly behind swift-build's "/path/Package.swift: Target-product: " prefix.
TOOL = re.compile(r"(?:^|: )(?P<tool>ld64\.lld|ld\.lld|ld|clang\+*|swiftc|swift-frontend|swift-driver|swift-build|"
                  r"swift-package|xtool)(?: \([^)]*\))?: (?P<sev>error|warning|fatal error): (?P<msg>.+)$")
LOOSE = re.compile(r"^(?P<sev>error|fatal error|Error|warning): (?P<msg>.+)$")
LD64_UNDEF = re.compile(r"Undefined symbols? for architecture (?P<arch>\S+?):?$")
LD64_DUP = re.compile(r"^duplicate symbols? '?(?P<sym>[^']+?)'? in:?$")
CLONE = re.compile(r"^error: (?:Failed to clone repository|Failed to fetch|Couldn.t fetch|Failed to update|"
                   r"Couldn.t update)\b.*$", re.I)
CRASH_START = re.compile(r"^(Stack dump:|Please submit a bug report|PLEASE submit a bug report|"
                         r"PLEASE append the compiler crash|💣 Program crashed)", re.I)
CRASH_LINE = re.compile(r"^(\d+\.\s|\s*#?\d+\s+0x[0-9a-f]+|\d+\s+\S+\s+0x[0-9a-f]+|Stack dump|Please submit|"
                        r"PLEASE |Program arguments:|Thread \d+|Backtrace|Crashed|\*\*\*|at /|\s*\d+ \[)", re.I)
# The last word of a failed tool, not the cause: shown only if nothing better was found.
TRAILER = re.compile(r"failed with a nonzero exit code|^error: Build failed\.?$|^Error: (exited|signaled)\(\d+\)$|"
                     r"linker command failed with exit code|compile command failed due to signal|"
                     r"^error: fatalError$|^error: Build cancelled", re.I)
# Linker chatter that every xtool build prints; never a diagnostic.
NOISE = re.compile(r"does not support linking for platform iOS|auto-linked (framework|library) not found|"
                   r"too many errors emitted|^warning: (no|could not find) (\S+ )?(serialized )?diagnostics? file",
                   re.I)

HINTS = {
    "undefined": "Code calls symbols that are never defined: add the missing source file or package product to "
                 "the target's dependencies in Package.swift, or remove the call.",
    "duplicate": "The same symbol is defined twice: remove or rename one definition (often two copies of a file "
                 "or two @_cdecl/@_silgen_name with one name).",
    "missing-lib": "That library or framework isn't in the iOS SDK: drop it from linkerSettings or use an "
                   "iOS-compatible package.",
    "linker": "Linking failed: fix the first linker error above, then rebuild.",
    "module": "That module isn't available to this target: add its package and product to the target's "
              "dependencies in Package.swift (macOS-only modules don't exist on iOS).",
    "manifest": "Package.swift doesn't compile: fix the lines listed, then rebuild.",
    "resolve": "Package versions conflict: change the version requirement in Package.swift (or Package.resolved), "
               "then rebuild.",
    "fetch": "A package can't be downloaded: check its URL in Package.swift, then rebuild.",
    "network": "A package download failed on the network: check the internet connection, then rebuild.",
    "unknown-dep": "A target depends on a package or product that isn't declared: fix the names in "
                   "Package.swift's dependencies.",
    "app-id-limit": "Free Apple IDs can register 10 new app ids per 7 days: reuse a bundle id that is already "
                    "registered (ios_signing_status lists them) or wait.",
    "app-limit": "A free Apple ID can have 3 of your apps on an iPhone at once: remove one of them from the iPhone "
                 "(ios_app_list, ios_app_uninstall), then retry.",
    "auth": "The Apple ID sign-in is missing or expired: sign in again on iOS Studio's Account page "
            "(ios_open_app(page='account')); ios_account_status shows the state.",
    "certificate": "Certificate problem: ios_signing_status shows the certificates; replacing one revokes the old "
                   "one, so it is the user's decision (the build asks; answer with ios_build_respond).",
    "provisioning": "Provisioning failed: re-sign with ios_project_renew, and connect a new iPhone over USB once "
                    "(ios_run) so it gets registered.",
    "codesign": "Code signing failed: check ios_signing_status, then rebuild with ios_build_start(action='ipa').",
    "apple": "Apple's servers didn't answer: this is usually temporary, retry in a minute.",
    "developer-mode": "Developer Mode is off on the iPhone: turn it on in Settings › Privacy & Security "
                      "(ios_device_prepare explains the steps).",
    "device": "The iPhone isn't reachable or trusted: unlock it and tap Trust, then reconnect "
              "(ios_device_reconnect; ios_device_prepare for pairing).",
    "toolchain": "The iOS toolchain or SDK is missing or broken: run ios_toolchain_check.",
    "crash": "The Swift compiler crashed on this code: simplify it (split long expressions, add explicit types), "
             "then rebuild.",
    "xtool-yml": "xtool.yml doesn't match the package: check its version, bundleID and product against "
                 "Package.swift (ios_project_info shows them).",
    "local-package": "A .package(path:) dependency points to a folder that doesn't exist (or has no "
                     "Package.swift): fix the path in Package.swift's dependencies.",
    "info-plist": "The Info.plist is invalid: fix the file named in the message (it must be a dictionary).",
}

# Location-less errors, first match wins: (pattern, category, hint key).
RULES: list[tuple[re.Pattern, str, str | None]] = [(re.compile(p, re.I), c, h) for p, c, h in [
    (r"app ?id limit|up to 10 app ids|maximum number of app ids|no more app ids|too many app ids", "signing",
     "app-id-limit"),
    (r"maximum number of (installed )?apps|free develop\w* (provisioning )?profile|0xe8008021", "signing",
     "app-limit"),
    (r"xtool auth|requires logging in|not logged in|session.*expired|log ?in again|unauthori[sz]ed|"
     r"invalid.*token|\b401\b|-2240\d|-20209|failedsecondaryauth|2-factor|not signed in", "signing", "auth"),
    (r"developer ?mode", "device", "developer-mode"),
    (r"\bpairing|\btrust\b|passwordprotected|unlock your device|invalidhostid|device not found|no devices|"
     r"could not connect to (the )?device|usbmux|lockdown|deviceinfofetchfailed|installer\.error", "device",
     "device"),
    (r"certificate", "signing", "certificate"),
    (r"provisioning|\bprofile\b|entitlement|0xe800801[25]|device.*not.*(included|registered)", "signing",
     "provisioning"),
    (r"code ?sign|signature|no signers found|signing error|signer", "signing", "codesign"),
    (r"anisette|gsa\.apple|developerservices|apple\.com", "signing", "apple"),
    (r"darwin sdk|swift ?sdk|could not find sdk|no sdk|unknown sdk|xtool setup|bad path for swift|"
     r"invalid sdk", "toolchain", "toolchain"),
    (r"xtool\.yml|decodingerror|schema|orgid|bundleid|library products?|product name", "config", "xtool-yml"),
    (r"info\.plist", "config", "info-plist"),
    (r"dependencies could not be resolved|could not resolve package|depends on .* \d", "package", "resolve"),
    (r"the package at .* cannot be accessed|doesn.t exist in file system", "package", "local-package"),
    (r"invalid manifest|manifest parse|package\.swift", "package", "manifest"),
    (r"unknown (package|dependency)|product '[^']+' .*not found|no such product|unknown product", "package",
     "unknown-dep"),
    (r"failed to (clone|fetch)|couldn.t (fetch|clone)|repository .* not found", "package", "fetch"),
    (r"no such module|unable to resolve module|missing required module", "compiler", "module"),
    (r"undefined symbol|duplicate symbol|library not found|framework not found|\bld\b", "linker", "linker"),
    (r"stack dump|compiler crashed|due to signal", "toolchain", "crash"),
]]
NETWORK = re.compile(r"could not resolve host|failed to connect|timed out|network (connection|is unreachable)|"
                     r"connection (refused|reset|lost)|unable to access|ssl|temporary failure", re.I)


def clip(s: str, n: int = MAX_MESSAGE) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[:n - 1] + "…"


def classify(text: str) -> tuple[str, str | None]:
    """(category, hint) for a location-less message."""
    if NETWORK.search(text):
        if re.search(r"git|clone|fetch|repositor|package", text, re.I):
            return "package", HINTS["network"]
        return "other", HINTS["apple"] if re.search(r"apple", text, re.I) else None
    for rx, cat, hint in RULES:
        if rx.search(text):
            return cat, HINTS.get(hint) if hint else None
    return "other", None


def _clean(msg: str) -> str:
    """Drops the parts of a tool message that only cost bytes (command lines, compiler arguments)."""
    # xtool prints Apple's errors as Swift values: apiError(code: 9401, reason: Optional("…")) -> "… (9401)"
    msg = re.sub(r'^apiError\(code: (-?\d+), reason: Optional\("(.*)"\)\)$', r"\2 (\1)", msg)
    msg = re.sub(r"\s*Command line:.*$", "", msg)
    msg = re.sub(r"\s*\(compiled with: \[.*$", "", msg)
    msg = re.sub(r"[;:,]\s*valid packages are:\s*$", "", msg)
    return msg.rstrip(" :;")


def _short(path: str) -> str:
    """`/a/b/Products/Debug-iphoneos/App.o(Foo.o)` -> `App.o(Foo.o)`."""
    return path.rsplit("/", 1)[-1]


def _mangled(name: str) -> bool:
    return bool(re.match(r"^_?(\$s|\$S|_T0|\$e)", name))


def _find_demangler() -> str | None:
    found = shutil.which("swift-demangle")
    if found:
        return found
    swift = shutil.which("swift")
    if swift:
        cand = Path(os.path.realpath(swift)).parent / "swift-demangle"
        if cand.is_file():
            return str(cand)
    return None


def demangle(names: list[str]) -> dict[str, str]:
    """Swift-mangled symbols -> readable names, in one swift-demangle call (best effort, 5 s)."""
    todo = sorted({n for n in names if _mangled(n)})
    tool = _find_demangler() if todo else None
    if not tool:
        return {}
    try:
        r = subprocess.run([tool, "--simplified", "--compact"], input="\n".join(todo) + "\n",
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return {}
    out = r.stdout.splitlines()
    return {a: b.strip() for a, b in zip(todo, out) if b.strip() and b.strip() != a}


class Parser:
    def __init__(self, demangler: Callable[[list[str]], dict[str, str]] | None = demangle):
        self.demangler = demangler
        self.block: dict | None = None
        self.trailers: list[str] = []
        self.seen: set = set()
        self.loose = 0
        self.errors = 0

    # ---------------------------------------------------------------- public
    def feed(self, line: str) -> list[dict]:
        raw = line.rstrip()
        s = raw.strip()
        if not s:
            return []
        out: list[dict] = []
        if self.block is not None:
            if self.block["lines"] < MAX_BLOCK_LINES and self._continue(raw, s):
                self.block["lines"] += 1
                return []
            out += self._close()
        out += self._start(raw, s)
        return self._keep(out)

    def finish(self, failed: bool = True) -> list[dict]:
        out = self._close() if self.block is not None else []
        out = self._keep(out)
        if failed and not self.errors and self.trailers:
            # The most specific last word: "Ld … failed" says more than "Build failed" or "exited(1)".
            generic = re.compile(r"^(Build failed|Build cancelled|exited\(|signaled\()", re.I)
            msg = next((t for t in self.trailers if not generic.match(t)), self.trailers[0])
            if generic.match(msg):
                d = self._diag(msg, "other", hint="The build stopped without a specific error message: read "
                                                  "ios_build_log(level='error') for the lines before it.")
            elif re.search(r"^Ld\b|linker", msg):
                d = self._diag(msg, "linker", hint=HINTS["linker"])
            elif re.search(r"due to signal", msg):
                d = self._diag(msg, "toolchain", hint=HINTS["crash"])
            elif re.search(r"Swift(Compile|Driver|EmitModule)|compile", msg):
                d = self._diag(msg, "compiler", hint="The compiler failed without a file:line error: read "
                                                     "ios_build_log(level='error') around this line.")
            else:
                d = self._diag(msg, "other")
            out += self._keep([d])
        return out

    # ---------------------------------------------------------------- helpers
    def _diag(self, message: str, category: str, severity: str = "error", hint: str | None = None,
              file: str | None = None, line: int | None = None, col: int | None = None,
              details: list[str] | None = None) -> dict:
        d: dict = {"severity": severity, "message": clip(_clean(message)), "category": category}
        if file:
            d["file"], d["line"], d["col"] = file, int(line or 0), int(col or 0)
        if hint:
            d["hint"] = hint
        if details:
            d["details"] = [clip(x, MAX_DETAIL_CHARS) for x in details[:MAX_DETAILS]]
            if len(details) > MAX_DETAILS:
                d["details"].append(f"+{len(details) - MAX_DETAILS} more")
        return d

    def _keep(self, diags: list[dict]) -> list[dict]:
        out = []
        for d in diags:
            key = (d["severity"], d["category"], d.get("file"), d.get("line"), d.get("col"), d["message"])
            if key in self.seen:
                continue
            if not d.get("file"):
                if self.loose >= MAX_LOOSE:
                    continue
                self.loose += 1
            self.seen.add(key)
            if d["severity"] == "error":
                self.errors += 1
            out.append(d)
        return out

    # ---------------------------------------------------------------- one line
    def _start(self, raw: str, s: str) -> list[dict]:
        if NOISE.search(s):
            return []
        m = DIAG.match(s) or DIAG_SB.match(s)
        if m:
            return [self._compiler(m)]
        if TRAILER.search(s):
            if len(self.trailers) < 5:
                self.trailers.append(clip(_clean(re.sub(r"^(?:.*?: )?(?:clang: )?(?:error|Error): ", "", s))))
            return []
        u = LD64_UNDEF.search(s)
        if u:
            self._open("undefined", tool="ld")
            return []
        dm = LD64_DUP.match(s)
        if dm:
            self._open("duplicate", tool="ld")
            self.block["symbols"].append({"name": dm.group("sym"), "refs": []})
            return []
        t = TOOL.search(s)
        if t:
            return self._tool(t)
        if CRASH_START.match(s):
            self._open("crash")
            self._crash_line(s)
            return []
        if CLONE.match(s):
            self._open("clone", head=re.sub(r"^error:\s*", "", s))
            return []
        if re.match(r"^error: Dependencies could not be resolved", s):
            self._open("resolve", head=s[len("error: "):])
            return []
        lm = LOOSE.match(s)
        if lm:
            sev = "warning" if lm.group("sev") == "warning" else "error"
            msg = lm.group("msg").strip()
            cat, hint = classify(msg)
            return [self._diag(msg, cat, sev, hint)]
        return []

    def _compiler(self, m: re.Match) -> dict:
        sev = m.group("sev")
        sev = "error" if sev == "fatal error" else sev
        msg = m.group("msg").strip()
        group = None
        g = re.search(r"\s*\[#(\w+)\]$", msg)
        if g:
            msg, group = msg[:g.start()], g.group(1)
        file = m.group("file")
        cat, hint = "compiler", None
        if file.endswith("/Package.swift"):
            cat, hint = "package", HINTS["manifest"] if sev == "error" else None
        elif re.search(r"no such module|unable to resolve module|missing required module", msg):
            hint = HINTS["module"]
        d = self._diag(msg, cat, sev, hint, file=file, line=int(m.group("line")), col=int(m.group("col") or 0))
        d["message"] = msg if len(msg) <= MAX_MESSAGE else d["message"]
        if group:
            d["group"] = group
        return d

    def _tool(self, t: re.Match) -> list[dict]:
        tool, msg = t.group("tool"), t.group("msg").strip()
        sev = "warning" if t.group("sev") == "warning" else "error"
        is_ld = tool in ("ld64.lld", "ld.lld", "ld")
        if is_ld and sev == "warning":
            return []                                   # linker warnings are noise in xtool builds
        if is_ld:
            u = re.match(r"undefined symbols?: (.+)$", msg)
            if u:
                self._open("undefined", tool=tool, merge=True)
                self.block["symbols"].append({"name": u.group(1).strip(), "refs": []})
                return []
            dd = re.match(r"duplicate symbols?: (.+)$", msg)
            if dd:
                self._open("duplicate", tool=tool, merge=True)
                self.block["symbols"].append({"name": dd.group(1).strip(), "refs": []})
                return []
            hint = HINTS["missing-lib"] if re.search(r"(library|framework) not found|unable to find "
                                                      r"(library|framework)", msg) else HINTS["linker"]
            self._open("ld-other", head=msg, hint=hint)
            return []
        cat, hint = classify(msg)
        if tool.startswith("clang") and cat == "other":
            cat = "compiler"
        return [self._diag(msg, cat, sev, hint)]

    # ---------------------------------------------------------------- blocks
    def _open(self, kind: str, merge: bool = False, **extra) -> None:
        if merge and self.block is not None and self.block["kind"] == kind:
            return
        self.block = {"kind": kind, "lines": 0, "symbols": [], "details": [], **extra}

    def _continue(self, raw: str, s: str) -> bool:
        b = self.block
        kind = b["kind"]
        if kind in ("undefined", "duplicate", "ld-other"):
            if s.startswith(">>>"):
                self._ref(s[3:].strip())
                return True
            t = TOOL.search(s)
            if t and t.group("tool") in ("ld64.lld", "ld.lld", "ld"):
                msg = t.group("msg")
                if kind in ("undefined", "duplicate") and re.match(kind + r" symbols?: ", msg) \
                        and t.group("sev") != "warning":
                    b["symbols"].append({"name": msg.split(":", 1)[1].strip(), "refs": []})
                    return True
                if re.search(r"symbol\(s\) not found|duplicate symbols? for architecture|too many errors", msg) \
                        or t.group("sev") == "warning":
                    return True
                return False
            if b.get("tool") == "ld":      # ld64 layout
                q = re.match(r'^"(?P<sym>[^"]+)", referenced from:$', s)
                if q:
                    b["symbols"].append({"name": q.group("sym"), "refs": []})
                    return True
                if re.match(r"^ld: (symbol\(s\) not found|\d+ duplicate symbols?)", s) or s.startswith("NOTE:") \
                        or s.startswith("(maybe you meant"):
                    return True
                if raw[:1].isspace() and b["symbols"]:
                    # "_$s4Main3fooyyF in Main.o" (undefined) or "/path/A.o" (duplicate)
                    r = re.match(r"^(?P<fn>\S+) in (?P<obj>\S+)$", s)
                    b["symbols"][-1]["refs"].append({"func": r.group("fn"), "obj": _short(r.group("obj"))} if r
                                                    else {"obj": _short(s)})
                    return True
            return False
        if kind == "clone":
            if raw[:1].isspace() or re.match(r"^(fatal|remote|error: RPC|ssh|git):", s):
                if not s.startswith("Cloning into"):
                    b["details"].append(s)
                return True
            return False
        if kind == "resolve":
            if raw[:1].isspace() or re.match(r"^('|because |and |depends on )", s):
                b["head"] += " " + s
                return True
            return False
        if kind == "crash":
            if CRASH_LINE.match(s) or CRASH_START.match(s) or raw[:1].isspace():
                self._crash_line(s)
                return True
            return False
        return False

    def _ref(self, s: str) -> None:
        """One lld `>>>` line: who references (or defines) the current symbol."""
        b = self.block
        if not b["symbols"]:
            b["details"].append(s)
            return
        sym = b["symbols"][-1]
        refs = sym["refs"]
        m = re.match(r"^(referenced|defined) by (?P<what>.+?)(?: \((?P<abs>/[^)]+?):(?P<line>\d+)\))?$", s) \
            or re.match(r"^defined in (?P<what>.+?)(?: \((?P<abs>/[^)]+?):(?P<line>\d+)\))?$", s)
        if m:
            what = m.group("what")
            fn = re.search(r":\(symbol (.+?)\+0x[0-9a-f]+\)$", what)
            refs.append({"src": what if not fn else None, "obj": _short(what.split(":(symbol")[0]) if fn else None,
                         "func": fn.group(1) if fn else None, "file": m.group("abs"),
                         "line": int(m.group("line")) if m.group("line") else None})
            return
        if re.match(r"^referenced \d+ more times?", s):
            return
        # the object line under a "referenced by File.swift:5 (...)": /…/App.o(Foo.o):(symbol $s…+0x14)
        o = re.match(r"^(?P<obj>\S.*?)(?::\(symbol (?P<fn>.+?)\+0x[0-9a-f]+\))?$", s)
        if o and refs and not refs[-1].get("obj"):
            refs[-1]["obj"] = _short(o.group("obj"))
            if o.group("fn"):
                refs[-1]["func"] = o.group("fn")

    def _crash_line(self, s: str) -> None:
        b = self.block
        # The innermost "While ..." frame (the last one) says best what the compiler was doing, and where.
        w = re.match(r"^\d+\.\s+(While .+?)(?: \(at /.*| at \[.*| \(in .*)?$", s)
        if w:
            b["while"] = w.group(1)
            at = re.search(r"(/[^:\s\"\[\]()]+\.swift):(\d+):(\d+)", s)
            if at:
                b["at"] = (at.group(1), int(at.group(2)), int(at.group(3)))
        if not b.get("file"):
            f = re.search(r'source_file "(/[^"]+)"', s) or re.search(r"(?:^|\s)(/\S+\.swift)\b", s)
            if f:
                b["file"] = f.group(1)
        if re.match(r"^\d+\.\s+(While|Contents of|Compiling|Swift version)", s) and len(b["details"]) < MAX_DETAILS:
            b["details"].append(re.sub(r"^\d+\.\s+", "", s))

    def _close(self) -> list[dict]:
        b, self.block = self.block, None
        if b is None:
            return []
        kind = b["kind"]
        if kind in ("undefined", "duplicate"):
            return [self._symbols_diag(b)]
        if kind == "ld-other":
            return [self._diag(b["head"], "linker", hint=b.get("hint"), details=b["details"])]
        if kind == "clone":
            text = " ".join([b["head"]] + b["details"])
            cause = next((re.sub(r"^(fatal|remote|error):\s*", "", x) for x in reversed(b["details"])
                          if re.match(r"^(fatal|remote|error):", x)), None)
            msg = b["head"].rstrip(":") + (f": {cause}" if cause else "")
            hint = HINTS["network"] if NETWORK.search(text) else HINTS["fetch"]
            return [self._diag(msg, "package", hint=hint, details=b["details"])]
        if kind == "resolve":
            return [self._diag(b["head"], "package", hint=HINTS["resolve"])]
        if kind == "crash":
            at = b.get("at")
            file, line, col = at if at else (b.get("file"), 0, 0)
            msg = "The Swift compiler crashed" + (f" ({b['while'][0].lower() + b['while'][1:]})" if b.get("while")
                                                  else "")
            return [self._diag(msg, "toolchain", hint=HINTS["crash"], file=file, line=line, col=col,
                               details=b["details"])]
        return []

    def _symbols_diag(self, b: dict) -> dict:
        syms = b["symbols"]
        kind = b["kind"]
        names = [x["name"] for x in syms]
        funcs = [r["func"] for x in syms for r in x["refs"] if r.get("func")]
        readable = {}
        if self.demangler:
            try:
                readable = self.demangler(names + funcs) or {}
            except Exception:  # noqa: BLE001  demangling is a nicety
                readable = {}
        nice = lambda n: readable.get(n, n)  # noqa: E731
        shown = ", ".join(nice(n) for n in names[:MAX_NAMES]) + (f" and {len(names) - MAX_NAMES} more"
                                                                 if len(names) > MAX_NAMES else "")
        label = "Undefined symbol" if kind == "undefined" else "Duplicate symbol"
        msg = f"{label}{'s' if len(names) != 1 else ''}: {shown}" if names else f"{label}s"
        details = []
        for x in syms:
            refs = x["refs"]
            if kind == "undefined":
                parts = []
                for r in refs[:2]:
                    where = (Path(r["file"]).name + f":{r['line']}") if r.get("file") else (r.get("src") or "")
                    who = f"{nice(r['func'])} in " if r.get("func") else ""
                    parts.append(f"{who}{r.get('obj') or where}" + (f" ({where})" if r.get("obj") and where else ""))
                details.append(f"{nice(x['name'])} ← referenced from " + ("; ".join(parts) if parts else "?"))
            else:
                objs = [r.get("obj") or r.get("src") or "" for r in refs]
                objs = [o for o in objs if o and "<compiler-generated>" not in o]
                details.append((f"{nice(x['name'])} defined in " + ", ".join(dict.fromkeys(objs))) if objs
                               else nice(x["name"]))
        # The first real source location (a referencing or defining line) makes the diagnostic clickable.
        first = next((r for x in syms for r in x["refs"] if r.get("file") and "<" not in r["file"]), None)
        hint = HINTS["undefined"] if kind == "undefined" else HINTS["duplicate"]
        return self._diag(msg, "linker", hint=hint, details=details,
                          file=first["file"] if first else None, line=first["line"] if first else None)


def parse(lines, failed: bool = True, demangler=demangle) -> list[dict]:
    """Every diagnostic in a finished log (tests, re-parsing stored logs)."""
    p = Parser(demangler)
    out: list[dict] = []
    for ln in lines:
        out += p.feed(ln)
    return out + p.finish(failed)
