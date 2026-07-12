// swift-tools-version: 6.0

import PackageDescription

let package = Package(
    name: "TimeLapseNative",
    platforms: [
        .macOS(.v15),
    ],
    products: [
        .executable(name: "TimeLapseNative", targets: ["TimeLapseNative"]),
    ],
    targets: [
        .executableTarget(
            name: "TimeLapseNative",
            linkerSettings: [
                .linkedFramework("Security"),
            ]
        ),
        .testTarget(name: "TimeLapseNativeTests", dependencies: ["TimeLapseNative"]),
    ]
)
