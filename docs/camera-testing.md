# Testing camera code with files

A camera pipeline (a Vision request, a Core ML model, barcode reading, your own image processing) is hard to test by holding the phone up to things. The camera test loop runs the same code on images and videos you pick on this computer and brings the results back, so a run is repeatable and you can compare one build with the next.

It works with your own development builds, the apps you build and install from iOS Studio. Their whole container is reachable over the same connection the app already uses for the iPhone.

## How a run goes

1. iOS Studio deletes `Documents/CameraTestOutput` and `Documents/CameraTestInput` in the app's container and creates them again, empty.
2. It copies your test images and videos into `Documents/CameraTestInput`.
3. It stops the app if it's running and launches it with the launch arguments (`-cameraTestMode 1` by default) and any environment variables you set. It records the app's log from here on.
4. It waits until the app writes `Documents/CameraTestOutput/done.json`, the app quits, or the timeout passes (60 seconds by default).
5. It copies everything in `Documents/CameraTestOutput` to `~/.cache/ios-studio/cameratest/<run id>/pulled/CameraTestOutput/`, together with the log as `syslog.txt`.
6. It checks for a new crash report from the app.

The app's side is the hook below: when it starts with `-cameraTestMode`, it skips the camera, runs its frame handler on every file in `CameraTestInput`, and writes the results to `done.json`.

## Add the hook

Copy this into your app, for example as `Sources/<App>/CameraTest.swift`:

```swift
import AVFoundation
import CoreImage
import UIKit

enum CameraTest {
    static var isOn: Bool { ProcessInfo.processInfo.arguments.contains("-cameraTestMode") }
    static let docs = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
    static let input = docs.appendingPathComponent("CameraTestInput")
    static let output = docs.appendingPathComponent("CameraTestOutput")

    /// Runs `handle` on every frame of every file in CameraTestInput, then writes
    /// CameraTestOutput/done.json as {"results": [...]}. `handle` gets the frame and the file
    /// name and returns a JSON-friendly dictionary; "file" and "frame" are added to it.
    static func run(handle: @escaping (CVPixelBuffer, String) -> [String: Any]) {
        DispatchQueue.global(qos: .userInitiated).async {
            let fm = FileManager.default
            try? fm.createDirectory(at: output, withIntermediateDirectories: true)
            var results: [[String: Any]] = []
            let files = (try? fm.contentsOfDirectory(at: input, includingPropertiesForKeys: nil)) ?? []
            for url in files.sorted(by: { $0.path < $1.path }) {
                for (i, buf) in frames(url).enumerated() {
                    var r = handle(buf, url.lastPathComponent)
                    r["file"] = url.lastPathComponent
                    r["frame"] = i
                    results.append(r)
                }
            }
            let json = try? JSONSerialization.data(withJSONObject: ["results": results], options: .prettyPrinted)
            try? json?.write(to: output.appendingPathComponent("done.json"))
        }
    }

    /// A still image gives one frame, a video every frame (32BGRA, like the camera's usual format).
    static func frames(_ url: URL) -> [CVPixelBuffer] {
        let ext = url.pathExtension.lowercased()
        if ["mov", "mp4", "m4v"].contains(ext) {
            return videoFrames(url)
        }
        guard let image = UIImage(contentsOfFile: url.path) else { return [] }
        var cg = image.cgImage
        if cg == nil, let ci = image.ciImage ?? CIImage(image: image) {
            cg = CIContext().createCGImage(ci, from: ci.extent)
        }
        guard let cg, let buf = pixelBuffer(cg) else { return [] }
        return [buf]
    }

    static func videoFrames(_ url: URL) -> [CVPixelBuffer] {
        let asset = AVURLAsset(url: url)
        let semaphore = DispatchSemaphore(value: 0)
        var track: AVAssetTrack?
        Task {
            track = try? await asset.loadTracks(withMediaType: .video).first
            semaphore.signal()
        }
        semaphore.wait()
        guard let track, let reader = try? AVAssetReader(asset: asset) else { return [] }
        let out = AVAssetReaderTrackOutput(track: track, outputSettings: [
            kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA,
        ])
        out.alwaysCopiesSampleData = false
        guard reader.canAdd(out) else { return [] }
        reader.add(out)
        guard reader.startReading() else { return [] }
        var buffers: [CVPixelBuffer] = []
        while let sample = out.copyNextSampleBuffer() {
            if let buf = CMSampleBufferGetImageBuffer(sample) {
                buffers.append(buf)
            }
        }
        return buffers
    }

    /// Draws a CGImage into a new 32BGRA pixel buffer of the same size.
    static func pixelBuffer(_ image: CGImage) -> CVPixelBuffer? {
        let attrs: [String: Any] = [
            kCVPixelBufferCGImageCompatibilityKey as String: true,
            kCVPixelBufferCGBitmapContextCompatibilityKey as String: true,
            kCVPixelBufferIOSurfacePropertiesKey as String: [:],
        ]
        var buffer: CVPixelBuffer?
        guard CVPixelBufferCreate(kCFAllocatorDefault, image.width, image.height,
                                  kCVPixelFormatType_32BGRA, attrs as CFDictionary, &buffer) == kCVReturnSuccess,
              let buf = buffer else { return nil }
        CVPixelBufferLockBaseAddress(buf, [])
        defer { CVPixelBufferUnlockBaseAddress(buf, []) }
        guard let ctx = CGContext(data: CVPixelBufferGetBaseAddress(buf), width: image.width, height: image.height,
                                  bitsPerComponent: 8, bytesPerRow: CVPixelBufferGetBytesPerRow(buf),
                                  space: CGColorSpaceCreateDeviceRGB(),
                                  bitmapInfo: CGImageAlphaInfo.premultipliedFirst.rawValue
                                      | CGBitmapInfo.byteOrder32Little.rawValue) else { return nil }
        ctx.draw(image, in: CGRect(x: 0, y: 0, width: image.width, height: image.height))
        return buf
    }
}
```

Then call it where the camera would start:

```swift
// At camera start:
if CameraTest.isOn {
    CameraTest.run { buf, _ in model.process(buf) }   // return a dictionary per frame
    return
}
```

`model.process` stands for whatever your app does with a frame. Return what you want to see in `done.json`, such as labels, confidences and bounding boxes. To get images back as well, write them into `CameraTest.output` from your handler; every file there is copied to this computer.

A few notes:

- `videoFrames` keeps every frame of a video in memory, which is fine for a few seconds of test footage. For longer clips, process each sample inside the `while` loop instead of collecting the buffers, as a real camera callback would.
- The hook only runs with the `-cameraTestMode` launch argument, so normal launches are unaffected. You can leave it in debug builds or wrap it in `#if DEBUG`.
- Still images come in as stored, without applying their EXIF orientation, which is also how camera frames arrive. Handle orientation the way your camera path does.
- Write `done.json` last. iOS Studio stops waiting as soon as it appears.
- `JSONSerialization` accepts strings, numbers, booleans, arrays, dictionaries and `NSNull`. Convert anything else (CGRect, enums) before returning it.

## Run it from iOS Studio

Open the iPhone page, then Files › Camera test:

1. Pick the app. Only your own apps on the phone are listed.
2. Add images or videos with Add images or videos, or drop them on the list. The list is kept for the next run.
3. Change the launch arguments, environment variables or timeout if your hook needs it.
4. Press Run test and keep the iPhone unlocked.

The steps tick off as they finish. Then you see `done.json` formatted, the images the app wrote (click one to open it), the app's log during the run, and a link to the crash report if the app crashed. Open folder shows all the copied outputs.

If the run ends with "No done.json", the app either doesn't have the hook, wasn't started with `-cameraTestMode`, or ran out of time. The log usually shows which.

## Run it from an AI agent

The MCP server's `ios_camera_test` tool does the same run (see [mcp.md](mcp.md)):

```
ios_camera_test(project_path="MyApp", inputs=["~/Pictures/tests/cat.jpg", "~/Pictures/tests/street.mov"])
```

`bundle_id` is the app's bundle id; pass `project_path` (a project name or path) instead to use its bundle id. The result has a summary of `done.json` (the number of results, the most common values of each text field and min/mean/max of each number field, and the first few results), the path of the full `done.json`, the output files, the key log lines and any new crash with its top frames. A run longer than about 20 seconds comes back with `state: "running"` and a `test_id`; the agent calls `ios_camera_test(test_id=...)` again to wait for it.

An agent can use this loop to change the processing code, rebuild with `ios_run`, and rerun the same inputs to compare results.

## Under the hood

The daemon command is `cameratest.run` (see [deviced-protocol.md](deviced-protocol.md#files)). It goes through the daemon's one connection to the phone. The files travel over house_arrest (the app's container), the launch uses the same CoreDevice call as Open on iPhone, and the log comes from the shared log stream filtered to the app.
