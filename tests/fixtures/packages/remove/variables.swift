// swift-tools-version: 6.0
import PackageDescription

let externalDependencies: [Package.Dependency] = [
    Package.Dependency.package(url: "https://github.com/apple/swift-collections", from: "1.1.0"),
    .package(url: "https://github.com/apple/swift-log.git", from: "1.5.0"),
]

let shared: [Target.Dependency] = [
    .product(name: "Collections", package: "swift-collections"),
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
