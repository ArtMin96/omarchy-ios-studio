// swift-tools-version: 5.9
// iOS Studio hot reload runtime (copied here by iOS Studio; turn hot reload off in iOS Studio to remove it).
// Debug builds get the loader; release builds of this package contain no loader code.
import PackageDescription

let package = Package(
    name: "hot-reload",
    platforms: [.iOS(.v17)],
    products: [
        .library(name: "HotReload", targets: ["HotReload"]),
    ],
    targets: [
        .target(
            name: "HotReloadLoader",
            cSettings: [.define("IOSSTUDIO_HOT_RELOAD", to: "1", .when(configuration: .debug))],
            linkerSettings: [.linkedFramework("CoreFoundation")]
        ),
        .target(
            name: "HotReload",
            dependencies: ["HotReloadLoader"],
            swiftSettings: [.define("IOSSTUDIO_HOT_RELOAD", .when(configuration: .debug))]
        ),
    ]
)
