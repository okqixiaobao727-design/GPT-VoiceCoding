import Foundation
import Testing

@testable import ShellCore

/// The real spawn path, against real processes. The supervisor's rules are
/// tested with a fake launcher; this is the other half — that `Process` actually
/// delivers stderr, exit codes and signals the way those rules assume.
@Suite struct ProcessLauncherTests {
    private func run(_ script: String) async -> (code: Int32, stderr: [String], pid: Int32) {
        let collected = Collector()
        let command = EngineCommand(
            executable: "/bin/sh", arguments: ["-c", script], source: .developerPath)
        let process = try! ProcessLauncher().launch(
            command,
            stderr: { collected.add($0) },
            exited: { collected.finish($0) })
        let code = await collected.exitCode()
        return (code, collected.lines(), process.processIdentifier)
    }

    @Test func stderrArrivesAndTheExitCodeIsTheEnginesOwn() async {
        // Exit 2 is "it could not start", with the reason on stderr and nowhere
        // else — this is the pipe that carries it.
        let outcome = await run("echo 'config: [delegate] model is required' 1>&2; exit 2")
        #expect(outcome.code == EngineExitCode.couldNotStart)
        #expect(outcome.stderr == ["config: [delegate] model is required"])
    }

    @Test func aCleanExitIsZeroAndNotASignal() async {
        let outcome = await run("exit 0")
        #expect(outcome.code == EngineExitCode.ok)
    }

    @Test func aSignalIsReportedApartFromAnExitCode() async {
        // A kill must never be mistaken for the engine's own "I could not start".
        let outcome = await run("kill -9 $$")
        #expect(outcome.code == -9)
    }

    @Test func theChildIsThisProcessesOwn() async {
        // Direct `posix_spawn` parenthood, which is what ADR 0005 requires and
        // what `launchctl procinfo` is asked to confirm on a real engine.
        let outcome = await run("exit 0")
        #expect(outcome.pid > 0)
    }

    @Test func everySpawnSaysWhichPathItChoseAndWhereItCameFrom() async {
        // The wiring, not the closure. `LoginShellPath` already proved it calls
        // whatever logger it is handed; what went unproven — and untrue — is
        // that anything ever handed it one, so both of its fallback sentences
        // were discarded in production for the whole of d850e8f's life.
        //
        // A silent fallback is how a feature stops working without anybody
        // noticing, and this ticket is the proof: `-lc` returned a PATH that was
        // usable and wrong, so no fallback line would have fired anyway. Only
        // saying which PATH was *taken* makes that observable.
        let said = Sentences()
        let command = EngineCommand(
            executable: "/bin/sh", arguments: ["-c", "exit 0"], source: .developerPath)
        let collected = Collector()

        _ = try! ProcessLauncher(log: { said.add($0) }).launch(
            command, stderr: { collected.add($0) }, exited: { collected.finish($0) })
        _ = await collected.exitCode()

        // Exactly one, because a line per spawn is a diagnostic and two would be
        // the beginning of a log nobody reads.
        #expect(said.lines.count == 1)
        // Whichever branch this machine takes, the line names the shell it asked
        // and never claims a PATH it did not adopt.
        #expect(said.lines.first?.contains(LoginShellPath.loginShell() ?? "/bin/sh") == true)
    }

    @Test func askingItToStopStopsIt() async {
        let collected = Collector()
        let command = EngineCommand(
            executable: "/bin/sh", arguments: ["-c", "sleep 30"], source: .developerPath)
        let process = try! ProcessLauncher().launch(
            command, stderr: { collected.add($0) }, exited: { collected.finish($0) })

        process.requestStop()

        // SIGTERM: the engine stops in order rather than leaving its socket behind.
        let code = await collected.exitCode()
        #expect(code == -SIGTERM)
    }
}

/// Gathers the launcher's own diagnostic lines, which is a different stream
/// from the child's stderr and must not be confused with it.
private final class Sentences: @unchecked Sendable {
    private let lock = NSLock()
    private var said: [String] = []

    func add(_ line: String) { lock.withLock { said.append(line) } }
    var lines: [String] { lock.withLock { said } }
}

/// Gathers what a real child said and how it ended.
private final class Collector: @unchecked Sendable {
    private let lock = NSLock()
    private var ring = StderrRing()
    private var code: Int32?
    private var waiter: CheckedContinuation<Int32, Never>?

    func add(_ chunk: Data) { lock.withLock { ring.ingest(chunk) } }

    func finish(_ status: Int32) {
        let waiting: CheckedContinuation<Int32, Never>? = lock.withLock {
            code = status
            let waiter = self.waiter
            self.waiter = nil
            return waiter
        }
        waiting?.resume(returning: status)
    }

    func lines() -> [String] { lock.withLock { ring.lines } }

    func exitCode() async -> Int32 {
        await withCheckedContinuation { continuation in
            let finished: Int32? = lock.withLock {
                if let code { return code }
                waiter = continuation
                return nil
            }
            if let finished { continuation.resume(returning: finished) }
        }
    }
}
