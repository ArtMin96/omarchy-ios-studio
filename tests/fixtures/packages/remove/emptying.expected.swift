// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "Solo",
    products: [.library(name: "Solo", targets: ["Solo"])],
    dependencies: [],
    targets: [
        .target(
            name: "Solo",
            dependencies: []
        ),
        .testTarget(name: "SoloTests", dependencies: ["Solo"]),
    ]
)
