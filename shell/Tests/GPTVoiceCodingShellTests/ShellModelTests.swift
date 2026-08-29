import Foundation
import ShellTestSupport
import Testing

@testable import GPTVoiceCodingShell
@testable import ShellCore

@MainActor
@Suite struct ShellModelTests {
    @Test func aRepairedCredentialStartsThePreflightHeldEngineExactlyOnce() async throws {
        let shell = try ShellHarness()
        await shell.model.startEngineAfterInstallation()

        #expect(shell.model.credentialState == .missing)
        #expect(shell.launcher.launchCount == 0)

        try shell.fixture.writeEnvironment("A_TELEGRAM_TOKEN=ready\n")
        #expect(await shell.launcher.waitForLaunches(1))
        try shell.fixture.writeEnvironment("A_TELEGRAM_TOKEN=still-ready\n")
        try? await Task.sleep(for: .milliseconds(50))

        #expect(shell.model.credentialState == .ready)
        #expect(shell.launcher.launchCount == 1)
        await shell.model.stopEngine()
    }

    @Test func invalidIntermediateCredentialsDoNotReleaseThePreflightHold() async throws {
        let shell = try ShellHarness()
        await shell.model.startEngineAfterInstallation()

        try shell.fixture.writeEnvironment("not-an-assignment\n")
        #expect(
            await waitForShellState {
                if case .unreadable = shell.model.credentialState { return true }
                return false
            })
        #expect(shell.launcher.launchCount == 0)

        try shell.fixture.writeEnvironment("A_TELEGRAM_TOKEN=ready\n")
        #expect(await shell.launcher.waitForLaunches(1))
        await shell.model.stopEngine()
    }

    @Test func repairingTheCredentialCannotRestartAnAlreadyRunningEngine() async throws {
        let shell = try ShellHarness()
        await shell.model.startEngineAfterInstallation()
        await shell.supervisor.start()
        #expect(await shell.launcher.waitForLaunches(1))
        #expect(
            await waitForShellState {
                if case .running = shell.model.health { return true }
                return false
            })

        try shell.fixture.writeEnvironment("A_TELEGRAM_TOKEN=ready\n")
        #expect(await waitForShellState { shell.model.credentialState == .ready })
        try? await Task.sleep(for: .milliseconds(50))

        #expect(shell.launcher.launchCount == 1)
        await shell.model.stopEngine()
    }

    @Test func panelSaveKeepsItsSingleOrderlyStartWhilePreflightIsHeld() async throws {
        let shell = try ShellHarness()
        await shell.model.startEngineAfterInstallation()

        #expect(await shell.model.saveTelegramToken("ready"))
        #expect(await shell.launcher.waitForLaunches(1))
        try? await Task.sleep(for: .milliseconds(50))

        #expect(shell.launcher.launchCount == 1)
        await shell.model.stopEngine()
    }

    @Test func aPreflightHeldEngineStartsOnceWhenTheCredentialBecomesReady() {
        var recovery = CredentialStartRecovery()

        #expect(recovery.prepare(for: .missing) == .watch)
        #expect(recovery.credentialChanged(to: .ready, health: .notStarted) == .start)
        #expect(recovery.credentialChanged(to: .ready, health: .notStarted) == .none)
    }

    @Test func invalidCredentialChangesKeepThePreflightHoldOpen() {
        var recovery = CredentialStartRecovery()
        let invalidStates: [TelegramCredentials.State] = [
            .missing,
            .unsafe(.permissions(path: "/tmp/unsafe")),
            .unreadable(
                .environment(path: "/tmp/malformed", problem: .missingAssignment(line: 1))),
        ]

        #expect(recovery.prepare(for: .missing) == .watch)
        for state in invalidStates {
            #expect(recovery.credentialChanged(to: state, health: .notStarted) == .none)
        }
        #expect(recovery.credentialChanged(to: .ready, health: .notStarted) == .start)
    }

    @Test func aCredentialChangeCannotRestartAnEngineThatIsAlreadyRunning() {
        var recovery = CredentialStartRecovery()

        #expect(recovery.prepare(for: .missing) == .watch)
        #expect(recovery.credentialChanged(to: .ready, health: .running(pid: 123)) == .none)
        #expect(recovery.credentialChanged(to: .ready, health: .notStarted) == .none)
    }

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

@MainActor
private final class ShellHarness {
    let fixture: TelegramCredentialFixture
    let launcher: RecordingEngineLauncher
    let supervisor: EngineSupervisor
    let model: ShellModel

    init() throws {
        let fixture = try TelegramCredentialFixture()
        let launcher = RecordingEngineLauncher()
        let socketPath = fixture.directory.appendingPathComponent("engine.sock").path
        let supervisor = EngineSupervisor(
            launcher: launcher,
            socketPath: socketPath,
            resolveCommand: {
                EngineCommand(
                    executable: "/usr/bin/true", arguments: [], source: .developerPath)
            })
        self.fixture = fixture
        self.launcher = launcher
        self.supervisor = supervisor
        model = ShellModel(
            location: EngineLocation(configPath: fixture.configPath, socketPath: socketPath),
            credentials: fixture.credentials,
            panel: ControlPanel(client: UnreachableControlPlane()),
            supervisor: supervisor)
    }
}

@MainActor
private func waitForShellState(
    within seconds: TimeInterval = 3, _ condition: @escaping @MainActor () -> Bool
) async -> Bool {
    let deadline = Date().addingTimeInterval(seconds)
    while Date() < deadline {
        if condition() { return true }
        try? await Task.sleep(for: .milliseconds(10))
    }
    return false
}

private struct UnreachableControlPlane: ControlPlaneDialing {
    func ask(_ request: Request) async throws -> Reply {
        throw ControlPlaneFailure.engineUnreachable("not connected in this shell test")
    }
}

private final class RecordingEngineLauncher: EngineLaunching, @unchecked Sendable {
    private let lock = NSLock()
    private var launches = 0

    var launchCount: Int { lock.withLock { launches } }

    func launch(
        _ command: EngineCommand,
        stderr: @escaping @Sendable (Data) -> Void,
        exited: @escaping @Sendable (Int32) -> Void
    ) throws -> EngineProcess {
        let pid = lock.withLock {
            launches += 1
            return Int32(launches + 2000)
        }
        return HeldEngineProcess(pid: pid, exited: exited)
    }

    func waitForLaunches(_ expected: Int, within seconds: TimeInterval = 3) async -> Bool {
        let deadline = Date().addingTimeInterval(seconds)
        while Date() < deadline {
            if launchCount >= expected { return true }
            try? await Task.sleep(for: .milliseconds(10))
        }
        return false
    }
}

private final class HeldEngineProcess: EngineProcess, @unchecked Sendable {
    let processIdentifier: Int32
    private let exited: @Sendable (Int32) -> Void
    private let lock = NSLock()
    private var hasExited = false

    init(pid: Int32, exited: @escaping @Sendable (Int32) -> Void) {
        processIdentifier = pid
        self.exited = exited
    }

    func requestStop() {
        let shouldExit = lock.withLock {
            guard !hasExited else { return false }
            hasExited = true
            return true
        }
        if shouldExit { exited(-SIGTERM) }
    }

    func forceStop() {
        requestStop()
    }
}
