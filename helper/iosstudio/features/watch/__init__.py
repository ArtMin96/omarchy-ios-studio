"""Watch mode (compile on save): group `watch` (stdlib only). See docs/features/watch-mode.md.

    watch run    --project P [--auto-run] [--debounce-ms 600] [--no-initial-build]   streaming (JSON lines)
    watch run    --project P ... --detach [--owner PID]                                 one-shot (MCP)
    watch status [--project P] [--wait-seconds N]                                      one-shot
    watch stop   [--project P]                                                         one-shot
    watch serve  --project P ... --owner PID                                            the watcher itself

One watcher per user at a time (builds are heavy). It is its own process (`watch serve`), started detached
by whoever asks first (the app through `watch run`, the MCP server through `watch run --detach`), found
through files in the runtime dir ($IOSSTUDIO_RUNTIME_DIR or $XDG_RUNTIME_DIR, /ios-studio[-demo]/):

    watch.lock          flock held by the watcher while it lives (its pid inside): a free lock = no watcher
    watch.json          the latest status snapshot (kept for STOPPED_KEEP after it stops)
    watch-events.jsonl  every snapshot as a JSON line; `watch run` tails it for the app
    watch-owners.json   {pid: process start time}: who uses the watcher. It stops when none of them is alive
    watch-control.json  settings changed while running (auto-run, debounce), applied on SIGUSR1
    watch.log           the watcher's stderr

Each save (inotify on Sources/ and the root's Package.swift, xtool.yml, *.plist, *.entitlements; a 1 s
mtime scan when inotify isn't available) waits for the debounce, then runs the same `build compile` as the
Build button (history, logs and diagnostics included). A change during a build cancels it and starts one
more after it; content that didn't change (a save without edits, a regenerated identical xtool.yml) is
ignored. With auto-run a clean compile is followed by Run (build, install, launch) when an iPhone is
connected and an Apple ID is signed in; otherwise the snapshot's `run` says why it was skipped.

Snapshot (the `watch` event, watch.json, `watch status` data):
    {event: "watch", seq, state: watching|changed|compiling|ok|failed|stopped, reason, message, project,
     path, pid, mode: inotify|poll, modeReason?, autoRun, debounceMs, startedAt, updatedAt, owners,
     changed: [rel path ≤ 5], changedCount, buildId (running or last), logFile, progress?: {fraction, detail},
     builds, superseded,
     lastBuild?: {id, result: ok|failed, duration, time, errors, warnings, errorCode?, errorMessage?, logFile,
                  diagnostics: [≤ 5 {severity, category, file, line, col, message}] (errors first)},
     run?: {state: checking|building|installing|launching|launched|failed|skipped, message, buildId? (the run's
            own build), forBuild (the clean compile it follows), bundleId?},
     stoppedAt?, stopReason?, replaced?: {path, project}}
`watch run` (streaming) prints the current snapshot, then each new one, and a final
{"event": "done", "ok": true, "data": snapshot} once the watcher stops or this follower detaches.

Package layout: commands (this group's handlers), lifecycle (start/stop/status/follow), watcher (the watcher
process and its scheduler), builds (helper build children and auto-run), fswatch (inotify/polling), runtime
(runtime files, owners, process helpers).
"""
from .commands import GROUPS, demo

__all__ = ["GROUPS", "demo"]
