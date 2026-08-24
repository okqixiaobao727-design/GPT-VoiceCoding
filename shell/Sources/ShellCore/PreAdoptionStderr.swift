import Foundation

/// The engine's stderr for as long as it has one — which is only until it adopts
/// its log.
///
/// Only the engine's *pre-adoption* words arrive here: once it owns its log
/// (ADR 0004) its stderr is that file and no longer this pipe. That is exactly
/// the window that matters, because an exit-2 refusal is said before adoption,
/// so this pipe is the only place it exists.
///
/// Two things end this pipe, and the descriptor alone cannot tell them apart:
///
/// - the engine adopted its log, dropped the pipe's last writer, and is running
///   on happily; or
/// - the engine died, which drops the last writer too.
///
/// Both are the same end of the same pipe, which is why this type is about the
/// *end* rather than about the process.
///
/// **The ordering constraint the code cannot show:** `finish()` must deliver the
/// residue *before* its caller reports the exit. A process that dies immediately
/// after writing its reason is the ordinary case here — exit 2 is precisely that
/// — and reporting the death first would race the words that explain it out of
/// existence. They are the only explanation the Retry panel ever gets.
final class PreAdoptionStderr: @unchecked Sendable {
    /// The child's end. `Process.standardError` is given the whole pipe rather
    /// than its write handle, so that Foundation closes *this* process's copy of
    /// the write end at spawn. Without that, this process would itself be a
    /// writer, and the pipe would never reach the end this type exists to
    /// notice.
    let pipe = Pipe()

    private let deliver: @Sendable (Data) -> Void
    private let lock = NSLock()
    private var monitoring = false

    init(deliver: @escaping @Sendable (Data) -> Void) {
        self.deliver = deliver
    }

    /// Whether this is still watching the pipe.
    var isMonitoring: Bool { lock.withLock { monitoring } }

    /// Begin forwarding what the engine says.
    func read() {
        lock.withLock { monitoring = true }
        pipe.fileHandleForReading.readabilityHandler = { [self] handle in
            let chunk = handle.availableData
            if !chunk.isEmpty { deliver(chunk) }
        }
    }

    /// Stop watching, then hand over whatever is still in the pipe.
    ///
    /// Called when the process is known to be gone. See the ordering constraint
    /// above: the caller reports the exit only once this has returned.
    func finish() {
        stopMonitoring()
        if let remaining = try? pipe.fileHandleForReading.readToEnd(), !remaining.isEmpty {
            deliver(remaining)
        }
    }

    private func stopMonitoring() {
        pipe.fileHandleForReading.readabilityHandler = nil
        lock.withLock { monitoring = false }
    }
}
