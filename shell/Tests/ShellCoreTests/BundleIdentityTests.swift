import Foundation
import Testing

@testable import ShellCore

/// The bundle's identity is a file, and three of its keys are load-bearing
/// rather than decorative. They are asserted here because they are cheap to
/// state, easy to lose in a merge, and each one is a decision an ADR made.
@Suite struct BundleIdentityTests {
    private var plist: [String: Any] {
        // `#filePath` rather than a bundle resource: this asserts what is in the
        // repository, which is what #12 will consume.
        let url = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()  // ShellCoreTests
            .deletingLastPathComponent()  // Tests
            .deletingLastPathComponent()  // shell
            .appendingPathComponent("Resources/Info.plist")
        let data = (try? Data(contentsOf: url)) ?? Data()
        return (try? PropertyListSerialization.propertyList(from: data, format: nil))
            as? [String: Any] ?? [:]
    }

    @Test func itIsAMenuBarAppAndNotADockOne() {
        // Launch at login means a menu-bar item, not a window.
        #expect(plist["LSUIElement"] as? Bool == true)
    }

    @Test func theGrantHasABundleToAttachTo() {
        // ADR 0005: bundle containment is what earns the microphone grant, so
        // this identifier is what the grant belongs to.
        let identifier = plist["CFBundleIdentifier"] as? String
        #expect(identifier?.isEmpty == false)
    }

    @Test func macOSHasTheAppsOwnSentenceToShow() {
        // The probe confirmed it is this string the user sees, beside the app's
        // name — the bundled interpreter never appears.
        let usage = plist["NSMicrophoneUsageDescription"] as? String
        #expect(usage?.isEmpty == false)
    }

    @Test func thePlistNamesTheExecutableTheScriptCopies() {
        #expect(plist["CFBundleExecutable"] as? String == "GPTVoiceCodingShell")
    }

    @Test func theBundledEnginePathIsCodeSideAndSingular() {
        // The one name a plist cannot hold, kept in one line for #12 to change.
        #expect(BundleLayout.engineInterpreterRelativePath == "engine/bin/python3")
    }
}
