// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "Glance",
    platforms: [.iOS(.v17)],
    products: [.library(name: "Glance", targets: ["Glance"])],
    dependencies: [
        .package(url: "https://github.com/apple/swift-algorithms", from: "1.2.0"),
    ],
    targets: [
        .target(
            name: "Glance",
            dependencies: [
                .product(name: "Algorithms", package: "swift-algorithms"),
            ]
        )
    ]
)
