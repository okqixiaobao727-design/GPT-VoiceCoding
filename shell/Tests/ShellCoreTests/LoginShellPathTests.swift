import Foundation
import Testing

@testable import ShellCore

/// The engine's `PATH`, and every way asking for it can go wrong.
///
/// The rule under test is one sentence: this may make a spawn better and may
/// never make one worse. So the interesting cases are all the failures, and each
/// of them has to leave the environment untouched rather than half-applied.
@Suite struct LoginShellPathTests {
    private let inherited = ["PATH": "/usr/bin:/bin", "SHELL": "/bin/zsh", "HOME": "/Users/nobody"]

    @Test func aUsableAnswerReplacesPathAndNothingElse() {
        let read = LoginShellPath.apply(
            to: inherited,
            read: { _, _ in .said("/opt/homebrew/bin:/usr/bin:/bin") })
        let applied = read.environment

        #expect(
            read.outcome == .adopted(shell: "/bin/zsh", path: "/opt/homebrew/bin:/usr/bin:/bin"))
        #expect(applied["PATH"] == "/opt/homebrew/bin:/usr/bin:/bin")
        // A login shell can export anything. Importing more than PATH would make
        // somebody's profile a second, invisible configuration file.
        #expect(applied["HOME"] == "/Users/nobody")
        #expect(applied["SHELL"] == "/bin/zsh")
        #expect(applied.count == inherited.count)
    }

    @Test func aShellThatSaysNothingChangesNothing() {
        let read = LoginShellPath.apply(to: inherited, read: { _, _ in .saidNothing })
        #expect(read.environment == inherited)
        #expect(read.outcome == .saidNothingUsable(shell: "/bin/zsh"))
        // Failing open is *right* here: what came back would have made the spawn
        // worse. So it is a log line, and the user is not shown a panel they can
        // do nothing about.
        #expect(read.outcome.reason == nil)
    }

    @Test func aShellThatRanOutOfTimeStillChangesNothingAndIsStillReported() {
        // The two halves of #118 in one assertion. The environment is untouched,
        // because a timeout is no reason to make a spawn worse — and the outcome
        // carries something to say, because the PATH left behind is launchd's and
        // no coding agent is on it.
        let read = LoginShellPath.apply(to: inherited, read: { _, _ in .ranOutOfTime })

        #expect(read.environment == inherited)
        #expect(read.outcome == .ranOutOfTime(shell: "/bin/zsh", budget: LoginShellPath.timeout))
        let reason = read.outcome.reason
        #expect(reason?.contains("/bin/zsh") == true)
        // The budget is in the sentence, spelled as seconds and not as "10.0".
        #expect(reason?.contains("10s") == true)
        // And it is a sentence. The first version of this was a `"""` literal,
        // and the formatter's indentation on its continuation lines went to the
        // panel verbatim — "so the engine —                 and every Session".
        // A string whose only reader is a person reading prose has no other
        // oracle than a screen, and it took one; this is the cheaper one.
        #expect(reason?.contains("  ") == false)
        #expect(reason?.contains("\n") == false)
    }

    @Test func theBudgetIsAboveWhatTheMachineWasMeasuredAt() {
        // The number this replaced was 2.0, chosen because it looked reasonable,
        // and the reference machine's own profile went over it 5 times in 10 at
        // load ~107 (#118). This is the guard that stops it being talked back
        // down: the worst wall time ever measured for this reader, and the budget
        // has to be clear of it with room, not merely above it.
        let worstMeasured: TimeInterval = 5.70
        #expect(LoginShellPath.timeout >= worstMeasured * 1.5)
    }

    @Test(arguments: [
        "",  // said nothing
        "   \n ",  // said whitespace
        "warning: your profile is broken",  // a profile complaining, not an answer
        "/opt/homebrew/bin\n/usr/bin",  // two lines: not a PATH
        "\n/opt/homebrew/bin",  // a newline is a newline wherever it sits …
        "/opt/homebrew/bin\n",  // … including at the end
        "/opt/homebrew/bin\r",  // a bare CR is a line break too, not just LF
        "/opt/homebrew\r\n/bin",  // and so is the pair, in the middle
        "/opt/homebrew\u{2028}/bin",  // Unicode has its own, and they still count
        "relative:paths:only",  // nothing absolute in it
    ])
    func anAnswerThatIsNotAPathChangesNothing(_ answer: String) {
        let read = LoginShellPath.apply(to: inherited, read: { _, _ in .said(answer) })
        #expect(read.environment["PATH"] == "/usr/bin:/bin")
        #expect(read.outcome == .saidNothingUsable(shell: "/bin/zsh"))
    }

    @Test func withNoLoginShellToAskItKeepsWhatItHas() {
        var without = inherited
        without["SHELL"] = ""
        // getpwuid still answers on a real machine, so this asserts the branch
        // rather than the absence: whatever happens, PATH is a PATH.
        let read = LoginShellPath.apply(to: without, read: { _, _ in .saidNothing })
        #expect(read.environment["PATH"] == "/usr/bin:/bin")
    }

    @Test func itSaysWhyWhenItCouldNotAsk() {
        // Silent fallback is how a feature stops working without anybody
        // noticing. The line is the difference between "off" and "broken".
        var said: [String] = []
        _ = LoginShellPath.apply(
            to: inherited, read: { _, _ in .saidNothing }, log: { said.append($0) })
        #expect(said.count == 1)
        #expect(said[0].contains("/bin/zsh"))

        // Including the one it *could* ask, and including the one that ran out of
        // time: one line per spawn, whatever happened, is what makes the log a
        // record rather than an alarm.
        var everyEnding: [String] = []
        for answer: LoginShellPath.Answer in [
            .said("/opt/homebrew/bin"), .ranOutOfTime, .saidNothing,
        ] {
            _ = LoginShellPath.apply(
                to: inherited, read: { _, _ in answer }, log: { everyEnding.append($0) })
        }
        #expect(everyEnding.count == 3)
        #expect(everyEnding.allSatisfy { $0.contains("/bin/zsh") })
    }

    @Test func theLoginShellIsTheUsersAndNeverAHardCodedOne() {
        #expect(LoginShellPath.loginShell(environment: ["SHELL": "/opt/fish"]) == "/opt/fish")
        // Empty is not an answer; the password database is asked instead, and on
        // any real machine it has one.
        #expect(LoginShellPath.loginShell(environment: ["SHELL": ""])?.isEmpty == false)
    }

    // -- the real reader, against real shells ---------------------------------

    @Test func itReadsAPathOutOfARealShell() {
        let answer = LoginShellPath.readFromLoginShell("/bin/sh", LoginShellPath.timeout)
        guard case .said(let path) = answer else {
            Issue.record("a real shell answered \(answer)")
            return
        }
        #expect(LoginShellPath.usable(path) != nil)
    }

    @Test func aShellThatIsNotThereIsNotAnError() {
        #expect(LoginShellPath.readFromLoginShell("/no/such/shell", 1.0) == .saidNothing)
    }

    @Test func aProfileThatHangsIsBoundedAndGivesNothing() throws {
        // The bound is what stops one person's `.zprofile` from holding the
        // engine's supervisor open — and the supervisor is what restarts the
        // engine, so a spawn that never returns is the whole shell wedged.
        let hanging = try fakeShell("sleep 30")
        defer { try? FileManager.default.removeItem(at: hanging) }

        let started = Date()
        let answer = LoginShellPath.readFromLoginShell(hanging.path, 0.3)
        let elapsed = Date().timeIntervalSince(started)

        // `.ranOutOfTime` and not merely "nothing": this is the one ending the
        // user is shown, and a reader that collapsed it into the others is how
        // #118 stayed silent for the whole of the 2.0 s budget's life.
        #expect(answer == .ranOutOfTime)
        #expect(elapsed < 3.0)
    }

    @Test func aProfileThatBackgroundsSomethingIsNotAProfileThatIsSlow() throws {
        // `ssh-agent`, `gpg-agent`, any `&`-ed job in `~/.zshrc`: the shell hands
        // its stdout to a child that outlives it, so the *pipe* stays open for
        // hours after the shell has printed its answer and exited. Waiting for
        // EOF spent the whole budget on a machine doing nothing, threw away a
        // PATH that had already arrived, and — once this started reporting —
        // showed the user a panel blaming a load their machine did not have.
        //
        // So the deadline is on the shell. The answer is here the moment the
        // shell is gone, whoever is still holding the pipe.
        let backgrounding = try fakeShell(
            #"sleep 5 & printf '%s' "$MARK/opt/homebrew/bin:/usr/bin$MARK""#)
        defer { try? FileManager.default.removeItem(at: backgrounding) }

        let budget: TimeInterval = 5.0
        let started = Date()
        let answer = LoginShellPath.readFromLoginShell(backgrounding.path, budget)
        let elapsed = Date().timeIntervalSince(started)

        #expect(answer == .said("/opt/homebrew/bin:/usr/bin"))
        // Well inside the budget rather than merely under it: the defect this
        // guards spends the budget exactly, so a bound of "less than the budget"
        // would be the one number that cannot tell them apart.
        #expect(elapsed < budget / 2, "took \(elapsed)s of a \(budget)s budget")

        // `.said` is itself the proof that the reader **finished** before this
        // returned, and so that the descriptor it owned is closed: the only way
        // past the join is for `awaitEnd` to have succeeded, and every other way
        // out is `.ranOutOfTime`. The `sleep` still holds the write end, so the
        // reader cannot have reached EOF — it left because it was asked to.
        //
        // Asserted through the control flow rather than by counting `/dev/fd`:
        // that counter is process-wide, the suites run in parallel, and a test
        // that samples it around a 1 s call is measuring the other suites. This
        // repository has one such assertion already and it has the noise floor in
        // its comment to prove it.
    }

    @Test func aReaderThatNeverRanIsReportedRatherThanCalledSilence() {
        // The last hole in "it never fails silently". If nothing schedules the
        // reader — and the app blocks global-queue threads elsewhere, so the load
        // this budget was sized for is exactly when that bites — then there are
        // no bytes to parse. Calling that "the shell said nothing" drops the
        // engine onto launchd's PATH with no panel and no way to tell.
        let neverRuns = NeverFinishingPipe()

        #expect(LoginShellPath.answer(draining: neverRuns, remaining: 0.05) == .ranOutOfTime)
        // Asked to leave even so: a reader nobody waited for is still a reader
        // holding a descriptor.
        #expect(neverRuns.wasAskedToStop)
    }

    @Test func aReaderThatFinishesWithNothingIsStillJustNothing() {
        // The other side of it. A reader that *did* run and found no answer is
        // the ordinary "that was not a PATH" case, and must not be inflated into
        // a timeout the user is shown — a panel that cries wolf is a panel people
        // learn to close.
        let ranAndSaidNothing = FinishedPipe(data: Data("chatter, no sentinels".utf8))

        #expect(LoginShellPath.answer(draining: ranAndSaidNothing, remaining: 5) == .saidNothing)
    }

    @Test func itTakesThePathOutFromBetweenTheSentinelsAndLeavesTheNoise() throws {
        // An interactive shell is what reads `.zshrc`, and `.zshrc` is where a
        // zsh user's `PATH` actually is — but interactive is also what lets a
        // prompt framework write to stdout on the way past. The sentinels are
        // what make those two facts compatible.
        let noisy = try fakeShell(
            #"printf 'p10k instant prompt\n'; printf '%s' "$MARK/opt/homebrew/bin:/usr/bin$MARK"; printf 'welcome back\n'"#
        )
        defer { try? FileManager.default.removeItem(at: noisy) }

        let answer = LoginShellPath.readFromLoginShell(noisy.path, LoginShellPath.timeout)

        #expect(answer == .said("/opt/homebrew/bin:/usr/bin"))
        guard case .said(let said) = answer else { return }
        #expect(LoginShellPath.usable(said) == "/opt/homebrew/bin:/usr/bin")
    }

    @Test func aSentinelInTheNoiseIsNotAnAnswerEither() throws {
        // Taking the first two occurrences would read the gap between the
        // chatter's sentinel and the real one — path-shaped enough to pass
        // `usable`, and a truncated PATH is a worse spawn, which this may never
        // produce. Three occurrences is a collision, and a collision is a
        // fallback.
        let colliding = try fakeShell(
            #"printf '%s /opt/junk' "$MARK"; printf '%s' "$MARK/opt/homebrew/bin:/usr/bin$MARK""#
        )
        defer { try? FileManager.default.removeItem(at: colliding) }

        #expect(
            LoginShellPath.readFromLoginShell(colliding.path, LoginShellPath.timeout)
                == .saidNothing)
    }

    @Test func noiseWithoutSentinelsIsNotAnAnswer() throws {
        // A shell that exits 0 having printed something path-shaped, but never
        // reached the `printf`, has not answered. Taking its chatter would be
        // making a spawn worse, which this is not allowed to do.
        let mute = try fakeShell(#"printf 'p10k instant prompt\n/usr/bin:/bin\n'"#)
        defer { try? FileManager.default.removeItem(at: mute) }

        #expect(
            LoginShellPath.readFromLoginShell(mute.path, LoginShellPath.timeout) == .saidNothing)
    }

    @Test func aShellThatFailsGivesNothingEvenIfItPrinted() throws {
        // Exit status is checked as well as the text, because a profile can
        // print something path-shaped on its way to failing.
        let failing = try fakeShell(#"printf '%s/usr/bin:/bin%s' "$MARK" "$MARK"; exit 1"#)
        defer { try? FileManager.default.removeItem(at: failing) }

        #expect(
            LoginShellPath.readFromLoginShell(failing.path, LoginShellPath.timeout) == .saidNothing)
    }

    /// A throwaway executable standing in for somebody's login shell: it runs
    /// `body` and nothing else, with `MARK` bound to the sentinel so no test
    /// spells the marker out and then drifts from it.
    ///
    /// The arguments the reader passes — `-lic` and the script — are ignored,
    /// which is the point: these cases are about what a shell *says*, and a real
    /// one cannot be made to say them on demand.
    private func fakeShell(_ body: String) throws -> URL {
        let script = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("fake-shell-\(UUID().uuidString)")
        try "#!/bin/sh\nMARK='\(LoginShellPath.sentinel)'\n\(body)\n".write(
            to: script, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes(
            [.posixPermissions: 0o755], ofItemAtPath: script.path)
        return script
    }
}

/// A reader that never finishes, however long it is given — the global queue
/// with nothing spare, which is the one state a real `PipeReader` cannot be
/// asked to be in on demand.
private final class NeverFinishingPipe: DrainedPipe, @unchecked Sendable {
    private let lock = NSLock()
    private var asked = false

    func awaitEnd(_ seconds: TimeInterval) -> Bool { false }
    func stop() { lock.withLock { asked = true } }
    var data: Data { Data() }
    var wasAskedToStop: Bool { lock.withLock { asked } }
}

/// A reader that finished at once, holding whatever it read.
private final class FinishedPipe: DrainedPipe, @unchecked Sendable {
    let data: Data
    init(data: Data) { self.data = data }
    func awaitEnd(_ seconds: TimeInterval) -> Bool { true }
    func stop() {}
}
