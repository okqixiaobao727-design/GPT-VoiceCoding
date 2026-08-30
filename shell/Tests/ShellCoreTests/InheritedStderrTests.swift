import Foundation
import Testing

@testable import ShellCore

/// The end of the inherited stderr pipe. No process here on purpose: a bare pipe
/// is the public launcher's boundary, and it makes the ordering observable without
/// substituting a process implementation.
@Suite struct InheritedStderrTests {
    @Test func theEndOfThePipeEndsTheWatchRatherThanSpinning() async throws {
        // A descriptor at the end of a pipe is permanently readable, so anything
        // still watching reads emptiness at the speed of the machine — one core,
        // for as long as the engine lives.
        let said = Collected()
        let stderr = InheritedStderr(deliver: { said.add($0) })
        stderr.read()

        try stderr.pipe.fileHandleForWriting.write(contentsOf: Data("starting\n".utf8))
        try await said.waits(for: "starting\n")
        try stderr.pipe.fileHandleForWriting.close()

        try await waitUntil("the watch came down") { !stderr.isMonitoring }
        #expect(!stderr.isMonitoring)
    }

    @Test func aRefusalSurvivesTheDeathThatFollowsIt() async throws {
        // Exit 2: the words are written and the writer is gone a moment later.
        // Both arrive, and they arrive before the caller is told about the exit.
        let said = Collected()
        let stderr = InheritedStderr(deliver: { said.add($0) })
        stderr.read()

        try stderr.pipe.fileHandleForWriting.write(
            contentsOf: Data("config: [delegate] model is required\n".utf8))
        try stderr.pipe.fileHandleForWriting.close()

        await stderr.finish()

        // `finish()` returns only after the one reader's delivery has returned.
        #expect(said.text() == "config: [delegate] model is required\n")
        #expect(!stderr.isMonitoring)
    }

    @Test func finishWaitsForAnInFlightDelivery() async throws {
        let gate = DeliveryGate()
        let stderr = InheritedStderr(deliver: { chunk in
            await gate.holdDelivery(chunk)
        })
        stderr.read()

        let refusal = Data("config: refusal\n".utf8)
        try stderr.pipe.fileHandleForWriting.write(contentsOf: refusal)
        try stderr.pipe.fileHandleForWriting.close()
        await gate.waitUntilDeliveryStarts()

        let finishing = Task {
            await gate.recordFinishCalled()
            await stderr.finish()
            await gate.recordFinishReturned()
        }
        await gate.waitUntilFinishIsCalled()
        await gate.releaseDelivery()
        await finishing.value

        #expect(
            await gate.events() == [
                .deliveryStarted,
                .finishCalled,
                .deliveryFinished(refusal),
                .finishReturned,
            ])
    }

    @Test func finishingAnAlreadyDrainedPipeFindsNothingTwice() async throws {
        // The watch has already seen the end, and only later does `finish()` run.
        // It must find nothing, say nothing twice, and not hang with no writer.
        let said = Collected()
        let stderr = InheritedStderr(deliver: { said.add($0) })
        stderr.read()

        try stderr.pipe.fileHandleForWriting.write(contentsOf: Data("starting\n".utf8))
        try await said.waits(for: "starting\n")
        try stderr.pipe.fileHandleForWriting.close()
        try await waitUntil("the watch came down") { !stderr.isMonitoring }

        await stderr.finish()

        #expect(said.text() == "starting\n")
        #expect(!stderr.isMonitoring)
    }
}

/// Waited for something that never happened. An error rather than a quiet
/// return: a poll that gives up silently turns every wait built on it into an
/// assertion that cannot fail.
private struct NeverHappened: Error, CustomStringConvertible {
    let what: String
    var description: String { "waited, and \(what) never happened" }
}

/// Polls a condition the way a caller would have to: the end of a pipe is
/// noticed on Foundation's own queue, not on this one.
private func waitUntil(
    _ what: String, within limit: Duration = .seconds(2), _ condition: @Sendable () -> Bool
) async throws {
    let deadline = ContinuousClock.now + limit
    while ContinuousClock.now < deadline {
        if condition() { return }
        try await Task.sleep(for: .milliseconds(10))
    }
    throw NeverHappened(what: what)
}

/// What the pipe handed over, in order.
private final class Collected: @unchecked Sendable {
    private let lock = NSLock()
    private var data = Data()

    func add(_ chunk: Data) { lock.withLock { data.append(chunk) } }

    func text() -> String { lock.withLock { String(decoding: data, as: UTF8.self) } }

    func waits(for expected: String) async throws {
        try await waitUntil("the pipe handed over \(String(reflecting: expected))") {
            self.text() == expected
        }
    }
}

private enum DeliveryEvent: Equatable {
    case deliveryStarted
    case finishCalled
    case deliveryFinished(Data)
    case finishReturned
}

private actor DeliveryGate {
    private var recorded: [DeliveryEvent] = []
    private var deliveryStarted = false
    private var finishCalled = false
    private var releaseRequested = false
    private var deliveryStartWaiters: [CheckedContinuation<Void, Never>] = []
    private var finishCallWaiters: [CheckedContinuation<Void, Never>] = []
    private var releaseWaiters: [CheckedContinuation<Void, Never>] = []

    func holdDelivery(_ chunk: Data) async {
        recorded.append(.deliveryStarted)
        deliveryStarted = true
        for waiter in deliveryStartWaiters { waiter.resume() }
        deliveryStartWaiters.removeAll()

        if !releaseRequested {
            await withCheckedContinuation { releaseWaiters.append($0) }
        }
        recorded.append(.deliveryFinished(chunk))
    }

    func waitUntilDeliveryStarts() async {
        guard !deliveryStarted else { return }
        await withCheckedContinuation { deliveryStartWaiters.append($0) }
    }

    func recordFinishCalled() {
        recorded.append(.finishCalled)
        finishCalled = true
        for waiter in finishCallWaiters { waiter.resume() }
        finishCallWaiters.removeAll()
    }

    func waitUntilFinishIsCalled() async {
        guard !finishCalled else { return }
        await withCheckedContinuation { finishCallWaiters.append($0) }
    }

    func releaseDelivery() {
        releaseRequested = true
        for waiter in releaseWaiters { waiter.resume() }
        releaseWaiters.removeAll()
    }

    func recordFinishReturned() { recorded.append(.finishReturned) }
    func events() -> [DeliveryEvent] { recorded }
}
