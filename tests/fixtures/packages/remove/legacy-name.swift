// swift-tools-version: 5.5
import PackageDescription

let package = Package(
    name: "Photos",
    products: [.library(name: "Photos", targets: ["Photos"])],
    dependencies: [
        .package(name: "Kingfisher", url: "https://github.com/onevcat/Kingfisher.git", from: "7.0.0"),
        .package(name: "Lottie", url: "https://github.com/airbnb/lottie-spm.git", from: "4.4.0"),
    ],
    targets: [
        .target(name: "Photos", dependencies: [
            .product(name: "Kingfisher", package: "Kingfisher"),
            .product(name: "Lottie", package: "Lottie"),
        ]),
    ]
)
