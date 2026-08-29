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
        #expect(await waitUntil { shell.launcher.launchCount >= 1 })
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
            await waitUntil {
                if case .unreadable = shell.model.credentialState { return true }
                return false
            })
        #expect(shell.launcher.launchCount == 0)

        try shell.fixture.writeEnvironment("A_TELEGRAM_TOKEN=ready\n")
        #expect(await waitUntil { shell.launcher.launchCount >= 1 })
        await shell.model.stopEngine()
    }

    @Test func aCredentialThatChangesDuringSpawnCanBeRepairedAgain() async throws {
        let fixture = try TelegramCredentialFixture()
        let launcher = CredentialRacingLauncher(fixture: fixture)
        let (_, model) = makeShell(fixture: fixture, launcher: launcher)
        await model.startEngineAfterInstallation()

        try fixture.writeEnvironment("A_TELEGRAM_TOKEN=first-repair\n")
        #expect(
            await waitUntil {
                if case .cannotSpawn = model.health { return true }
                return false
            })
        try fixture.writeEnvironment("A_TELEGRAM_TOKEN=second-repair\n")

        #expect(await waitUntil { launcher.launchCount >= 2 })
        #expect(await waitUntil { model.credentialState == .ready })
        await model.stopEngine()
    }

    @Test func aNonCredentialSpawnFailureDoesNotBecomeACredentialRetry() async throws {
        let fixture = try TelegramCredentialFixture()
        let launcher = NonCredentialFailingLauncher()
        let (_, model) = makeShell(fixture: fixture, launcher: launcher)
        await model.startEngineAfterInstallation()

        try fixture.writeEnvironment("A_TELEGRAM_TOKEN=first-repair\n")
        #expect(await waitUntil { launcher.launchCount >= 1 })
        #expect(
            await waitUntil {
                if case .cannotSpawn = model.health { return true }
                return false
            })

        try fixture.writeEnvironment("A_TELEGRAM_TOKEN=duplicate-ready\n")
        try? await Task.sleep(for: .milliseconds(50))

        #expect(launcher.launchCount == 1)
        await model.stopEngine()
    }

    @Test func repairingTheCredentialCannotRestartAnAlreadyRunningEngine() async throws {
        let shell = try ShellHarness()
        await shell.model.startEngineAfterInstallation()
        await shell.supervisor.start()
        #expect(await waitUntil { shell.launcher.launchCount >= 1 })
        #expect(
            await waitUntil {
                if case .running = shell.model.health { return true }
                return false
            })

        try shell.fixture.writeEnvironment("A_TELEGRAM_TOKEN=ready\n")
        try? await Task.sleep(for: .milliseconds(50))

        #expect(shell.launcher.launchCount == 1)
        await shell.model.stopEngine()
    }

    @Test func panelSaveKeepsItsSingleOrderlyStartWhilePreflightIsHeld() async throws {
        let shell = try ShellHarness()
        await shell.model.startEngineAfterInstallation()

        #expect(await shell.model.saveTelegramToken("ready"))
        #expect(await waitUntil { shell.launcher.launchCount >= 1 })
        try? await Task.sleep(for: .milliseconds(50))

        #expect(shell.launcher.launchCount == 1)
        await shell.model.stopEngine()
    }

    @Test func aPreflightHeldEngineStartsOnceWhenTheCredentialBecomesReady() {
        var recovery = CredentialStartRecovery()

        #expect(recovery.prepare(for: .missing) == .watch)
        #expect(recovery.credentialChanged(to: .ready, health: .notStarted) == .start)
        #expect(recovery.credentialChanged(to: .ready, health: .notStarted) == .none)
        #expect(
            recovery.engineChanged(to: .running(pid: 123), credentialState: .ready)
                == .stopWatching)
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
        #expect(
            recovery.credentialChanged(to: .ready, health: .running(pid: 123))
                == .stopWatching)
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
        let (supervisor, model) = makeShell(fixture: fixture, launcher: launcher)
        self.fixture = fixture
        self.launcher = launcher
        self.supervisor = supervisor
        self.model = model
    }
}

@MainActor
private func makeShell(
    fixture: TelegramCredentialFixture, launcher: any EngineLaunching
) -> (supervisor: EngineSupervisor, model: ShellModel) {
    let socketPath = fixture.directory.appendingPathComponent("engine.sock").path
    let supervisor = EngineSupervisor(
        launcher: launcher,
        socketPath: socketPath,
        resolveCommand: {
            EngineCommand(executable: "/usr/bin/true", arguments: [], source: .developerPath)
        })
    let model = ShellModel(
        location: EngineLocation(configPath: fixture.configPath, socketPath: socketPath),
        credentials: fixture.credentials,
        panel: ControlPanel(client: UnreachableControlPlane()),
        supervisor: supervisor)
    return (supervisor, model)
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

}

private final class CredentialRacingLauncher: EngineLaunching, @unchecked Sendable {
    private let fixture: TelegramCredentialFixture
    private let lock = NSLock()
    private var launches = 0

    var launchCount: Int { lock.withLock { launches } }

    init(fixture: TelegramCredentialFixture) {
        self.fixture = fixture
    }

    func launch(
        _ command: EngineCommand,
        stderr: @escaping @Sendable (Data) -> Void,
        exited: @escaping @Sendable (Int32) -> Void
    ) throws -> EngineProcess {
        let attempt = lock.withLock {
            launches += 1
            return launches
        }
        if attempt == 1 {
            try fixture.writeEnvironment("not-an-assignment\n")
            throw TelegramCredentialPreflightFailure(fixture.credentials.load().state)
        }
        return HeldEngineProcess(pid: Int32(attempt + 3000), exited: exited)
    }

}

private final class NonCredentialFailingLauncher: EngineLaunching, @unchecked Sendable {
    private let lock = NSLock()
    private var launches = 0

    var launchCount: Int { lock.withLock { launches } }

    func launch(
        _ command: EngineCommand,
        stderr: @escaping @Sendable (Data) -> Void,
        exited: @escaping @Sendable (Int32) -> Void
    ) throws -> EngineProcess {
        lock.withLock { launches += 1 }
        throw NonCredentialLaunchFailure()
    }
}

private struct NonCredentialLaunchFailure: Error {}

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
