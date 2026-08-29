import Foundation
import ShellTestSupport
import Testing

@testable import GPTVoiceCodingShell

@Suite struct CredentialFileObserverTests {
    @Test func creatingTheMissingCredentialFileRaisesAChange() async throws {
        let fixture = try TelegramCredentialFixture()
        let changes = ChangeCounter()
        let observer = try CredentialFileObserver(path: fixture.environmentPath) {
            changes.record()
        }
        defer { observer.cancel() }

        try fixture.writeEnvironment("A_TELEGRAM_TOKEN=ready\n")

        #expect(await changes.waitForChange())
    }

    @Test func repairingUnsafePermissionsRaisesAChange() async throws {
        let fixture = try TelegramCredentialFixture()
        try fixture.writeEnvironment("A_TELEGRAM_TOKEN=ready\n", mode: 0o644)
        let changes = ChangeCounter()
        let observer = try CredentialFileObserver(path: fixture.environmentPath) {
            changes.record()
        }
        defer { observer.cancel() }

        #expect(chmod(fixture.environmentPath, 0o600) == 0)

        #expect(await changes.waitForChange())
    }

    @Test func anAtomicReplacementKeepsTheReplacementUnderObservation() async throws {
        let fixture = try TelegramCredentialFixture()
        try fixture.writeEnvironment("A_TELEGRAM_TOKEN=first\n")
        let changes = ChangeCounter()
        let observer = try CredentialFileObserver(path: fixture.environmentPath) {
            changes.record()
        }
        defer { observer.cancel() }

        _ = try fixture.credentials.save(token: "second")
        #expect(await changes.waitForChange())
        let afterReplacement = changes.value
        try fixture.writeEnvironment("A_TELEGRAM_TOKEN=third\n")

        #expect(await changes.waitForMore(than: afterReplacement))
    }
}

private final class ChangeCounter: @unchecked Sendable {
    private let lock = NSLock()
    private var count = 0

    func record() {
        lock.withLock { count += 1 }
    }

    var value: Int { lock.withLock { count } }

    func waitForChange(within seconds: TimeInterval = 3) async -> Bool {
        let deadline = Date().addingTimeInterval(seconds)
        while Date() < deadline {
            if lock.withLock({ count > 0 }) { return true }
            try? await Task.sleep(for: .milliseconds(10))
        }
        return false
    }

    func waitForMore(than previous: Int, within seconds: TimeInterval = 3) async -> Bool {
        let deadline = Date().addingTimeInterval(seconds)
        while Date() < deadline {
            if value > previous { return true }
            try? await Task.sleep(for: .milliseconds(10))
        }
        return false
    }
}
