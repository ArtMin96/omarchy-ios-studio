"""Debug in editor (VS Code, Zed): the editor's own lldb-dap attaches to the app on the iPhone through the
debugserver forward iOS Studio holds open. The stream that holds it is editor_debug_bridge.py.

Two pieces of state, both outside the project:

* The editor's debug config, written once per project: one configuration ("Attach to <App> on iPhone (iOS
  Studio)") in `.vscode/launch.json` (`"type": "swift"`, the Swift extension that editor setup recommends,
  which runs the toolchain's lldb-dap) or `.zed/debug.json` (`"adapter": "Swift"`, Zed's Swift extension).
  Every other configuration and comment in those files stays byte for byte; only our entry is added or
  updated. Its one attach command sources the app's attach file by a path relative to the home folder, so the
  project files carry no machine path.
* The attach file, `<state>/editor-debug/<bundleId>.lldb`: the lldb commands that connect to the forward and
  attach to the app's process. While a bridge runs it holds the current port and pid, guarded by the bridge's
  pid (a stale file fails instead of connecting to an old port); otherwise it's a stub that fails with "Start
  'Debug in editor' in iOS Studio first". Its lldb commands (iosstudio-check, -connect, ...) come from
  editor_debug_lldb.py, copied next to it as iosstudio_editor_attach.py.

  debug editor-config --project P [--editor vscode|zed|neovim] [--dry-run]
      -> {project, bundleId, appName, configName, editors: [{id, name, path, state, action}], dryRun, applied,
          scriptPath, notes}
  debug editor-status [--bundle-id B]
      -> {bridges: [{bundleId, installedBundleId, appName, project, port, host, pid, launched, editor, since,
          helperPid, scriptPath}]}
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

from iosstudio.core import appdirs, fileio, jsonc
from iosstudio.core.jsonc import JsoncError
from iosstudio.core.output import HelperError
from iosstudio.features import config_files

LLDB_MODULE = Path(__file__).resolve().parent / "editor_debug_lldb.py"
MODULE_NAME = "iosstudio_editor_attach"
KEEP_FILES = 20              # attach files (and their bridge state) kept, newest first; running ones always stay
MAX_JSON = 512 * 1024
ATTACH_WAIT = 300            # lldb-dap's `timeout`: seconds to wait for the app to stop after attaching (Wi-Fi is slow)
EDITOR_NAMES = {"vscode": "VS Code", "zed": "Zed", "neovim": "Neovim"}
CONFIG_FILES = {"vscode": ".vscode/launch.json", "zed": ".zed/debug.json"}
EDITOR_USAGE = "--editor must be vscode, zed or neovim."
NEOVIM_NOTE = ("Neovim: add the nvim-dap snippet from the Debug in editor guide (docs/features/editor-debugging.md); "
               "iOS Studio doesn't change your Neovim config.")


def folder() -> Path:
    return appdirs.state_dir() / "editor-debug"


def file_key(bundle_id: str) -> str:
    """A bundle id as a file name (they're already safe; anything else becomes _)."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", bundle_id or "app").strip(".") or "app"


def plain_bundle_id(bid: str) -> str:
    """'XTL-<TEAM>.com.example.app' (how free teams install it) -> 'com.example.app'."""
    return bid.split(".", 1)[1] if bid.startswith("XTL-") and "." in bid else bid


def script_path(bundle_id: str) -> Path:
    return folder() / f"{file_key(plain_bundle_id(bundle_id))}.lldb"


def state_path(bundle_id: str) -> Path:
    return folder() / f"{file_key(plain_bundle_id(bundle_id))}.json"


def config_name(app: str) -> str:
    return f"Attach to {app} on iPhone (iOS Studio)"


# ================================================================ the editor configs

def attach_command(script: Path, home: Path | None = None) -> str:
    """The one attachCommand: source the attach file. lldb doesn't expand ~ in `command source`, and a home path
    in .vscode/ or .zed/ breaks the project for everyone else, so a file under the home folder is found from
    lldb's Python with os.path.expanduser."""
    home = home or appdirs.HOME
    try:
        rel = script.relative_to(home)
    except ValueError:
        return f"command source {_q(str(script))}"
    path = "~/" + rel.as_posix()
    return f"script import os; lldb.debugger.HandleCommand('command source \"%s\"' % os.path.expanduser({path!r}))"


def vscode_config(app: str, script: Path, home: Path | None = None) -> dict:
    return {"name": config_name(app), "type": "swift", "request": "attach",
            "attachCommands": [attach_command(script, home)], "timeout": ATTACH_WAIT}


def zed_config(app: str, script: Path, home: Path | None = None) -> dict:
    return {"label": config_name(app), "adapter": "Swift", "request": "attach",
            "attachCommands": [attach_command(script, home)], "timeout": ATTACH_WAIT}


def _ours(item: Any, name: str, script: Path) -> bool:
    """Our entry: same name/label, or an entry that sources this app's attach file."""
    if not isinstance(item, dict):
        return False
    if item.get("name") == name or item.get("label") == name:
        return True
    cmds = item.get("attachCommands")
    return isinstance(cmds, list) and any(isinstance(c, str) and f"/editor-debug/{script.name}" in c for c in cmds)


def _list_node(root: jsonc.Node, path: list[str]) -> jsonc.Node | None:
    node = root
    for key in path:
        if node.kind != "obj":
            return None
        m = next((m for m in node.members if m.key == key), None)
        if m is None:
            return None
        node = m.node
    return node if node.kind == "arr" else None


def upsert_entry(text: str, list_path: list[str], entry: dict, script: Path) -> dict:
    """Put `entry` into the JSON list at list_path ([] = the root list) of a JSONC document, keeping every
    other byte. -> {status: added|updated|current|skip, text?, reason?}. An empty document starts fresh."""
    name = entry.get("name") or entry.get("label")
    if not text.strip():
        doc: Any = {"version": "0.2.0", "configurations": [entry]} if list_path == ["configurations"] else [entry]
        return {"status": "added", "text": json.dumps(doc, indent=2, ensure_ascii=False) + "\n"}
    try:
        root = jsonc.parse(text)
    except JsoncError as exc:
        return {"status": "skip", "reason": f"isn't valid JSON ({exc})"}
    if list_path and root.kind == "obj" and _list_node(root, list_path) is None:
        if any(m.key == list_path[0] for m in root.members):
            return {"status": "skip", "reason": f"has a \"{list_path[0]}\" that isn't a list"}
        res = config_files.merge_missing(text, {list_path[0]: [entry]})
        if res["status"] != "merged":
            return {"status": "skip", "reason": res.get("reason") or "couldn't be merged safely"}
        return _verified(text, res["text"], list_path, entry, name, script, "added")
    node = _list_node(root, list_path) if list_path else (root if root.kind == "arr" else None)
    if node is None:
        return {"status": "skip", "reason": "isn't a JSON list ([ … ])" if not list_path else
                "isn't a JSON object ({ … })"}
    hit = next((i for i, item in enumerate(node.items) if _ours(item.value, name, script)), None)
    unit = config_files._file_indent_unit(text)
    if hit is None:
        new = config_files._apply_edits(text, config_files._append_edits(text, node, [(None, entry)], unit))
        return _verified(text, new, list_path, entry, name, script, "added")
    item = node.items[hit]
    if item.value == entry:
        return {"status": "current"}
    indent, own_line = config_files.line_indent(text, item.start)
    single = "\n" not in text[node.start:node.end]
    rendered = config_files.render(entry, indent if own_line else "", None if single else unit)
    new = text[:item.start] + rendered + text[item.end:]
    return _verified(text, new, list_path, entry, name, script, "updated")


def _verified(old: str, new: str, list_path: list[str], entry: dict, name: str, script: Path, status: str) -> dict:
    """The edit must parse, contain our entry once, and keep everything else as it was."""
    try:
        before, after = jsonc.load(old), jsonc.load(new)
    except JsoncError as exc:
        return {"status": "skip", "reason": f"couldn't be changed safely ({exc})"}

    def split(doc: Any) -> tuple[Any, list]:
        node = doc
        for k in list_path:
            node = node.get(k) if isinstance(node, dict) else None
        items = node if isinstance(node, list) else []
        if isinstance(doc, dict) and list_path:
            rest = {k: v for k, v in doc.items() if k != list_path[0]}
            return rest, items
        return None, items
    rest_b, items_b = split(before)
    rest_a, items_a = split(after)
    others_b = [x for x in items_b if not _ours(x, name, script)]
    others_a = [x for x in items_a if not _ours(x, name, script)]
    mine = [x for x in items_a if _ours(x, name, script)]
    if rest_a != rest_b:
        return {"status": "skip", "reason": "couldn't be changed safely; add the configuration by hand"}
    if others_a != others_b or mine != [entry]:
        return {"status": "skip", "reason": "couldn't be changed safely; add the configuration by hand"}
    return {"status": status, "text": new}


def editor_action(project: Path, editor: str, entry: dict, script: Path) -> dict:
    """{id, name, path, state, action} for one editor's config file. Private `_text`: what to write."""
    rel = CONFIG_FILES[editor]
    path = project / rel
    out: dict[str, Any] = {"id": editor, "name": EDITOR_NAMES[editor], "path": str(path)}
    act: dict[str, Any] = {"path": str(path), "scope": "project"}
    text, problem = config_files.read_limited(path, MAX_JSON)
    if problem:
        return {**out, "state": "missing", "action": {**act, "kind": "skip", "detail": f"{rel} {problem}, so it's left alone."}}
    res = upsert_entry(text or "", ["configurations"] if editor == "vscode" else [], entry, script)
    st = res["status"]
    label = entry.get("name") or entry.get("label")
    if st == "current":
        return {**out, "state": "present", "action": {**act, "kind": "skip", "detail": f"“{label}” is already there."}}
    if st == "skip":
        return {**out, "state": "missing", "action": {**act, "kind": "skip", "detail": f"{rel} {res['reason']}, so it's left alone."}}
    state = "outdated" if st == "updated" else "missing"
    if text is None or not text.strip():
        kind, detail = "write", f"New file with “{label}”."
    elif st == "updated":
        kind, detail = "merge", f"Updates “{label}”; every other configuration stays as it is."
    else:
        kind, detail = "merge", f"Adds “{label}”; every other configuration stays as it is."
    preview = res["text"] if kind == "write" else config_files.unified_diff(path, text or "", res["text"])
    return {**out, "state": state, "action": {**act, "kind": kind, "detail": detail, "preview": preview[:8000]},
            "_text": res["text"]}


def pick_editors(editor: str | None) -> tuple[list[str], list[str]]:
    """(editors to configure, notes). --editor wins; else the remembered one; else every installed one."""
    from iosstudio.features.editor_setup import Env, preferred
    from iosstudio.features.editor_setup.editors import vscode_installed, zed_installed
    notes: list[str] = []
    if editor:
        if editor not in EDITOR_NAMES:
            raise HelperError("usage", EDITOR_USAGE)
        want = editor
    else:
        env = Env.from_os()
        want = preferred(env)
        if want is None:
            found = [e for e, f in (("vscode", vscode_installed), ("zed", zed_installed)) if f(env)]
            if not found:
                notes.append("Neither VS Code nor Zed is installed, so there's no debug configuration to write.")
            return found, notes
    if want == "neovim":
        return [], [NEOVIM_NOTE]
    return [want], notes


def _zed_dap_note() -> str | None:
    """Zed's Swift extension looks for lldb-dap on PATH; the toolchain keeps it next to swift."""
    import shutil
    if shutil.which("lldb-dap"):
        return None
    return ("Zed finds lldb-dap only on PATH, and the Swift toolchain keeps it next to swift. Set it once in Zed's "
            "settings: \"dap\": {\"Swift\": {\"binary\": \"<toolchain>/usr/bin/lldb-dap\"}} (see the Debug in "
            "editor guide).")


def project_identity(project: Path) -> dict:
    """{bundleId, appName} from xtool.yml / Package.swift."""
    from iosstudio.features.projects import model
    info = model.project_info(project)
    bid = info.get("bundleId")
    if not bid:
        raise HelperError("no-bundle-id", "This project has no bundle ID yet (xtool.yml sets it). Build it once in "
                          "iOS Studio first.", {"label": "Open Projects", "page": "projects"})
    return {"bundleId": str(bid), "appName": str(info.get("name") or info.get("product") or project.name)}


def editor_config(project: Path, editor: str | None = None, dry_run: bool = False, home: Path | None = None) -> dict:
    ident = project_identity(project)
    bid, app = ident["bundleId"], ident["appName"]
    script = script_path(bid)
    ids, notes = pick_editors(editor)
    editors = []
    for e in ids:
        entry = vscode_config(app, script, home) if e == "vscode" else zed_config(app, script, home)
        editors.append(editor_action(project, e, entry, script))
    applied = False
    if not dry_run:
        for ed in editors:
            if ed["action"]["kind"] in ("write", "merge") and "_text" in ed:
                path = Path(ed["path"])
                if ed["action"]["kind"] == "write" and path.exists() and path.read_text().strip():
                    ed["action"].update(kind="skip", detail="The file appeared meanwhile; it's left as it is.")
                    continue
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    config_files.write_keeping_mode(path, ed["_text"])
                except OSError as exc:
                    raise HelperError("write-failed", f"Couldn't write {appdirs.tilde(path)}: {exc.strerror}.") from None
                ed["action"]["done"] = True
                applied = True
        if editors and not running_bridge(bid):
            write_stub(bid, app)
    if "zed" in ids:
        n = _zed_dap_note()
        if n:
            notes.append(n)
    return {"project": str(project), "bundleId": bid, "appName": app, "configName": config_name(app),
            "editors": [{k: v for k, v in ed.items() if not k.startswith("_")} for ed in editors],
            "dryRun": dry_run, "applied": applied, "scriptPath": str(script), "notes": notes}


# ================================================================ the attach file

def _q(s: str) -> str:
    """A double-quoted lldb command argument."""
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def install_module() -> Path:
    """Copy the lldb commands next to the attach files (rewritten when they changed)."""
    dest = folder() / f"{MODULE_NAME}.py"
    text = LLDB_MODULE.read_text()
    try:
        if dest.read_text() == text:
            return dest
    except OSError:
        pass
    dest.parent.mkdir(parents=True, exist_ok=True)
    fileio.atomic_write(dest, text)
    return dest


def _header(app: str, bid: str) -> list[str]:
    return [f"# iOS Studio: attach the editor's debugger to {app} ({bid}) on your iPhone.",
            "# iOS Studio rewrites this file while \"Debug in editor\" runs; changes here are lost."]


def stub_text(bid: str, app: str) -> str:
    module = folder() / f"{MODULE_NAME}.py"
    msg = (f"iOS Studio isn't debugging {app} right now. Start 'Debug in editor' in iOS Studio first, then attach "
           "again.")
    return "\n".join(_header(app, bid) + [f"command script import {_q(str(module))}", f"iosstudio-fail {_q(msg)}"]) + "\n"


def attach_text(bid: str, app: str, *, port: int, host: str, pid: int, helper_pid: int, helper_start: str,
                local_exe: str | None, remote_path: str | None, sysroot: str | None,
                platform: str = "remote-ios") -> str:
    """What lldb_driver.Driver.connect/attach does, as lldb commands."""
    module = folder() / f"{MODULE_NAME}.py"
    lines = _header(app, bid) + [
        f"command script import {_q(str(module))}",
        f"iosstudio-check {int(helper_pid)} {helper_start}",
        # Otherwise every image that isn't in the sysroot is read from the app's memory (minutes over Wi-Fi).
        "settings set target.memory-module-load-level minimal",
        f"platform select {platform}" + (f" --sysroot {_q(sysroot)}" if sysroot else ""),
    ]
    if local_exe:
        lines.append(f"target create {_q(local_exe)}")
        if remote_path:
            lines.append(f"iosstudio-remote-file {_q(remote_path)}")
    lines += [f"iosstudio-connect connect://{host or '127.0.0.1'}:{int(port)}",
              f"process attach --pid {int(pid)}"]
    return "\n".join(lines) + "\n"


def write_stub(bid: str, app: str) -> Path:
    install_module()
    p = script_path(bid)
    fileio.atomic_write(p, stub_text(bid, app))
    prune()
    return p


def write_attach(bid: str, app: str, **kw) -> Path:
    install_module()
    p = script_path(bid)
    fileio.atomic_write(p, attach_text(bid, app, **kw))
    return p


def prune() -> None:
    """Keep the newest KEEP_FILES apps' attach files; a running bridge's always stay."""
    d = folder()
    if not d.is_dir():
        return
    live = {state_path(b["bundleId"]).stem for b in bridges()}
    files = [p for p in d.iterdir() if p.suffix in (".lldb", ".json")]
    stems = sorted({p.stem for p in files}, key=lambda s: max(q.stat().st_mtime for q in files if q.stem == s),
                   reverse=True)
    for s in stems[KEEP_FILES:]:
        if s in live:
            continue
        for q in files:
            if q.stem == s:
                q.unlink(missing_ok=True)


# ================================================================ running bridges

def proc_start(pid: int) -> str | None:
    """The process's start time (clock ticks since boot, /proc/<pid>/stat field 22): with the pid, it names one
    process even after the pid is reused."""
    try:
        with open(f"/proc/{int(pid)}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError, ValueError):
        return None


def save_state(bid: str, data: dict) -> None:
    fileio.save_json(state_path(bid), data)


def clear_state(bid: str) -> None:
    state_path(bid).unlink(missing_ok=True)


def _alive(d: Any) -> bool:
    return isinstance(d, dict) and bool(d.get("helperPid")) and proc_start(int(d["helperPid"])) == d.get("helperStart")


def bridges(bundle_id: str | None = None) -> list[dict]:
    """The bridges that are running now (dead ones' state files are ignored)."""
    d = folder()
    out = []
    if not d.is_dir():
        return out
    want = plain_bundle_id(bundle_id) if bundle_id else None
    for p in sorted(d.glob("*.json")):
        data = fileio.load_json(p)
        if not _alive(data):
            continue
        if want and plain_bundle_id(str(data.get("bundleId") or "")) != want:
            continue
        out.append({k: data.get(k) for k in ("bundleId", "installedBundleId", "appName", "project", "port", "host",
                                              "pid", "launched", "editor", "since", "helperPid", "scriptPath")})
    return out


def running_bridge(bundle_id: str) -> dict | None:
    hit = bridges(bundle_id)
    return hit[0] if hit else None


# ================================================================ attach detection

def tcp_peers(port: int, proc_net: Path = Path("/proc/net")) -> int:
    """Open loopback connections to `port` (the editor's lldb-dap talking to the forward), from /proc/net/tcp*:
    client sockets whose remote end is 127.0.0.1:<port> (or ::1 / ::ffff:127.0.0.1) and ESTABLISHED (01)."""
    n = 0
    want = f"{int(port):04X}"
    for name in ("tcp", "tcp6"):
        try:
            text = (proc_net / name).read_text()
        except OSError:
            continue
        for line in text.splitlines()[1:]:
            f = line.split()
            if len(f) < 4 or f[3] != "01":
                continue
            addr, _, rport = f[2].partition(":")
            if rport != want:
                continue
            if addr in ("0100007F", "00000000000000000000000001000000", "0000000000000000FFFF00000100007F"):
                n += 1
    return n


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def helper_identity() -> tuple[int, str]:
    pid = os.getpid()
    return pid, proc_start(pid) or "0"
