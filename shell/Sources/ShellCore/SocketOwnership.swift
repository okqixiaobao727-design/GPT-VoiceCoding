import Darwin
import Foundation

/// Who owns the socket, mirrored from `control_plane/ownership.py`.
///
/// A Unix socket file is the whole authorisation story for a local interface: any
/// process that can reach the path can speak the protocol, so the path's owner and
/// mode are the only proof this side has that the peer is this user and not
/// another account on the same machine. The engine checks the same two things
/// from its side; both are handed a path they did not create.
public enum SocketOwnership {
    /// Darwin caps `sun_path` at 104 bytes including its terminator, so 103 is
    /// the longest path that can be bound — or dialled.
    public static let maxSocketPathBytes = 103

    /// Refuse a path too long to dial, in words rather than in an `errno`.
    ///
    /// Asked before anything else, because a path this long does not exist either
    /// and "unavailable" would send the reader looking for a missing engine
    /// instead of a misconfigured path.
    public static func verifyDialable(_ path: String) throws {
        let count = path.utf8.count
        guard count <= maxSocketPathBytes else {
            throw ControlPlaneFailure.engineUnreachable(
                "a socket path may not exceed \(maxSocketPathBytes) bytes; \(path) is \(count)")
        }
    }

    /// Refuse a socket this user does not exclusively own.
    ///
    /// `lstat`, so a symlink planted by another account is refused rather than
    /// followed.
    public static func verifyPrivate(_ path: String) throws {
        var metadata = stat()
        guard lstat(path, &metadata) == 0 else {
            throw ControlPlaneFailure.engineUnreachable(
                "\(path) is unavailable: \(String(cString: strerror(errno)))")
        }
        guard metadata.st_mode & S_IFMT == S_IFSOCK else {
            throw ControlPlaneFailure.engineUnreachable("\(path) is not a Unix socket")
        }
        guard metadata.st_uid == geteuid(), metadata.st_mode & 0o077 == 0 else {
            throw ControlPlaneFailure.engineUnreachable("\(path) is not private to this user")
        }
    }

    /// Whether anything is actually listening — the one question `lstat` cannot
    /// answer, because a socket file outlives the process that bound it.
    ///
    /// This is what an exit-2 child is checked against: a second engine refuses
    /// and exits 2 without touching the first one's socket, so a live socket here
    /// means the start failed for a reason that is not a crash.
    public static func isConnectable(_ path: String, timeout: TimeInterval = 1) -> Bool {
        guard let descriptor = try? connectedSocket(to: path, timeout: timeout) else {
            return false
        }
        close(descriptor)
        return true
    }

    /// A connected `AF_UNIX` stream, or a named failure.
    static func connectedSocket(to path: String, timeout: TimeInterval) throws -> Int32 {
        BrokenPipes.ignore()
        try verifyDialable(path)
        let bytes = Array(path.utf8)

        var address = sockaddr_un()
        address.sun_family = sa_family_t(AF_UNIX)
        address.sun_len = UInt8(MemoryLayout<sockaddr_un>.size)
        withUnsafeMutableBytes(of: &address.sun_path) { field in
            field.copyBytes(from: bytes)
        }

        let descriptor = socket(AF_UNIX, SOCK_STREAM, 0)
        guard descriptor >= 0 else {
            throw ControlPlaneFailure.engineUnreachable(
                "cannot open a socket: \(String(cString: strerror(errno)))")
        }
        // Writing to a socket the peer has closed must be an `EPIPE` this code can
        // report, not a signal that takes the whole process down.
        var noSignal: Int32 = 1
        setsockopt(
            descriptor, SOL_SOCKET, SO_NOSIGPIPE, &noSignal, socklen_t(MemoryLayout<Int32>.size))
        var window = timeval(
            tv_sec: Int(timeout), tv_usec: Int32((timeout - timeout.rounded(.down)) * 1_000_000))
        setsockopt(
            descriptor, SOL_SOCKET, SO_RCVTIMEO, &window, socklen_t(MemoryLayout<timeval>.size))
        setsockopt(
            descriptor, SOL_SOCKET, SO_SNDTIMEO, &window, socklen_t(MemoryLayout<timeval>.size))

        let connected = withUnsafePointer(to: &address) { pointer in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) { generic in
                connect(descriptor, generic, socklen_t(MemoryLayout<sockaddr_un>.size))
            }
        }
        guard connected == 0 else {
            let reason = String(cString: strerror(errno))
            close(descriptor)
            throw ControlPlaneFailure.engineUnreachable("no engine listening on \(path): \(reason)")
        }
        return descriptor
    }
}
