// swift-tools-version: 6.0
import PackageDescription

// A local target named like a product of the package: the bare "Collections" is the target and stays.
let package = Package(
    name: "Mixed",
    products: [.library(name: "Mixed", targets: ["Mixed"])],
    dependencies: [
        .package(url: "https://github.com/apple/swift-collections", from: "1.1.0"),
    ],
    targets: [
        .target(name: "Collections"),
        .target(
            name: "Mixed",
            dependencies: [
                "Collections",
                "DequeModule",
                .product(name: "OrderedCollections", package: "swift-collections", condition: .when(platforms: [.iOS])),
            ]
        ),
    ]
)
