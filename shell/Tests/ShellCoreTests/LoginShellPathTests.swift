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
        let hanging = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("hanging-shell-\(UUID().uuidString)")
        try "#!/bin/sh\nsleep 30\n".write(to: hanging, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes(
            [.posixPermissions: 0o755], ofItemAtPath: hanging.path)
        defer { try? FileManager.default.removeItem(at: hanging) }

        let started = Date()
        let answer = LoginShellPath.readFromLoginShell(hanging.path, 0.3)
        let elapsed = Date().timeIntervalSince(started)

        #expect(answer == nil)
        #expect(elapsed < 3.0)
    }

    @Test func aShellThatFailsGivesNothingEvenIfItPrinted() throws {
        // Exit status is checked as well as the text, because a profile can
        // print something path-shaped on its way to failing.
        let failing = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("failing-shell-\(UUID().uuidString)")
        try "#!/bin/sh\nprintf /usr/bin:/bin\nexit 1\n".write(
            to: failing, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes(
            [.posixPermissions: 0o755], ofItemAtPath: failing.path)
        defer { try? FileManager.default.removeItem(at: failing) }

        #expect(LoginShellPath.readFromLoginShell(failing.path, LoginShellPath.timeout) == nil)
    }
}
