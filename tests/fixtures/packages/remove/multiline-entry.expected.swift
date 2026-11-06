// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "Shop",
    products: [.library(name: "Shop", targets: ["Shop"])],
    dependencies: [
        .package(
            url: "https://github.com/onevcat/Kingfisher.git",
            "8.0.0" ..< "9.0.0"
        ),
    ],
    targets: [
        .target(
            name: "Shop",
            dependencies: [
                .product(name: "Kingfisher", package: "Kingfisher"),
            ]
        ),
    ]
)
