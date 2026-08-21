// swift-tools-version: 6.0
import PackageDescription

// The menu-bar shell is a SwiftPM package, not an Xcode project: it has to build
// from a checkout that has only the Command Line Tools, and CI has no reason to
// hold an .xcodeproj for two targets. `shell/scripts/dev-app.sh` wraps the built
// executable in the .app the shell needs in order to be a menu-bar app at all.
let package = Package(
    name: "GPTVoiceCodingShell",
    platforms: [.macOS(.v14)],
    products: [
        .executable(name: "GPTVoiceCodingShell", targets: ["GPTVoiceCodingShell"]),
        .library(name: "ShellCore", targets: ["ShellCore"]),
    ],
    dependencies: [.package(url: "https://github.com/swiftlang/swift-testing.git", from: "6.0.0")],
    targets: [
        // Everything that can be decided without a window. The app target holds
        // the views and nothing else, so the shell's actual behaviour is testable
        // without one.
        .target(name: "ShellCore"),
        .executableTarget(name: "GPTVoiceCodingShell", dependencies: ["ShellCore"]),
        .testTarget(
            name: "ShellCoreTests",
            dependencies: ["ShellCore", .product(name: "Testing", package: "swift-testing")]),
    ]
)
