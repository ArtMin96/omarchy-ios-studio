"""Run presets: saved launch arguments, environment and language per project (stdlib only; used by projects.py).

Like an Xcode scheme's Run options, a preset keeps what an app is launched with, so testing German text or a
debug flag doesn't mean typing it again each time:

  name        1-40 characters, unique per project (case-insensitive)
  args        launch arguments, passed to the app as-is (ProcessInfo.arguments / UserDefaults)
  env         environment variables (names [A-Za-z_][A-Za-z0-9_]*)
  language    an app language such as "de", "pt-BR" or "zh-Hans"     -> -AppleLanguages (de)
  region      a locale such as "de_DE" (formats dates, numbers)      -> -AppleLocale de_DE
  uiTestMode  a "UI test mode" switch for the app's own code         -> -UITestMode YES
              (iOS does nothing with it; the app reads it, e.g.
              UserDefaults.standard.bool(forKey: "UITestMode"), to skip onboarding or use test data)

resolve() turns a preset into the {args, env} that the device daemon's app.launch takes: the preset's own args
first, then the generated ones above. -AppleLanguages and -AppleLocale are argument-domain UserDefaults, so they
apply to that one launch only; the phone's own language setting doesn't change.

Stored per project in projects.json: meta[<path>]["runPresets"] (a list of presets) and meta[<path>]["runPreset"]
(the selected name, absent when none). Every function here accepts missing or partial data from older files.
"""
from __future__ import annotations

import json
import re
from typing import Any

from iosstudio.core.output import HelperError

ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# language: ISO 639 code, optional script and region (de, pt-BR, zh-Hans, zh-Hant-TW, es-419)
LANGUAGE = re.compile(r"^[a-z]{2,3}(-[A-Z][a-z]{3})?(-([A-Z]{2}|[0-9]{3}))?$")
# region: a locale identifier (de_DE, en_GB, zh_Hans_CN, es_419); a bare language is allowed too
REGION = re.compile(r"^[a-z]{2,3}(_[A-Z][a-z]{3})?(_([A-Z]{2}|[0-9]{3}))?$")
MAX_NAME = 40
MAX_PRESETS = 50
MAX_ARGS = 64
MAX_ARG = 1000
MAX_ENV = 64
MAX_VALUE = 4096
FIELDS = ("name", "args", "env", "language", "region", "uiTestMode")


def _invalid(msg: str) -> HelperError:
    return HelperError("invalid-preset", msg)


def default() -> dict:
    return {"name": "", "args": [], "env": {}, "language": None, "region": None, "uiTestMode": False}


def _clean_tag(value: Any, sep: str) -> str | None:
    """'de-de' / 'de_DE' / ' pt-br ' -> the canonical casing with `sep` between parts; None when empty."""
    if value is None or value is False:
        return None
    s = str(value).strip()
    if not s:
        return None
    parts = re.split(r"[-_]", s)
    out = [parts[0].lower()]
    for part in parts[1:]:
        if len(part) == 4 and part.isalpha():
            out.append(part[:1].upper() + part[1:].lower())    # script: Hans
        elif part.isalpha():
            out.append(part.upper())                            # region: DE
        else:
            out.append(part)                                    # 419
    return sep.join(out)


def normalize(raw: Any) -> dict:
    """Any dict -> a preset with every field present and the usual spellings fixed (trimmed name, strings for
    args and env values, 'pt_br' -> 'pt-BR', 'de-de' -> 'de_DE'). Doesn't check anything: see validate()."""
    raw = raw if isinstance(raw, dict) else {}
    out = default()
    out["name"] = str(raw.get("name") or "").strip()
    args = raw.get("args")
    if isinstance(args, str):
        args = [args]
    if isinstance(args, (list, tuple)):
        out["args"] = [str(a) for a in args if a is not None]
    env = raw.get("env")
    if isinstance(env, dict):
        out["env"] = {str(k).strip(): ("" if v is None else str(v)) for k, v in env.items()}
    out["language"] = _clean_tag(raw.get("language"), "-")
    out["region"] = _clean_tag(raw.get("region"), "_")
    ui = raw.get("uiTestMode")
    out["uiTestMode"] = ui is True or (isinstance(ui, str) and ui.strip().lower() in ("1", "true", "yes", "on"))
    return out


def validate(preset: dict) -> dict:
    """Checks a normalized preset; raises HelperError('invalid-preset') with a message for the user."""
    name = preset.get("name") or ""
    if not name:
        raise _invalid("A run preset needs a name.")
    if len(name) > MAX_NAME:
        raise _invalid(f"The preset name is too long ({len(name)} characters, at most {MAX_NAME}).")
    if any(ord(c) < 32 for c in name):
        raise _invalid("The preset name can't contain line breaks or control characters.")
    args = preset.get("args") or []
    if len(args) > MAX_ARGS:
        raise _invalid(f"Too many launch arguments ({len(args)}, at most {MAX_ARGS}).")
    for a in args:
        if len(a) > MAX_ARG:
            raise _invalid(f"A launch argument is too long ({len(a)} characters, at most {MAX_ARG}).")
        if "\0" in a:
            raise _invalid("Launch arguments can't contain NUL characters.")
    env = preset.get("env") or {}
    if len(env) > MAX_ENV:
        raise _invalid(f"Too many environment variables ({len(env)}, at most {MAX_ENV}).")
    for k, v in env.items():
        if not ENV_NAME.match(k):
            raise _invalid(f"'{k}' isn't a valid environment variable name (letters, digits and _, "
                           "not starting with a digit).")
        if len(v) > MAX_VALUE:
            raise _invalid(f"The value of {k} is too long ({len(v)} characters, at most {MAX_VALUE}).")
        if "\0" in v:
            raise _invalid(f"The value of {k} can't contain NUL characters.")
    lang = preset.get("language")
    if lang is not None and not LANGUAGE.match(lang):
        raise _invalid(f"'{lang}' isn't a language code. Use one like de, fr, pt-BR or zh-Hans.")
    region = preset.get("region")
    if region is not None and not REGION.match(region):
        raise _invalid(f"'{region}' isn't a region. Use a locale like de_DE, en_GB or fr_CA.")
    return preset


def resolve(preset: dict) -> dict:
    """A preset -> {"args": [...], "env": {...}} for app.launch: the preset's args, then the generated ones."""
    p = normalize(preset)
    args = list(p["args"])
    if p["language"]:
        args += ["-AppleLanguages", f"({p['language']})"]
    if p["region"]:
        args += ["-AppleLocale", p["region"]]
    if p["uiTestMode"]:
        args += ["-UITestMode", "YES"]
    return {"args": args, "env": dict(p["env"])}


def with_resolved(preset: dict) -> dict:
    return {**preset, "resolved": resolve(preset)}


# ---------------------------------------------------------------- stored list

def stored(meta: dict | None) -> list[dict]:
    """The saved presets of one project (meta = projects.json meta[path]); unreadable entries are skipped."""
    raw = (meta or {}).get("runPresets")
    out: list[dict] = []
    seen: set[str] = set()
    for item in raw if isinstance(raw, list) else []:
        p = normalize(item)
        if not p["name"] or p["name"].casefold() in seen:
            continue
        try:
            validate(p)
        except HelperError:
            continue
        seen.add(p["name"].casefold())
        out.append(p)
    return out


def selected(meta: dict | None, presets: list[dict] | None = None) -> str | None:
    """The selected preset's name as saved (its current spelling), or None when none or it no longer exists."""
    want = (meta or {}).get("runPreset")
    if not isinstance(want, str) or not want:
        return None
    presets = presets if presets is not None else stored(meta)
    hit = _index(presets, want)
    return None if hit is None else presets[hit]["name"]


def _index(presets: list[dict], name: str) -> int | None:
    key = str(name).strip().casefold()
    return next((i for i, p in enumerate(presets) if p["name"].casefold() == key), None)


def find(presets: list[dict], name: str) -> dict:
    """The preset called `name` (case-insensitive); raises HelperError('unknown-preset') listing the others."""
    i = _index(presets, name)
    if i is None:
        if presets:
            have = ", ".join(p["name"] for p in presets)
            msg = f"No run preset named '{name}'. This project has: {have}."
        else:
            msg = f"No run preset named '{name}'. This project has no run presets yet."
        raise HelperError("unknown-preset", msg)
    return presets[i]


def save(presets: list[dict], raw: Any, rename_from: str | None = None) -> tuple[list[dict], dict]:
    """Adds or replaces a preset (by name, or the one called `rename_from`). Returns (new list, saved preset)."""
    p = validate(normalize(raw))
    out = list(presets)
    at = None
    if rename_from:
        at = _index(out, find(out, rename_from)["name"])
    clash = _index(out, p["name"])
    if clash is not None and at is not None and clash != at:
        raise _invalid(f"There's already a preset named '{out[clash]['name']}'. Pick another name.")
    if at is None:
        at = clash
    if at is None:
        if len(out) >= MAX_PRESETS:
            raise _invalid(f"A project can have at most {MAX_PRESETS} run presets. Delete one first.")
        out.append(p)
    else:
        out[at] = p
    return out, p


def delete(presets: list[dict], name: str) -> tuple[list[dict], dict]:
    """Removes the preset called `name`; raises unknown-preset. Returns (new list, removed preset)."""
    gone = find(presets, name)
    return [p for p in presets if p is not gone], gone


def store(meta: dict, presets: list[dict], selected_name: str | None) -> None:
    """Writes presets and the selection into meta[path], dropping the keys when they're empty."""
    if presets:
        meta["runPresets"] = [{k: p[k] for k in FIELDS} for p in presets]
    else:
        meta.pop("runPresets", None)
    hit = _index(presets, selected_name) if selected_name else None
    if hit is not None:
        meta["runPreset"] = presets[hit]["name"]
    else:
        meta.pop("runPreset", None)


def listing(path: str, presets: list[dict], selected_name: str | None) -> dict:
    """The `projects presets` result shape."""
    return {"path": path, "presets": [with_resolved(p) for p in presets], "selected": selected_name}


def act(presets: list[dict], selected_name: str | None, flags: dict) -> tuple[list[dict], str | None, dict, bool]:
    """One `projects presets` action on a project's presets (no I/O, shared with demo mode).
    flags: save (preset JSON) [+ rename_from] | delete NAME [+ dry_run] | select NAME ('' clears) | resolve NAME.
    Returns (presets, selected, extra result fields, whether anything must be written)."""
    actions = [k for k in ("save", "delete", "select", "resolve") if k in flags]
    if len(actions) > 1:
        raise HelperError("usage", "Use one of --save, --delete, --select or --resolve at a time.")
    action = actions[0] if actions else None
    value = flags.get(action) if action else None
    if action in ("delete", "resolve") and (not isinstance(value, str) or not value.strip()):
        raise HelperError("usage", f"--{action} needs a preset name.")
    if action == "save":
        if not isinstance(value, str) or not value.strip():
            raise HelperError("usage", "--save needs the preset as JSON.")
        try:
            raw = json.loads(value)
        except json.JSONDecodeError:
            raise _invalid("--save must be a JSON object such as {\"name\": \"German\", \"language\": \"de\"}.") from None
        if not isinstance(raw, dict):
            raise _invalid("--save must be a JSON object such as {\"name\": \"German\", \"language\": \"de\"}.")
        old = flags.get("rename_from")
        old = old if isinstance(old, str) and old.strip() else None
        old_name = find(presets, old)["name"] if old else None
        presets, p = save(presets, raw, old)
        if old_name and selected_name and selected_name.casefold() == old_name.casefold():
            selected_name = p["name"]          # the selection follows a renamed preset
        return presets, selected_name, {"saved": p["name"]}, True
    if action == "delete":
        dry = bool(flags.get("dry_run"))
        after, gone = delete(presets, value)
        if dry:
            return presets, selected_name, {"deleted": gone["name"], "dryRun": True}, False
        if selected_name and selected_name.casefold() == gone["name"].casefold():
            selected_name = None
        return after, selected_name, {"deleted": gone["name"], "dryRun": False}, True
    if action == "select":
        if value is True or not str(value).strip():
            return presets, None, {}, selected_name is not None
        name = find(presets, value)["name"]
        return presets, name, {}, name != selected_name
    if action == "resolve":
        p = find(presets, value)
        return presets, selected_name, {"resolve": {"name": p["name"], **resolve(p)}}, False
    return presets, selected_name, {}, False


def result(path: str, presets: list[dict], selected_name: str | None, extra: dict) -> dict:
    """The JSON a `projects presets` call returns ({name, args, env} for --resolve)."""
    if "resolve" in extra:
        return extra["resolve"]
    return {**listing(path, presets, selected_name), **extra}
