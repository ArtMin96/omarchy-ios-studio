// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "Weather",
    products: [.library(name: "Weather", targets: ["Weather"])],
    dependencies: [
        .package(path: "../WeatherKitMock"),
        .package(url: "https://github.com/apple/swift-algorithms", from: "1.2.0"),
    ],
    targets: [
        .target(
            name: "Weather",
            dependencies: [
                .product(name: "WeatherKitMock", package: "WeatherKitMock"),
                .product(name: "Algorithms", package: "swift-algorithms"),
            ]
        ),
    ]
)
