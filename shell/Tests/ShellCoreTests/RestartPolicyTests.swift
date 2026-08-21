import Foundation
import Testing

@testable import ShellCore

/// The restart rules, which are the shell's own (#11) rather than a seam's.
///
/// One definition of "fast failure" is used here and by the supervisor: died
/// before it had been up long enough to reset the counter.
@Suite struct RestartPolicyTests {
    let policy = RestartPolicy()

    @Test func aCleanExitIsStillRestarted() {
        // The KeepAlive lesson: an exit-0 crash class took the old Bridge down.
        let verdict = policy.verdict(
            after: EngineExit(code: 0, ranFor: 300), socket: .notProbed, consecutiveFastFailures: 0)
        #expect(verdict == .restart(after: 1, consecutiveFastFailures: 0))
    }

    @Test func anEngineThatStayedUpResetsTheCounter() {
        let verdict = policy.verdict(
            after: EngineExit(code: 1, ranFor: 61), socket: .notProbed, consecutiveFastFailures: 4)
        #expect(verdict == .restart(after: 1, consecutiveFastFailures: 0))
    }

    @Test(arguments: [(0, 1.0), (1, 2.0), (2, 4.0), (3, 8.0)])
    func fastFailuresBackOff(previous: Int, delay: TimeInterval) {
        let verdict = policy.verdict(
            after: EngineExit(code: 2, ranFor: 0.5), socket: .silent,
            consecutiveFastFailures: previous)
        #expect(verdict == .restart(after: delay, consecutiveFastFailures: previous + 1))
    }

    @Test func fiveConsecutiveFastFailuresStopAndWaitForAHuman() {
        let verdict = policy.verdict(
            after: EngineExit(code: 2, ranFor: 0.5), socket: .silent, consecutiveFastFailures: 4)
        #expect(verdict == .giveUp(.repeatedFailures(attempts: 5)))
    }

    @Test func exitTwoAgainstALiveSocketIsNotACrash() {
        // A second engine refuses and exits 2 without touching the first one's
        // socket, so this is not evidence the child that was running is gone.
        let verdict = policy.verdict(
            after: EngineExit(code: 2, ranFor: 0.2), socket: .answered, consecutiveFastFailures: 0)
        #expect(verdict == .giveUp(.anotherEngineIsListening))
    }

    @Test func exitTwoAgainstALiveSocketNeverExhaustsTheBudget() {
        // It does not increment the failure counter, so it cannot be the reason
        // the shell reports a crash loop.
        let verdict = policy.verdict(
            after: EngineExit(code: 2, ranFor: 0.2), socket: .answered, consecutiveFastFailures: 4)
        #expect(verdict == .giveUp(.anotherEngineIsListening))
    }

    @Test func onlyExitTwoAsksTheSocket() {
        #expect(policy.probesTheSocket(after: EngineExit(code: 2, ranFor: 0.2)))
        #expect(!policy.probesTheSocket(after: EngineExit(code: 1, ranFor: 0.2)))
        #expect(!policy.probesTheSocket(after: EngineExit(code: 0, ranFor: 0.2)))
    }

    @Test func theDelayIsCapped() {
        // The ladder is a rule, not a table: doubling, bounded. The ceiling of
        // five fast failures truncates it at 8s in practice.
        #expect(policy.delay(forFastFailure: 9) == 30)
    }
}
