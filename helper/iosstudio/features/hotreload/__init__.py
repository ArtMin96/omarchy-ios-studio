"""Hot reload on the iPhone (#13): swap the changed functions of a running Debug build without reinstalling.

  commands       the `hotreload` helper group (enable, disable, run, status, stop, pause, resume)
  session        one session (`hotreload run`): build/install/launch once, then reload each save (pause/resume)
  state          the sessions' runtime files (lock, state file with history, control file), status/stop/pause
  project_setup  the managed Package.swift block and the runtime package copied into .iosstudio/hot-reload
  buildinfo      the Baseline: what the last xtool build compiled and linked, cached under ~/.cache/ios-studio/hot-reload
  macho          a small Mach-O reader (symbols, indirect symbols, relocations, code signature)
  checks         what a patch can't change (stored properties, new types, enum cases, vtables, opaque body types…)
  patch          compile the changed files, link one dylib, sign it: make_patch() and warm_up()
  signing        sign a dylib with xtool's development identity (the private key never touches the disk)
"""
from iosstudio.features.hotreload.commands import GROUPS

__all__ = ["GROUPS"]
