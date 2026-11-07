"""Run presets: named launch arguments, environment, language/region and a UI-test-mode flag saved per project
(stored by the helper in projects.json; see helper/projects.py `projects presets`). ios_project_presets lists
them (read-only); ios_project_preset_set saves, deletes (dry run unless confirm=true) and selects them; ios_run, ios_app_launch and
ios_app_run_and_capture take `preset=<name>` and launch with its resolved arguments (resolve_preset below).
"""
from __future__ import annotations

import json
from typing import Annotated, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field

from .. import helper
from ..backends.projects import resolve_project
from ..core import budget as B
from ..core.params import MaxBytes, ProjectPath
from ..core.registry import fit_page, tool
from ..models import Out
from ..state import get_state

VALUE_CUT = 300     # one argument or env value in a result
MAX_ITEMS = 64      # args / env entries shown per preset


class PresetIn(BaseModel):
    """A preset as an agent passes it to ios_project_preset_set(action='save')."""
    name: str = Field(min_length=1, max_length=40, description="Preset name, e.g. 'German' (unique per project, "
                                                               "ignoring case)")
    args: list[str] = Field(default_factory=list, description="Launch arguments, e.g. ['-ShowDebugMenu', 'YES']")
    env: dict[str, str] = Field(default_factory=dict, description="Environment variables, e.g. {'API_ENV': 'staging'}")
    language: str | None = Field(None, description="App language code, e.g. 'de' (adds -AppleLanguages (de))")
    region: str | None = Field(None, description="Locale, e.g. 'de_DE' (adds -AppleLocale de_DE)")
    ui_test_mode: bool = Field(False, description="Add the UI-test-mode launch argument")

    def to_helper(self) -> dict:
        return {"name": self.name, "args": list(self.args), "env": dict(self.env), "language": self.language or None,
                "region": self.region or None, "uiTestMode": bool(self.ui_test_mode)}


class ResolvedLaunch(Out):
    args: list[str] = Field(default_factory=list, description="Every launch argument the preset adds, in order")
    env: dict[str, str] = Field(default_factory=dict)


class PresetInfo(Out):
    name: str
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    language: str | None = None
    region: str | None = None
    ui_test_mode: bool = False
    selected: bool | None = Field(None, description="True on the project's selected preset (iOS Studio's Run "
                                                    "button uses it)")
    resolved: ResolvedLaunch | None = Field(None, description="What a launch with this preset passes: the args "
                                                              "(language, region and UI-test flags included) and env")

    @classmethod
    def from_helper(cls, d: dict, selected: str | None) -> "PresetInfo":
        res = d.get("resolved") if isinstance(d.get("resolved"), dict) else None
        return cls(name=str(d.get("name") or ""), args=_args(d.get("args")), env=_env(d.get("env")),
                   language=d.get("language") or None, region=d.get("region") or None,
                   ui_test_mode=bool(d.get("uiTestMode")), selected=True if selected and same_name(d.get("name"), selected) else None,
                   resolved=ResolvedLaunch(args=_args(res.get("args")), env=_env(res.get("env"))) if res else None)


class PresetsResult(B.Budgeted, Out):
    done: bool = Field(description="True when something was saved, deleted or selected; false for a list or a "
                                   "dry run")
    action: str
    message: str
    project: str
    path: str
    selected: str | None = Field(None, description="Name of the selected preset, if any")
    total: int = Field(0, description="Presets the project has")
    count: int = Field(0, description="Presets in this result")
    presets: list[PresetInfo] = Field(default_factory=list)
    replaced: bool | None = Field(None, description="save: true when an existing preset with this name was "
                                                    "overwritten")
    would: list[str] = Field(default_factory=list, description="Dry run: what confirm=true would do")
    next_steps: list[str] = Field(default_factory=list)


def _args(v) -> list[str]:
    items = [B.clip(x, VALUE_CUT) for x in (v or []) if x is not None] if isinstance(v, list) else []
    return items[:MAX_ITEMS] + ([f"…[+{len(items) - MAX_ITEMS} more]"] if len(items) > MAX_ITEMS else [])


def _env(v) -> dict[str, str]:
    if not isinstance(v, dict):
        return {}
    return {str(k): B.clip(val, VALUE_CUT) for k, val in list(v.items())[:MAX_ITEMS]}


def same_name(a, b) -> bool:
    """Preset names match ignoring case (as the helper does)."""
    return a is not None and b is not None and str(a).strip().casefold() == str(b).strip().casefold()


def _names(data: dict) -> list[str]:
    return [str(p.get("name")) for p in (data.get("presets") or []) if isinstance(p, dict) and p.get("name")]


def _more_note(k: int, total: int) -> str:
    return f"Showing {k} of {total} presets; pass name=<preset> for one of them, or raise max_bytes."


def register(mcp: MCPServer) -> None:

    @tool(mcp, "Run presets", kind="read")
    async def ios_project_presets(
        ctx: Context,
        project_path: ProjectPath,
        name: Annotated[str | None, Field(description="Only this preset")] = None,
        max_bytes: MaxBytes = None,
    ) -> PresetsResult:
        """Run presets of a project: saved launch arguments, environment variables, language/region and a
        UI-test-mode flag, like Xcode scheme options. Each preset has `resolved` (the exact args and env a
        launch gets), and the selected one (iOS Studio's Run button) is marked. Pass a preset's name as
        `preset` to ios_run, ios_app_launch or ios_app_run_and_capture to launch with it. Save, delete or
        select one with ios_project_preset_set. Read-only."""
        return await _presets(ctx, project_path, "list", None, name, None, False, max_bytes)

    @tool(mcp, "Save, delete or select a run preset", kind="destructive")
    async def ios_project_preset_set(
        ctx: Context,
        project_path: ProjectPath,
        action: Annotated[Literal["save", "delete", "select"], Field(
            description="save: add or replace `preset`. delete: remove `name` (dry run unless confirm=true). "
                        "select: make `name` the preset iOS Studio's Run button uses ('' clears it)")],
        preset: Annotated[PresetIn | None, Field(description="save: the preset, e.g. {'name': 'German', 'language': "
                                                             "'de', 'region': 'de_DE'}")] = None,
        name: Annotated[str | None, Field(description="delete/select: the preset's name")] = None,
        rename_from: Annotated[str | None, Field(description="save: the preset's old name when renaming it")] = None,
        confirm: Annotated[bool, Field(description="delete: false = dry run. true only after the user agreed")] = False,
        max_bytes: MaxBytes = None,
    ) -> PresetsResult:
        """Change a project's run presets (list them with ios_project_presets).

        - save: adds `preset`, or replaces the one with the same name (the result says replaced=true).
          Rename with rename_from=<old name>. Example, the app in German:
          ios_project_preset_set(project_path='WeatherGlance', action='save',
              preset={'name': 'German', 'language': 'de', 'region': 'de_DE'})
          then ios_run(project_path='WeatherGlance', preset='German').
          language adds `-AppleLanguages (de)`, region adds `-AppleLocale de_DE`.
        - delete: a dry run that changes nothing unless confirm=true; ask the user first.
        - select: the preset iOS Studio's Run button uses (name='' clears it). Agents pass preset= instead.
        Presets are stored in iOS Studio's settings, not in the project folder."""
        return await _presets(ctx, project_path, action, preset, name, rename_from, confirm, max_bytes)


async def _presets(ctx: Context, project_path: str, action: str, preset: PresetIn | dict | None, name: str | None,
                   rename_from: str | None, confirm: bool, max_bytes: int | None) -> PresetsResult:
    """list / save / delete / select, for ios_project_presets and ios_project_preset_set."""
    state = get_state(ctx)
    if isinstance(preset, dict):    # called directly rather than through the MCP argument validation
        preset = PresetIn.model_validate(preset)
    p = await resolve_project(state, project_path)
    path = str(p["path"])
    pname = str(p.get("name") or path)
    flags: dict = {"path": path}
    nxt: list[str] = []
    replaced, would, done = False, [], False
    if action == "save":
        if preset is None:
            raise ToolError("action='save' needs preset, e.g. {'name': 'German', 'language': 'de', "
                            "'region': 'de_DE'}.")
        before = await helper.call("projects", "presets", {"path": path})
        existing = _names(before if isinstance(before, dict) else {})
        if rename_from and not any(same_name(rename_from, n) for n in existing):
            raise ToolError(f"No preset named '{rename_from}' to rename. Presets of {pname}: "
                            f"{', '.join(existing) or 'none'}.")
        replaced = any(same_name(preset.name, n) for n in existing) and not same_name(preset.name, rename_from)
        flags["save"] = json.dumps(preset.to_helper(), ensure_ascii=False)
        flags["rename_from"] = rename_from or None
        data = await helper.call("projects", "presets", flags)
        done = True
        if replaced:
            msg = f"Replaced the existing preset '{preset.name}' of {pname} with this one."
        elif rename_from and rename_from != preset.name:   # also a change of case
            msg = f"Renamed preset '{rename_from}' of {pname} to '{preset.name}' and saved it."
        else:
            msg = f"Saved preset '{preset.name}' of {pname}."
        nxt.append(f"Launch with it: ios_run(project_path='{path}', preset='{preset.name}'), or "
                   f"ios_app_run_and_capture(project_path='{path}', preset='{preset.name}') for an installed app.")
    elif action == "delete":
        if not name:
            raise ToolError("action='delete' needs name (ios_project_presets(project_path) lists them).")
        flags["delete"] = name
        flags["dry_run"] = not confirm or None
        data = await helper.call("projects", "presets", flags)
        if confirm:
            done, msg = True, f"Deleted preset '{name}' of {pname}."
        else:
            would = [f"Delete the run preset '{name}' of {pname}"]
            if same_name(data.get("selected") if isinstance(data, dict) else None, name):
                would.append("It is the selected preset, so the Run button would use none")
            msg = (f"Dry run: this would delete the run preset '{name}'. Nothing was changed. Ask the user to "
                   "confirm, then call again with confirm=true.")
            nxt.append("Ask the user whether to proceed; only then repeat the call with confirm=true.")
    elif action == "select":
        if name is None:
            raise ToolError("action='select' needs name (or name='' to clear the selection).")
        flags["select"] = name
        data = await helper.call("projects", "presets", flags)
        done = True
        msg = f"Selected preset '{name}' for {pname}'s Run button." if name else \
            f"Cleared the selected preset of {pname}."
    else:
        data = await helper.call("projects", "presets", flags)
        msg = f"Run presets of {pname}."
    data = data if isinstance(data, dict) else {}
    if data.get("demo") is True and action in ("save", "delete", "select") and done:
        msg += " Demo mode: nothing is written to disk."
    selected = data.get("selected") or None
    raw = [d for d in (data.get("presets") or []) if isinstance(d, dict)]
    total = len(raw)
    if action == "list" and name:
        raw = [d for d in raw if same_name(d.get("name"), name)]
        if not raw:
            raise ToolError(f"No preset named '{name}'. Presets of {pname}: "
                            f"{', '.join(_names(data)) or 'none'}.")
    elif action == "save" and preset is not None:   # show the saved one first
        raw.sort(key=lambda d: not same_name(d.get("name"), preset.name))
    items = [PresetInfo.from_helper(d, selected) for d in raw]
    if action == "list" and not total:
        nxt.append(f"No presets yet. Save one with ios_project_preset_set(project_path='{path}', action='save', "
                   "preset={'name': 'German', 'language': 'de', 'region': 'de_DE'}).")
    elif action == "list":
        nxt.append(f"Launch with one: ios_run(project_path='{path}', preset='<name>'), or "
                   "ios_app_launch / ios_app_run_and_capture with preset='<name>'.")
    model = PresetsResult(done=done, action=action, message=msg, project=pname, path=str(data.get("path") or path),
                          selected=selected, total=total, count=len(items), presets=items,
                          replaced=replaced if action == "save" else None,
                          would=would, next_steps=nxt).with_budget(max_bytes)
    return fit_page(model, "presets", 0, lambda k, end: _more_note(k, len(items)))
