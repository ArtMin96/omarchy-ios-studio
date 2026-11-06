// swift-tools-version: 6.0

import PackageDescription

let package = Package(
    name: "PkgDemo",
    platforms: [
        .iOS(.v17),
        .macOS(.v14),
    ],
    products: [
        // An xtool project should contain exactly one library product,
        // representing the main app.
        .library(
            name: "PkgDemo",
            targets: ["PkgDemo"]
        ),
    ],
    dependencies: [
        .package(url: "https://github.com/apple/swift-algorithms", from: "1.2.0"),
    ],
    targets: [
        .target(
            name: "PkgDemo",
            dependencies: [
                .product(name: "Algorithms", package: "swift-algorithms"),
            ]
        ),
    ]
)
