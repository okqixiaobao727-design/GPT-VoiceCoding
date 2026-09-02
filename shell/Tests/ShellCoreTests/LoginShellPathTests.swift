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
        //
        // `sleep 30` rather than a sleep shorter than the budget: the sleeper has
        // to still be holding the pipe when the budget runs out, or the defect
        // would end at EOF instead of at the budget and the bound below would be
        // measuring the sleep. It is left to exit on its own rather than killed
        // in teardown — it is a grandchild of this process, so a kill would mean
        // the script publishing its pid through a temporary file, and an idle
        // `sleep` holding the write end of a pipe nobody reads costs the tests
        // that follow it nothing.
        let backgrounding = try fakeShell(
            #"sleep 30 & printf '%s' "$MARK/opt/homebrew/bin:/usr/bin$MARK""#)
        defer { try? FileManager.default.removeItem(at: backgrounding) }

        let budget: TimeInterval = 12.0
        let started = Date()
        let answer = LoginShellPath.readFromLoginShell(backgrounding.path, budget)
        let elapsed = Date().timeIntervalSince(started)

        #expect(answer == .said("/opt/homebrew/bin:/usr/bin"))
        // Well inside the budget rather than merely under it: the defect this
        // guards spends the budget exactly, so a bound of "less than the budget"
        // would be the one number that cannot tell them apart.
        //
        // 4 s is the slowest this call has been measured at — 2.764 s, in #199's
        // full 189-test parallel run on a Command Line Tools-only Mac — plus a
        // second of room for the next machine that is slower. The gap it needs
        // is on the other side: this used to ask for `budget / 2` of a 5 s
        // budget, which is 2.5 s, and the measurement above walked straight
        // through it (#205). The budget is 12 s so that a returned answer and a
        // spent budget stay three times apart at this bound; a passing run takes
        // about a second, so the larger budget costs the suite nothing. With the
        // reader temporarily made to wait for EOF, this call took 12.011 s here,
        // so the bound still catches the defect it names.
        #expect(elapsed < 4.0, "took \(elapsed)s of a \(budget)s budget")

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

    @Test func aProfileThatBackgroundsSomethingNoisyIsNotSlowEither() throws {
        // The other half of the backgrounded job, and the one that bit. `sleep`
        // holds the write end without using it, so the reader sits in `poll` and
        // sees the stop the moment it is asked. A job that keeps *writing* —
        // a progress spinner, a `tail -f`, anything in a loop — makes the poll
        // ready every time round, so a reader that checks the stop flag only
        // when the poll came back empty never checks it at all. The PATH is in
        // hand, the shell exited in milliseconds, and the call still spends the
        // whole budget and raises the panel.
        //
        // The `sleep 0.01` in the writer's loop is load-bearing, and removing it
        // to make the job "noisier" is the way to break this test without any
        // assertion changing. An unpaced `while :; do printf x; done` fills the
        // reader's 1 MB `LoginShellPath.collectionCap` — `keep` returning false —
        // in about a second and a half, and the reader then ends *itself*:
        // measured on this machine at 1.571 s with the stop-check defect
        // reintroduced and 1.898 s with the reader made to wait for EOF, both
        // comfortably inside any bound that clears this suite's jitter. (The
        // 64 KB `drainCap` is not what ends it — `/bin/sh` writing single bytes
        // reaches 64 KB in 0.09 s here and 1 MB in 1.42 s, and 1.42 s is the
        // number that matches.) Paced at a byte every 10 ms, twelve seconds of
        // writing is a little over a kilobyte, the cap is unreachable, and the
        // deadline is the only thing that can end this call — which is what makes
        // the bound below mean something. The always-ready-poll defect the
        // paragraph above describes is proved by
        // `aReaderWhosePollIsNeverEmptyStillLeavesWhenAsked`, not by this fixture.
        let noisy = try fakeShell(
            #"(while :; do printf x; sleep 0.01; done) & printf '%s' "$MARK/opt/bin:/usr/bin$MARK""#
        )
        defer { try? FileManager.default.removeItem(at: noisy) }

        let budget: TimeInterval = 12.0
        let started = Date()
        let answer = LoginShellPath.readFromLoginShell(noisy.path, budget)
        let elapsed = Date().timeIntervalSince(started)

        #expect(answer == .said("/opt/bin:/usr/bin"))
        // The same bound and the same measurement as the backgrounding test
        // above: 4 s is #199's worst observed 2.764 s plus a second. This test
        // is the one that flaked while #205 was being diagnosed, at 1.667 s
        // against the 1.5 s that `budget / 2` left it on a 3 s budget — one in
        // nine full parallel runs on the same machine, none in five runs of this
        // suite alone. The writer here never reaches EOF, so a reader that
        // waited for one spends the whole 12 s and this bound catches it —
        // measured at 12.017 s with the reader temporarily made to wait for EOF.
        #expect(elapsed < 4.0, "took \(elapsed)s of a \(budget)s budget")
    }

    @Test func aReaderWhosePollIsNeverEmptyStillLeavesWhenAsked() throws {
        // The stop check sits at the *top* of the reader's loop, and this is the
        // only test that says so. The version before it checked the flag when the
        // poll came back empty, which for a job writing without pause is never:
        // the poll is ready every time round, the read always succeeds, and the
        // reader stays until something else ends it.
        //
        // Asked through the reader rather than through `readFromLoginShell`,
        // because that path can only be watched with a clock, and a clock here
        // measures the other seventeen suites. The two assertions below are what
        // the stop check means, with no elapsed time in either: the reader left,
        // and it left with a handful of bytes rather than the megabyte the defect
        // would have taken. Under the defect the first still succeeds — the
        // reader ends itself once `collectionCap` is reached, in about 1.4 s here
        // — and the second is what fails, so this cannot pass by hanging either.
        let pipe = Pipe()
        let writer = Process()
        writer.executableURL = URL(fileURLWithPath: "/bin/sh")
        writer.arguments = ["-c", "while :; do printf x; done"]
        writer.standardOutput = pipe.fileHandleForWriting
        try writer.run()
        defer {
            // Killed here rather than left to a `SIGPIPE`: this one is a direct
            // child, so unlike the fixtures above there is a pid to signal, and a
            // writer spinning a core through the rest of the suite is exactly the
            // load these timing tests are trying not to have.
            writer.terminate()
            writer.waitUntilExit()
            try? pipe.fileHandleForWriting.close()
        }

        let reader = PipeReader(duplicating: pipe.fileHandleForReading.fileDescriptor)
        reader.stop()

        // Five seconds is a liveness window and not a bound: the reader leaves
        // within one `readerStopCheckInterval` of being asked, and the number is
        // this large so that a busy machine cannot make a passing run look like a
        // wedged one.
        #expect(reader.awaitEnd(5.0))
        // One pipe buffer of what was already written, plus at most `drainCap`
        // taken on the way out — a quarter of `collectionCap` is far above both
        // and far below the megabyte a reader that never saw the stop collects.
        #expect(reader.data.count < LoginShellPath.collectionCap / 4)
    }

    @Test func aReaderThatNeverRanIsReportedRatherThanCalledSilence() {
        // The last hole in "it never fails silently". If nothing schedules the
        // reader — and the app blocks global-queue threads elsewhere, so the load
        // this budget was sized for is exactly when that bites — then there are
        // no bytes to parse. Calling that "the shell said nothing" drops the
        // engine onto launchd's PATH with no panel and no way to tell.
        let neverRuns = NeverFinishingPipe()
        let remaining: TimeInterval = 1.0

        #expect(LoginShellPath.answer(draining: neverRuns, remaining: remaining) == .notCollected)
        // Asked to leave even so: a reader nobody waited for is still a reader
        // holding a descriptor.
        #expect(neverRuns.wasAskedToStop)
        // And both waits were **bounded**, by what was left of the budget rather
        // than by a number of their own — the half of the join that a stub which
        // ignored its argument would let through untested.
        #expect(
            neverRuns.waits == [
                LoginShellPath.readerSettle,
                remaining - LoginShellPath.readerSettle,
            ])
    }

    @Test func noDescriptorToReadWithIsReportedAndNotCalledSilence() {
        // `dup` refusing is `EMFILE`, which is the crash-loop case
        // `aLaunchThatNeverSpawnsKeepsNoPipeAfterwards` exists for. With no
        // descriptor there are no bytes, and no bytes read back as "the shell
        // said nothing" is the silent launchd PATH again, through another door.
        #expect(LoginShellPath.answer(draining: NeverStartedPipe(), remaining: 5) == .notCollected)
    }

    @Test func theAppTakesTheBlameWhenItWasTheAppsFault() {
        // Two reported outcomes, two different sentences. Somebody sent to edit
        // a `.zshrc` that printed its PATH correctly is somebody this panel has
        // actively misled.
        let ours = LoginShellPath.Outcome.answerNotCollected(shell: "/bin/zsh")
        let theirs = LoginShellPath.Outcome.ranOutOfTime(shell: "/bin/zsh", budget: 10)

        #expect(ours.reason?.contains("this app's own doing") == true)
        #expect(ours.reason?.contains("did not print") == false)
        #expect(theirs.reason?.contains("did not print a PATH within 10s") == true)
        #expect(theirs.reason?.contains("this app's own doing") == false)
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
    private var windows: [TimeInterval] = []

    func awaitEnd(_ seconds: TimeInterval) -> Bool {
        lock.withLock { windows.append(seconds) }
        return false
    }
    func stop() { lock.withLock { asked = true } }
    var data: Data { Data() }
    var failed: Bool { false }
    var wasAskedToStop: Bool { lock.withLock { asked } }
    /// Every window it was given, so the bound itself is under test.
    var waits: [TimeInterval] { lock.withLock { windows } }
}

/// A reader that never got a descriptor at all.
private final class NeverStartedPipe: DrainedPipe, @unchecked Sendable {
    func awaitEnd(_ seconds: TimeInterval) -> Bool { true }
    func stop() {}
    var data: Data { Data() }
    var failed: Bool { true }
}

/// A reader that finished at once, holding whatever it read.
private final class FinishedPipe: DrainedPipe, @unchecked Sendable {
    let data: Data
    init(data: Data) { self.data = data }
    func awaitEnd(_ seconds: TimeInterval) -> Bool { true }
    func stop() {}
    var failed: Bool { false }
}
