// swift-tools-version: 6.0
import PackageDescription

let banner = #"Use .package(url: "https://github.com/apple/swift-collections", from: "1.0.0"), not ")"."#
let notes = """
    dependencies: [
        .package(url: "https://github.com/apple/swift-collections", from: "1.1.0"),
    ]
    """
let root = "Sources"

let package = Package(
    name: "Odd",
    products: [.library(name: "Odd", targets: ["Odd"])],
    dependencies: [
        .package(url: "https://example.com/team(a),b/swift-collections.git", from: "1.1.0"),
        .package(url: "https://github.com/apple/swift-log.git", from: "1.5.0"),
    ],
    targets: [
        .target(
            name: "Odd",
            dependencies: [
                .product(name: "Collections", package: "swift-collections"),
                .product(name: "Logging", package: "swift-log"),
            ],
            path: "\(root)/Odd",
            swiftSettings: [.unsafeFlags(["-Xfrontend", "-warn-long-expression-type-checking=200"])]
        ),
    ]
)
