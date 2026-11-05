#!/bin/sh
# Rebuilds Crashy.dwarf: the dSYM DWARF (an arm64 Mach-O carrying the same LC_UUID as the executable)
# of Crashy.swift, built for arm64-apple-ios with xtool's Swift SDK. Paths in the debug info are mapped to
# /work/Crashy and /sdk so no personal paths end up in the fixture. Changing it changes the UUID and the
# frame offsets in tests/test_crash_symbolication.py.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
sdkb=${XDG_CONFIG_HOME:-$HOME/.config}/swiftpm/swift-sdks/darwin.artifactbundle
sdk=$(ls -d "$sdkb"/Developer/Platforms/iPhoneOS.platform/Developer/SDKs/iPhoneOS*.sdk | tail -1)
work=$(mktemp -d)
mkdir -p "$work/Sources/Crashy"
cp "$here/Crashy.swift" "$work/Sources/Crashy/"
cd "$work"
swiftc -target arm64-apple-ios17.0 -sdk "$sdk" -resource-dir "$sdkb/Developer/Toolchains/XcodeDefault.xctoolchain/usr/lib/swift" \
  -g -Onone -parse-as-library -module-name Crashy \
  -debug-prefix-map "$work=/work/Crashy" -debug-prefix-map "$sdkb=/sdk" -file-compilation-dir /work/Crashy \
  -c Sources/Crashy/Crashy.swift -o Crashy.o
"$sdkb/toolset/bin/orig/ld64.lld" -arch arm64 -platform_version ios 17.0 17.0 -syslibroot "$sdk" \
  -lSystem -L"$sdk/usr/lib/swift" -lswiftCore Crashy.o -o Crashy
dsymutil Crashy -o Crashy.dSYM
cp Crashy.dSYM/Contents/Resources/DWARF/Crashy "$here/Crashy.dwarf"
rm -rf "$work"
