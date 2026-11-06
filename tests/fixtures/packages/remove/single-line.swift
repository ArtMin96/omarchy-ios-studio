// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "Tiny",
    products: [.library(name: "Tiny", targets: ["Tiny"])],
    dependencies: [.package(url: "https://github.com/apple/swift-collections", from: "1.1.0"), .package(url: "https://github.com/apple/swift-log.git", from: "1.5.0")],
    targets: [.target(name: "Tiny", dependencies: [.product(name: "Logging", package: "swift-log"), .product(name: "Collections", package: "swift-collections")])]
)
