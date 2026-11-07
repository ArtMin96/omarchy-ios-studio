"""Screenshot baselines, visual diffs and screenshot sets over launch configs (helper/screenshots.py).

ios_screenshot_compare captures the iPhone's screen through the device daemon (as ios_screenshot does) and saves it
as a screen's baseline or compares it with the saved one: diff %, verdict, the changed regions and image paths. The
status bar is masked. Image bytes only with include_image=true (the diff image, small).

ios_screenshot_matrix launches the app once per launch config (run presets or inline language/region/args/env) and
captures each named screen, saving one image per config x name into a set folder with a manifest. It's a job of
this server (core/jobs.py, kind 'matrix'): the first call starts it and waits up to wait_seconds; call again with
job_id to wait more or page the shots; ios_job_cancel stops it. Partial results stay on disk; at the end the app is put back the way it was
(relaunched plainly if it was running before, else stopped).

ios_screenshot_baselines lists a project's baselines; ios_screenshot_baseline_delete deletes one (dry run unless
confirm=true).
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, ImageContent
from pydantic import BaseModel, Field

from .. import helper
from ..backends.device import apps, dcall, launch, match_app
from ..backends.projects import resolve_project
from ..core import budget as B
from ..core import deadline
from ..core.job_status import still_running, wait_with_progress
from ..core.jobs import Job
from ..core.params import MaxBytes, ProjectPath, Udid
from ..core.registry import MAX_WAIT, fit_page, to_result, tool
from ..models import Out, iso
from ..state import AppState, get_state

log = logging.getLogger("iosstudio.screenshots")

REGIONS_CAP = 10          # regions listed per compare (biggest first)
IMAGE_DIM = 512           # include_image: long edge of the inline diff image
IMAGE_BYTES = 80_000      # ... and its size cap
SHOTS_PAGE = 20           # matrix shots per page
HELPER_TIMEOUT = 90       # a pure-Python compare of a full-size screenshot takes a few seconds

ScreenName = Annotated[str, Field(min_length=1, max_length=60, description="Screen name the baseline is saved "
                                                                           "under, e.g. 'Home' or 'settings-dark'")]


# ------------------------------------------------------------------ models

class Region(Out):
    x: int
    y: int
    w: int
    h: int
    pixels: int = Field(0, description="Changed pixels inside")


class CompareResult(B.Budgeted, Out):
    name: str
    verdict: Literal["same", "changed", "no-baseline", "size-mismatch", "saved"]
    diff_percent: float | None = Field(None, description="Changed pixels as a percent of the compared ones "
                                                         "(status bar excluded)")
    threshold: float | None = None
    changed_pixels: int | None = None
    width: int | None = None
    height: int | None = None
    baseline_width: int | None = None
    baseline_height: int | None = None
    masked_top: int | None = Field(None, description="Pixels at the top ignored (status bar)")
    regions_total: int = 0
    regions: list[Region] = Field(default_factory=list, description=f"Changed areas in pixels, biggest first "
                                                                    f"(at most {REGIONS_CAP})")
    baseline_path: str | None = None
    current_path: str | None = Field(None, description="The screenshot just taken (a kept copy)")
    diff_path: str | None = Field(None, description="Current screenshot dimmed, changed pixels pink, regions "
                                                    "boxed yellow")
    baseline_created: str | None = None
    replaced: bool | None = Field(None, description="save: an older baseline with this name was overwritten")
    inline_image: bool = False
    message: str = ""
    next_steps: list[str] = Field(default_factory=list)


class BaselineInfo(Out):
    name: str
    path: str
    created: str | None = None
    width: int | None = None
    height: int | None = None
    device: str | None = None
    bundle: str | None = None


class BaselinesResult(B.Budgeted, Out):
    action: str
    done: bool = Field(description="True when a baseline was deleted; false for a list or a dry run")
    project: str
    message: str
    total: int = 0
    count: int = 0
    baselines: list[BaselineInfo] = Field(default_factory=list)
    would: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)


class ConfigIn(BaseModel):
    """An inline launch config for ios_screenshot_matrix (like a run preset, not saved)."""
    name: str | None = Field(None, max_length=40, description="Label and folder name, e.g. 'German' (default: the "
                                                              "language)")
    language: str | None = Field(None, description="App language, e.g. 'de' (-AppleLanguages (de))")
    region: str | None = Field(None, description="Locale, e.g. 'de_DE' (-AppleLocale de_DE)")
    args: list[str] = Field(default_factory=list, description="Launch arguments")
    env: dict[str, str] = Field(default_factory=dict, description="Environment variables")
    ui_test_mode: bool = False

    def to_helper(self) -> dict:
        return {"name": self.name, "language": self.language, "region": self.region, "args": list(self.args),
                "env": dict(self.env), "uiTestMode": self.ui_test_mode}


class NameIn(BaseModel):
    """A screen to capture after launch. press navigates there first (from the previous screen)."""
    name: str = Field(min_length=1, max_length=60)
    press: str | None = Field(None, max_length=200, description="Label or accessibility identifier to press first "
                                                                "(as ios_ui_press(label=...))")
    wait: float | None = Field(None, ge=0, le=30, description="Seconds to wait before the capture (default 1.5 "
                                                              "after a press, else 0)")


class MatrixShot(Out):
    config: str
    name: str
    path: str | None = None
    width: int | None = None
    height: int | None = None
    error: str | None = None


class MatrixStatus(B.Budgeted, Out):
    job_id: str = Field(description="The matrix's job: call ios_screenshot_matrix(job_id=...) to wait or page")
    state: Literal["running", "succeeded", "failed", "cancelled"]
    project: str
    bundle_id: str | None = None
    configs: list[str] = Field(default_factory=list, description="Config keys (folder names), in order")
    names: list[str] = Field(default_factory=list)
    total: int = Field(0, description="configs x names")
    captured: int = 0
    failed: int = 0
    current: str | None = Field(None, description="running: '<config>/<name>' being captured")
    elapsed_seconds: float = 0
    set_dir: str | None = None
    shots: list[MatrixShot] = Field(default_factory=list, description="This page of the shots (once finished)")
    next_offset: int | None = None
    app_restored: str | None = Field(None, description="What was done with the app at the end: relaunched, "
                                                        "stopped, or why not")
    error: str | None = None
    next_steps: list[str] = Field(default_factory=list)


# ------------------------------------------------------------------ helpers

def _iso(ts: Any) -> str | None:
    return iso(ts)


def _t(p: Any) -> str | None:
    return helper.tilde(str(p)) if p else None


def _regions(raw: Any) -> list[Region]:
    out = []
    for r in (raw or [])[:REGIONS_CAP]:
        if isinstance(r, dict):
            out.append(Region(x=int(r.get("x") or 0), y=int(r.get("y") or 0), w=int(r.get("w") or 0),
                              h=int(r.get("h") or 0), pixels=int(r.get("pixels") or 0)))
    return out


def _thumb(path: str | None) -> tuple[bytes | None, str | None]:
    if not path or not Path(path).is_file():
        return None, None
    try:
        img = B.fit_image(Path(path), IMAGE_DIM, IMAGE_BYTES, "jpeg", (70, 55, 40))
    except Exception:  # noqa: BLE001
        return None, None
    return (img.data, img.format) if img.fits else (None, None)


async def _capture(state: AppState, udid: str | None) -> dict:
    shot = await dcall(state, "screenshot", udid=udid, timeout=MAX_WAIT) or {}
    if not shot.get("path") or not Path(str(shot["path"])).is_file():
        raise ToolError("The iPhone returned no screenshot file. Retry with the phone unlocked and the screen on.")
    return shot


def compare_model(d: dict, saved: bool = False) -> CompareResult:
    """Helper `screenshots compare` / `save` data -> CompareResult (regions capped, paths ~-relative)."""
    if saved:
        return CompareResult(name=str(d.get("name")), verdict="saved", width=d.get("width"), height=d.get("height"),
                             baseline_path=_t(d.get("baseline_path")), baseline_created=_iso(d.get("created")),
                             replaced=bool(d.get("replaced")),
                             message=f"Saved baseline '{d.get('name')}' ({d.get('width')}x{d.get('height')})."
                                     + (" It replaced the older one." if d.get("replaced") else ""))
    verdict = d.get("verdict") or "changed"
    total = int(d.get("regions_total") or len(d.get("regions") or []))
    regions = _regions(d.get("regions"))
    pct = d.get("diff_percent")
    msg = {"same": f"Same as the baseline ({pct}% changed, threshold {d.get('threshold')}%).",
           "changed": f"Changed: {pct}% of the screen differs (threshold {d.get('threshold')}%) in {total} "
                      f"region(s); see diff_path.",
           "no-baseline": f"No baseline named '{d.get('name')}' yet; nothing to compare with.",
           "size-mismatch": f"The screenshot is {d.get('width')}x{d.get('height')} but the baseline is "
                            f"{d.get('baseline_width')}x{d.get('baseline_height')} (another iPhone or orientation)."
           }.get(verdict, "")
    cut = total - len(regions)
    return CompareResult(
        name=str(d.get("name")), verdict=verdict, diff_percent=pct, threshold=d.get("threshold"),
        changed_pixels=d.get("changed_pixels"), width=d.get("width"), height=d.get("height"),
        baseline_width=d.get("baseline_width"), baseline_height=d.get("baseline_height"),
        masked_top=d.get("masked_top"), regions_total=total, regions=regions,
        baseline_path=_t(d.get("baseline_path")), current_path=_t(d.get("current_path")),
        diff_path=_t(d.get("diff_path")), baseline_created=_iso(d.get("baseline_created")), message=msg,
        truncated=cut > 0, omitted=max(0, cut),
        note=f"{cut} smaller regions not listed; the diff image shows all of them." if cut > 0 else None)


# ------------------------------------------------------------------ matrix job

@dataclass
class Matrix:
    """A matrix's own state, kept in its job's data["matrix"]."""
    plan: dict
    project: str
    bundle: str
    udid: str | None
    settle: float
    current: str | None = None
    shots: list[dict] = field(default_factory=list)
    restored: str | None = None
    manifest_done: bool = False

    @property
    def total(self) -> int:
        return len(self.plan["configs"]) * len(self.plan["names"])


def _m(job: Job) -> Matrix:
    return job.data["matrix"]


def _at(job: Job, current: str | None) -> None:
    m = _m(job)
    m.current = current
    job.report(len(m.shots) / max(1, m.total), current)


async def _record(job: Job, cfg: str, name: str, shot: str | None = None, error: str | None = None) -> None:
    m = _m(job)
    flags: dict = {"project": m.plan["project"], "set": m.plan["set_id"], "config": cfg, "name": name}
    if shot:
        flags["shot"] = shot
    else:
        flags["error"] = B.clip(error or "Not captured.", 300)
    try:
        d = await helper.call("screenshots", "matrix-save", flags, timeout=HELPER_TIMEOUT) or {}
    except ToolError as exc:
        d = {"config": cfg, "name": name, "error": B.clip_error(str(exc))}
    m.shots.append(d)
    _at(job, m.current)


async def _restore(state: AppState, m: Matrix, was_running: bool | None, app_id: str | None) -> None:
    """Put the app back: relaunched plainly (no matrix args) if it ran before the matrix, else stopped."""
    if not app_id:
        m.restored = "not launched"
        return
    try:
        if was_running:
            await dcall(state, "app.launch", {"bundleId": app_id}, udid=m.udid, timeout=60)
            m.restored = "relaunched without the matrix's launch arguments (it was running before)"
        else:
            await dcall(state, "app.kill", {"bundleId": app_id}, udid=m.udid, timeout=20)
            m.restored = "stopped (it wasn't running before)"
    except (ToolError, Exception) as exc:  # noqa: BLE001
        m.restored = f"couldn't be restored: {B.clip_error(str(exc))}"


async def _run(state: AppState, job: Job) -> dict:
    m = _m(job)
    names = m.plan["names"]
    app_id, was_running = None, None
    end_state, error = "failed", None
    try:
        items = await apps(state, m.udid)
        a = match_app(items, m.bundle)
        if a is None:
            raise ToolError(f"No app '{m.bundle}' is installed on the iPhone. Install it with "
                            "ios_run(project_path) first.")
        app_id, was_running = a["bundleId"], bool(a.get("running"))
        for cfg in m.plan["configs"]:
            key = cfg["key"]
            _at(job, f"{key} (launching)")
            try:
                await launch(state, m.udid, app_id, cfg.get("args") or None, cfg.get("env") or None)
            except ToolError as exc:
                for n in names:
                    await _record(job, key, n["name"], error=f"Launch failed: {B.clip_error(str(exc))}")
                continue
            await asyncio.sleep(m.settle)
            for n in names:
                _at(job, f"{key}/{n['name']}")
                try:
                    if n.get("press"):
                        await dcall(state, "ui.press", {"label": n["press"], "match": "exact", "bundleId": app_id},
                                    udid=m.udid, timeout=40)
                    if n.get("wait"):
                        await asyncio.sleep(float(n["wait"]))
                    shot = await _capture(state, m.udid)
                except ToolError as exc:
                    await _record(job, key, n["name"], error=B.clip_error(str(exc)))
                    continue
                await _record(job, key, n["name"], shot=str(shot["path"]))
        end_state = "done"
        return {"set_dir": _t(m.plan.get("set_dir")), "captured": sum(1 for x in m.shots if x.get("path")),
                "total": m.total}
    except asyncio.CancelledError:
        end_state = "cancelled"
        raise
    except ToolError as exc:
        error = B.clip_error(str(exc))
        raise
    except Exception as exc:
        log.exception("screenshot matrix %s failed", job.id)
        error = B.clip_error(str(exc) or exc.__class__.__name__)
        raise ToolError(error) from None
    finally:
        m.current = None
        await asyncio.shield(_restore(state, m, was_running, app_id))
        await asyncio.shield(_finish(job, end_state, error))


async def _finish(job: Job, end_state: str, error: str | None) -> None:
    """Write how the matrix ended into its manifest (state, error, app_restored, finished)."""
    deadline.clear()       # may run from a done-callback: never under a tool call's budget
    m = _m(job)
    m.manifest_done = True
    flags = {"project": m.plan.get("project"), "set": m.plan.get("set_id"), "state": end_state,
             "error": error, "restored": m.restored}
    try:
        await helper.call("screenshots", "matrix-finish", flags, timeout=HELPER_TIMEOUT)
    except Exception as exc:  # noqa: BLE001  the shots are on disk either way
        log.warning("couldn't finish the manifest of %s: %s", job.id, exc)


def _on_done(job: Job, task: asyncio.Task) -> None:
    """A job cancelled before its first step never ran _run's finally: finish its manifest as cancelled."""
    if not _m(job).manifest_done:
        with contextlib.suppress(RuntimeError):
            asyncio.get_running_loop().create_task(_finish(job, "cancelled", None))


def matrix_status(job: Job, offset: int = 0, limit: int = SHOTS_PAGE, max_bytes: int | None = None
                  ) -> MatrixStatus:
    mx = _m(job)
    ok = [s for s in mx.shots if s.get("path")]
    err = (job.error or {}).get("message") if job.state == "failed" else None
    m = MatrixStatus(
        job_id=job.id, state=job.state if job.state != "needs_input" else "running", project=mx.project,
        bundle_id=mx.bundle, configs=[c["key"] for c in mx.plan["configs"]], names=[n["name"] for n in mx.plan["names"]],
        total=mx.total, captured=len(ok), failed=len(mx.shots) - len(ok), current=mx.current,
        elapsed_seconds=job.elapsed, set_dir=_t(mx.plan.get("set_dir")), app_restored=mx.restored,
        error=B.clip_error(str(err)) if err else None, full_output_path=_t(mx.plan.get("manifest_path")))
    if not job.finished:
        m.next_steps = [f"Running ({len(mx.shots)}/{mx.total}). " + still_running(job)]
        return m.with_budget(max_bytes)
    page = mx.shots[offset:offset + limit]
    m.shots = [MatrixShot(config=str(s.get("config")), name=str(s.get("name")), path=_t(s.get("path")),
                          width=s.get("width"), height=s.get("height"),
                          error=B.clip(s["error"], 200) if s.get("error") else None) for s in page]
    end = offset + len(page)
    m.next_offset = end if end < len(mx.shots) else None
    if m.next_offset is not None:
        m.truncated, m.omitted = True, len(mx.shots) - len(page)
        m.note = (f"Shots {offset}-{end - 1} of {len(mx.shots)}; call again with job_id and offset={end}, or read "
                  "the manifest at full_output_path.")
    m.next_steps = ["Compare one with ios_screenshot_compare(shot_path=<path>, name=...), or open set_dir."] \
        if ok and offset == 0 else []
    m.with_budget(max_bytes)
    return fit_page(m, "shots", offset, lambda k, e: f"Shots {offset}-{e - 1} of {len(mx.shots)}; call again "
                                                      f"with offset={e}, or read the manifest at full_output_path.")


# ------------------------------------------------------------------ tools

def register(mcp: MCPServer) -> None:

    @tool(mcp, "Compare screenshot with baseline", kind="write", open_world=True)
    async def ios_screenshot_compare(
        ctx: Context,
        project_path: ProjectPath,
        name: ScreenName,
        save_baseline: Annotated[bool, Field(description="Save this screenshot as the baseline of `name` "
                                                         "(replacing an older one) instead of comparing")] = False,
        threshold: Annotated[float, Field(ge=0, le=100, description="Percent of pixels that may change before the "
                                                                    "verdict is 'changed'")] = 0.1,
        tolerance: Annotated[int, Field(ge=0, le=255, description="Per-channel difference a pixel may have and still "
                                                                  "count as the same (compression noise)")] = 24,
        mask_top: Annotated[int | None, Field(ge=0, le=4000, description="Pixels at the top to ignore; default: the "
                                                                         "status bar (54 pt on Face ID iPhones)")] = None,
        include_image: Annotated[bool, Field(description="Also return the diff image (or the saved baseline), "
                                                         f"at most {IMAGE_DIM} px and {IMAGE_BYTES // 1000} KB")] = False,
        shot_path: Annotated[str | None, Field(description="Compare this PNG (e.g. a matrix shot) instead of taking "
                                                           "a screenshot")] = None,
        udid: Udid = None,
        max_bytes: MaxBytes = None,
    ) -> Annotated[CallToolResult, CompareResult]:
        """Visual regression check: take a screenshot of the iPhone and compare it with the saved baseline of
        this screen, or save it as that baseline (save_baseline=true). Returns diff_percent, a verdict (same /
        changed / no-baseline / size-mismatch), the changed regions in pixels (biggest first), and file paths:
        current_path (the screenshot), baseline_path and diff_path (the screenshot dimmed with changed pixels
        pink and regions boxed). The status bar (clock, battery) is ignored, and so is slight compression noise.
        No image bytes unless include_image=true. Typical loop: save_baseline=true before a change, then the
        same call without it after rebuilding. Baselines are per project; list them with ios_screenshot_baselines
        and delete one with ios_screenshot_baseline_delete."""
        state = get_state(ctx)
        p = await resolve_project(state, project_path)
        if shot_path:
            src = helper.expand(shot_path)
            if not src.is_file() or not (helper.inside_home(src) or helper.demo_mode()):
                raise ToolError(f"No screenshot at {helper.tilde(str(src))} (it must be a PNG inside your home "
                                "folder).")
            shot_file = str(src)
        else:
            shot_file = str((await _capture(state, udid))["path"])
        flags: dict = {"project": p["path"], "name": name, "shot": shot_file}
        if save_baseline:
            flags.update(device=udid or await state.active_udid(), bundle=p.get("bundleId"))
            model = compare_model(await helper.call("screenshots", "save", flags, timeout=HELPER_TIMEOUT) or {}, True)
            model.next_steps = [f"After the change, call ios_screenshot_compare(project_path, name='{model.name}')."]
            img_src = (await helper.call("screenshots", "list", {"project": p["path"]})) if include_image else None
            img_path = next((b["path"] for b in (img_src or {}).get("baselines") or []
                             if b.get("name") == model.name), None)
        else:
            flags.update(threshold=threshold, tolerance=tolerance,
                         mask_top="auto" if mask_top is None else mask_top)
            d = await helper.call("screenshots", "compare", flags, timeout=HELPER_TIMEOUT) or {}
            model = compare_model(d)
            img_path = d.get("diff_path") or (d.get("current_path") if model.verdict == "no-baseline" else None)
            if model.verdict == "no-baseline":
                model.next_steps = [f"Save one with ios_screenshot_compare(project_path, name='{model.name}', "
                                    "save_baseline=true)."]
            elif model.verdict == "size-mismatch":
                model.next_steps = ["Use the same iPhone and orientation as the baseline, or save a new baseline."]
            elif model.verdict == "changed" and not include_image:
                model.next_steps = ["include_image=true shows the diff (small); if the change is intended, save it "
                                    "with save_baseline=true."]
        model.with_budget(max_bytes)
        img, fmt = (None, None)
        if include_image and img_path:
            img, fmt = await asyncio.to_thread(_thumb, img_path)
            if img is None:
                model.note = ((model.note or "") + " The image couldn't be shrunk enough to send; open the path.").strip()
        model.inline_image = img is not None
        res = to_result(model)
        if img is not None:
            res.content.insert(0, ImageContent(type="image", data=base64.b64encode(img).decode(),
                                               mime_type=f"image/{fmt}"))
        return res

    @tool(mcp, "Screenshot baselines", kind="read")
    async def ios_screenshot_baselines(
        ctx: Context,
        project_path: ProjectPath,
        max_bytes: MaxBytes = None,
    ) -> BaselinesResult:
        """The saved screenshot baselines of a project: name, size, when, which iPhone. Baselines live in iOS
        Studio's data folder, not in the project; ios_screenshot_baseline_delete removes one. Read-only."""
        state = get_state(ctx)
        p = await resolve_project(state, project_path)
        pname = str(p.get("name") or p["path"])
        d = await helper.call("screenshots", "list", {"project": p["path"]}) or {}
        items = [BaselineInfo(name=str(b.get("name")), path=_t(b.get("path")) or "", created=_iso(b.get("created")),
                              width=b.get("width"), height=b.get("height"), device=b.get("device"),
                              bundle=b.get("bundle")) for b in d.get("baselines") or [] if isinstance(b, dict)]
        model = BaselinesResult(action="list", done=False, project=pname, total=len(items), count=len(items),
                                baselines=items, message=f"{len(items)} baseline(s) of {pname}.",
                                next_steps=[] if items else
                                ["Save one with ios_screenshot_compare(project_path, name='Home', save_baseline=true)."]
                                ).with_budget(max_bytes)
        return fit_page(model, "baselines", 0, lambda k, e: f"Showing {k} of {len(items)}; raise max_bytes.")

    @tool(mcp, "Delete a screenshot baseline", kind="destructive", idempotent=True)
    async def ios_screenshot_baseline_delete(
        ctx: Context,
        project_path: ProjectPath,
        name: Annotated[str, Field(min_length=1, description="The baseline's name (ios_screenshot_baselines lists "
                                                             "them)")],
        confirm: Annotated[bool, Field(description="false = dry run. true only after the user agreed")] = False,
    ) -> BaselinesResult:
        """Delete one screenshot baseline of a project and its kept compare runs. A dry run that changes nothing
        unless confirm=true; ask the user first."""
        state = get_state(ctx)
        p = await resolve_project(state, project_path)
        pname = str(p.get("name") or p["path"])
        flags: dict = {"project": p["path"], "name": name, "confirm": True if confirm else None}
        d = await helper.call("screenshots", "delete", flags) or {}
        if not confirm:
            return BaselinesResult(action="delete", done=False, project=pname,
                                   message=f"Dry run: this would delete the baseline '{d.get('name')}' of {pname}. "
                                           "Nothing was changed. Ask the user, then call again with confirm=true.",
                                   would=[f"Delete {_t(d.get('baseline_path'))} and its details"] +
                                   ([f"Delete its {d['compare_runs']} kept compare run(s) (screenshot + diff, "
                                     f"{int(d.get('compare_bytes') or 0) // 1000} KB)"] if d.get("compare_runs") else []),
                                   next_steps=["Ask the user whether to proceed; only then repeat with "
                                               "confirm=true."]).with_budget(None)
        return BaselinesResult(action="delete", done=True, project=pname,
                               message=f"Deleted the baseline '{d.get('name')}' of {pname}"
                                       + (f" and {d['compare_runs']} compare run(s)" if d.get("compare_runs") else "")
                                       + ".").with_budget(None)

    @tool(mcp, "Screenshot matrix", kind="write", open_world=True)
    async def ios_screenshot_matrix(
        ctx: Context,
        project_path: Annotated[str | None, Field(description="Project folder or name (needed to start; not with "
                                                              "job_id)")] = None,
        presets: Annotated[list[str] | None, Field(description="Run preset names of the project (ios_project_presets), "
                                                               "one launch each")] = None,
        configs: Annotated[list[ConfigIn] | None, Field(description="Inline launch configs, e.g. [{'name': 'French', "
                                                                    "'language': 'fr', 'region': 'fr_FR'}]")] = None,
        names: Annotated[list[str | NameIn] | None, Field(description="Screens to capture after each launch, in order, "
                                                                      "e.g. ['Home', {'name': 'Settings', 'press': "
                                                                      "'Settings'}]; default ['launch']")] = None,
        bundle_id: Annotated[str | None, Field(description="Default: the project's bundle id")] = None,
        settle_seconds: Annotated[float, Field(ge=0, le=30, description="Wait after each launch before the first "
                                                                        "capture")] = 3.0,
        job_id: Annotated[str | None, Field(description="Follow up a matrix started earlier (its job_id)")] = None,
        wait_seconds: Annotated[float, Field(ge=0, le=MAX_WAIT)] = MAX_WAIT,
        offset: Annotated[int, Field(ge=0, description="Finished: first shot of the page")] = 0,
        udid: Udid = None,
        max_bytes: MaxBytes = None,
    ) -> MatrixStatus:
        """Screenshot set over launch configs, e.g. every screen in several languages for App Store
        screenshots or a localization check. For each config (run presets by name and/or inline configs with
        language, region, args, env; presets first) the installed app is launched with it, and after
        settle_seconds each name in `names` is captured in order; a name with press=<label> presses that
        element first (as ios_ui_press), so screens are reached by chaining presses from the launch screen.
        One PNG per config x name in a set folder plus manifest.json (full_output_path). At most 12 configs
        and 12 names. A background job: returns state='running' and a job_id; call again with it to wait or
        page shots with offset; ios_job_cancel stops it. Partial results stay. At the end the app is relaunched
        plainly if it was running before, else stopped. Needs the app installed (ios_run) and the phone
        unlocked; it relaunches the app, so unsaved state in it is lost."""
        state = get_state(ctx)
        if job_id:
            job = state.jobs.get(job_id, "matrix")
        else:
            if not project_path:
                raise ToolError("Pass project_path to start a matrix (or job_id to follow one).")
            p = await resolve_project(state, project_path)
            bundle = bundle_id or p.get("bundleId")
            if not bundle:
                raise ToolError("The project has no bundle id; pass bundle_id.")
            cfg = [{"preset": n} for n in presets or []] + [c.to_helper() if isinstance(c, ConfigIn) else c
                                                             for c in configs or []]
            nms = [n.model_dump(exclude_none=True) if isinstance(n, NameIn) else n for n in names or []]
            plan = await helper.call("screenshots", "matrix-plan", {
                "project": p["path"], "configs": json.dumps(cfg, ensure_ascii=False) if cfg else None,
                "names": json.dumps(nms, ensure_ascii=False) if nms else None, "bundle": bundle})
            mx = Matrix(plan or {}, str(p.get("name") or p["path"]), bundle, udid, settle_seconds)
            job = state.jobs.run(lambda j: _run(state, j), "matrix", title=f"Screenshot matrix of {mx.project}",
                                 tool="ios_screenshot_matrix", data={"matrix": mx})
            job.task.add_done_callback(lambda t, j=job: _on_done(j, t))
        await wait_with_progress(state, job, wait_seconds, ctx)
        return matrix_status(job, offset, SHOTS_PAGE, max_bytes)
