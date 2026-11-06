// swift-tools-version: 5.5
import PackageDescription

let package = Package(
    name: "Photos",
    products: [.library(name: "Photos", targets: ["Photos"])],
    dependencies: [
        .package(name: "Kingfisher", url: "https://github.com/onevcat/Kingfisher.git", from: "7.0.0"),
    ],
    targets: [
        .target(name: "Photos", dependencies: [
            .product(name: "Kingfisher", package: "Kingfisher"),
        ]),
    ]
)
