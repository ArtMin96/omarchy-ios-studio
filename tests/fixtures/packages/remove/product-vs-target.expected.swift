// swift-tools-version: 6.0
import PackageDescription

// A local target named like a product of the package: the bare "Collections" is the target and stays.
let package = Package(
    name: "Mixed",
    products: [.library(name: "Mixed", targets: ["Mixed"])],
    dependencies: [],
    targets: [
        .target(name: "Collections"),
        .target(
            name: "Mixed",
            dependencies: [
                "Collections",
            ]
        ),
    ]
)
