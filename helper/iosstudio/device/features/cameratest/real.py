"""cameratest.run: the camera test loop for your own development builds, as one job.

clear Documents/CameraTestOutput and CameraTestInput -> push the chosen images/videos to
CameraTestInput -> (re)launch the app with launch arguments (default `-cameraTestMode 1`) while
recording its log -> wait for CameraTestOutput/done.json, the app exiting, or the timeout -> pull
CameraTestOutput -> report done.json, the output files, the log and any new crash report.

Progress comes as `cameratest` events {testId, step, state, title, detail}; the pushes and pulls
inside it also send `transfer` events. transfer.cancel {transferId: testId} stops it. The app hook
that reads CameraTestInput and writes done.json is in docs/camera-testing.md.
Uses the files module's helpers (features/files.py), so it loads only when that one does.
"""
from __future__ import annotations

import asyncio
import contextlib
import posixpath
import time
from pathlib import Path

from pymobiledevice3.exceptions import AfcException

from iosstudio.core import fileio
from iosstudio.core.appdirs import CACHE_DIR
from iosstudio.device.features.files import core as df
from iosstudio.device.features.files import real as F
from iosstudio.device.real import core
from iosstudio.device.real.core import classify, dlog, emit, err, safe_name

OUT_DIR = CACHE_DIR / "cameratest"
RUNS_KEEP = 20                  # test result folders kept (newest first)
RUNS_MAX_BYTES = 1 << 30
STEPS = [("check", "Check the app"), ("clear", "Clear the test folders"), ("push", "Copy the test files"),
         ("launch", "Launch the app"), ("wait", "Wait for results"), ("pull", "Copy the results"),
         ("crashes", "Look for crash reports")]
LOG_KEEP = 400            # log lines returned in the reply (the whole log goes to syslog.txt)


class Steps:
    def __init__(self, tid: str):
        self.tid = tid
        self.state = {k: "pending" for k, _ in STEPS}
        self.titles = dict(STEPS)
        self.detail: dict[str, str] = {}
        self.current = None

    def set(self, key: str, state: str, detail: str = "") -> None:
        self.state[key] = state
        if detail:
            self.detail[key] = detail
        if state == "running":
            self.current = key
        emit("cameratest", {"testId": self.tid, "step": key, "state": state, "title": self.titles[key],
                            "detail": detail})

    def view(self) -> list[dict]:
        return [{"id": k, "title": t, "state": self.state[k], "detail": self.detail.get(k, "")} for k, t in STEPS]

    def fail_current(self, detail: str) -> None:
        if self.current and self.state[self.current] == "running":
            self.set(self.current, "failed", detail)


async def call(d, cmd: str, args: dict):
    fn = d.registry.lookup(cmd)
    if fn is None:
        raise err("failed", f"The {cmd} command isn't available.")
    return await fn(d, args)


def register(reg):
    @reg.command("cameratest.run")
    async def c_cameratest_run(d, a):
        s = d.need_session(a)
        bid = str(a.get("bundleId") or "")
        if not bid:
            raise err("bad-args", "Which app? (bundleId missing)")
        inputs = [Path(str(p).removeprefix("file://")).expanduser() for p in (a.get("inputs") or [])]
        missing = [str(p) for p in inputs if not p.is_file()]
        if not inputs:
            raise err("bad-args", "Choose at least one test image or video.")
        if missing:
            raise err("not-found", f"{len(missing)} test file(s) aren't there anymore.", None, "\n".join(missing[:20]))
        args = df.split_args(a.get("args")) if a.get("args") is not None else list(df.DEFAULT_TEST_ARGS)
        env = df.parse_env(a.get("env"))
        timeout = max(5.0, min(float(a.get("timeout") or 60), 900.0))
        tr = d.registry.provided.get("transfers")
        tid = str(a.get("testId") or (tr.new_id("ct") if tr else f"ct{int(time.time())}"))
        out = OUT_DIR / safe_name(tid)
        await asyncio.to_thread(fileio.prune, OUT_DIR.glob("*"), RUNS_KEEP, RUNS_MAX_BYTES, protect=[out])
        steps = Steps(tid)
        started = time.time()
        result = {"testId": tid, "bundleId": bid, "args": args, "env": env, "timeout": timeout,
                  "outputDir": str(out / "output"), "timedOut": False, "exited": False, "done": None,
                  "doneRaw": None, "outputs": [], "logLines": [], "logCount": 0, "logFile": None,
                  "newCrashes": [], "pid": None}
        cap = df.LogCapture()
        hub = core.BROKER.syslog if core.BROKER is not None else None
        track = tr.track(tid, d.client(), "cameratest.run") if tr else contextlib.nullcontext()
        with track:
            try:
                # 1. the app must be one of yours (its whole container is reachable)
                steps.set("check", "running")
                info = await F.app_info(d, s, bid)
                if not info["developer"]:
                    raise err("not-dev-app", "Camera tests need one of your own apps, built and installed from "
                              "iOS Studio. This one is from the App Store.")
                exe = info["executable"] or info["name"]
                steps.set("check", "done", info["name"])
                async with s.house_arrest(bid) as afc:
                    # 2. fresh folders
                    steps.set("clear", "running")
                    for p in (df.CAMERA_OUT, df.CAMERA_IN):
                        with contextlib.suppress(AfcException):
                            await afc.rm(p, force=True)
                        await afc.makedirs(p)
                    steps.set("clear", "done")
                    # 3. inputs
                    steps.set("push", "running")
                    prog = df.Progress(emit, tid, "cameratest.push", sum(p.stat().st_size for p in inputs), len(inputs),
                                       {"bundleId": bid})
                    for p in inputs:
                        prog.start_file(p.name)
                        await F.write_from(afc, p, posixpath.join(df.CAMERA_IN, p.name), prog)
                        prog.file_done()
                    prog.send("done", force=True)
                    steps.set("push", "done", f"{len(inputs)} file(s)")
                    # crash baseline, then the log, then a fresh launch
                    before = None
                    with contextlib.suppress(Exception):
                        before = {c["name"] for c in await call(d, "crash.list", {"process": exe, "flush": True})}
                    if hub is not None:
                        with contextlib.suppress(Exception):
                            await hub.start(cap, {"filters": {"bundleId": bid, "minLevel": a.get("minLevel") or "info"}})
                    steps.set("launch", "running", " ".join(args))
                    with contextlib.suppress(Exception):
                        await call(d, "app.kill", {"bundleId": bid})
                        await asyncio.sleep(0.5)
                    res = await call(d, "app.launch", {"bundleId": bid, "args": args, "env": env})
                    result["pid"] = (res or {}).get("pid")
                    steps.set("launch", "done", f"pid {result['pid']}" if result["pid"] else "")
                    # 4. wait for done.json / exit / timeout
                    steps.set("wait", "running", f"up to {timeout:g} s")
                    t0 = time.monotonic()
                    done_path = posixpath.join(df.CAMERA_OUT, "done.json")
                    gone_since = None
                    last_check = 0.0
                    while True:
                        if await afc.exists(done_path):
                            break
                        el = time.monotonic() - t0
                        if el >= timeout:
                            result["timedOut"] = True
                            break
                        if el - last_check >= 3 and el > 2:
                            last_check = el
                            with contextlib.suppress(Exception):
                                rsd = await s.get_rsd()
                                pids = await F_app_pids(d, rsd, bid)
                                if not pids:
                                    gone_since = gone_since or time.monotonic()
                                    if time.monotonic() - gone_since > 2:
                                        result["exited"] = True
                                        break
                                else:
                                    gone_since = None
                        await asyncio.sleep(1.0)
                    waited = round(time.monotonic() - t0, 1)
                    if result["timedOut"]:
                        steps.set("wait", "failed", f"no done.json after {waited} s")
                    elif result["exited"]:
                        steps.set("wait", "failed", f"the app closed after {waited} s without done.json")
                    else:
                        steps.set("wait", "done", f"{waited} s")
                    # 5. results (whatever is there, even after a timeout)
                    steps.set("pull", "running")
                    prog = df.Progress(emit, tid, "cameratest.pull", None, 0, {"bundleId": bid})
                    try:
                        files = await F.pull_tree(afc, [df.CAMERA_OUT], out / "pulled", prog, "/")
                    except AfcException:
                        files = []
                    prog.send("done", force=True)
                    base = out / "pulled" / "CameraTestOutput"
                    result["outputDir"] = str(base)
                    result["outputs"] = sorted(files)
                    done_file = base / "done.json"
                    if done_file.is_file():
                        result["done"], result["doneRaw"] = df.read_done(done_file)
                    steps.set("pull", "done", f"{len(files)} file(s)")
            except asyncio.CancelledError:
                steps.fail_current("cancelled")
                raise
            except BaseException as exc:  # noqa: BLE001
                e = F.afc_error(exc, "the app's files")
                steps.fail_current(classify(e).message)
                raise e
            finally:
                if hub is not None:
                    with contextlib.suppress(Exception):
                        await hub.stop(cap, emit_state=False)
            # 6. the log and new crash reports
            lines = [df.LogCapture.fmt(e) for e in cap.lines]
            result["logCount"] = cap.count
            result["logLines"] = lines[-LOG_KEEP:]
            if lines:
                logf = out / "syslog.txt"
                logf.parent.mkdir(parents=True, exist_ok=True)
                logf.write_text("\n".join(lines) + "\n")
                result["logFile"] = str(logf)
            steps.set("crashes", "running")
            if before is not None:
                if result["exited"]:
                    await asyncio.sleep(1.5)   # the phone writes the report just after the process dies
                with contextlib.suppress(Exception):
                    after = await call(d, "crash.list", {"process": exe, "flush": True})
                    result["newCrashes"] = [c for c in after if c["name"] not in before
                                            and float(c.get("ts") or 0) >= started - 5][:5]
            steps.set("crashes", "done" if not result["newCrashes"] else "failed",
                      f"{len(result['newCrashes'])} new" if result["newCrashes"] else "none")
            result["ok"] = result["done"] is not None and not result["newCrashes"]
            result["seconds"] = round(time.time() - started, 1)
            result["steps"] = steps.view()
            dlog(f"cameratest {tid}: ok={result['ok']} timedOut={result['timedOut']} outputs={len(result['outputs'])}")
            return result


async def F_app_pids(d, rsd, bid: str) -> list[int]:
    from iosstudio.device.features.apps.real import app_pids
    return await app_pids(d, rsd, bid)
