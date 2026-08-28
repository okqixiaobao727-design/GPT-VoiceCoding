import Foundation
import Testing

@testable import ShellCore

/// The whole chain, wired as the app wires it: the real `ProcessLauncher`, the
/// real `RestartPolicy`, real children. Only the clock is a stand-in, so the
/// backoff ladder is asserted instead of waited out.
///
/// The one exception is the `PATH`: the real launcher starts a login shell on
/// every spawn, ~0.45 s of somebody's profile, and this suite's assertions are
/// five sequential spawns inside a fixed budget — so it was red under load on an
/// unchanged product, which is `#36`. It reads no login shell. What it is about
/// is the restart ladder; `LoginShellPathTests` and `ProcessLauncherTests` are
/// where the real reader is exercised.
///
/// The child is a script rather than the engine because the engine needs a
/// configured machine; what it imitates is the behaviour that matters here —
/// exit 2 with the reason on stderr, which is what the engine really does (`the
/// engine cannot start: no engine configuration at …`).
@Suite struct SupervisionIntegrationTests {
    private func stubEngine(_ script: String) -> (URL, @Sendable () throws -> EngineCommand) {
        let directory = URL(fileURLWithPath: "/tmp/gvc-stub-\(UUID().uuidString.prefix(8))")
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let file = directory.appendingPathComponent("engine.sh")
        FileManager.default.createFile(
            atPath: file.path, contents: Data("#!/bin/sh\n\(script)\n".utf8),
            attributes: [.posixPermissions: 0o755])
        return (
            directory,
            { EngineCommand(executable: file.path, arguments: [], source: .developerPath) }
        )
    }

    @Test func aCrashLoopEndsInAnHonestStopWithTheEnginesOwnWords() async {
        let (directory, command) = stubEngine(
            "echo 'the engine cannot start: no engine configuration at /tmp/nope.toml' 1>&2\nexit 2"
        )
        defer { try? FileManager.default.removeItem(at: directory) }

        let clock = TestClock()
        let log = HealthLog()
        let supervisor = EngineSupervisor(
            launcher: ProcessLauncher(readPath: LoginShellPath.unasked),
            socketPath: "/tmp/gvc-nothing-here.sock",
            resolveCommand: command, clock: clock,
            socketAnswers: { _ in false }, observer: { log.record($0) })

        await supervisor.start()
        let stopped = await log.wait(for: {
            if case .stopped = $0 { return true }
            return false
        })

        #expect(stopped == .stopped(.repeatedFailures(attempts: 5)))
        #expect(clock.sleeps() == [1, 2, 4, 8])
        // Verbatim: after log adoption the engine mirrors only this final refusal
        // sentence onto the inherited stderr pipe.
        let held = await supervisor.lines()
        #expect(held == ["the engine cannot start: no engine configuration at /tmp/nope.toml"])
        await supervisor.shutDown()
    }

    @Test func aCleanExitBringsItStraightBack() async {
        // The KeepAlive lesson, against a real child: exiting 0 is not permission
        // to stay dead.
        let (directory, command) = stubEngine("exit 0")
        defer { try? FileManager.default.removeItem(at: directory) }

        let clock = TestClock()
        let log = HealthLog()
        let supervisor = EngineSupervisor(
            launcher: ProcessLauncher(readPath: LoginShellPath.unasked),
            socketPath: "/tmp/gvc-nothing-here.sock",
            resolveCommand: command, clock: clock,
            socketAnswers: { _ in false }, observer: { log.record($0) })

        await supervisor.start()
        _ = await log.wait(for: {
            if case .stopped = $0 { return true }
            return false
        })

        // Exit 0 is not exit 2, so the socket is never asked about, and every one
        // of the five was a spawn that followed a clean exit.
        let running = log.all().filter {
            if case .running = $0 { return true }
            return false
        }
        #expect(running.count == 5)
        await supervisor.shutDown()
    }

    @Test func nothingToSpawnStopsWithoutPretendingItCrashed() async {
        let clock = TestClock()
        let log = HealthLog()
        let supervisor = EngineSupervisor(
            launcher: ProcessLauncher(readPath: LoginShellPath.unasked),
            socketPath: "/tmp/gvc-nothing-here.sock",
            resolveCommand: {
                EngineCommand(
                    executable: "/tmp/gvc-no-such-engine", arguments: [], source: .developerPath)
            }, clock: clock, socketAnswers: { _ in false }, observer: { log.record($0) })

        await supervisor.start()
        let state = await log.wait(for: {
            if case .cannotSpawn = $0 { return true }
            return false
        })

        #expect(state != nil)
        await supervisor.shutDown()
    }
}
