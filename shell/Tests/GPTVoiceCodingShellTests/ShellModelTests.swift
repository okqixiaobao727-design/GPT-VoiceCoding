import Foundation
import ShellCore
import ShellTestSupport
import Testing

@testable import GPTVoiceCodingShell

@MainActor
@Suite struct ShellModelTests {
    @Test func aMissingNamedVariableStopsAtPreflight() throws {
        let fixture = try TelegramCredentialFixture()

        let state = ShellModel.preflight(credentials: fixture.credentials)

        #expect(state == .missing)
    }

    @Test func closingTheCredentialRowClearsAFailedSaveHint() throws {
        let sourceURL = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()  // GPTVoiceCodingShellTests
            .deletingLastPathComponent()  // Tests
            .deletingLastPathComponent()  // shell
            .appendingPathComponent("Sources/GPTVoiceCodingShell/ControlPanelView.swift")
        let source = try String(contentsOf: sourceURL, encoding: .utf8)
        let row =
            source.components(separatedBy: "private struct TelegramCredentialRow: View").last?
            .components(separatedBy: "private struct EngineHealthRow: View").first ?? ""

        #expect(row.contains(".onDisappear { shell.clearCredentialSaveFailure() }"))
    }
}
