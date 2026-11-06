// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "Solo",
    products: [.library(name: "Solo", targets: ["Solo"])],
    dependencies: [
        .package(url: "https://github.com/apple/swift-collections", from: "1.1.0"),
    ],
    targets: [
        .target(
            name: "Solo",
            dependencies: [
                .product(name: "Collections", package: "swift-collections"),
                .product(name: "DequeModule", package: "swift-collections"),
            ]
        ),
        .testTarget(name: "SoloTests", dependencies: ["Solo", .product(name: "Collections", package: "swift-collections")]),
    ]
)
