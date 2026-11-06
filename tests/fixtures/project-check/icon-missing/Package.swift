// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "App",
    platforms: [.iOS(.v17)],
    products: [.library(name: "App", targets: ["App"])],
    targets: [.target(name: "App")]
)
