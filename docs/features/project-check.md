# Project check: config mistakes found before building

Some mistakes in a project's settings don't stop a build. xtool ignores an `xtool.yml` key it doesn't
know, so `bundleId:` instead of `bundleID:` does nothing and says nothing. An app that uses the camera
without `NSCameraUsageDescription` in its `Info.plist` builds, installs and opens fine, then crashes the
first time it asks for the camera. The project check reads the project's files and finds mistakes like
these in well under a second.

It only reads. It never changes a file, and its findings are warnings or notes, never errors, so it
never stops a build.

## The checks

| Check | What it looks at | Why |
|---|---|---|
| `xtool-keys` | Unknown or misspelled `xtool.yml` keys, at the top level and inside `extensions:` items, with the closest valid key (`bundleId` → `bundleID`, `iconFile` → `iconPath`). Also: no `version` key or a version other than 1, neither `bundleID` nor `orgID`, an `infoPath` or `entitlementsPath` that points to a missing file, and an `Info.plist` that isn't a valid property list. | xtool skips unknown keys without a word. Keys are case-sensitive. |
| `usage-descriptions` | An `NS…UsageDescription` key missing from `Info.plist` while the Swift sources use the feature it covers (see the table below), or one that is there but empty. | iOS ends the app the first time it asks for access without one. |
| `icon` | `iconPath` exists, is a real PNG (by its header, and named `.png`), is 1024×1024 and has no transparency (an alpha color type or a `tRNS` chunk). No `iconPath` at all is a note. | xtool only takes PNG files, and iOS fills transparent parts with black. Without an icon the iPhone shows a blank one. |
| `entitlements` | Entitlements in the `entitlementsPath` file that a free Apple ID can't get (see below), and an entitlements file that isn't a valid property list. | xtool leaves them out when it signs for a free team, so the feature just doesn't work on the phone. |
| `bundle-id` | The bundle id, from `bundleID` or from `orgID` plus the product name, is reverse-DNS: letters, numbers, dashes and dots, at least two parts, no empty part, no spaces or underscores. | |
| `version` | `CFBundleShortVersionString` and `CFBundleVersion` in `Info.plist` are text of one to three numbers separated by dots. An Xcode build variable like `$(MARKETING_VERSION)` gets its own message. Only checked when `Info.plist` sets them. | xtool doesn't fill in Xcode build variables. |

The product name for an `orgID` bundle id is `product:` from `xtool.yml`, else the only `.library` in
`Package.swift`, else the folder name.

### Usage descriptions

The check looks for these names in the Swift sources (outside `//` comments and lines that start a
`/* … */` comment). The first key is the one the hint tells you to add; any of the listed keys satisfies
the check.

| Key | Feature | Found from |
|---|---|---|
| `NSCameraUsageDescription` | the camera | `AVCaptureSession`, `AVCaptureDevice`, `AVCapturePhotoOutput`, `AVCaptureVideoDataOutput`, `AVCaptureMovieFileOutput`, `AVCaptureVideoPreviewLayer`, `AVCaptureDeviceInput`, `sourceType = .camera`, `DataScannerViewController`, `VNDocumentCameraViewController` |
| `NSMicrophoneUsageDescription` | the microphone | `AVAudioRecorder`, `requestRecordPermission`, `.inputNode`, `AVCaptureDevice.default(for: .audio`, `setCategory(.playAndRecord` / `.record` |
| `NSLocationWhenInUseUsageDescription` | location | `CLLocationManager`, `CLLocationUpdate`, `CLMonitor` |
| `NSLocationAlwaysAndWhenInUseUsageDescription` | location in the background | `requestAlwaysAuthorization` |
| `NSPhotoLibraryUsageDescription` | the photo library | `PHAsset.fetch…`, `PHAssetCollection.fetch…`, `PHImageManager`, `PHCachingImageManager`, `PHPhotoLibrary.requestAuthorization()` / `(for: .readWrite)`, `PHPhotoLibrary.authorizationStatus()` / `(for: .readWrite)` |
| `NSPhotoLibraryAddUsageDescription` (or `NSPhotoLibraryUsageDescription`) | saving to the photo library | `UIImageWriteToSavedPhotosAlbum`, `UISaveVideoAtPathToSavedPhotosAlbum`, `PHAssetCreationRequest`, `PHAssetChangeRequest.creationRequest…`, `.requestAuthorization(for: .addOnly` |
| `NSContactsUsageDescription` | contacts | `CNContactStore` |
| `NSCalendarsFullAccessUsageDescription` (or `NSCalendarsWriteOnlyAccessUsageDescription`, `NSCalendarsUsageDescription`) | the calendar | `requestFullAccessToEvents`, `requestWriteOnlyAccessToEvents`, `requestAccess(to: .event`, `EKEvent(eventStore:` |
| `NSRemindersFullAccessUsageDescription` (or `NSRemindersUsageDescription`) | reminders | `requestFullAccessToReminders`, `requestAccess(to: .reminder`, `EKReminder(eventStore:` |
| `NSBluetoothAlwaysUsageDescription` | Bluetooth | `CBCentralManager`, `CBPeripheralManager` |
| `NSHealthShareUsageDescription` | Health data | `HKHealthStore` |
| `NSHealthUpdateUsageDescription` | writing Health data | a non-empty `toShare:` argument |
| `NSSpeechRecognitionUsageDescription` | speech recognition | `SFSpeechRecognizer` |
| `NSFaceIDUsageDescription` | Face ID | `LAContext` |
| `NFCReaderUsageDescription` | NFC | `NFCReaderSession`, `NFCNDEFReaderSession`, `NFCTagReaderSession`, `NFCVASReaderSession` |
| `NSMotionUsageDescription` | motion and fitness data | `CMMotionActivityManager`, `CMPedometer` |
| `NSUserTrackingUsageDescription` | tracking (App Tracking Transparency) | `ATTrackingManager` |
| `NSAppleMusicUsageDescription` | the music library | `MPMediaLibrary`, `MPMediaQuery`, `MusicAuthorization` |
| `NSLocalNetworkUsageDescription` | the local network | `NWBrowser`, `NetServiceBrowser`, `MCNearbyServiceBrowser`, `MCNearbyServiceAdvertiser` |
| `NSHomeKitUsageDescription` | HomeKit | `HMHomeManager` |
| `NSSiriUsageDescription` | Siri | `requestSiriAuthorization` |

APIs that don't make iOS ask for permission aren't listed: `PhotosPicker`, `PHPickerViewController`,
`CNContactPicker` and `CMMotionManager` (raw accelerometer) need no usage description.

The issue points at the first file and line that uses the feature. When the project has no
`Info.plist` at all (no `infoPath`), the hint says to add one and point `infoPath` at it.

### Paid-only entitlements

| Feature | Entitlement keys |
|---|---|
| push notifications | `aps-environment` |
| associated domains (universal links) | `com.apple.developer.associated-domains` |
| app groups | `com.apple.security.application-groups` |
| iCloud | `com.apple.developer.icloud-container-identifiers`, `com.apple.developer.icloud-services`, `com.apple.developer.icloud-container-environment`, `com.apple.developer.ubiquity-container-identifiers`, `com.apple.developer.ubiquity-kvstore-identifier` |
| Apple Pay | `com.apple.developer.in-app-payments` |
| Sign in with Apple | `com.apple.developer.applesignin` |
| Wallet passes | `com.apple.developer.pass-type-identifiers` |
| network extensions | `com.apple.developer.networking.networkextension` |
| personal VPN | `com.apple.developer.networking.vpn.api`, `com.apple.networking.vpn.configuration` |
| multipath networking | `com.apple.developer.networking.multipath` |
| SiriKit | `com.apple.developer.siri` |
| Wi-Fi Aware | `com.apple.developer.wifi-aware` |

xtool marks the capabilities behind most of these as not free and leaves them out when signing for a
free team. Apple doesn't offer iCloud, Apple Pay, Sign in with Apple and Wallet to free teams at all.
The check doesn't know which kind of Apple ID you use, so the warning shows either way; the hint says it
only applies to free accounts.

## Valid xtool.yml keys

The valid keys come from xtool's own source. iOS Studio looks in `~/.cache/omarchy-apple-dev/xtool-*/`
for `Sources/PackLib/PackSchema.swift` (or `Planner.swift`), newest first, and reads the properties (or
`CodingKeys`) of `struct PackSchemaBase` and its `struct Extension`. So a newer xtool's keys are picked
up without a change to iOS Studio.

When there's no cached xtool source, a built-in list from xtool's pinned commit (`f300489`) is used:

- top level: `version`, `orgID`, `bundleID`, `product`, `infoPath`, `entitlementsPath`, `iconPath`,
  `resources`, `extensions`, `skipLSP`
- inside `extensions:` items: `product`, `bundleID`, `infoPath`, `resources`, `entitlementsPath`

The result's `keysSource` says which one was used: `xtool-cache` or `builtin`.

## When it runs

- **Project cards.** Each card on Projects runs `projects check` when it appears, when it gets the
  cursor and when its More opens, one project at a time, and not again within a minute. Opening the
  Projects page checks every project the same way. A finished build checks its project again. See
  [Settings warnings on a project card](../guide.md#settings-warnings-on-a-project-card).
- **Every build.** Run, Check build, Build .ipa and Re-sign run the checks before xtool starts (after
  the prebuild command, when the project has one, since that may rewrite `xtool.yml`). A USB install of
  an existing build doesn't. This run:
  - has a budget of 0.8 seconds; past it the checks stop and report what they have,
  - adds only the warnings, at most 20, as the build's first diagnostics (category `config`, with a
    `check` id). Notes, such as "no icon", are left out,
  - never fails, never blocks the build and never changes a file. If the check itself breaks, the
    build goes on without it.

  These warnings count in the build's warnings and show in the Problems list with a "Pre-build check"
  chip, ahead of xtool's own warnings.
- **MCP.** `ios_project_check(project_path, check=, max_issues=, max_bytes=)`; see [mcp.md](../mcp.md).
  The same warnings appear in `ios_build_diagnostics` as `category='config'`.
- **Helper.** `iosstudio-helper projects check --path P [--max N]`. It has no time budget, and `--max`
  (default 50) caps the issues returned.

## Output

`projects check` returns:

```json
{
  "path": "/home/you/Projects/Scanner",
  "issues": [
    {"severity": "warning", "category": "config", "check": "xtool-keys",
     "message": "xtool.yml has an unknown key 'bundleId': did you mean 'bundleID'?",
     "file": "xtool.yml", "line": 3,
     "hint": "Rename it to 'bundleID'. xtool ignores keys it doesn't know without a word, so this setting does nothing: fix the spelling (keys are case-sensitive)."}
  ],
  "total": 1, "shown": 1, "truncated": false,
  "counts": {"error": 0, "warning": 1, "note": 0},
  "elapsedMs": 14, "keysSource": "xtool-cache",
  "checks": ["xtool-keys", "usage-descriptions", "icon", "entitlements", "bundle-id", "version"]
}
```

An issue has the shape of a build diagnostic: `severity` (`warning` or `note`), `category` (always
`config`), `check`, `message`, `file` (relative to the project, `../` for a local package next to it,
or `null`), `line` (or `null`) and `hint`. In a build, `file` is made absolute like the other
diagnostics. `counts` and `total` cover every issue, `issues` only the first `--max`.

## Demo mode

With `IOSSTUDIO_DEMO=1`, `projects check` returns the same four made-up issues for any project: an
unknown `bundleId` key, the camera used without `NSCameraUsageDescription`, an icon with transparency and
`aps-environment` on a free account. Every demo build shows the first two as pre-build warnings.

## Limits

- `xtool.yml` is read as simple flat YAML: top-level keys and the keys of `extensions:` items. Anchors,
  flow mappings and other fancier YAML are left alone.
- The source scan matches names with regular expressions. It skips `//` comment lines and lines that
  start a `/* … */` comment, but not other lines inside a block comment or names inside strings. It
  reads at most 3000 `.swift` files and skips files over 1 MB (usually generated code) and folders
  starting with `.` or named `xtool` or `build`.
- Sources are `Sources/`, target `path:`s in `Package.swift` and the `Sources/` of local
  `.package(path:)` dependencies. Remote packages aren't scanned, so a package that uses the camera for
  you isn't caught.
- Only the app's own `Info.plist` and `entitlementsPath` are read. Extensions' `infoPath` and
  `entitlementsPath` are checked for unknown keys only.
- The usage-description triggers and the paid-only entitlement list are kept by hand.
- A pre-build run cut off by the 0.8 s budget skips the remaining checks rather than guessing; the
  project card's check has no budget and runs them all.

## Still to check on a real iPhone

- That a free-account build really drops each listed entitlement, and that a paid account keeps them.
- That each trigger in the usage table makes iOS ask for permission on current iOS versions (and
  crash without the key), and that the listed picker APIs don't.
- That an icon with transparency shows a black background on the home screen.

## Code

`helper/iosstudio/features/project_check.py` (the checks, the trigger and entitlement tables, key lookup, `summarize`),
`helper/iosstudio/features/projects/` (`projects check` in commands.py, `precheck` in build.py), `helper/iosstudio/demo/demo.py` (demo issues),
`mcp/iosstudio_mcp/tools/project_check.py`, `services/ProjectService.qml` (`check`, `checkSoon`,
`checkAll`, `checkFor`, `configCount`), `components/projects/ProjectCheckView.qml`,
`components/common/BuildDiagnosticRow.qml` (the chip). Tests: `tests/helper/test_project_check.py` and `tests/cli/test_project_check_cli.py` with one fixture
project per case in `tests/fixtures/project-check/`, and `tests/mcp/test_mcp_project_check.py`.
