// swift-tools-version: 5.5
import PackageDescription

let package = Package(
    name: "Photos",
    products: [.library(name: "Photos", targets: ["Photos"])],
    dependencies: [
        .package(name: "Lottie", url: "https://github.com/airbnb/lottie-spm.git", from: "4.4.0"),
    ],
    targets: [
        .target(name: "Photos", dependencies: [
            .product(name: "Lottie", package: "Lottie"),
        ]),
    ]
)
