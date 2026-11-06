// swift-tools-version: 6.0
import PackageDescription

/* Dependencies are pinned in Package.resolved. /* nested comment */ still a comment */
let package = Package(
    name: "Notes",
    products: [.library(name: "Notes", targets: ["Notes"])],
    dependencies: [
        // .package(url: "https://github.com/apple/swift-collections", branch: "main"),
        .package(url: "https://github.com/apple/swift-collections", from: "1.1.0"), // data structures
        /* charts */ .package(url: "https://github.com/danielgindi/Charts.git", from: "5.0.0"),
    ],
    targets: [
        .target(
            name: "Notes",
            dependencies: [
                .product(name: "DequeModule", package: "swift-collections"), // queue for sync
            ]
        ),
    ]
)
