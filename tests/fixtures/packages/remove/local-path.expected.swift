// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "Weather",
    products: [.library(name: "Weather", targets: ["Weather"])],
    dependencies: [
        .package(url: "https://github.com/apple/swift-algorithms", from: "1.2.0"),
    ],
    targets: [
        .target(
            name: "Weather",
            dependencies: [
                .product(name: "Algorithms", package: "swift-algorithms"),
            ]
        ),
    ]
)
