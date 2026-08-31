import Foundation
import Testing

@testable import ShellCore

/// The contract every site that used to hand-roll this latch relied on:
/// resolve once, wake everyone, let late waiters straight through.
@Suite struct OneShotTests {
    @Test func everyConcurrentWaiterGetsTheOneValue() async {
        let latch = OneShot<Int32>()
        async let first = latch.value()
        async let second = latch.value()
        async let third = latch.value()
        // Resolve only once all three are actually suspended, so this is the
        // several-waiters-at-once case the exit latch meets — a supervisor and
        // a shutdown path both on the same child — and not three late reads.
        await latch.waitUntilWaitersArrive(3)
        latch.resolve(-15)
        #expect(await [first, second, third] == [-15, -15, -15])
    }

    @Test func theFirstResolveWins() async {
        let latch = OneShot<Int32>()
        async let waiter = latch.value()
        await latch.waitUntilWaitersArrive()
        latch.resolve(2)
        // A second resolve must neither restate the value nor resume the
        // waiter again — a continuation resumed twice traps, so this fails
        // loudly rather than quietly.
        latch.resolve(9)
        #expect(await waiter == 2)
        #expect(await latch.value() == 2)
    }

    @Test func aLateWaiterIsNotSuspended() async {
        let latch = OneShot<Int32>()
        latch.resolve(0)
        #expect(await latch.value() == 0)
    }

    @Test func theSynchronousReadNeedsNoAwait() {
        let latch = OneShot<Int32>()
        #expect(!latch.isResolved)
        latch.resolve(1)
        #expect(latch.isResolved)
    }

    @Test func aWokenWaiterMayReenterTheLatch() async {
        // Resumption happens outside the lock, so a waiter that immediately
        // resolves again and reads the synchronous property completes instead
        // of deadlocking against the latch that just woke it.
        let latch = OneShot<Int32>()
        async let reentrant: Bool = {
            _ = await latch.value()
            latch.resolve(99)
            return latch.isResolved
        }()
        await latch.waitUntilWaitersArrive()
        latch.resolve(7)
        #expect(await reentrant)
        #expect(await latch.value() == 7)
    }

    @Test func theVoidGateLatchesOnTheFactAlone() async {
        let gate = OneShot<Void>()
        #expect(!gate.isResolved)
        gate.resolve()
        #expect(gate.isResolved)
        await gate.value()
    }
}

extension OneShot {
    /// Yield until the expected waiters have registered. A resolve that lands
    /// first is still correct — the late-waiter case is its own test — but it
    /// would no longer be the concurrent case under test, so these tests wait
    /// on the fact rather than on a number of scheduler turns.
    fileprivate func waitUntilWaitersArrive(_ expected: Int = 1) async {
        while waiterCount < expected { await Task.yield() }
    }
}
