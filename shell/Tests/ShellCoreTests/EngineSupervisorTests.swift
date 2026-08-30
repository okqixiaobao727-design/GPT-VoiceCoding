import Foundation
import Testing

@testable import ShellCore

@Suite struct EngineSupervisorTests {
    private func supervisor(
        script: [FakeLauncher.Run],
        clock: TestClock,
        log: HealthLog,
        socketAnswers: @escaping @Sendable (String) -> Bool = { _ in false },
        deaf: Bool = false,
        command: @escaping @Sendable () throws -> EngineCommand = {
            EngineCommand(executable: "/usr/bin/true", arguments: [], source: .developerPath)
        }
    ) -> (EngineSupervisor, FakeLauncher) {
        let launcher = FakeLauncher(script: script, clock: clock, deaf: deaf)
        let supervisor = EngineSupervisor(
            launcher: launcher, socketPath: "/tmp/gvc-test.sock", resolveCommand: command,
            clock: clock, socketAnswers: socketAnswers, observer: { log.record($0) })
        return (supervisor, launcher)
    }

    @Test func timeSpentBeforeTheChildExistedIsNotUptimeTheEngineHad() async throws {
        // `ProcessLauncher` reads the user's login shell inside `launch`, and the
        // supervisor's clock starts before it asks. Counted as uptime, a slow
        // profile makes a permanently broken engine look like one that took hold:
        // 8 s of login shell plus a 55 s run reads as 63 s, past
        // `steadyStateSeconds`, so `consecutiveFastFailures` resets to 0 on every
        // death and the ladder never climbs. The shell then restarts a broken
        // engine for ever at the 1 s first delay — the endless silent retry loop
        // `RestartPolicy` exists to prevent, and #118's ten-second budget is what
        // put it in reach.
        //
        // Discounted, the same run is 55 s, which is the fast failure it always
        // was, and the ladder ends where it should.
        let clock = TestClock()
        let log = HealthLog()
        let launcher = SlowToSpawnLauncher(
            readCost: 8, uptime: 55, code: 2, clock: clock)
        let supervisor = EngineSupervisor(
            launcher: launcher, socketPath: "/tmp/gvc-test.sock",
            resolveCommand: {
                EngineCommand(executable: "/usr/bin/true", arguments: [], source: .developerPath)
            },
            clock: clock, socketAnswers: { _ in false }, observer: { log.record($0) })

        await supervisor.start()
        let stopped = await log.wait(for: {
            if case .stopped = $0 { return true }
            return false
        })

        #expect(stopped == .stopped(.repeatedFailures(attempts: 5)))
        #expect(clock.sleeps() == [1, 2, 4, 8])
        await supervisor.shutDown()
    }

    @Test func itSpawnsOnStart() async throws {
        let clock = TestClock()
        let log = HealthLog()
        let (supervisor, launcher) = supervisor(script: [], clock: clock, log: log)

        await supervisor.start()
        let running = await log.wait(for: {
            if case .running = $0 { return true }
            return false
        })

        #expect(running == .running(pid: 1001))
        #expect(launcher.launchCount() == 1)
        await supervisor.shutDown()
    }

    @Test func aCleanExitIsStillRestarted() async throws {
        // The KeepAlive lesson: exit 0 is not permission to stop.
        let clock = TestClock()
        let log = HealthLog()
        let (supervisor, launcher) = supervisor(
            script: [.init(uptime: 300, code: 0)], clock: clock, log: log)

        await supervisor.start()
        _ = await log.wait(for: { $0 == .running(pid: 1002) })

        #expect(launcher.launchCount() == 2)
        await supervisor.shutDown()
    }

    @Test func aKillIsRestarted() async throws {
        let clock = TestClock()
        let log = HealthLog()
        let (supervisor, launcher) = supervisor(
            script: [.init(uptime: 120, code: -9)], clock: clock, log: log)

        await supervisor.start()
        _ = await log.wait(for: { $0 == .running(pid: 1002) })

        #expect(launcher.launchCount() == 2)
        await supervisor.shutDown()
    }

    @Test func fastFailuresClimbTheLadderAndThenStop() async throws {
        let clock = TestClock()
        let log = HealthLog()
        let (supervisor, launcher) = supervisor(
            script: Array(repeating: FakeLauncher.Run(uptime: 0.5, code: 2), count: 5),
            clock: clock, log: log)

        await supervisor.start()
        let stopped = await log.wait(for: {
            if case .stopped = $0 { return true }
            return false
        })

        #expect(stopped == .stopped(.repeatedFailures(attempts: 5)))
        // Five spawns, four waits, and then it says so instead of spawning a sixth.
        #expect(launcher.launchCount() == 5)
        #expect(clock.sleeps() == [1, 2, 4, 8])
        await supervisor.shutDown()
    }

    @Test func anEngineThatTookHoldForgivesTheEarlierFailures() async throws {
        let clock = TestClock()
        let log = HealthLog()
        let (supervisor, launcher) = supervisor(
            script: [
                .init(uptime: 0.5, code: 2), .init(uptime: 0.5, code: 2),
                .init(uptime: 90, code: 1), .init(uptime: 0.5, code: 2),
            ], clock: clock, log: log)

        await supervisor.start()
        _ = await log.wait(for: { $0 == .running(pid: 1005) })

        // The 90-second run resets the counter, so the failure after it is the
        // first one again: 1s, 2s, then 1s.
        #expect(clock.sleeps() == [1, 2, 1, 1])
        #expect(launcher.launchCount() == 5)
        await supervisor.shutDown()
    }

    @Test func aLiveSocketAfterExitTwoStopsRatherThanFightingIt() async throws {
        // A second engine refuses and exits 2 without touching the first one's
        // socket. Spawning again would be spawning against a live engine.
        let clock = TestClock()
        let log = HealthLog()
        let (supervisor, launcher) = supervisor(
            script: [.init(uptime: 0.2, code: 2)], clock: clock, log: log,
            socketAnswers: { _ in true })

        await supervisor.start()
        let stopped = await log.wait(for: {
            if case .stopped = $0 { return true }
            return false
        })

        #expect(stopped == .stopped(.anotherEngineIsListening))
        #expect(launcher.launchCount() == 1)
        await supervisor.shutDown()
    }

    @Test func theCrashLoopPanelHoldsTheLastRunsWordsVerbatim() async throws {
        // The engine mirrors exit 2's final reason onto inherited stderr while
        // keeping its log (ADR 0004). The panel needs the run that finally
        // exhausted the budget rather than a complaint from an earlier attempt.
        let clock = TestClock()
        let log = HealthLog()
        let (supervisor, _) = supervisor(
            script: (1...5).map {
                .init(uptime: 0.2, code: 2, stderr: "config: refusal number \($0)\n")
            }, clock: clock, log: log)

        await supervisor.start()
        _ = await log.wait(for: {
            if case .stopped = $0 { return true }
            return false
        })

        let held = await supervisor.lines()
        #expect(held == ["config: refusal number 5"])
        await supervisor.shutDown()
    }

    @Test func exitHealthWaitsUntilTheRunsStderrHasBeenDelivered() async {
        let log = HealthLog()
        let process = ControlledExitProcess(
            pid: 4242, stderr: Data("config: ordered refusal\n".utf8), code: 2)
        let supervisor = EngineSupervisor(
            launcher: SingleProcessLauncher(process: process),
            socketPath: "/tmp/gvc-test.sock",
            resolveCommand: {
                EngineCommand(executable: "/usr/bin/true", arguments: [], source: .developerPath)
            },
            socketAnswers: { _ in true }, observer: { log.record($0) })

        await supervisor.start()
        await process.waitUntilStderrReturns()

        #expect(await supervisor.lines() == ["config: ordered refusal"])
        #expect(await supervisor.health == .running(pid: 4242))
        #expect(!log.all().contains(.stopped(.anotherEngineIsListening)))

        await process.allowExit()
        let stopped = await log.wait(for: { $0 == .stopped(.anotherEngineIsListening) })
        #expect(stopped == .stopped(.anotherEngineIsListening))
        await supervisor.shutDown()
    }

    @Test func aFinishedRunCannotDeliverIntoTheNextRunsFreshRing() async throws {
        let clock = TestClock()
        let log = HealthLog()
        let first = PendingTailProcess(pid: 1001)
        let launcher = PendingTailLauncher(first: first, clock: clock)
        let supervisor = EngineSupervisor(
            launcher: launcher, socketPath: "/tmp/gvc-test.sock",
            resolveCommand: {
                EngineCommand(executable: "/usr/bin/true", arguments: [], source: .developerPath)
            },
            clock: clock, socketAnswers: { _ in false }, observer: { log.record($0) })

        await supervisor.start()
        await first.waitUntilTailIsPending()

        // The first run has an accepted final delivery that has not happened yet.
        // It cannot finish, so the supervisor cannot clear the ring or launch run 2.
        #expect(launcher.launchCount() == 1)
        #expect(await supervisor.lines() == ["config: first half"])

        await first.releaseTail()
        _ = await log.wait(for: { $0 == .running(pid: 1002) })

        // The nonescaping delivery closure died with run 1. Both of its chunks
        // completed before run 2 cleared the ring, so neither can arrive late.
        #expect(launcher.launchCount() == 2)
        #expect(await supervisor.lines().isEmpty)
        await supervisor.shutDown()
    }

    @Test func nothingToSpawnIsItsOwnState() async throws {
        // Not a crash loop. An installation with no interpreter never dies,
        // because it never starts, and saying "it keeps crashing" would be false.
        let clock = TestClock()
        let log = HealthLog()
        let (supervisor, launcher) = supervisor(
            script: [], clock: clock, log: log,
            command: { throw EngineCommandFailure.noInterpreter("no python3 on the path") })

        await supervisor.start()
        let state = await log.wait(for: {
            if case .cannotSpawn = $0 { return true }
            return false
        })

        #expect(state == .cannotSpawn(.command("no python3 on the path")))
        #expect(launcher.launchCount() == 0)
        await supervisor.shutDown()
    }

    @Test func aCredentialPreflightFailureKeepsItsTypedReason() async {
        let log = HealthLog()
        let supervisor = EngineSupervisor(
            launcher: CredentialRefusingLauncher(state: .missing),
            socketPath: "/tmp/gvc-test.sock",
            resolveCommand: {
                EngineCommand(executable: "/usr/bin/true", arguments: [], source: .developerPath)
            },
            observer: { log.record($0) })

        await supervisor.start()
        let state = await log.wait(for: {
            if case .cannotSpawn = $0 { return true }
            return false
        })

        #expect(state == .cannotSpawn(.credentials(.missing)))
        await supervisor.shutDown()
    }

    @Test func retryForgivesTheCountAndStartsAgain() async throws {
        let clock = TestClock()
        let log = HealthLog()
        let (supervisor, launcher) = supervisor(
            script: Array(repeating: FakeLauncher.Run(uptime: 0.5, code: 2), count: 5),
            clock: clock, log: log)

        await supervisor.start()
        _ = await log.wait(for: {
            if case .stopped = $0 { return true }
            return false
        })
        await supervisor.retry()
        // The sixth child specifically: the five before it are still in the log.
        _ = await log.wait(for: { $0 == .running(pid: 1006) })

        #expect(launcher.launchCount() == 6)
        // A fresh ring, because the person retrying is asserting the old news is old.
        let held = await supervisor.lines()
        #expect(held.isEmpty)
        await supervisor.shutDown()
    }

    @Test func shuttingDownAsksTheChildToStopAndDoesNotSpawnAnother() async throws {
        let clock = TestClock()
        let log = HealthLog()
        let (supervisor, launcher) = supervisor(script: [], clock: clock, log: log)

        await supervisor.start()
        _ = await log.wait(for: {
            if case .running = $0 { return true }
            return false
        })
        await supervisor.shutDown()

        let health = await supervisor.health
        #expect(health == .shutDown)
        #expect(launcher.launchCount() == 1)
        // SIGTERM, and nothing harsher: the engine stops in order and leaves no
        // socket behind for the next start to trip over.
        #expect(launcher.lastChild()?.stopRequested == true)
        #expect(launcher.lastChild()?.stopForced == false)
    }

    @Test func shutdownDoesNotForceAChildThatExitedWhileItsStderrStillDrains() async {
        let process = ExitedButDrainingProcess(pid: 4242)
        let clock = GatedGraceClock()
        let supervisor = EngineSupervisor(
            launcher: DrainingExitedLauncher(process: process),
            socketPath: "/tmp/gvc-test.sock",
            resolveCommand: {
                EngineCommand(executable: "/usr/bin/true", arguments: [], source: .developerPath)
            },
            clock: clock)

        await supervisor.start()
        await process.waitUntilDrainIsHeld()

        let shutdown = Task { await supervisor.shutDown() }
        await clock.waitUntilGraceStarts()
        await clock.expireGrace()

        #expect(await process.waitForForceDecision() == .exitChecked)
        #expect(!process.stopForced)

        await process.releaseDrain()
        await shutdown.value
    }

    @Test func aChildThatIgnoresTheAskingIsStoppedAnyway() async throws {
        // A shell that cannot quit because its child will not listen is the same
        // dishonest wait this design exists to remove. The bound is stated once,
        // on the supervisor.
        let clock = TestClock()
        let log = HealthLog()
        let (supervisor, launcher) = supervisor(script: [], clock: clock, log: log, deaf: true)

        await supervisor.start()
        _ = await log.wait(for: {
            if case .running = $0 { return true }
            return false
        })
        await supervisor.shutDown()

        let health = await supervisor.health
        #expect(health == .shutDown)
        #expect(launcher.lastChild()?.stopRequested == true)
        #expect(launcher.lastChild()?.stopForced == true)
        #expect(EngineSupervisor.stopGraceSeconds == 5)
    }
}

private struct CredentialRefusingLauncher: EngineLaunching {
    let state: TelegramCredentials.State

    func launch(_ command: EngineCommand) throws -> EngineProcess {
        throw TelegramCredentialPreflightFailure(state)
    }
}

/// A launcher that spends time before its child exists, the way the real one
/// does while it reads somebody's login shell — and reports how much.
///
/// Local to this file on purpose. `FakeLauncher` models a child's whole life
/// inside `launch` and reports no overhead, which is what keeps its nine scripted
/// uptimes meaning what they say; this is the one case that needs the other
/// shape, and it should not cost the shared double anything.
private final class SlowToSpawnLauncher: EngineLaunching, @unchecked Sendable {
    private let readCost: TimeInterval
    private let uptime: TimeInterval
    private let code: Int32
    private let clock: TestClock
    private let lock = NSLock()
    private var launches = 0

    init(readCost: TimeInterval, uptime: TimeInterval, code: Int32, clock: TestClock) {
        self.readCost = readCost
        self.uptime = uptime
        self.code = code
        self.clock = clock
    }

    func launch(_ command: EngineCommand) throws -> EngineProcess {
        let count = lock.withLock {
            launches += 1
            return launches
        }
        // Before the child exists: this is the login shell, and it is exactly
        // what the supervisor must not count.
        clock.advance(readCost)
        return SlowlySpawnedProcess(
            pid: Int32(2000 + count), launchOverhead: readCost,
            uptime: uptime, code: code, clock: clock)
    }
}

private final class SlowlySpawnedProcess: EngineProcess, @unchecked Sendable {
    let processIdentifier: Int32
    let launchOverhead: TimeInterval
    private let uptime: TimeInterval
    private let code: Int32
    private let clock: TestClock
    private let lock = NSLock()
    private var exited = false

    init(
        pid: Int32,
        launchOverhead: TimeInterval,
        uptime: TimeInterval,
        code: Int32,
        clock: TestClock
    ) {
        processIdentifier = pid
        self.launchOverhead = launchOverhead
        self.uptime = uptime
        self.code = code
        self.clock = clock
    }

    var hasExited: Bool { lock.withLock { exited } }

    func waitForExit(
        deliveringStderr: @Sendable (Data) async -> Void
    ) async -> Int32 {
        // This is the child's own life, distinct from the launch overhead above.
        clock.advance(uptime)
        lock.withLock { exited = true }
        return code
    }

    func requestStop() {}
    func forceStop() {}
}

private struct SingleProcessLauncher: EngineLaunching {
    let process: ControlledExitProcess

    func launch(_ command: EngineCommand) throws -> EngineProcess { process }
}

private final class ControlledExitProcess: EngineProcess, @unchecked Sendable {
    let processIdentifier: Int32
    private let stderr: Data
    private let code: Int32
    private let gate = ProcessGate()
    private let lock = NSLock()
    private var exited = false

    init(pid: Int32, stderr: Data, code: Int32) {
        processIdentifier = pid
        self.stderr = stderr
        self.code = code
    }

    var hasExited: Bool { lock.withLock { exited } }

    func waitForExit(
        deliveringStderr: @Sendable (Data) async -> Void
    ) async -> Int32 {
        await deliveringStderr(stderr)
        await gate.reachAndWait()
        lock.withLock { exited = true }
        return code
    }

    func waitUntilStderrReturns() async { await gate.waitUntilReached() }
    func allowExit() async { await gate.release() }
    func requestStop() {}
    func forceStop() {}
}

private final class PendingTailLauncher: EngineLaunching, @unchecked Sendable {
    private let first: PendingTailProcess
    private let clock: TestClock
    private let lock = NSLock()
    private var launches = 0

    init(first: PendingTailProcess, clock: TestClock) {
        self.first = first
        self.clock = clock
    }

    func launch(_ command: EngineCommand) throws -> EngineProcess {
        let attempt = lock.withLock {
            launches += 1
            return launches
        }
        if attempt == 1 { return first }
        return FakeProcess(pid: Int32(1000 + attempt), run: nil, clock: clock)
    }

    func launchCount() -> Int { lock.withLock { launches } }
}

private final class PendingTailProcess: EngineProcess, @unchecked Sendable {
    let processIdentifier: Int32
    private let gate = ProcessGate()
    private let lock = NSLock()
    private var exited = false

    init(pid: Int32) { processIdentifier = pid }

    var hasExited: Bool { lock.withLock { exited } }

    func waitForExit(
        deliveringStderr: @Sendable (Data) async -> Void
    ) async -> Int32 {
        await deliveringStderr(Data("config: first half\n".utf8))
        await gate.reachAndWait()
        await deliveringStderr(Data("config: final tail\n".utf8))
        lock.withLock { exited = true }
        return 2
    }

    func waitUntilTailIsPending() async { await gate.waitUntilReached() }
    func releaseTail() async { await gate.release() }
    func requestStop() {}
    func forceStop() {}
}

private actor ProcessGate {
    private var reached = false
    private var released = false
    private var reachedWaiters: [CheckedContinuation<Void, Never>] = []
    private var releaseWaiters: [CheckedContinuation<Void, Never>] = []

    func reachAndWait() async {
        reached = true
        for waiter in reachedWaiters { waiter.resume() }
        reachedWaiters.removeAll()
        guard !released else { return }
        await withCheckedContinuation { releaseWaiters.append($0) }
    }

    func waitUntilReached() async {
        guard !reached else { return }
        await withCheckedContinuation { reachedWaiters.append($0) }
    }

    func release() {
        released = true
        for waiter in releaseWaiters { waiter.resume() }
        releaseWaiters.removeAll()
    }
}

private struct DrainingExitedLauncher: EngineLaunching {
    let process: ExitedButDrainingProcess

    func launch(_ command: EngineCommand) throws -> EngineProcess { process }
}

private enum ForceDecision: Equatable {
    case exitChecked
    case forced
}

private final class ExitedButDrainingProcess: EngineProcess, @unchecked Sendable {
    let processIdentifier: Int32
    private let drain = ProcessGate()
    private let lock = NSLock()
    private let decisions: AsyncStream<ForceDecision>
    private let decisionContinuation: AsyncStream<ForceDecision>.Continuation
    private var exited = false
    private var forced = false

    init(pid: Int32) {
        processIdentifier = pid
        (decisions, decisionContinuation) = AsyncStream.makeStream(of: ForceDecision.self)
    }

    var hasExited: Bool {
        let observed = lock.withLock { exited }
        decisionContinuation.yield(.exitChecked)
        return observed
    }

    func waitForExit(
        deliveringStderr: @Sendable (Data) async -> Void
    ) async -> Int32 {
        lock.withLock { exited = true }
        await drain.reachAndWait()
        return 0
    }

    func waitUntilDrainIsHeld() async { await drain.waitUntilReached() }
    func releaseDrain() async { await drain.release() }
    func requestStop() {}

    func forceStop() {
        lock.withLock { forced = true }
        decisionContinuation.yield(.forced)
    }

    var stopForced: Bool { lock.withLock { forced } }

    func waitForForceDecision() async -> ForceDecision? {
        for await decision in decisions { return decision }
        return nil
    }
}

private final class GatedGraceClock: SupervisorClock, @unchecked Sendable {
    private let grace = ProcessGate()

    var now: TimeInterval { 0 }

    func sleep(_ seconds: TimeInterval) async {
        await grace.reachAndWait()
    }

    func waitUntilGraceStarts() async { await grace.waitUntilReached() }
    func expireGrace() async { await grace.release() }
}
