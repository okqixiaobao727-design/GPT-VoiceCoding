import Darwin
import Foundation

/// A surface's side of the wire: dial, ask once, read one line, hang up.
///
/// Bounded and timed on purpose. The engine being down is the ordinary case — the
/// shell restarts it, a developer runs it by hand — so "down" must arrive as a
/// named failure within a known time, never as a surface that hangs waiting for
/// an answer that is not coming.
public protocol ControlPlaneDialing: Sendable {
    func ask(_ request: Request) async throws -> Reply
}

/// Long enough for a launch on a busy machine, short enough that a surface which
/// is never going to be answered says so while the user is still watching.
public let defaultControlPlaneTimeout: TimeInterval = 10

public struct UnixSocketControlPlane: ControlPlaneDialing {
    public var path: String
    public var timeout: TimeInterval

    public init(path: String, timeout: TimeInterval = defaultControlPlaneTimeout) {
        self.path = path
        self.timeout = timeout
    }

    public func ask(_ request: Request) async throws -> Reply {
        let line = try request.terminatedLine()
        let path = self.path
        let timeout = self.timeout
        return try await withCheckedThrowingContinuation { continuation in
            // Blocking sockets on a background queue rather than an event loop:
            // one request, one reply, one connection, and the timeouts are the
            // socket's own.
            DispatchQueue.global(qos: .userInitiated).async {
                continuation.resume(
                    with: Result {
                        try exchange(line, over: path, timeout: timeout)
                    })
            }
        }
    }
}

/// One request out, one line back, on one connection.
private func exchange(_ line: Data, over path: String, timeout: TimeInterval) throws -> Reply {
    try SocketOwnership.verifyDialable(path)
    try SocketOwnership.verifyPrivate(path)
    let descriptor = try SocketOwnership.connectedSocket(to: path, timeout: timeout)
    defer { close(descriptor) }

    try writeAll(line, to: descriptor, path: path)
    let reply = try Reply.of(try readLine(from: descriptor, path: path))
    guard reply.protocolVersion == controlPlaneProtocolVersion else {
        throw ControlPlaneFailure.protocolMismatch(
            received: reply.protocolVersion, supported: controlPlaneProtocolVersion)
    }
    return reply
}

private func writeAll(_ payload: Data, to descriptor: Int32, path: String) throws {
    var sent = 0
    while sent < payload.count {
        let written = payload.withUnsafeBytes { bytes -> Int in
            write(descriptor, bytes.baseAddress!.advanced(by: sent), payload.count - sent)
        }
        guard written > 0 else {
            throw ControlPlaneFailure.engineUnreachable(
                "the engine at \(path) stopped reading: \(String(cString: strerror(errno)))")
        }
        sent += written
    }
}

/// Read up to and including one newline, refusing anything past the byte bound.
private func readLine(from descriptor: Int32, path: String) throws -> Data {
    var line = Data()
    var byte: UInt8 = 0
    while true {
        let read = Darwin.read(descriptor, &byte, 1)
        if read == 0 {
            throw ControlPlaneFailure.engineUnreachable(
                "the engine at \(path) closed without replying")
        }
        if read < 0 {
            if errno == EAGAIN || errno == EWOULDBLOCK {
                throw ControlPlaneFailure.engineUnreachable(
                    "the engine at \(path) did not answer in time")
            }
            throw ControlPlaneFailure.engineUnreachable(
                "the engine at \(path) could not be read: \(String(cString: strerror(errno)))")
        }
        if byte == UInt8(ascii: "\n") { return line }
        line.append(byte)
        guard line.count <= maxRequestBytes else {
            // There is no honest way to resync inside a line.
            throw ControlPlaneFailure.engineUnreachable(
                "the engine at \(path) sent more than \(maxRequestBytes) bytes on one line")
        }
    }
}
