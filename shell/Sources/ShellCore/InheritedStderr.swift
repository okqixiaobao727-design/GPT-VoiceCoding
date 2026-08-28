import Foundation

/// The stderr pipe the engine inherited from this shell.
///
/// Before the engine owns its log, its words arrive through fd 2. At adoption it
/// keeps a duplicate of this descriptor and points fd 2 at the log (ADR 0004);
/// after that, only the single final refusal sentence is mirrored here.
///
/// The engine drops the last writer when it exits. Before #137 adoption itself
/// also ended the pipe; keeping the duplicate open is what lets a later startup
/// refusal reach the shell without reading the engine's log.
///
/// **The ordering constraint the code cannot show:** `finish()` delivers the
/// residue *before* its caller reports the exit. A process that dies immediately
/// after writing its reason is the ordinary case here — exit 2 is precisely that
/// — and reporting the death first would race the words that explain it out of
/// existence. They are the only explanation the Retry panel ever gets.
///
/// **How far that holds, exactly.** It holds for the case this pipe exists for: a
/// refusal is a single short write, well under the 64 KB pipe buffer, so it
/// arrives in one delivery, whole and ahead of the exit.
///
/// It does **not** hold for a large message — a traceback — that straddles the
/// buffer while the process is dying. `finish()` clears the readability handler,
/// which stops the next invocation but not one already running on Foundation's
/// own queue, so that handler can be inside `availableData` while `finish()` is
/// inside `readToEnd()`. The two readers then split the message between them and
/// can deliver it out of order, and the handler's delivery can land after the
/// exit has already been reported.
///
/// That is issue #33, along with the same guarantee failing a layer up in
/// `EngineSupervisor`. It is stated here rather than left implied because a
/// promise documented without its limit is worse than one never written down:
/// the next reader trusts it exactly where it is weakest.
final class InheritedStderr: @unchecked Sendable {
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
            // An empty read **is** the end of the pipe, and a descriptor at its
            // end is permanently readable — so a watch that reads emptiness and
            // returns is asked again at once, and again, for as long as the
            // watch is up. Stop watching, which is the only thing the end of a
            // pipe ever asks for.
            guard !chunk.isEmpty else { return stopMonitoring() }
            deliver(chunk)
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

    /// Give up on a pipe that will never end by itself.
    ///
    /// `finish()`'s drain waits for the end of the pipe, and after a launch that
    /// never spawned there is no end coming: Foundation closes this process's
    /// copy of the write end when it hands the pipe to a child, so a child that
    /// was never made leaves this process holding it, waiting on itself. There
    /// is nothing to drain either — nothing ever wrote.
    func abandon() {
        stopMonitoring()
        try? pipe.fileHandleForWriting.close()
        try? pipe.fileHandleForReading.close()
    }

    private func stopMonitoring() {
        pipe.fileHandleForReading.readabilityHandler = nil
        lock.withLock { monitoring = false }
    }
}
