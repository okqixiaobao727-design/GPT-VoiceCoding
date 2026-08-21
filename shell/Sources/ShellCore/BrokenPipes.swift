import Darwin

/// Turn `SIGPIPE` off, once, for this process.
///
/// `SIGPIPE` exists to kill a program that writes to a closed peer and has no
/// idea what to do about it. This one always does: every write here reports
/// `EPIPE` to the code that made it, and that code answers with a named failure.
/// Leaving the default disposition in place would mean a peer hanging up could
/// take the whole shell down — including the supervisor that is the only thing
/// keeping the engine alive.
///
/// `SO_NOSIGPIPE` is set on the sockets this package opens as well, but that
/// only covers the sockets this package opens. This covers the process.
public enum BrokenPipes {
    private static let ignored: Bool = {
        signal(SIGPIPE, SIG_IGN)
        return true
    }()

    /// Idempotent: the first call installs it, the rest are free.
    public static func ignore() { _ = ignored }
}
