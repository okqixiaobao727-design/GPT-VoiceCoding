import Foundation

/// Resolve once, wake everyone waiting, let late waiters through immediately.
///
/// One latch shape recurs wherever the shell has to publish a fact that is
/// settled exactly once — a child's exit code, a test gate's release. Written
/// out at each site it is a lock, an optional, a continuation list and a
/// drain-all, and every copy is a chance to hold the lock across a resumption
/// or to resume a continuation twice. It is written here instead, once.
///
/// Deliberately a lock rather than an `actor`: the exit latch backs a
/// synchronous `hasExited`, which an actor could not answer without suspending.
/// The type is internal — the test targets reach it with `@testable import`,
/// so nothing about it belongs in what `ShellCore` exports.
final class OneShot<Value: Sendable>: @unchecked Sendable {
    private let lock = NSLock()
    private var stored: Value?
    private var waiters: [CheckedContinuation<Value, Never>] = []

    /// Whether the value has been settled, answered without suspending.
    var isResolved: Bool { lock.withLock { stored != nil } }

    /// How many waiters are suspended here right now. The contract tests need a
    /// barrier that says *the waiters have arrived* — without one, "resolve
    /// after they are waiting" can only be guessed at by yielding some chosen
    /// number of times, and a guess that lands early tests the late-waiter path
    /// instead while still passing.
    var waiterCount: Int { lock.withLock { waiters.count } }

    /// First call wins: it stores the value and resumes every waiter. Every
    /// later call is a no-op, so no continuation is ever resumed twice.
    /// Waiters are resumed outside the lock — a waiter that resolves again or
    /// reads `isResolved` on being woken must not meet a held lock.
    func resolve(_ value: Value) {
        let waiting: [CheckedContinuation<Value, Never>] = lock.withLock {
            guard stored == nil else { return [] }
            stored = value
            let waiting = waiters
            waiters.removeAll()
            return waiting
        }
        for waiter in waiting { waiter.resume(returning: value) }
    }

    /// Suspends until resolved; returns at once if it already is. Any number of
    /// waiters may be suspended here at the same time.
    func value() async -> Value {
        await withCheckedContinuation { continuation in
            let settled: Value? = lock.withLock {
                if let stored { return stored }
                waiters.append(continuation)
                return nil
            }
            if let settled { continuation.resume(returning: settled) }
        }
    }
}

extension OneShot where Value == Void {
    /// The gates latch on the fact alone; there is no value to name.
    func resolve() { resolve(()) }
}
