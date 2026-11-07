"""Demo: cameratest.run on the made-up iPhone.

Runs the same steps and events as the real one (helper/iosstudio/device/features/cameratest.py) against the
demo containers: the inputs really land in Documents/CameraTestInput of the demo app, and a fake
"model" writes done.json (made-up labels and boxes) plus one annotated PNG per input to
CameraTestOutput. Without `-cameraTestMode` in the arguments the app never writes done.json, so the
run ends with a timeout (capped at 6 s in demo mode), like a real app without the hook.
"""
from __future__ import annotations

import asyncio
import json
import random
import shutil
import time

from iosstudio.core.output import HelperError
from iosstudio.core.subproc import demo_delay
from iosstudio.demo import demo_art
from iosstudio.device.demo.core import DIR, event
from iosstudio.device.features.files import core as df
from iosstudio.device.features.files import demo as F

OUT_DIR = DIR / "cameratest"
STEPS = [("check", "Check the app"), ("clear", "Clear the test folders"), ("push", "Copy the test files"),
         ("launch", "Launch the app"), ("wait", "Wait for results"), ("pull", "Copy the results"),
         ("crashes", "Look for crash reports")]
LABELS = ["cat", "dog", "bicycle", "coffee cup", "plant", "laptop", "person", "street sign"]


def register(reg):
    @reg.command("cameratest.run")
    async def run(ctx, a):
        bid = str(a.get("bundleId") or "")
        if not bid:
            raise HelperError("bad-args", "Which app? (bundleId missing)")
        from pathlib import Path
        inputs = [Path(str(p).removeprefix("file://")).expanduser() for p in (a.get("inputs") or [])]
        if not inputs:
            raise HelperError("bad-args", "Choose at least one test image or video.")
        missing = [str(p) for p in inputs if not p.is_file()]
        if missing:
            raise HelperError("not-found", f"{len(missing)} test file(s) aren't there anymore.", None, "\n".join(missing[:20]))
        args = df.split_args(a.get("args")) if a.get("args") is not None else list(df.DEFAULT_TEST_ARGS)
        env = df.parse_env(a.get("env"))
        timeout = max(5.0, min(float(a.get("timeout") or 60), 900.0))
        tr = ctx.registry.provided.get("transfers")
        tid = str(a.get("testId") or tr.new_id("ct"))
        state = {k: "pending" for k, _ in STEPS}
        detail: dict[str, str] = {}
        titles = dict(STEPS)
        cur = [None]

        def step(k, st, d=""):
            state[k] = st
            if d:
                detail[k] = d
            if st == "running":
                cur[0] = k
            event("cameratest", {"testId": tid, "step": k, "state": st, "title": titles[k], "detail": d})

        started = time.time()
        out = OUT_DIR / df.safe_name(tid) if hasattr(df, "safe_name") else OUT_DIR / tid
        rnd = random.Random(sum(map(ord, "".join(p.name for p in inputs))))
        log: list[str] = []

        def say(level, msg):
            log.append(f"{time.strftime('%H:%M:%S')} {level:<7} {exe}: {msg}")

        with tr.track(tid, ctx.client(), "cameratest.run"):
            try:
                step("check", "running")
                await asyncio.sleep(demo_delay(0.3))
                # Like real: every App Store app is not-dev-app (before files' no-file-sharing check).
                if not F._app(bid)["developer"]:
                    raise HelperError("not-dev-app", "Camera tests need one of your own apps, built and installed from "
                                      "iOS Studio. This one is from the App Store.")
                info, _, _, base = F._access({"bundleId": bid})
                exe = info["executable"]
                step("check", "done", info["name"])
                step("clear", "running")
                cin, cout = base / df.CAMERA_IN.lstrip("/"), base / df.CAMERA_OUT.lstrip("/")
                for d in (cin, cout):
                    shutil.rmtree(d, ignore_errors=True)
                    d.mkdir(parents=True)
                await asyncio.sleep(demo_delay(0.3))
                step("clear", "done")
                step("push", "running")
                prog = df.Progress(event, tid, "cameratest.push", sum(p.stat().st_size for p in inputs), len(inputs),
                                   {"bundleId": bid})
                for p in inputs:
                    prog.start_file(p.name)
                    await F._copy(p, cin / p.name, prog)
                    prog.file_done()
                prog.send("done", force=True)
                step("push", "done", f"{len(inputs)} file(s)")
                step("launch", "running", " ".join(args))
                await asyncio.sleep(demo_delay(0.6))
                step("launch", "done", "pid 812")
                say("default", f"Launched with arguments: {' '.join(args) or '(none)'}")
                hooked = "-cameraTestMode" in args
                step("wait", "running", f"up to {timeout:g} s")
                t0 = time.monotonic()
                results = []
                if hooked:
                    say("info", f"CameraTest: {len(inputs)} input file(s) in CameraTestInput")
                    for i, p in enumerate(sorted(inputs, key=lambda x: x.name)):
                        await asyncio.sleep(demo_delay(0.7))
                        video = df.media_kind(p.name) == "video"
                        for f in range(rnd.randint(3, 6) if video else 1):
                            lab = rnd.choice(LABELS)
                            conf = round(rnd.uniform(0.55, 0.99), 3)
                            x, y = round(rnd.uniform(0.05, 0.5), 3), round(rnd.uniform(0.05, 0.5), 3)
                            results.append({"file": p.name, "frame": f, "label": lab, "confidence": conf,
                                            "box": [x, y, round(rnd.uniform(0.2, 0.45), 3), round(rnd.uniform(0.2, 0.45), 3)],
                                            "ms": round(rnd.uniform(6, 22), 1)})
                            say("info", f"CameraTest: {p.name} frame {f}: {lab} ({conf:.2f})")
                        if conf < 0.6:
                            say("error", f"CameraTest: low confidence for {p.name}")
                        demo_art.photo(cout / f"{p.stem}-annotated.png", i + 7, 320, 240)
                    (cout / "done.json").write_text(json.dumps({"results": results}, indent=2))
                    say("default", "CameraTest: wrote done.json")
                    step("wait", "done", f"{time.monotonic() - t0:.1f} s")
                else:
                    say("default", "Camera session started (no test mode)")
                    await asyncio.sleep(min(timeout, 6.0))
                    step("wait", "failed", f"no done.json after {time.monotonic() - t0:.1f} s")
                step("pull", "running")
                prog = df.Progress(event, tid, "cameratest.pull", None, 0, {"bundleId": bid})
                pulled = out / "pulled" / "CameraTestOutput"
                pulled.mkdir(parents=True, exist_ok=True)
                files = []
                for f in sorted(cout.iterdir()):
                    prog.start_file(f.name)
                    await F._copy(f, pulled / f.name, prog)
                    files.append(str(pulled / f.name))
                    prog.file_done()
                prog.send("done", force=True)
                step("pull", "done", f"{len(files)} file(s)")
                step("crashes", "running")
                await asyncio.sleep(demo_delay(0.3))
                step("crashes", "done", "none")
            except asyncio.CancelledError:
                if cur[0] and state[cur[0]] == "running":
                    step(cur[0], "failed", "cancelled")
                raise
            except HelperError as exc:
                if cur[0] and state[cur[0]] == "running":
                    step(cur[0], "failed", exc.message)
                raise
        done = None
        if (pulled / "done.json").exists():
            done, _ = df.read_done(pulled / "done.json")
        logf = out / "syslog.txt"
        logf.write_text("\n".join(log) + "\n")
        return {"testId": tid, "bundleId": bid, "args": args, "env": env, "timeout": timeout,
                "outputDir": str(pulled), "timedOut": not hooked, "exited": False, "done": done, "doneRaw": None,
                "outputs": files, "logLines": log, "logCount": len(log), "logFile": str(logf), "newCrashes": [],
                "pid": 812, "ok": done is not None, "seconds": round(time.time() - started, 1),
                "steps": [{"id": k, "title": t, "state": state[k], "detail": detail.get(k, "")} for k, t in STEPS]}
