// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "Appender",
    products: [.library(name: "Appender", targets: ["Appender"])],
    targets: [
        .target(name: "Appender", dependencies: [.product(name: "Collections", package: "swift-collections")]),
    ]
)

package.dependencies.append(.package(url: "https://github.com/apple/swift-collections", from: "1.1.0"))
