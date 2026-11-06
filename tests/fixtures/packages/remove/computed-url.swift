// swift-tools-version: 6.0
import PackageDescription

let apple = "https://github.com/apple"

let package = Package(
    name: "Computed",
    products: [.library(name: "Computed", targets: ["Computed"])],
    dependencies: [
        .package(url: apple + "/swift-collections", from: "1.1.0"),
        .package(url: "\(apple)/swift-log.git", from: "1.5.0"),
    ],
    targets: [
        .target(name: "Computed", dependencies: [.product(name: "Collections", package: "swift-collections")]),
    ]
)
