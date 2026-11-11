"""Demo mode of the `hotreload` group: answers for the demo projects as if done; no project file is written.

enable/disable build their diff from a made-up Package.swift with the real block (project_setup.block), so the
UI shows what a real project gets. WeatherGlance's views `import HotReload`, so turning it off keeps the stub.

run/status/stop/pause/resume are the real session commands on the demo runtime dir with DemoSession: nothing is
built or pushed; a scripted session plays starting -> building (progress) -> installing -> launching -> watching,
then a save of ContentView.swift every few seconds: reloading -> reloaded (~2.4 s, steps filled) -> watching ->
reloading -> failed (unsupported-change: a stored property `count` added to ContentView) -> … until stopped.
Paused, the scripted saves collect as pending; resume reloads them in one patch (always succeeds).
Pretend-work pauses scale with IOSSTUDIO_DEMO_DELAY (the gaps between saves never go below SAVE_GAP_MIN).
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from iosstudio.core import output, subproc
from iosstudio.core.output import HelperError
from iosstudio.features.hotreload import session as S

IMPORTING = {"WeatherGlance": ["Sources/WeatherGlance/ContentView.swift", "Sources/WeatherGlance/ForecastView.swift"]}
DEFAULT_ON = ("WeatherGlance",)


def _switch_file() -> Path:
    from iosstudio.features.watch.runtime import runtime_dir
    return runtime_dir() / "hotreload-demo-switch.json"


def demo_setting(path: str) -> dict:
    """{enabled, mode, importedBy?} of a demo project: what enable/disable last set (in the demo runtime dir,
    so it lasts until the session ends), else WeatherGlance on and the others off."""
    name = path.rstrip("/").split("/")[-1]
    try:
        mode = json.loads(_switch_file().read_text()).get(path)
    except (OSError, ValueError, AttributeError):
        mode = None
    if mode not in ("on", "off", "stub"):
        mode = "on" if name in DEFAULT_ON else "off"
    out = {"enabled": mode == "on", "mode": mode}
    if mode == "stub":
        out["importedBy"] = IMPORTING.get(name, [])
    return out


def _remember(path: str, mode: str) -> None:
    f = _switch_file()
    try:
        data = json.loads(f.read_text())
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    data[path] = mode
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(dict(list(data.items())[-200:])))
    except OSError:
        pass


def _project(flags: dict) -> tuple[str, str]:
    raw = str(flags.get("project") or flags.get("path") or "")
    if not raw or raw == "True":
        raise HelperError("usage", "Missing --project.")
    return raw.rstrip("/").split("/")[-1], raw.rstrip("/")


def _manifest(name: str) -> str:
    return ('// swift-tools-version: 6.0\nimport PackageDescription\n\nlet package = Package(\n'
            f'    name: "{name}",\n    platforms: [.iOS(.v17)],\n    products: [\n'
            f'        .library(name: "{name}", targets: ["{name}"]),\n    ],\n    targets: [\n'
            f'        .target(name: "{name}"),\n    ]\n)\n')


def _diff(before: str, after: str) -> str:
    from iosstudio.features import format as swift_format
    return swift_format.unified("Package.swift", before, after)


def enable(flags: dict) -> int:
    from iosstudio.features.hotreload import project_setup as S
    name, path = _project(flags)
    base = _manifest(name)
    if not flags.get("dry_run"):
        _remember(path, "on")
    return output.ok({"enabled": True, "mode": "on", "appTarget": name, "changed": True,
                      "diff": _diff(base, S.append_block(base, name)), "truncated": False,
                      "packagePath": f"{path}/Package.swift", "dryRun": bool(flags.get("dry_run")),
                      "runtimeUpdated": True, "demo": True})


def disable(flags: dict) -> int:
    from iosstudio.features.hotreload import project_setup as S
    name, path = _project(flags)
    base = _manifest(name)
    before = S.append_block(base, name)
    users = IMPORTING.get(name, [])
    after = S.append_block(base, name, linker=False) if users else base
    if not flags.get("dry_run"):
        _remember(path, "stub" if users else "off")
    return output.ok({"enabled": False, "mode": "stub" if users else "off", "importedBy": users, "changed": True,
                      "diff": _diff(before, after), "truncated": False, "packagePath": f"{path}/Package.swift",
                      "dryRun": bool(flags.get("dry_run")), "runtimeUpdated": True, "demo": True})


# ================================================================ the scripted session

SAVE_GAP = 4.0          # seconds between scripted saves
SAVE_GAP_MIN = 0.3      # never faster, even with IOSSTUDIO_DEMO_DELAY=0 (it would flood the stream)
STEPS = (("check", 0.04), ("compile", 0.9), ("link", 0.8), ("sign", 0.2), ("push", 0.3))
PAUSED_FILES = ("ContentView.swift", "ForecastView.swift")


class DemoSession(S.Session):
    """Session with the build, the phone and the patches made up (see the module doc)."""

    def __init__(self, root: Path, **kw):
        super().__init__(root, watcher_factory=lambda _root: (None, None), **kw)   # no files are watched
        self.next_save: float | None = None
        self.saves = 0
        self.script = 0          # reloads played (every second one fails)

    def _sleep(self, seconds: float) -> None:
        if self.stopping.wait(subproc.demo_delay(seconds)):
            raise subproc.Cancelled()

    def _file(self, name: str = "ContentView.swift") -> str:
        return f"Sources/{self.root.name}/{name}"

    def start(self) -> None:
        from iosstudio.device.demo.core import UDID
        self.udid = self.udid or UDID
        self.publish(udid=self.udid)
        build_id = f"hr-{int(time.time())}"
        self.publish("building", "Building the app", buildId=build_id,
                     progress={"fraction": 0.0, "detail": "Starting the build"})
        for frac, detail in ((0.15, "Resolving packages"), (0.35, f"Compiling {self.root.name}"),
                             (0.6, "Compiling ContentView.swift"), (0.8, "Linking"), (0.9, "Signing")):
            self._sleep(0.45)
            self.publish(progress={"fraction": frac, "detail": detail})
        self._sleep(0.3)
        self.publish("installing", "Installing on your iPhone", progress=None)
        self._sleep(0.8)
        self.bundle_id = f"com.example.{self.root.name.lower()}"
        self.publish("launching", "Opening the app on your iPhone")
        self._sleep(0.6)
        self.publish("launching", "Getting hot reload ready")
        self.baseline = "demo"
        self._sleep(0.3)

    def on_started(self) -> None:
        super().on_started()
        self.next_save = self.clock() + max(SAVE_GAP_MIN, subproc.demo_delay(SAVE_GAP * 0.75))

    def poll_changes(self) -> list[str]:
        if self.next_save is None or self.clock() < self.next_save or (self.worker and not self.sched.paused):
            return []
        self.saves += 1
        self.next_save = self.clock() + max(SAVE_GAP_MIN, subproc.demo_delay(SAVE_GAP))
        name = PAUSED_FILES[self.saves % len(PAUSED_FILES)] if self.sched.paused else "ContentView.swift"
        return [self._file(name)]

    def reload_once(self, seq: int, files: list[str]) -> S.Outcome:
        t0 = time.monotonic()
        out = S.Outcome(seq, files)
        self.script += 1
        fails = self.script % 2 == 0 and len(files) == 1
        steps: dict[str, float] = {}
        try:
            for name, secs in STEPS:
                self.on_step(name)
                t = time.monotonic()
                self._sleep(secs)
                steps[name] = int(round((time.monotonic() - t) * 1000))
                if fails and name == "check":
                    f = files[0]
                    blocker = {"kind": "stored-property", "type": "ContentView", "name": "count", "file": f,
                               "line": 12, "message": "Adds the stored property count to ContentView."}
                    out.error = {"code": "unsupported-change", "message": blocker["message"] + " This change "
                                 "needs a full build.", "fix": S.RUN_FIX, "file": f, "line": 12, "blockers": [blocker]}
                    return out
        except subproc.Cancelled:
            out.cancelled = True
            return out
        steps["apply"] = 3.1
        out.ok = True
        out.reload = {"seq": seq, "ok": True, "files": files[:S.RELOAD_FILES], "fileCount": len(files),
                      "ms": int(round((time.monotonic() - t0) * 1000)), "steps": steps, "swapped": 3, "reverted": 0,
                      "vtable": 0, "notSwapped": 0, "time": time.time(), "demo": True}
        return out


def _session(name: str):
    def handler(flags: dict) -> int:
        from iosstudio.features.hotreload import commands
        return getattr(commands, f"cmd_{name}")(flags, demo=True)
    return handler


def disable_demo(flags: dict) -> int:
    """disable stops a running demo session first, like the real one."""
    from iosstudio.features.hotreload import commands, state
    path = commands._path_flag(flags) if (flags.get("project") or flags.get("path")) else ""
    if path and not flags.get("dry_run") and state.session_pid(state.key(path)) is not None:
        state.stop(state.key(path))
    return disable(flags)


ACTIONS = {"enable": enable, "disable": disable_demo, **{n: _session(n) for n in
                                                         ("run", "status", "stop", "pause", "resume")}}


def run(action: str, flags: dict) -> int:
    fn = ACTIONS.get(action)
    if fn is None:
        output.fail("usage", f"Unknown command 'hotreload {action}'.")
        return output.EXIT_USAGE
    try:
        return fn(flags)
    except HelperError as exc:
        return output.fail(exc.code, exc.message, exc.fix, exc.detail)
