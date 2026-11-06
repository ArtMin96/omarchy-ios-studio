// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "Crlf",
    products: [.library(name: "Crlf", targets: ["Crlf"])],
    dependencies: [
        .package(url: "https://github.com/apple/swift-collections", from: "1.1.0"),
        .package(url: "https://github.com/apple/swift-log.git", from: "1.5.0"),
    ],
    targets: [
        .target(name: "Crlf", dependencies: [
            .product(name: "Collections", package: "swift-collections"),
        ]),
    ]
)
