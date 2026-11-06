// swift-tools-version:5.3
import PackageDescription

let package = Package(
    name: "Legacy",
    products: [.library(name: "Legacy", targets: ["Legacy"])],
    dependencies: [
        .package(url: "https://github.com/Alamofire/Alamofire.git", .upToNextMajor(from: "5.8.0")),
        .package(url: "https://github.com/SnapKit/SnapKit.git", from: "5.7.0"),
    ],
    targets: [
        .target(name: "Core"),
        .target(name: "Legacy", dependencies: ["Alamofire", "Core", "SnapKit"]),
        .testTarget(name: "LegacyTests", dependencies: ["Legacy", .byName(name: "Alamofire")]),
    ]
)
