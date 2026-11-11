#!/bin/sh
# Rebuilds the small objects here (needs xtool's darwin SDK and a hot-reload baseline.json of any SwiftUI app,
# from buildinfo.capture_baseline): each <variant>-<hr>.o is <variant>.swift compiled as module "Fix" together
# with hr_<hr>.swift (a .hotReloadable() returning `some View` or `AnyView`), without debug info.
#   base    Card and Wrapped (ends body with .hotReloadable()): a VStack with one Text
#   text    the same with another string        struct  another view inside each VStack
# usage: make.sh /path/to/baseline.json
set -eu
cd "$(dirname "$0")"
for hr in some any; do
  for v in base text struct; do
    python3 - "$1" "$v.swift" "hr_$hr.swift" "$v-$hr.o" <<'PY'
import json, subprocess, sys
b = json.load(open(sys.argv[1]))
fe = next(iter(b["frontend"].values()))
k = fe.index("-primary-file"); j = k + 2
while not fe[j].startswith("-"):
    j += 1
args = fe[:k] + ["-primary-file", sys.argv[2], sys.argv[3]] + fe[j:]
args[args.index("-module-name") + 1] = "Fix"
drop = {"-g", "-debug-info-format=dwarf", "-dwarf-version=4", "-serialize-debugging-options", "-enable-testing"}
args = [a for a in args if a not in drop]
args[args.index("-o") + 1] = sys.argv[4]
subprocess.run(args, check=True)
PY
  done
done
rm -f text-some.o
