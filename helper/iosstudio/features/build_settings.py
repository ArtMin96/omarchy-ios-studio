"""Per-project build settings (stdlib only; used by features/projects/).

Some projects build through their own scripts: a script regenerates xtool.yml first, the build needs
environment variables, or the SDK module build needs a higher open-file limit. These settings let
iOS Studio build such a project the same way, without running the project's whole wrapper script:

  prebuild        a command run in the project folder before each build (e.g. `./generate.sh`). It is
                  split with shlex and run as an argv list, never through a shell: no pipes, `&&`,
                  globs or `$VAR` expansion. A script that needs those goes in a file the command runs.
  env             extra environment variables for the prebuild command and xtool (names
                  [A-Za-z_][A-Za-z0-9_]*; they replace inherited values, PATH included)
  openFiles       the soft open-file limit (RLIMIT_NOFILE) to raise to before the build; bounded by the
                  hard limit, a build fails with a clear message when the hard limit is lower
  allowGenerated  build a project whose xtool.yml says it's GENERATED (normally refused, because a
                  stale generated file could build something other than the real app)

They're stored per project in projects.json, under meta[<path>]["build"]. A project without settings
has none of them; every function here accepts a missing or partial dict.
"""
from __future__ import annotations

import re
import resource
import shlex
from pathlib import Path
from typing import Any

KEYS = ("prebuild", "env", "openFiles", "allowGenerated")
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MAX_ENV = 64
MAX_VALUE = 4096
MAX_PREBUILD = 2000
MIN_OPEN_FILES, MAX_OPEN_FILES = 256, 1 << 24
DEFAULT_OPEN_FILES = 65536      # what every build gets without a setting (SDK module builds need it)
# Never suggested from a script: the user's own values of these are what the build should see.
_NO_SUGGEST = {"PATH", "HOME", "USER", "SHELL", "PWD", "OLDPWD", "TERM", "LANG", "IFS", "PS1",
               "XDG_CONFIG_HOME", "XDG_RUNTIME_DIR", "LD_PRELOAD", "LD_LIBRARY_PATH"}
SCRIPTS = ("run.sh", "build.sh", "generate.sh", "prebuild.sh", "xtool.sh")


class Invalid(ValueError):
    """A setting that can't be saved; the message is for the user."""


# ---------------------------------------------------------------- validation

def empty() -> dict:
    return {"prebuild": None, "env": {}, "openFiles": None, "allowGenerated": False}


def current(meta: dict | None) -> dict:
    """The stored settings of one project (meta = projects.json meta[path]), with every key present."""
    raw = (meta or {}).get("build") or {}
    out = empty()
    if isinstance(raw.get("prebuild"), str) and raw["prebuild"].strip():
        out["prebuild"] = raw["prebuild"].strip()
    if isinstance(raw.get("env"), dict):
        out["env"] = {str(k): str(v) for k, v in raw["env"].items() if ENV_NAME.match(str(k))}
    if isinstance(raw.get("openFiles"), int) and not isinstance(raw.get("openFiles"), bool):
        out["openFiles"] = raw["openFiles"]
    out["allowGenerated"] = raw.get("allowGenerated") is True
    return out


def is_set(settings: dict) -> bool:
    return settings != empty()


def check_prebuild(cmd: Any) -> str | None:
    """'' / None -> None (no prebuild). Raises Invalid for something that can't be run as an argv."""
    if cmd is None:
        return None
    if not isinstance(cmd, str):
        raise Invalid("The prebuild command must be text.")
    cmd = cmd.strip()
    if not cmd:
        return None
    if len(cmd) > MAX_PREBUILD or "\x00" in cmd or "\n" in cmd:
        raise Invalid("The prebuild command must be one line (at most 2000 characters).")
    try:
        argv = shlex.split(cmd)
    except ValueError as exc:
        raise Invalid(f"The prebuild command can't be split into arguments ({exc}). Check its quotes.") from None
    if not argv:
        return None
    if re.fullmatch(r"[|&;<>]+|&&|\|\|", argv[0]) or any(a in ("|", "||", "&&", ";", ">", ">>", "<") for a in argv):
        raise Invalid("The prebuild command runs without a shell, so |, &&, ; and redirections don't work. "
                      "Put the steps in a script and run that.")
    return cmd


def check_env(env: Any) -> dict:
    if env is None:
        return {}
    if not isinstance(env, dict):
        raise Invalid("Environment variables must be name/value pairs.")
    if len(env) > MAX_ENV:
        raise Invalid(f"At most {MAX_ENV} environment variables.")
    out = {}
    for k, v in env.items():
        k = str(k).strip()
        if not ENV_NAME.match(k):
            raise Invalid(f"'{k}' isn't a valid variable name (letters, digits and _, not starting with a digit).")
        if isinstance(v, bool) or not isinstance(v, (str, int, float)):
            raise Invalid(f"The value of {k} must be text.")
        v = str(v)
        if "\x00" in v or len(v) > MAX_VALUE:
            raise Invalid(f"The value of {k} is too long or contains a NUL byte.")
        out[k] = v
    return out


def check_open_files(n: Any) -> int | None:
    if n is None or n == "" or n == 0:
        return None
    if isinstance(n, bool):
        raise Invalid("The open-file limit must be a number.")
    try:
        n = int(n)
    except (TypeError, ValueError):
        raise Invalid("The open-file limit must be a number.") from None
    if not MIN_OPEN_FILES <= n <= MAX_OPEN_FILES:
        raise Invalid(f"The open-file limit must be between {MIN_OPEN_FILES} and {MAX_OPEN_FILES}.")
    return n


def apply_patch(before: dict, patch: dict) -> dict:
    """New settings from a partial update. Keys left out keep their value. `env` merges: a name with a
    null value is removed; `envReplace: true` replaces the whole map instead. prebuild ''/null clears it,
    openFiles 0/null clears it."""
    if not isinstance(patch, dict):
        raise Invalid("Build settings must be an object.")
    unknown = set(patch) - set(KEYS) - {"envReplace"}
    if unknown:
        raise Invalid(f"Unknown build setting(s): {', '.join(sorted(unknown))}. Known: {', '.join(KEYS)}.")
    after = {**empty(), **before, "env": dict(before.get("env") or {})}
    if "prebuild" in patch:
        after["prebuild"] = check_prebuild(patch["prebuild"])
    if "env" in patch:
        raw = patch["env"] or {}
        if not isinstance(raw, dict):
            raise Invalid("Environment variables must be name/value pairs.")
        removed = [k for k, v in raw.items() if v is None]
        kept = check_env({k: v for k, v in raw.items() if v is not None})
        env = {} if patch.get("envReplace") else after["env"]
        for k in removed:
            env.pop(str(k).strip(), None)
        env.update(kept)
        after["env"] = check_env(env)
    if "openFiles" in patch:
        after["openFiles"] = check_open_files(patch["openFiles"])
    if "allowGenerated" in patch:
        v = patch["allowGenerated"]
        if isinstance(v, str):
            v = v.lower() in ("1", "true", "yes", "on")
        after["allowGenerated"] = bool(v)
    return after


def stored(settings: dict) -> dict:
    """What goes into projects.json: only the keys that are set."""
    out: dict = {}
    if settings.get("prebuild"):
        out["prebuild"] = settings["prebuild"]
    if settings.get("env"):
        out["env"] = dict(settings["env"])
    if settings.get("openFiles"):
        out["openFiles"] = settings["openFiles"]
    if settings.get("allowGenerated"):
        out["allowGenerated"] = True
    return out


def diff(before: dict, after: dict) -> list[str]:
    """One plain line per change."""
    out = []
    if before.get("prebuild") != after.get("prebuild"):
        if not after.get("prebuild"):
            out.append(f"Prebuild: remove `{before.get('prebuild')}`")
        elif not before.get("prebuild"):
            out.append(f"Prebuild: run `{after['prebuild']}` in the project folder before each build")
        else:
            out.append(f"Prebuild: `{before['prebuild']}` -> `{after['prebuild']}`")
    b_env, a_env = before.get("env") or {}, after.get("env") or {}
    for k in sorted(set(b_env) | set(a_env)):
        if k not in a_env:
            out.append(f"Environment: remove {k}")
        elif k not in b_env:
            out.append(f"Environment: set {k}={a_env[k]}")
        elif b_env[k] != a_env[k]:
            out.append(f"Environment: {k}={b_env[k]} -> {k}={a_env[k]}")
    if before.get("openFiles") != after.get("openFiles"):
        b, a = before.get("openFiles"), after.get("openFiles")
        out.append(f"Open-file limit: {b or f'default ({DEFAULT_OPEN_FILES})'} -> {a or f'default ({DEFAULT_OPEN_FILES})'}")
    if bool(before.get("allowGenerated")) != bool(after.get("allowGenerated")):
        out.append("Generated project: " + ("build it (the prebuild regenerates xtool.yml first)"
                                            if after.get("allowGenerated") else "refuse to build it again"))
    return out


# ---------------------------------------------------------------- applying them

def prebuild_argv(cmd: str) -> list[str]:
    """The argv for a stored prebuild command (run with cwd = the project folder, no shell)."""
    return shlex.split(cmd)


def merged_env(base: dict, settings: dict) -> dict:
    env = dict(base)
    env.update(settings.get("env") or {})
    return env


def nofile_target(want: int | None, soft: int, hard: int) -> tuple[int | None, str | None]:
    """(soft limit to set or None to leave it, error message or None).

    want=None is the default: raise to DEFAULT_OPEN_FILES if the hard limit allows, silently capped.
    An explicit `want` above the hard limit is an error, since the build would then fail half-way."""
    inf = resource.RLIM_INFINITY
    if want is None:
        target = DEFAULT_OPEN_FILES if hard == inf else min(DEFAULT_OPEN_FILES, hard)
        return (target if soft != inf and soft < target else None), None
    if hard != inf and want > hard:
        return None, (f"The open-file limit can't be raised to {want}: this session's hard limit is {hard}. "
                      f"Lower the setting to {hard} or less, or raise the hard limit (DefaultLimitNOFILE in "
                      "/etc/systemd/user.conf or `nofile` in /etc/security/limits.conf, then log in again).")
    return (want if soft != inf and soft < want else None), None


def check_nofile(want: int | None) -> str | None:
    """The error for this session, or None if `want` can be applied."""
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    return nofile_target(want, soft, hard)[1]


def raise_nofile(want: int | None) -> None:
    """In the build's child (preexec): raise the soft open-file limit. Never raises."""
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        target, _ = nofile_target(want, soft, hard)
        if target is not None:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
    except (ValueError, OSError):
        pass


# ---------------------------------------------------------------- suggestions

_EXPORT = re.compile(r"^\s*export\s+([A-Za-z_][A-Za-z0-9_]*)=(\S+)\s*(?:#.*)?$")
_ASSIGN = re.compile(r"^\s*(?:readonly\s+|declare\s+(?:-\w+\s+)?)?([A-Za-z_][A-Za-z0-9_]*)=(\d+)\s*(?:#.*)?$")
_ULIMIT = re.compile(r"\bulimit\s+(?:-S\s*)?-(?:S?n|nS)\s+\"?(\$\{?[A-Za-z_][A-Za-z0-9_]*\}?|\d+)\"?")
_GENERATED_BY = re.compile(r"GENERATED by\s+(\S+)")


def _literal(value: str) -> str | None:
    """A shell word's value if it's a plain literal ('x', "x" without $ or backticks, or bare)."""
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1]
    if len(value) >= 2 and value[0] == value[-1] == '"':
        value = value[1:-1]
    if any(c in value for c in "$`\\\"'(){};|&<>*?"):
        return None
    return value


def suggest(project: Path, settings: dict | None = None) -> dict | None:
    """Settings the project's own build scripts imply (run.sh exporting variables, raising ulimit -n,
    running generate.sh first; an xtool.yml that says it's GENERATED). Only suggests; never applied.
    Returns {settings: {...}, reasons: [...]} with just the keys that differ from `settings`, or None."""
    settings = settings or empty()
    found: dict = {}
    reasons: list[str] = []
    env: dict = {}
    yml_head = ""
    try:
        with open(project / "xtool.yml", errors="replace") as fh:
            yml_head = fh.read(400)
    except OSError:
        pass
    generated = "GENERATED" in yml_head
    generator = None
    m = _GENERATED_BY.search(yml_head)
    if m:
        name = Path(m.group(1)).name
        if (project / name).is_file():
            generator = name
    for script in SCRIPTS:
        f = project / script
        try:
            if not f.is_file() or f.stat().st_size > 512 * 1024:
                continue
            text = f.read_text(errors="replace")
        except OSError:
            continue
        nums: dict[str, int] = {}
        for raw in text.splitlines():
            line = raw.split("#", 1)[0] if not raw.lstrip().startswith("#") else ""
            if not line.strip():
                continue
            a = _ASSIGN.match(line)
            if a:
                nums[a.group(1)] = int(a.group(2))
            e = _EXPORT.match(line)
            if e and e.group(1) not in _NO_SUGGEST:
                v = _literal(e.group(2))
                if v is not None and e.group(1) not in env:
                    env[e.group(1)] = v
                    reasons.append(f"{script} exports {e.group(1)}={v}")
            u = _ULIMIT.search(line)
            if u and "openFiles" not in found:
                ref = u.group(1).strip("${}")
                n = int(ref) if ref.isdigit() else nums.get(ref)
                if n:
                    found["openFiles"] = n
                    reasons.append(f"{script} raises the open-file limit to {n}")
            if script != "generate.sh" and not generator and re.search(r"(^|[\s/\"'])generate\.sh\b", line) \
                    and (project / "generate.sh").is_file():
                generator = "generate.sh"
    if generator:
        found["prebuild"] = f"./{generator}"
        reasons.append(f"xtool.yml is written by {generator}" if generated else f"run.sh runs {generator} before xtool")
    if generated:
        found["allowGenerated"] = True
    if env:
        found["env"] = env
    # Keep only what isn't already set the same way.
    out: dict = {}
    if found.get("prebuild") and not settings.get("prebuild"):
        out["prebuild"] = found["prebuild"]
    new_env = {k: v for k, v in (found.get("env") or {}).items() if (settings.get("env") or {}).get(k) != v}
    if new_env:
        out["env"] = new_env
    if found.get("openFiles") and settings.get("openFiles") != found["openFiles"]:
        out["openFiles"] = found["openFiles"]
    if found.get("allowGenerated") and not settings.get("allowGenerated"):
        out["allowGenerated"] = True
    if not out:
        return None
    return {"settings": out, "reasons": reasons[:12]}
