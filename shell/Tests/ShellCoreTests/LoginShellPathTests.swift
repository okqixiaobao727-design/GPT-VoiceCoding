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
        let applied = LoginShellPath.applied(
            to: inherited,
            read: { _, _ in "/opt/homebrew/bin:/usr/bin:/bin" })

        #expect(applied["PATH"] == "/opt/homebrew/bin:/usr/bin:/bin")
        // A login shell can export anything. Importing more than PATH would make
        // somebody's profile a second, invisible configuration file.
        #expect(applied["HOME"] == "/Users/nobody")
        #expect(applied["SHELL"] == "/bin/zsh")
        #expect(applied.count == inherited.count)
    }

    @Test func aShellThatSaysNothingChangesNothing() {
        // Timeouts and spawn failures both arrive here as nil.
        let applied = LoginShellPath.applied(to: inherited, read: { _, _ in nil })
        #expect(applied == inherited)
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
        let applied = LoginShellPath.applied(to: inherited, read: { _, _ in answer })
        #expect(applied["PATH"] == "/usr/bin:/bin")
    }

    @Test func withNoLoginShellToAskItKeepsWhatItHas() {
        var without = inherited
        without["SHELL"] = ""
        // getpwuid still answers on a real machine, so this asserts the branch
        // rather than the absence: whatever happens, PATH is a PATH.
        let applied = LoginShellPath.applied(to: without, read: { _, _ in nil })
        #expect(applied["PATH"] == "/usr/bin:/bin")
    }

    @Test func itSaysWhyWhenItCouldNotAsk() {
        // Silent fallback is how a feature stops working without anybody
        // noticing. The line is the difference between "off" and "broken".
        var said: [String] = []
        _ = LoginShellPath.applied(to: inherited, read: { _, _ in nil }, log: { said.append($0) })
        #expect(said.count == 1)
        #expect(said[0].contains("/bin/zsh"))
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
        #expect(LoginShellPath.usable(answer ?? "") != nil)
    }

    @Test func aShellThatIsNotThereIsNotAnError() {
        #expect(LoginShellPath.readFromLoginShell("/no/such/shell", 1.0) == nil)
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

        #expect(answer == nil)
        #expect(elapsed < 3.0)
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

        #expect(answer == "/opt/homebrew/bin:/usr/bin")
        #expect(LoginShellPath.usable(answer ?? "") == "/opt/homebrew/bin:/usr/bin")
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

        #expect(LoginShellPath.readFromLoginShell(colliding.path, LoginShellPath.timeout) == nil)
    }

    @Test func noiseWithoutSentinelsIsNotAnAnswer() throws {
        // A shell that exits 0 having printed something path-shaped, but never
        // reached the `printf`, has not answered. Taking its chatter would be
        // making a spawn worse, which this is not allowed to do.
        let mute = try fakeShell(#"printf 'p10k instant prompt\n/usr/bin:/bin\n'"#)
        defer { try? FileManager.default.removeItem(at: mute) }

        #expect(LoginShellPath.readFromLoginShell(mute.path, LoginShellPath.timeout) == nil)
    }

    @Test func aShellThatFailsGivesNothingEvenIfItPrinted() throws {
        // Exit status is checked as well as the text, because a profile can
        // print something path-shaped on its way to failing.
        let failing = try fakeShell(#"printf '%s/usr/bin:/bin%s' "$MARK" "$MARK"; exit 1"#)
        defer { try? FileManager.default.removeItem(at: failing) }

        #expect(LoginShellPath.readFromLoginShell(failing.path, LoginShellPath.timeout) == nil)
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
