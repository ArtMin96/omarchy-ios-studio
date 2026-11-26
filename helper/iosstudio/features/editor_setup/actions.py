"""Editor setup's plan: actions (write / merge / skip / backup) for the files it would change, and applying
them (never overwriting, writing through symlinks with the file's mode kept)."""
from __future__ import annotations

import json
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from iosstudio.core import fileio, jsonc
from iosstudio.core.jsonc import JsoncError
from iosstudio.core.output import HelperError
from iosstudio.features import config_files

from .env import SETTING_KEY, Env, load_settings

MAX_JSON = 512 * 1024        # bigger editor settings files are skipped, not parsed


def read_json_file(path: Path) -> tuple[str | None, str | None]:
    """(text, problem). text None + problem None: the file doesn't exist."""
    return config_files.read_limited(path, MAX_JSON)


def _kept_note(kept: list) -> str:
    n = len(kept)
    return f" Your own value for {n} setting{'s' if n != 1 else ''} is kept." if n else ""


Replace = dict[tuple[str, ...], Callable[[Any], bool]]
_MISSING = object()


def _dig(data: Any, keys: tuple[str, ...]) -> Any:
    for k in keys:
        if not isinstance(data, dict) or k not in data:
            return _MISSING
        data = data[k]
    return data


def replace_values(text: str, want: dict, replace: Replace) -> tuple[str, list[str]]:
    """(text, labels): the values at `replace`'s key paths that its predicate calls ours from before swapped for
    `want`'s, every other byte kept. Unparseable text comes back as it is (the merge then skips the file)."""
    try:
        data = jsonc.load(text)
    except JsoncError:
        return text, []
    done = []
    for keys, ours in replace.items():
        cur, new = _dig(data, keys), _dig(want, keys)
        if cur is _MISSING or new is _MISSING or cur == new or not ours(cur):
            continue
        try:
            text = config_files.set_member(text, list(keys), new)
        except JsoncError:
            continue
        done.append(config_files.key_label(list(keys)))
    return text, done


def json_action(root: Path, rel: str, want: dict, scope: str = "project", what: str = "",
                replace: Replace | None = None) -> dict:
    """The write/merge/skip action that puts `want` into root/rel. `replace`: key paths whose value is
    overwritten with want's when the predicate says iOS Studio wrote it before (other values are never
    overwritten)."""
    path = root / rel
    act: dict[str, Any] = {"path": str(path), "scope": scope}
    text, problem = read_json_file(path)
    if problem:
        return {**act, "kind": "skip", "detail": f"The file {problem}, so it's left alone."}
    if text is None:
        body = json.dumps(want, indent=2, ensure_ascii=False) + "\n"
        return {**act, "kind": "write", "detail": what or "New file.",
                "keys": [config_files.key_label([k]) for k in want], "preview": body, "_text": body}
    new, replaced = replace_values(text, want, replace) if replace else (text, [])
    res = config_files.merge_missing(new, want)
    kept = res.get("kept") or []
    if res["status"] == "skip":
        return {**act, "kind": "skip", "detail": f"The file {res['reason']}, so it's left alone.", "kept": kept}
    if res["status"] == "unchanged" and not replaced:
        return {**act, "kind": "skip", "detail": "Already set up." + _kept_note(kept), "kept": kept,
                "configured": True}
    n = len(res["added"])
    parts = [f"Updates {', '.join(replaced)}, which iOS Studio set before"] if replaced else []
    if n:
        parts.append(f"adds {n} missing setting{'s' if n != 1 else ''}")
    d = "; ".join(parts)
    d = d[0].upper() + d[1:] + "; everything else stays." + _kept_note(kept)
    return {**act, "kind": "merge", "detail": d, "keys": replaced + res["added"], "kept": kept,
            "_text": res.get("text") or new}


def backup_action(path: Path) -> dict:
    """A copy of a user-level file next to it (<name>.bak.<time>) before it changes."""
    return {"path": f"{path}.bak.{int(time.time())}", "scope": "user", "kind": "backup",
            "detail": f"A copy of {path.name} before it changes.", "_from": str(path)}


def user_json_actions(path: Path, want: dict, what: str, replace: Replace | None = None) -> list[dict]:
    """A merge into a user-level JSON file, preceded by a backup action."""
    act = json_action(path.parent, path.name, want, scope="user", what=what, replace=replace)
    return [backup_action(path), act] if act["kind"] == "merge" else [act]


def public(a: dict) -> dict:
    """An action without its private fields (_text, _from, _setting)."""
    return {k: v for k, v in a.items() if not k.startswith("_")}


def is_pending(a: dict) -> bool:
    return a["kind"] in ("write", "merge")


def apply_actions(acts: list[dict], env: Env) -> None:
    """Carry the actions out in order and mark each done. A file that appeared since the plan is never
    overwritten (its action turns into a skip)."""
    for a in acts:
        if a["kind"] == "backup":
            shutil.copy2(a["_from"], a["path"])
            a["done"] = True
        elif "_setting" in a:
            s = load_settings(env)
            s[SETTING_KEY] = a["_setting"]
            fileio.save_json(env.settings_file, s)
            a["done"] = True
        elif is_pending(a):
            path = Path(a["path"])
            if a["kind"] == "write" and path.exists():
                a["kind"], a["detail"] = "skip", "The file appeared meanwhile; it's left as it is."
                continue
            config_files.write_keeping_mode(path, a["_text"])
            a["done"] = True


def apply_or_fail(acts: list[dict], env: Env) -> None:
    """apply_actions, with a write error as the helper's write-failed error."""
    try:
        apply_actions(acts, env)
    except OSError as exc:
        raise HelperError("write-failed", f"Couldn't write {exc.filename or 'a file'}: {exc.strerror}.") from None
