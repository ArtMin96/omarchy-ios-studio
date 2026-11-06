// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "Shop",
    products: [.library(name: "Shop", targets: ["Shop"])],
    dependencies: [
        .package(
            url: "https://github.com/pointfreeco/swift-composable-architecture",
            from: "1.15.0"
        ),
    ],
    targets: [
        .target(
            name: "Shop",
            dependencies: [
                .product(
                    name: "ComposableArchitecture",
                    package: "swift-composable-architecture"
                ),
            ]
        ),
    ]
)
