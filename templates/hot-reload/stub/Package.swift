// swift-tools-version: 5.9
// iOS Studio hot reload, turned off: the same API as the runtime, doing nothing, so code that still uses it
// builds. No loader and no linker flags. Remove `import HotReload` from your sources and iOS Studio removes this.
import PackageDescription

let package = Package(
    name: "hot-reload",
    platforms: [.iOS(.v17)],
    products: [
        .library(name: "HotReload", targets: ["HotReload"]),
    ],
    targets: [
        .target(name: "HotReload"),
    ]
)
