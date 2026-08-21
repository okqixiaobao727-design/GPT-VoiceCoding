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
        // Exit 2's reason exists only on stderr — it is said before the engine
        // adopts its own log (ADR 0004) — so this ring is the only copy, and the
        // one that matters is the run that finally exhausted the budget.
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

    @Test func aRestartStartsAFreshRing() async throws {
        // The previous run's complaint is not this run's, and showing it beside a
        // healthy engine would be the shell reporting news that has been fixed.
        let clock = TestClock()
        let log = HealthLog()
        let (supervisor, _) = supervisor(
            script: [.init(uptime: 0.2, code: 2, stderr: "config: old news\n")],
            clock: clock, log: log)

        await supervisor.start()
        _ = await log.wait(for: { $0 == .running(pid: 1002) })

        let held = await supervisor.lines()
        #expect(held.isEmpty)
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

        #expect(state == .cannotSpawn("no python3 on the path"))
        #expect(launcher.launchCount() == 0)
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
