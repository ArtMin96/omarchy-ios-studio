// swift-tools-version: 6.0
import PackageDescription

/* Dependencies are pinned in Package.resolved. /* nested comment */ still a comment */
let package = Package(
    name: "Notes",
    products: [.library(name: "Notes", targets: ["Notes"])],
    dependencies: [
        // .package(url: "https://github.com/apple/swift-collections", branch: "main"),
        /* charts */ .package(url: "https://github.com/danielgindi/Charts.git", from: "5.0.0"),
        .package(url: "https://github.com/apple/swift-log.git", from: "1.5.0"), /* logging */
    ],
    targets: [
        .target(
            name: "Notes",
            dependencies: [
                .product(name: "Logging", package: "swift-log"),
            ]
        ),
    ]
)
