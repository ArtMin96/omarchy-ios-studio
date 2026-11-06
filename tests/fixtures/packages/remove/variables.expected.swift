// swift-tools-version: 6.0
import PackageDescription

let externalDependencies: [Package.Dependency] = [
    .package(url: "https://github.com/apple/swift-log.git", from: "1.5.0"),
]

let shared: [Target.Dependency] = [
    .product(name: "Logging", package: "swift-log"),
]

let package = Package(
    name: "Vars",
    products: [.library(name: "Vars", targets: ["Vars"])],
    dependencies: externalDependencies,
    targets: [
        .target(name: "Vars", dependencies: shared),
    ]
)
