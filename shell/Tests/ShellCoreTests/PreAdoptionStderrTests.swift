import Foundation
import Testing

@testable import ShellCore

/// The end of the pipe, both ways it happens. No process here on purpose: the
/// descriptor cannot tell an engine that adopted its log from one that died, so
/// a bare pipe is the honest way to say which of the two the code is being asked
/// about.
@Suite struct PreAdoptionStderrTests {
    @Test func adoptionEndsTheWatchRatherThanSpinningOnTheEnd() async throws {
        // ADR 0004's healthy start: the engine takes its log, its stderr stops
        // being this pipe, and the pipe ends while the engine runs on. A
        // descriptor at its end is permanently readable, so anything still
        // watching reads emptiness at the speed of the machine — one core, for
        // as long as the engine lives.
        let said = Collected()
        let stderr = PreAdoptionStderr(deliver: { said.add($0) })
        stderr.read()

        try stderr.pipe.fileHandleForWriting.write(contentsOf: Data("starting\n".utf8))
        try await said.waits(for: "starting\n")
        try stderr.pipe.fileHandleForWriting.close()

        try await waitUntil { !stderr.isMonitoring }
        #expect(!stderr.isMonitoring)
    }

    @Test func aRefusalSurvivesTheDeathThatFollowsIt() async throws {
        // Exit 2: the words are written and the writer is gone a moment later.
        // Both arrive, and they arrive before the caller is told about the exit.
        let said = Collected()
        let stderr = PreAdoptionStderr(deliver: { said.add($0) })
        stderr.read()

        try stderr.pipe.fileHandleForWriting.write(
            contentsOf: Data("config: [delegate] model is required\n".utf8))
        try stderr.pipe.fileHandleForWriting.close()

        stderr.finish()

        #expect(said.text() == "config: [delegate] model is required\n")
        #expect(!stderr.isMonitoring)
    }

    @Test func theExitAfterAnAdoptionAsksAnAlreadyDrainedPipeForNothing() async throws {
        // The overlap: the pipe ended at adoption and the watch is already down,
        // and only later does the engine exit and `finish()` run. It must find
        // nothing, say nothing twice, and not hang on a pipe with no writer.
        let said = Collected()
        let stderr = PreAdoptionStderr(deliver: { said.add($0) })
        stderr.read()

        try stderr.pipe.fileHandleForWriting.write(contentsOf: Data("starting\n".utf8))
        try await said.waits(for: "starting\n")
        try stderr.pipe.fileHandleForWriting.close()
        try await waitUntil { !stderr.isMonitoring }

        stderr.finish()

        #expect(said.text() == "starting\n")
        #expect(!stderr.isMonitoring)
    }
}

/// Polls a condition the way a caller would have to: the end of a pipe is
/// noticed on Foundation's own queue, not on this one.
private func waitUntil(
    within limit: Duration = .seconds(2), _ condition: @Sendable () -> Bool
) async throws {
    let deadline = ContinuousClock.now + limit
    while ContinuousClock.now < deadline {
        if condition() { return }
        try await Task.sleep(for: .milliseconds(10))
    }
}

/// What the pipe handed over, in order.
private final class Collected: @unchecked Sendable {
    private let lock = NSLock()
    private var data = Data()

    func add(_ chunk: Data) { lock.withLock { data.append(chunk) } }

    func text() -> String { lock.withLock { String(decoding: data, as: UTF8.self) } }

    func waits(for expected: String) async throws {
        try await waitUntil { self.text() == expected }
    }
}
