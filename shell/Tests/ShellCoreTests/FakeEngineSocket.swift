import Darwin
import Foundation

@testable import ShellCore

/// A stand-in engine on a real Unix socket.
///
/// The client's whole job is the wire, so the fake is a real socket rather than a
/// protocol double: a fake that returned a `Reply` object would prove nothing
/// about framing, the byte bound, or the ownership check.
final class FakeEngineSocket: @unchecked Sendable {
    /// What the fake does with the request it just read.
    enum Behaviour {
        /// Answer with this line (a newline is appended).
        case answer(String)
        /// Accept, read, and never reply — the surface must time out.
        case silence
        /// Answer with one line longer than the wire allows.
        case flood
        /// Accept and hang up without a word.
        case hangUp
    }

    let directory: URL
    let path: String
    private let listener: Int32
    private var thread: Thread?
    /// The request lines the fake was actually sent.
    private(set) var received: [String] = []
    private let lock = NSLock()

    init(behaviour: Behaviour, mode: mode_t = 0o600) throws {
        BrokenPipes.ignore()
        // Short, because Darwin caps a socket path at 103 bytes and the system
        // temporary directory is nowhere near short enough.
        directory = URL(fileURLWithPath: "/tmp/gvc-shell-tests-\(UUID().uuidString.prefix(8))")
        try FileManager.default.createDirectory(
            at: directory, withIntermediateDirectories: true,
            attributes: [.posixPermissions: 0o700])
        path = directory.appendingPathComponent("control.sock").path

        listener = socket(AF_UNIX, SOCK_STREAM, 0)
        var address = sockaddr_un()
        address.sun_family = sa_family_t(AF_UNIX)
        address.sun_len = UInt8(MemoryLayout<sockaddr_un>.size)
        withUnsafeMutableBytes(of: &address.sun_path) { $0.copyBytes(from: Array(path.utf8)) }
        let bound = withUnsafePointer(to: &address) { pointer in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                bind(listener, $0, socklen_t(MemoryLayout<sockaddr_un>.size))
            }
        }
        guard bound == 0 else { throw FakeEngineFailure.couldNotBind(errno) }
        chmod(path, mode)
        listen(listener, 4)

        // Strong, and gated on the thread actually reaching `accept`. A weakly
        // captured server can be released before it ever serves, and a caller
        // that dials before the thread is scheduled is testing the scheduler.
        let ready = DispatchSemaphore(value: 0)
        let thread = Thread { [self] in serve(behaviour, ready: ready) }
        thread.start()
        self.thread = thread
        ready.wait()
    }

    private func serve(_ behaviour: Behaviour, ready: DispatchSemaphore) {
        var announced = false
        while true {
            if !announced {
                announced = true
                ready.signal()
            }
            let connection = accept(listener, nil, nil)
            guard connection >= 0 else { return }
            var noSignal: Int32 = 1
            setsockopt(
                connection, SOL_SOCKET, SO_NOSIGPIPE, &noSignal,
                socklen_t(MemoryLayout<Int32>.size))
            var line = Data()
            var byte: UInt8 = 0
            while Darwin.read(connection, &byte, 1) == 1, byte != UInt8(ascii: "\n") {
                line.append(byte)
            }
            lock.withLock { received.append(String(decoding: line, as: UTF8.self)) }

            switch behaviour {
            case .answer(let reply):
                _ = Data((reply + "\n").utf8).withUnsafeBytes {
                    write(connection, $0.baseAddress!, $0.count)
                }
            case .flood:
                let payload = Data((String(repeating: "x", count: maxRequestBytes + 16)).utf8)
                _ = payload.withUnsafeBytes { write(connection, $0.baseAddress!, $0.count) }
            case .silence:
                // Far longer than any timeout under test, so "it timed out" is
                // the only thing the surface can conclude — on a loaded machine
                // a shorter silence ends in a hang-up and tests the wrong thing.
                Thread.sleep(forTimeInterval: 120)
            case .hangUp:
                break
            }
            close(connection)
        }
    }

    func requests() -> [String] { lock.withLock { received } }

    func stop() {
        close(listener)
        try? FileManager.default.removeItem(at: directory)
    }
}

enum FakeEngineFailure: Error {
    case couldNotBind(Int32)
}
