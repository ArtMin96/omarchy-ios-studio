"""Pydantic output models. Every tool returns one of these, so clients get an outputSchema,
structuredContent and a JSON text block. Keep fields short and agent-meaningful (paths, bundle ids,
file:line), and convert the helper's camelCase payloads with the `from_*` constructors."""
from __future__ import annotations

import os
import time
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field, field_validator

from .budget import ERROR_CUT, LIST_CAP, Budgeted, clip, clip_data
from .helper import tilde


def iso(ts: Any) -> str | None:
    """Unix time (or the helper's {"ts": ...}) -> local ISO 8601 string."""
    if isinstance(ts, dict):
        ts = ts.get("ts")
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(float(ts))) if ts else None
    except (TypeError, ValueError):
        return None


class Out(BaseModel):
    model_config = {"extra": "ignore"}


# ------------------------------------------------------------------ shared

class ActionResult(Out):
    done: bool = Field(description="False for a dry run")
    message: str
    would: list[str] = Field(default_factory=list, description="Dry run: what confirm=true would do")
    data: dict[str, Any] | None = None
    next_steps: list[str] = Field(default_factory=list)

    @field_validator("message")
    @classmethod
    def _msg(cls, v: str) -> str:
        return clip(v, 600)

    @field_validator("would")
    @classmethod
    def _would(cls, v: list[str]) -> list[str]:
        out = [clip(x, ERROR_CUT) for x in v[:LIST_CAP]]
        return out + ([f"+{len(v) - LIST_CAP} more"] if len(v) > LIST_CAP else [])

    @field_validator("data")
    @classmethod
    def _data(cls, v: dict | None) -> dict | None:
        return clip_data(v) if v else v


class DiagGroup(Out):
    severity: str = Field(description="error | warning | note")
    category: str | None = Field(None, description="compiler | linker | package | signing | device | toolchain | "
                                                   "config | other")
    message: str
    count: int = 1
    locations: list[str] = Field(default_factory=list, description="First 'file:line:col', relative to the project "
                                                                   "(none for location-less problems)")
    more_locations: int = 0
    hint: str | None = Field(None, description="What usually fixes it, naming the tool to use where there is one")
    details: list[str] = Field(default_factory=list, description="A few lines of context, such as the undefined "
                                                                 "symbols and who references them")


class BuildBrief(Out):
    kind: str | None = None
    config: str | None = None
    result: str | None = Field(None, description="ok | failed | cancelled")
    time: str | None = None
    duration_seconds: float | None = None
    errors: int = 0
    warnings: int = 0
    error_code: str | None = None
    error_message: str | None = None
    log_file: str | None = None
    ipa: str | None = None

    @classmethod
    def from_helper(cls, b: dict | None) -> "BuildBrief | None":
        if not b:
            return None
        return cls(kind=b.get("kind"), config=b.get("config"), result=b.get("result"), time=iso(b.get("time")),
                   duration_seconds=b.get("duration"), errors=int(b.get("errors") or 0),
                   warnings=int(b.get("warnings") or 0), error_code=b.get("errorCode"),
                   error_message=b.get("errorMessage"), log_file=b.get("logFile"), ipa=b.get("ipa"))

    @field_validator("error_message")
    @classmethod
    def _err(cls, v: str | None) -> str | None:
        return clip(" ".join(v.split()), ERROR_CUT) if v else v


class SigningBrief(Out):
    expires: str | None = None
    days_left: float | None = None
    level: str | None = Field(None, description="ok | warn (<2 days) | error (expired)")
    udid: str | None = None
    via: str | None = None

    @classmethod
    def from_helper(cls, s: dict | None) -> "SigningBrief | None":
        if not s:
            return None
        return cls(expires=iso(s.get("expires")), days_left=s.get("daysLeft"), level=s.get("level"),
                   udid=s.get("udid"), via=s.get("via"))


# ------------------------------------------------------------------ projects

class ProjectBrief(Out):
    name: str
    path: str = Field(description="Absolute project folder; pass it as project_path to other tools")
    bundle_id: str | None = None
    ios_min: str | None = None
    branch: str | None = None
    auto_renew: bool = False
    last_build: BuildBrief | None = None
    signing: SigningBrief | None = None

    @classmethod
    def from_helper(cls, p: dict) -> "ProjectBrief":
        return cls(name=str(p.get("name")), path=str(p.get("path")), bundle_id=p.get("bundleId"),
                   ios_min=p.get("iosMin"), branch=p.get("branch"), auto_renew=bool(p.get("autoRenew")),
                   last_build=BuildBrief.from_helper(p.get("lastBuild")),
                   signing=SigningBrief.from_helper(p.get("signing")))


def _sources_dir(path: Any, product: Any) -> str | None:
    """Sources/<Product> when it exists; a package that puts its target elsewhere (path: in Package.swift,
    e.g. a generated adapter) gets None rather than a folder that isn't there."""
    d = f"{path}/Sources/{product}"
    return d if product and os.path.isdir(d) else None


class ProjectDetail(ProjectBrief):
    package: str | None = None
    product: str | None = None
    generated: bool = False
    note: str | None = None
    ipa: str | None = Field(None, description="Last signed .ipa, if one exists")
    icon: str | None = None
    sources: str | None = Field(None, description="Folder holding the app's Swift sources")
    next_steps: list[str] = Field(default_factory=list)

    @classmethod
    def from_helper(cls, p: dict, next_steps: list[str] | None = None) -> "ProjectDetail":
        base = ProjectBrief.from_helper(p).model_dump()
        product = p.get("product")
        return cls(**base, package=p.get("package"), product=product, generated=bool(p.get("generated")),
                   note=p.get("note"), ipa=p.get("ipa"), icon=p.get("icon"),
                   sources=_sources_dir(p.get("path"), product),
                   next_steps=next_steps or [])


class ProjectChange(Out):
    done: bool
    message: str
    project: ProjectDetail | None = None
    next_steps: list[str] = Field(default_factory=list)


class Convertible(Out):
    name: str
    path: str
    reason: str | None = None


class ProjectList(Budgeted, Out):
    total: int
    count: int
    offset: int
    has_more: bool
    next_offset: int | None = None
    projects: list[ProjectBrief]
    convertible: list[Convertible] = Field(default_factory=list,
                                           description="SwiftUI packages that ios_project_convert can turn into apps")
    missing: list[str] = Field(default_factory=list, description="Added projects whose folders are gone")
    roots: list[str] = Field(default_factory=list, description="Folders scanned for projects")
    default_dir: str | None = Field(None, description="Where ios_project_create puts new apps by default")
    next_steps: list[str] = Field(default_factory=list)


class Template(Out):
    id: str
    title: str | None = None
    description: str | None = None
    default: bool = False
    files: list[str] = Field(default_factory=list)


class TemplateList(Out):
    templates: list[Template]
    bundle_prefix: str
    default_dir: str


# ------------------------------------------------------------------ builds

class PromptOption(Out):
    value: str
    label: str | None = None
    destructive: bool = False


class PendingPrompt(Out):
    id: str
    kind: str | None = None
    title: str | None = None
    message: str
    options: list[PromptOption] = Field(default_factory=list)
    default: str | None = None
    destructive: bool = Field(False, description="True if any option is destructive: ask the human first")


class StepInfo(Out):
    id: str
    title: str
    state: str
    detail: str | None = None
    owner: str | None = Field(None, description="'ui' means the app (or the device client) does this step")


class ErrorInfo(Out):
    code: str | None = None
    message: str


class BuildResult(Out):
    ipa: str | None = None
    app: str | None = None
    bundle_id: str | None = None
    udid: str | None = None
    via: str | None = None
    installed: bool = False
    needs_install: bool = Field(False, description="The build was signed for Wi-Fi install, which still has to happen")
    launched: bool = False
    pid: int | None = None
    profile_expires: str | None = None
    duration_seconds: float | None = None


class BuildStatus(Out):
    build_id: str
    kind: str = Field(description="build | toolchain-install | renew")
    action: str
    title: str | None = None
    project: str | None = None
    project_path: str | None = None
    state: Literal["running", "needs_input", "succeeded", "failed", "cancelled"]
    view: Literal["full", "delta"] = Field("full", description="delta: only what changed since your last poll")
    step: str | None = None
    progress: float | None = Field(None, description="0..1")
    progress_detail: str | None = None
    eta_seconds: float | None = None
    elapsed_seconds: float
    errors: int = 0
    warnings: int = 0
    new_errors: int = 0
    new_warnings: int = 0
    new_log_lines: list[str] = Field(default_factory=list, description="Newest log lines since your last poll")
    new_log_count: int = 0
    diagnostics: list[DiagGroup] = Field(default_factory=list, description="Top grouped diagnostics, errors first")
    diagnostics_total: int = 0
    steps: list[StepInfo] = Field(default_factory=list)
    pending_prompt: PendingPrompt | None = None
    result: BuildResult | None = None
    error: ErrorInfo | None = None
    log_lines: int = 0
    log_file: str | None = None
    next_steps: list[str] = Field(default_factory=list)


class DiagnosticsPage(Budgeted, Out):
    source: str = Field(description="A live build_id or the build history")
    project: str | None = None
    build_result: str | None = None
    total: int = Field(description="Groups after the severity filter")
    count: int
    offset: int
    has_more: bool
    next_offset: int | None = None
    errors: int
    warnings: int
    notes: int = 0
    categories: dict[str, int] = Field(default_factory=dict, description="Errors and warnings per category")
    diagnostics: list[DiagGroup]
    next_steps: list[str] = Field(default_factory=list)


class LogPage(Budgeted, Out):
    BULK: ClassVar[tuple[str, ...]] = ("lines",)
    source: str
    log_file: str | None = None
    total_lines: int = Field(description="Lines in the log")
    matched: int | None = Field(None, description="Lines matching grep/level (when filtering)")
    start_line: int = Field(description="First returned line number (0-based)")
    end_line: int = Field(description="Line number after the last returned line")
    count: int
    has_more: bool = Field(description="More lines exist in the paging direction")
    next_offset: int | None = Field(None, description="Pass as offset for the following lines")
    before: int | None = Field(None, description="Pass as before= for the lines above these")
    lines: list[str] = Field(default_factory=list, description="'<n>: <line>'")


class HistoryEntry(BuildBrief):
    project: str | None = None
    path: str | None = None
    udid: str | None = None
    via: str | None = None

    @classmethod
    def from_entry(cls, b: dict) -> "HistoryEntry":
        return cls(**BuildBrief.from_helper(b).model_dump(), project=b.get("project"), path=b.get("path"),
                   udid=b.get("udid"), via=b.get("via"))


class BuildHistory(Budgeted, Out):
    total: int
    builds: list[HistoryEntry]
    has_more: bool = False


# ------------------------------------------------------------------ status / toolchain / account

class Problem(Out):
    area: str = Field(description="toolchain | account | device | signing | build")
    message: str
    fix: str = Field(description="What to do: a tool to call, or a step only the human can do")


class DeviceBrief(Out):
    udid: str
    name: str | None = None
    model: str | None = None
    ios_version: str | None = None
    connection: str | None = None
    transport: str | None = None
    developer_mode: bool | None = None
    source: str = Field(description="'device-client' (live) or 'last-known' (cached state file)")
    updated: str | None = None


class StatusReport(Out):
    ready: bool = Field(description="True when toolchain, Apple ID and iPhone are all ready to build and run")
    toolchain: str
    toolchain_ready: bool
    signed_in: bool
    apple_id: str | None = None
    team_id: str | None = None
    device: DeviceBrief | None = None
    projects: int
    project_names: list[str] = Field(default_factory=list)
    last_build: HistoryEntry | None = None
    running_jobs: list[str] = Field(default_factory=list)
    expiring: list[str] = Field(default_factory=list, description="Apps whose signing expires within 2 days")
    problems: list[Problem] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)


class SetupStep(Out):
    id: str
    title: str
    group: str | None = None
    state: str = Field(description="done | missing | broken | optional | unknown")
    detail: str | None = None
    essential: bool = False
    can_auto_install: bool = False
    needs_root: bool = False
    size: str | None = None
    time: str | None = None
    human_step: str | None = Field(None, description="Set when only the human can do it (page in the app)")


class ToolchainCheck(Out):
    ready: bool
    title: str
    done: int
    total: int
    essential_missing: list[str]
    auto_installable: list[str]
    steps: list[SetupStep]
    next_steps: list[str] = Field(default_factory=list)


class ToolInfo(Out):
    id: str
    name: str
    version: str | None = None
    status: str | None = None
    detail: str | None = None


class DiskItem(Out):
    id: str
    name: str
    size_mb: float
    cleanable: bool
    safe: bool
    note: str | None = None


class ToolchainInfo(Out):
    tools: list[ToolInfo]
    sdks: dict[str, Any] = Field(default_factory=dict)
    disk: list[DiskItem] = Field(default_factory=list)
    disk_total_mb: float | None = None
    reclaimable_mb: float | None = None
    free_mb: float | None = None
    disk_cached_at: str | None = None
    next_steps: list[str] = Field(default_factory=list)


class Certificate(Out):
    name: str | None = None
    kind: str | None = None
    expires: str | None = None
    days_left: float | None = None


class Profile(Out):
    bundle_id: str | None = Field(None, description="The app's bundle id (free teams prefix it with XTL-<TEAM>.)")
    registered_bundle_id: str | None = None
    expires: str | None = None
    days_left: float | None = None
    devices: int = 0


class AccountStatus(Out):
    signed_in: bool
    apple_id: str | None = None
    team_id: str | None = None
    team_name: str | None = None
    team_type: str | None = Field(None, description="free | paid | unknown")
    token_days_left: float | None = None
    has_local_signing_key: bool = False
    limits: dict[str, Any] | None = Field(None, description="Free-team limits (7-day profiles, 3 apps per device…)")
    counts: dict[str, Any] | None = None
    health: str | None = None
    certificates: list[Certificate] = Field(default_factory=list)
    certificates_total: int = 0
    profiles: list[Profile] = Field(default_factory=list, description="Soonest expiry first, at most 20")
    profiles_total: int = 0
    details_fetched: str | None = Field(None, description="When the Apple developer details were last fetched")
    details_stale: bool = False
    errors: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)


class ProjectSigning(Out):
    name: str
    path: str
    bundle_id: str | None = None
    signing: SigningBrief | None = None
    auto_renew: bool = False


class RenewalDue(Out):
    path: str | None = None
    days_left: float | None = None
    due: bool | None = None


class SigningStatus(Out):
    team_type: str | None = None
    limits: dict[str, Any] | None = None
    projects: list[ProjectSigning] = Field(description="Soonest expiry first, at most 20")
    projects_total: int = 0
    profiles: list[Profile] = Field(default_factory=list)
    profiles_total: int = 0
    autorenew_timer: dict[str, Any] = Field(default_factory=dict)
    renewal_due: list[RenewalDue] = Field(default_factory=list)
    renewal_due_total: int = 0
    next_steps: list[str] = Field(default_factory=list)


def short(path: str | None) -> str | None:
    return tilde(path)


# ------------------------------------------------------------------ device and apps (tools/device.py)

class DeviceEntry(Out):
    udid: str
    name: str | None = None
    model: str | None = None
    ios_version: str | None = None
    state: str | None = Field(None, description="connected | connecting | locked | untrusted | not-paired | "
                                                "unreachable | searching | idle | error")
    transport: str | None = Field(None, description="usb | wifi | null (not connected right now)")
    usb_paired: bool = False
    wifi_paired: bool = False
    active: bool = Field(False, description="Device commands default to the active iPhone")


class DeviceList(Out):
    devices: list[DeviceEntry]
    active_udid: str | None = None
    connection: str | None = Field(None, description="The active iPhone's connection message")
    next_steps: list[str] = Field(default_factory=list)


class DeviceDetails(Out):
    udid: str
    name: str | None = None
    model: str | None = None
    product_type: str | None = None
    ios_version: str | None = None
    build: str | None = None
    transport: str | None = None
    connection: str | None = None
    battery_percent: int | None = None
    charging: bool | None = None
    storage_free_gb: float | None = None
    storage_total_gb: float | None = None
    developer_mode: bool | None = None
    ddi_mounted: bool | None = Field(None, description="Developer disk image; needed to launch apps and logs")
    locked: bool | None = None
    wifi_paired: bool | None = None
    usb_paired: bool | None = None
    next_steps: list[str] = Field(default_factory=list)


class DevicePrepare(Out):
    ready: bool = Field(description="True when Developer Mode is on and the developer disk image is mounted")
    developer_mode: bool | None = None
    ddi_mounted: bool | None = None
    actions: list[str] = Field(default_factory=list, description="What this call did")
    human_steps: list[str] = Field(default_factory=list, description="Steps only the user can do on the iPhone")
    next_steps: list[str] = Field(default_factory=list)


class AppBrief(Out):
    name: str
    bundle_id: str = Field(description="Id as installed; pass it to the app tools (free teams: XTL-<TEAM>.<id>)")
    version: str | None = None
    developer: bool = Field(False, description="Signed with a development profile (your own apps, e.g. from xtool)")
    running: bool = False
    pid: int | None = None


class AppList(Budgeted, Out):
    scope: str
    total: int
    count: int
    offset: int
    has_more: bool
    next_offset: int | None = None
    apps: list[AppBrief]
    own_apps: int = Field(description="Development-signed apps on the phone (free teams may have at most 3)")
    next_steps: list[str] = Field(default_factory=list)


class AppInstallResult(Out):
    installed: bool
    bundle_id: str | None = Field(None, description="Id as installed (free teams: XTL-<TEAM>.<id>)")
    package_bundle_id: str | None = None
    path: str
    seconds: float | None = None
    next_steps: list[str] = Field(default_factory=list)


class AppLaunchResult(Out):
    bundle_id: str
    pid: int | None = None
    console_session_id: str | None = Field(None, description="console=true: read its print() output with "
                                                             "ios_app_console_read")
    console_error: str | None = Field(None, description="console=true: why the output isn't captured")
    next_steps: list[str] = Field(default_factory=list)


class ProcessInfo(Out):
    pid: int
    name: str
    bundle_id: str | None = None
    is_app: bool = False


class ProcessList(Budgeted, Out):
    total: int
    count: int
    processes: list[ProcessInfo]


class ScreenshotInfo(Out):
    path: str = Field(description="The downscaled image (the one sent inline)")
    original: str = Field(description="Full-resolution PNG from the iPhone")
    width: int
    height: int
    original_width: int | None = None
    original_height: int | None = None
    format: str
    bytes: int
    quality: int | None = None
    inline: bool = Field(True, description="False: no image in this result, open `path`")
    note: str | None = None


class LocationResult(Out):
    active: bool
    lat: float | None = None
    lon: float | None = None
    message: str


# ------------------------------------------------------------------ logs (tools/logs.py)

class LogSession(Out):
    session_id: str
    running: bool
    filters: dict[str, Any]
    cursor: int = Field(description="Pass to ios_logs_read; entries are numbered from 0")
    next_steps: list[str] = Field(default_factory=list)


class LogRead(Budgeted, Out):
    BULK: ClassVar[tuple[str, ...]] = ("lines",)
    session_id: str
    running: bool
    count: int
    next_cursor: int = Field(description="Pass as cursor on the next read")
    has_more: bool = Field(description="More is buffered past next_cursor")
    buffer_start: int = Field(description="Oldest seq still buffered")
    missed: int = Field(0, description="Overwritten before you read them")
    dropped: int = Field(0, description="Dropped because the phone logged faster than the stream")
    skipped: int = Field(0, description="Older matching lines skipped by newest=true")
    total_received: int
    lines: list[str] = Field(default_factory=list, description="'<seq> <time> <level> "
                             "<process>[<pid>] <subsystem>: <message>'")
    error: str | None = None
    next_steps: list[str] = Field(default_factory=list)


# ------------------------------------------------------------------ crashes (tools/crashes.py)

class CrashBrief(Out):
    name: str = Field(description="Pass to ios_crash_get")
    process: str | None = None
    date: str | None = Field(None, description="When it was written, in the iPhone's local time (ISO; includes the "
                                               "UTC offset when the phone's time zone is known)")
    kind: str | None = Field(None, description="crash | fault | resource | jetsam | hang | other")


class CrashList(Budgeted, Out):
    total: int
    count: int
    has_more: bool = False
    crashes: list[CrashBrief]
    next_steps: list[str] = Field(default_factory=list)


class CrashFrame(BaseModel):
    """One of the app's own frames resolved against the build that crashed (crash symbolication)."""
    index: int = Field(description="Frame number in the crashed thread")
    function: str | None = None
    file: str | None = Field(None, description="Source file, relative to the project")
    line: int | None = None
    column: int | None = None


class CrashDetail(Budgeted, Out):
    BULK: ClassVar[tuple[str, ...]] = ("text",)
    name: str
    path: str = Field(description="Local copy of the .ips report")
    resource_uri: str | None = None
    process: str | None = None
    bundle_id: str | None = None
    app_version: str | None = None
    os_version: str | None = None
    date: str | None = None
    exception_type: str | None = None
    signal: str | None = None
    termination_reason: str | None = None
    crashed_thread: int | None = None
    frames: list[str] = Field(default_factory=list, description="Top frames of the crashed thread; app frames "
                                                                "without symbols read '<image> + 0xOFFSET'")
    size: int | None = Field(None, description="Bytes in the .ips file")
    section: str = "summary"
    start_line: int | None = None
    end_line: int | None = None
    total_lines: int | None = None
    text: str | None = Field(None, description="The section's lines")
    symbolicated: Literal["full", "partial", "none", "not-needed"] | None = Field(
        None, description="full: every app frame of the crashed thread resolved to file:line; partial: some, or "
                          "names only; none: no matching build (see symbolication_note); not-needed: no app frames")
    symbolication_note: str | None = Field(None, description="Why symbolication is partial or none, and what to do")
    project_path: str | None = Field(None, description="Project the matching build came from; app_frames' files "
                                                       "are relative to it")
    app_frames: list[CrashFrame] = Field(default_factory=list, description="The crashed thread's app frames "
                                                                           "with function, file and line")
    next_steps: list[str] = Field(default_factory=list)


class RunCapture(Budgeted, Out):
    BULK: ClassVar[tuple[str, ...]] = ("key_lines",)
    bundle_id: str
    launched: bool
    pid: int | None = None
    running_at_end: bool | None = Field(None, description="Whether the process was still alive when capture ended")
    launch_error: str | None = None
    installed: AppInstallResult | None = None
    seconds: float
    log_counts: dict[str, int] = Field(default_factory=dict, description="Captured entries per level")
    key_lines: list[str] = Field(default_factory=list, description="Faults and errors first, then "
                                 "the latest lines")
    lines_omitted: int = 0
    dropped: int = 0
    new_crash: CrashDetail | None = None
    other_new_crashes: list[str] = Field(default_factory=list)
    summary: str
    next_steps: list[str] = Field(default_factory=list)
